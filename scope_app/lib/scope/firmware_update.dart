import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:file_selector/file_selector.dart';

const _maxImageBytes = 0x080ff800 - 0x08007000;

class FirmwareFile {
  final String fileName, name, sha256;
  final Uint8List bytes;
  const FirmwareFile(this.fileName, this.name, this.sha256, this.bytes);
}

class IapDevice {
  final String id, name, fingerprint;
  const IapDevice(this.id, this.name, this.fingerprint);
}

class UpdateEvent {
  final String kind, detail;
  final int done, total;
  const UpdateEvent(
    this.kind, {
    this.detail = '',
    this.done = 0,
    this.total = 0,
  });

  factory UpdateEvent.decode(Map<String, dynamic> json) => UpdateEvent(
    json['kind'] as String,
    detail: json['detail'] as String? ?? '',
    done: json['done'] as int? ?? 0,
    total: json['total'] as int? ?? 0,
  );
}

class FirmwareUpdateException implements Exception {
  final String message;
  const FirmwareUpdateException(this.message);
  @override
  String toString() => message;
}

String localUpdaterError(String detail) {
  if (detail == 'no 2C53T IAP disk found') {
    return 'Загрузчик 2C53T не найден. Проверьте кабель и включите прибор с зажатой MENU.';
  }
  if (detail.startsWith('leave just one 2C53T')) {
    return 'Оставьте подключённым только один 2C53T в режиме обновления.';
  }
  if (detail.startsWith('the IAP is not at Ready')) {
    return 'Загрузчик не готов. Выключите прибор и войдите в режим обновления заново.';
  }
  if (detail.startsWith('the image size') ||
      detail.startsWith('the image does not match the FNIRSI')) {
    return 'Этот файл не подходит. Выберите штатную прошивку 2C53T версии 1.2.0 или сборку r6j.';
  }
  if (detail.startsWith('unrecognized image') ||
      detail.startsWith('the image does not match its')) {
    return 'Файл не прошёл проверку. Если вы собрали r6j сами, оставьте рядом созданный при сборке manifest.json.';
  }
  if (detail == 'administrator permission was cancelled') {
    return 'Запрос прав администратора отменён.';
  }
  if (detail.startsWith('start pkexec') ||
      detail.startsWith('administrator permission is unavailable')) {
    return 'Не удалось запросить права администратора. Установите pkexec и проверьте, что в вашей графической сессии работает агент Polkit.';
  }
  return detail;
}

class FirmwareUpdater {
  const FirmwareUpdater();

  String get _helper {
    final directory = File(Platform.resolvedExecutable).parent;
    final path = Platform.isMacOS
        ? '${directory.parent.path}/Helpers/fnirsi-iap'
        : directory.uri
              .resolve(Platform.isWindows ? 'fnirsi-iap.exe' : 'fnirsi-iap')
              .toFilePath();
    if (!File(path).existsSync()) {
      throw const FirmwareUpdateException(
        'В этой сборке отсутствует помощник обновления. Установите полный архив приложения.',
      );
    }
    return path;
  }

  Future<FirmwareFile?> select() async {
    final file = await openFile(
      acceptedTypeGroups: const [
        XTypeGroup(label: 'Прошивка FNIRSI', extensions: ['bin']),
      ],
    );
    if (file == null) return null;
    final output = await _one(['image', file.path], 'image');
    if (await file.length() > _maxImageBytes) {
      throw const FirmwareUpdateException('Файл прошивки слишком большой.');
    }
    final bytes = BytesBuilder(copy: false);
    await for (final chunk in file.openRead(0, _maxImageBytes + 1)) {
      if (bytes.length + chunk.length > _maxImageBytes) {
        throw const FirmwareUpdateException('Файл прошивки слишком большой.');
      }
      bytes.add(chunk);
    }
    final data = bytes.takeBytes();
    if (data.length != output['size']) {
      throw const FirmwareUpdateException(
        'Файл изменился. Выберите его заново.',
      );
    }
    return FirmwareFile(
      file.name,
      output['name'] as String,
      output['sha256'] as String,
      data,
    );
  }

  Future<IapDevice> probe() async {
    final ready = await _one(['probe'], 'ready');
    return IapDevice(
      ready['id'] as String,
      ready['name'] as String,
      ready['fingerprint'] as String,
    );
  }

  Future<Map<String, dynamic>> _one(List<String> args, String kind) async {
    Map<String, dynamic>? expected;
    String? error;
    await for (final json in _run(args)) {
      if (json['kind'] == 'error') {
        error = json['detail'] as String;
      }
      if (json['kind'] == kind) expected = json;
    }
    if (error != null) throw FirmwareUpdateException(localUpdaterError(error));
    return expected ??
        (throw const FirmwareUpdateException(
          'Помощник обновления завершился без результата.',
        ));
  }

  Stream<UpdateEvent> flash(FirmwareFile file, IapDevice device) async* {
    var finished = false;
    await for (final json in _run([
      'flash',
      device.id,
      file.sha256,
      device.fingerprint,
    ], file.bytes)) {
      final event = UpdateEvent.decode(json);
      finished |= const {
        'success',
        'unconfirmed',
        'interrupted',
        'error',
      }.contains(event.kind);
      yield event;
    }
    if (!finished) {
      throw const FirmwareUpdateException(
        'Помощник обновления завершился без результата.',
      );
    }
  }

  Stream<Map<String, dynamic>> _run(
    List<String> args, [
    Uint8List? input,
  ]) async* {
    final process = await Process.start(_helper, args);
    final stderr = process.stderr
        .transform(const Utf8Decoder(allowMalformed: true))
        .join();
    const closeError = 'Не удалось передать файл помощнику.';
    final sent = Future<void>(() async {
      if (input != null) process.stdin.add(input);
      await process.stdin.close();
    }).then<String?>((_) => null, onError: (Object _) => closeError);
    var errorReported = false;
    await for (final line
        in process.stdout
            .transform(utf8.decoder)
            .transform(const LineSplitter())) {
      final message = jsonDecode(line) as Map<String, dynamic>;
      errorReported |=
          message['kind'] == 'error' || message['kind'] == 'interrupted';
      yield message;
    }
    final code = await process.exitCode;
    final detail = (await stderr).trim();
    final inputError = await sent;
    if (!errorReported && (code != 0 || inputError != null)) {
      throw FirmwareUpdateException(
        detail.isNotEmpty
            ? detail
            : inputError ?? 'Помощник обновления завершился с кодом $code.',
      );
    }
  }
}
