import 'dart:async';
import 'dart:ffi';
import 'dart:io';
import 'dart:isolate';
import 'dart:typed_data';
import 'package:ffi/ffi.dart';
import 'protocol.dart';

typedef _InitN = Int32 Function(Pointer<Pointer<Void>>);
typedef _InitD = int Function(Pointer<Pointer<Void>>);
typedef _ExitN = Void Function(Pointer<Void>);
typedef _ExitD = void Function(Pointer<Void>);
typedef _OpenN = Pointer<Void> Function(Pointer<Void>, Uint16, Uint16);
typedef _OpenD = Pointer<Void> Function(Pointer<Void>, int, int);
typedef _CloseN = Void Function(Pointer<Void>);
typedef _CloseD = void Function(Pointer<Void>);
typedef _ControlN =
    Int32 Function(
      Pointer<Void>,
      Uint8,
      Uint8,
      Uint16,
      Uint16,
      Pointer<Uint8>,
      Uint16,
      Uint32,
    );
typedef _ControlD =
    int Function(Pointer<Void>, int, int, int, int, Pointer<Uint8>, int, int);
typedef _ClaimN = Int32 Function(Pointer<Void>, Int32);
typedef _ClaimD = int Function(Pointer<Void>, int);
typedef _ErrorN = Pointer<Utf8> Function(Int32);
typedef _ErrorD = Pointer<Utf8> Function(int);

/// Small injectable boundary: USB reads and commands are serialized by one owner.
abstract class ScopeLink {
  Future<void> connect();
  Future<ScopeSettings> settings();
  Future<CaptureStatus> status();
  Future<CaptureStatus> command(int op, [int argument = 0]);
  Future<ScopeFrame> frame(CaptureStatus expected);
  Future<ScopeFrame?> preview();
  Future<CalibrationStatus> calibration();
  Future<void> close();
}

class UsbScopeLink implements ScopeLink {
  Isolate? _worker;
  SendPort? _send;
  ReceivePort? _receive;
  final _pending = <int, Completer<dynamic>>{};
  int _next = 0;

  Future<void> _ensureWorker() async {
    if (_send != null) return;
    final received = ReceivePort();
    final ready = Completer<SendPort>();
    _receive = received;
    received.listen((dynamic message) {
      if (message is SendPort) {
        if (!ready.isCompleted) ready.complete(message);
        return;
      }
      if (message is Map) {
        final id = message['id'];
        final waiting = _pending.remove(id);
        if (waiting == null) return;
        if (message['error'] != null) {
          waiting.completeError(ScopeException(message['error'] as String));
        } else {
          waiting.complete(message['value']);
        }
      }
    });
    _worker = await Isolate.spawn(_usbWorker, received.sendPort);
    _send = await ready.future.timeout(const Duration(seconds: 10));
  }

  Future<dynamic> _request(
    String op, [
    Map<String, dynamic> args = const {},
  ]) async {
    await _ensureWorker();
    final id = ++_next, result = Completer<dynamic>();
    _pending[id] = result;
    _send!.send({'id': id, 'op': op, ...args});
    try {
      return await result.future.timeout(const Duration(seconds: 35));
    } on TimeoutException {
      _pending.remove(id);
      throw const ScopeException(
        'Превышено время ожидания USB. Переподключите прибор.',
      );
    }
  }

  @override
  Future<void> connect() async {
    await _request('connect');
  }

  @override
  Future<ScopeSettings> settings() async =>
      ScopeSettings.decode(await _request('settings') as Uint8List);
  @override
  Future<CaptureStatus> status() async =>
      CaptureStatus.decode(await _request('status') as Uint8List);
  @override
  Future<CaptureStatus> command(int op, [int argument = 0]) async =>
      CaptureStatus.decode(
        await _request('command', {'operation': op, 'argument': argument})
            as Uint8List,
      );
  @override
  Future<ScopeFrame> frame(CaptureStatus expected) async {
    final result =
        await _request('frame', {
              'frameId': expected.frameId,
              'crc': expected.frameCrc,
              'length': expected.frameBytes,
            })
            as Map;
    final settings = ScopeSettings.decode(result['settings'] as Uint8List);
    return ScopeFrame.decode(result['bytes'] as Uint8List, expected, settings);
  }

  @override
  Future<ScopeFrame?> preview() async {
    final result = await _request('preview') as Map;
    if (result['notReady'] == true) return null;
    return ScopeFrame.decode(
      result['bytes'] as Uint8List,
      CaptureStatus.decode(result['status'] as Uint8List),
      ScopeSettings.decode(result['settings'] as Uint8List),
    );
  }

  @override
  Future<CalibrationStatus> calibration() async =>
      CalibrationStatus.decode(await _request('calibration') as Uint8List);

  @override
  Future<void> close() async {
    if (_send != null) {
      try {
        await _request('close').timeout(const Duration(seconds: 5));
      } catch (_) {
        /* worker may already be gone */
      }
    }
    _worker?.kill(priority: Isolate.immediate);
    _worker = null;
    _send = null;
    _receive?.close();
    _receive = null;
    for (final c in _pending.values) {
      if (!c.isCompleted) {
        c.completeError(const ScopeException('USB отключён.'));
      }
    }
    _pending.clear();
  }
}

void _usbWorker(SendPort main) {
  final port = ReceivePort();
  main.send(port.sendPort);
  _LibUsbScope? device;
  port.listen((dynamic raw) {
    final message = Map<String, dynamic>.from(raw as Map),
        id = message['id'] as int;
    try {
      dynamic result;
      switch (message['op']) {
        case 'connect':
          device?.close();
          device = _LibUsbScope();
          // Probe both protocol pages before reporting a successful connection.
          device!.stableSettings();
          device!.readStatus();
          break;
        case 'settings':
          result = _require(device).stableSettings();
        case 'status':
          result = _require(device).readStatus();
        case 'command':
          result = _require(
            device,
          ).command(message['operation'] as int, message['argument'] as int);
        case 'frame':
          result = _require(device).readFrame(
            message['frameId'] as int,
            message['crc'] as int,
            message['length'] as int,
          );
        case 'preview':
          result = _require(device).readPreview();
        case 'calibration':
          result = _require(device).transfer(0x60);
        case 'close':
          device?.close();
          device = null;
        default:
          throw const ScopeException('Неизвестная операция USB.');
      }
      main.send({'id': id, 'value': result});
    } catch (e) {
      if (message['op'] == 'connect') {
        device?.close();
        device = null;
      }
      main.send({
        'id': id,
        'error': e is ScopeException ? e.message : e.toString(),
      });
    }
  });
}

_LibUsbScope _require(_LibUsbScope? device) =>
    device ?? (throw const ScopeException('Прибор не подключён.'));

class _LibUsbScope {
  late final DynamicLibrary _lib;
  late final _InitD _init;
  late final _ExitD _exit;
  late final _OpenD _open;
  late final _CloseD _close;
  late final _ControlD _transfer;
  late final _ErrorD _errorName;
  late final _ClaimD _claim, _release;
  bool _claimed = false;
  Pointer<Void> _ctx = nullptr, _handle = nullptr;

  _LibUsbScope() {
    _lib = _loadLibrary();
    _init = _lib.lookupFunction<_InitN, _InitD>('libusb_init');
    _exit = _lib.lookupFunction<_ExitN, _ExitD>('libusb_exit');
    _open = _lib.lookupFunction<_OpenN, _OpenD>(
      'libusb_open_device_with_vid_pid',
    );
    _close = _lib.lookupFunction<_CloseN, _CloseD>('libusb_close');
    _transfer = _lib.lookupFunction<_ControlN, _ControlD>(
      'libusb_control_transfer',
    );
    _errorName = _lib.lookupFunction<_ErrorN, _ErrorD>('libusb_error_name');
    _claim = _lib.lookupFunction<_ClaimN, _ClaimD>('libusb_claim_interface');
    _release = _lib.lookupFunction<_ClaimN, _ClaimD>(
      'libusb_release_interface',
    );
    final context = calloc<Pointer<Void>>();
    try {
      final rc = _init(context);
      if (rc < 0) {
        throw ScopeException(
          'libusb не запущен: ${_errorName(rc).toDartString()}',
        );
      }
      _ctx = context.value;
      _handle = _open(_ctx, 0x2e3c, 0x5720);
      if (_handle == nullptr) {
        throw const ScopeException(
          '2C53T не найден или занят. Включите USB Sharing и закройте другие программы, работающие с прибором.',
        );
      }
      // WinUSB exposes the device only after its interface is claimed. On
      // macOS/Linux it remains untouched so the native storage driver stays bound.
      if (Platform.isWindows) {
        final claim = _claim(_handle, 0);
        if (claim < 0) {
          throw ScopeException(
            'Не удалось открыть WinUSB: ${_errorName(claim).toDartString()}',
          );
        }
        _claimed = true;
      }
    } catch (_) {
      close();
      rethrow;
    } finally {
      calloc.free(context);
    }
  }

  static DynamicLibrary _loadLibrary() {
    final exe = File(Platform.resolvedExecutable).parent.path;
    final candidates = Platform.isMacOS
        ? [
            '$exe/../Frameworks/libusb-1.0.dylib',
            '/opt/homebrew/lib/libusb-1.0.dylib',
            '/usr/local/lib/libusb-1.0.dylib',
            'libusb-1.0.dylib',
          ]
        : Platform.isWindows
        ? ['$exe\\libusb-1.0.dll', 'libusb-1.0.dll']
        : ['libusb-1.0.so.0', 'libusb-1.0.so'];
    for (final path in candidates) {
      try {
        return DynamicLibrary.open(path);
      } catch (_) {
        /* try next */
      }
    }
    throw const ScopeException(
      'Библиотека libusb-1.0 не найдена. См. инструкцию по установке.',
    );
  }

  Uint8List transfer(
    int request, [
    int value = 0,
    int index = 0,
    int length = 64,
  ]) {
    if (_handle == nullptr) throw const ScopeException('USB отключён.');
    final p = calloc<Uint8>(length);
    try {
      final n = _transfer(
        _handle,
        0xc0,
        request,
        value,
        index & 0xffff,
        p,
        length,
        2000,
      );
      if (n < 0) {
        throw ScopeException('Ошибка USB: ${_errorName(n).toDartString()}');
      }
      return Uint8List.fromList(p.asTypedList(n));
    } finally {
      calloc.free(p);
    }
  }

  Uint8List readStatus() {
    final data = transfer(0x5a);
    CaptureStatus.decode(data);
    return data;
  }

  Uint8List stableSettings() {
    final end = DateTime.now().add(const Duration(seconds: 25));
    while (true) {
      final data = transfer(0x5c), s = ScopeSettings.decode(data);
      if (s.stable) return data;
      if (DateTime.now().isAfter(end)) {
        throw const ScopeException('Прибор не подтвердил настройки.');
      }
      sleep(const Duration(milliseconds: 50));
    }
  }

  Uint8List command(int operation, int argument, {bool allowNoFrame = false}) {
    var data = transfer(0x5a, operation, argument);
    var status = CaptureStatus.decode(data);
    if (status.commandResult != 0) {
      if (allowNoFrame && status.commandResult == 7) return data;
      throw ScopeException(CaptureStatus.errorText(status.commandResult));
    }
    final id = status.commandId,
        end = DateTime.now().add(const Duration(seconds: 25));
    while (status.commandDone != id) {
      if (DateTime.now().isAfter(end)) {
        throw const ScopeException('Прибор не подтвердил USB-команду.');
      }
      sleep(const Duration(milliseconds: 50));
      data = readStatus();
      status = CaptureStatus.decode(data);
    }
    if (status.error != 0 && !(allowNoFrame && status.error == 7)) {
      throw ScopeException(status.errorMessage);
    }
    return data;
  }

  Map<String, dynamic> readFrame(int expectedId, int expectedCrc, int length) {
    if (expectedId == 0 || length != 0xe10) {
      throw const ScopeException('Нет полного сохранённого кадра.');
    }
    final bytes = Uint8List(length);
    for (var offset = 0; offset < length; offset += 64) {
      final size = length - offset < 64 ? length - offset : 64;
      final block = transfer(0x5b, offset, expectedId, size);
      if (block.length != size) {
        throw const ScopeException('Кадр изменился во время чтения.');
      }
      bytes.setRange(offset, offset + size, block);
    }
    final after = CaptureStatus.decode(readStatus());
    if (after.frameId != expectedId ||
        after.frameCrc != expectedCrc ||
        crc32(bytes) != expectedCrc) {
      throw const ScopeException(
        'Кадр изменился или контрольная сумма не совпала.',
      );
    }
    return {'bytes': bytes, 'settings': stableSettings()};
  }

  Map<String, dynamic> readPreview() {
    if (!ScopeSettings.decode(stableSettings()).liveSnapshotSupported) {
      throw const ScopeException(
        'Для живого просмотра обновите прошивку до r6j.',
      );
    }
    final previous = CaptureStatus.decode(readStatus());
    final raw = command(34, 0, allowNoFrame: true);
    final current = CaptureStatus.decode(raw);
    if (current.commandResult == 7 || current.error == 7) {
      return {'notReady': true};
    }
    if (current.phaseName != 'ready' || current.frameId == previous.frameId) {
      throw const ScopeException('Прибор не опубликовал новый кадр.');
    }
    final result = readFrame(
      current.frameId,
      current.frameCrc,
      current.frameBytes,
    );
    final frameSettings = result['settings'] as Uint8List;
    if (((frameSettings[63] >> 4) & 3) != 2) {
      throw const ScopeException('Прибор вернул кадр другого типа.');
    }
    result['status'] = raw;
    return result;
  }

  void close() {
    if (_handle != nullptr) {
      if (_claimed) {
        _release(_handle, 0);
        _claimed = false;
      }
      _close(_handle);
      _handle = nullptr;
    }
    if (_ctx != nullptr) {
      _exit(_ctx);
      _ctx = nullptr;
    }
  }
}
