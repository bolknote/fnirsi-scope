import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' as ui;
import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'scope/controller.dart';
import 'scope/decoder.dart';
import 'scope/measurements.dart';
import 'scope/protocol.dart';
import 'scope/recording.dart';
import 'scope/usb.dart';
import 'ui/firmware_update_dialog.dart';
import 'ui/waveform.dart';

void main() => runApp(const ScopeApp());

const ink = Color(0xffe8f0ed), muted = Color(0xff91a7a2);
const base = Color(0xff151c1c),
    panel = Color(0xff1b2524),
    border = Color(0xff354441);
const green = Color(0xffa4d9be), orange = Color(0xffe8a17d);

class ScopeApp extends StatelessWidget {
  final ScopeController? controller;
  const ScopeApp({super.key, this.controller});
  @override
  Widget build(BuildContext context) => MaterialApp(
    debugShowCheckedModeBanner: false,
    title: 'FNIRSI · Осциллограф',
    theme: ThemeData(
      brightness: Brightness.dark,
      useMaterial3: true,
      colorScheme: const ColorScheme.dark(
        primary: green,
        surface: panel,
        onSurface: ink,
      ),
      scaffoldBackgroundColor: base,
      textTheme: ThemeData.dark().textTheme.apply(
        bodyColor: ink,
        displayColor: ink,
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: const Color(0xff31413e),
          borderRadius: BorderRadius.circular(5),
        ),
      ),
    ),
    home: ScopeWindow(controller: controller),
  );
}

class ScopeWindow extends StatefulWidget {
  final ScopeController? controller;
  const ScopeWindow({super.key, this.controller});
  @override
  State<ScopeWindow> createState() => _ScopeWindowState();
}

enum _ScopePanel {
  signal('Сигнал'),
  measurements('Измерения'),
  capture('Захват'),
  decoder('Декодер');

  final String label;
  const _ScopePanel(this.label);
}

enum _FileAction { open, save, png, csv }

class _ScopeWindowState extends State<ScopeWindow> {
  late final ScopeController c;
  late final bool owns;
  final plotKey = GlobalKey();
  int selected = 0, a = 90, b = 210;
  int cursorChannelA = 0, cursorChannelB = 0;
  bool cursors = false;
  _ScopePanel tool = _ScopePanel.signal;
  int measurementChannel = 0, seriesCount = 5, timeoutSeconds = 30;
  final measures = {
    MeasurementKind.span,
    MeasurementKind.frequency,
    MeasurementKind.pulseWidth,
  };
  bool historyOpen = false;
  RecordedFrame? selectedFrame, referenceFrame;
  SerialProtocol protocol = SerialProtocol.uart;
  int baud = 115200, decodeChannel = 0;
  bool inverted = false, fileBusy = false;
  ScopeFrame? decodedFrame;
  SerialDecode? decoded;

  ScopeFrame? get visibleFrame => selectedFrame?.frame ?? c.frame;
  bool get canDecode => selectedFrame != null || !running;
  bool get available =>
      c.connected && !c.busy && !c.calibrating && c.controlAvailable;
  bool get canEdit => available && !c.capturing;
  bool get running =>
      c.connected &&
      (c.acquisition == Acquisition.live ||
          c.capturing ||
          c.settings?.running == true);
  bool get canCapture => canEdit && !c.dirty;

  @override
  void initState() {
    super.initState();
    owns = widget.controller == null;
    c = widget.controller ?? ScopeController(UsbScopeLink());
  }

  @override
  void dispose() {
    if (owns) c.dispose();
    super.dispose();
  }

  Future<void> _connect() async {
    await c.connect();
    if (mounted && c.settings != null) {
      setState(() => selected = c.settings!.triggerChannel - 1);
    }
  }

  Future<void> _updateFirmware() async {
    if (c.connected) {
      await c.disconnect();
      if (c.connected) return;
    }
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (context) => const FirmwareUpdateDialog(),
    );
  }

  void say(String text) {
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(text), behavior: SnackBarBehavior.floating),
      );
    }
  }

  Future<void> exportCsv() async {
    final frame = visibleFrame;
    if (frame == null || !frame.verified) return;
    try {
      final destination = await getSaveLocation(
        suggestedName: '2c53t-waveform.csv',
        acceptedTypeGroups: [
          const XTypeGroup(label: 'CSV', extensions: ['csv']),
        ],
      );
      if (destination == null) return;
      await XFile.fromData(
        Uint8List.fromList(utf8.encode(frame.csv())),
        mimeType: 'text/csv',
      ).saveTo(destination.path);
      say('Данные сохранены');
    } catch (e) {
      say('Не удалось сохранить CSV: $e');
    }
  }

  Future<void> exportPng() async {
    if (visibleFrame?.verified != true) return;
    try {
      final boundary =
          plotKey.currentContext?.findRenderObject() as RenderRepaintBoundary?;
      if (boundary == null) throw StateError('График не готов');
      final image = await boundary.toImage(pixelRatio: 2);
      final data = await image.toByteData(format: ui.ImageByteFormat.png);
      image.dispose();
      if (data == null) throw StateError('Не удалось создать изображение');
      final destination = await getSaveLocation(
        suggestedName: '2c53t-waveform.png',
        acceptedTypeGroups: [
          const XTypeGroup(label: 'PNG', extensions: ['png']),
        ],
      );
      if (destination == null) return;
      await XFile.fromData(
        data.buffer.asUint8List(),
        mimeType: 'image/png',
      ).saveTo(destination.path);
      say('Кадр сохранён');
    } catch (e) {
      say('Не удалось сохранить PNG: $e');
    }
  }

  Future<void> _fileAction(_FileAction action) async {
    if (fileBusy) return;
    setState(() => fileBusy = true);
    try {
      const types = XTypeGroup(
        label: 'Опыт FNIRSI Scope',
        extensions: [ScopeRecording.extension],
        uniformTypeIdentifiers: ['public.data'],
      );
      switch (action) {
        case _FileAction.open:
          final file = await openFile(acceptedTypeGroups: [types]);
          if (file == null || !mounted) return;
          if (await file.length() > ScopeRecording.maxFileBytes) {
            throw const ScopeException('Файл опыта превышает 4 МБ.');
          }
          final records = ScopeRecording.decode(await file.readAsString());
          if (!mounted) return;
          c.openRecording(records);
          setState(() {
            selectedFrame = records.last;
            referenceFrame = null;
            historyOpen = true;
            tool = _ScopePanel.measurements;
          });
          say('Открыто кадров: ${records.length}');
        case _FileAction.save:
          final snapshot = ScopeRecording.encode(c.recordings);
          final bytes = Uint8List.fromList(utf8.encode(snapshot));
          if (bytes.length > ScopeRecording.maxFileBytes) {
            throw const ScopeException(
              'Опыт превышает допустимый размер файла 4 МБ.',
            );
          }
          final destination = await getSaveLocation(
            suggestedName: 'Опыт 2C53T.${ScopeRecording.extension}',
            acceptedTypeGroups: [types],
          );
          if (destination == null) return;
          await XFile.fromData(bytes).saveTo(destination.path);
          say('Опыт сохранён');
        case _FileAction.png:
          await exportPng();
        case _FileAction.csv:
          await exportCsv();
      }
    } catch (e) {
      say(
        'Не удалось ${action == _FileAction.open ? 'открыть' : 'сохранить'} опыт: $e',
      );
    } finally {
      if (mounted) setState(() => fileBusy = false);
    }
  }

  Future<void> confirmCalibration() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Калибровка нуля'),
        content: const Text(
          'Отсоедините оба щупа от BNC-разъёмов осциллографа и обеспечьте '
          'стабильное питание. Калибровка может занять несколько минут. '
          'После неё усиление и точность напряжения могут оставаться непроверенными.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Отмена'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Оба щупа сняты — начать'),
          ),
        ],
      ),
    );
    if (confirmed == true && mounted) await c.startCalibration();
  }

  Future<void> showGainCheck() async {
    final channel = selected;
    final configure = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Как проверить точность напряжения'),
        content: const SizedBox(
          width: 480,
          child: SingleChildScrollView(
            child: Text(
              'У прибора сейчас номинальное усиление. Для проверки понадобится '
              'батарейка примерно на 1,5 В и отдельный мультиметр.\n\n'
              '1. Измерьте напряжение батарейки мультиметром. Установите '
              'переключатель щупа в положение ×1.\n\n'
              '2. Настройте выбранный канал: связь DC, щуп ×1, '
              '0,5 В/дел и нулевое смещение. Кнопка ниже сделает это за вас.\n\n'
              '3. Подключите зажим земли и наконечник щупа к минусу '
              'батарейки. Запустите просмотр и запомните положение линии.\n\n'
              '4. Оставьте зажим на минусе и коснитесь наконечником плюса. '
              'Посчитайте, на сколько крупных делений сместилась линия, '
              'умножьте это число на 0,5 В и сравните с мультиметром. '
              'Повторите проверку для другого канала.\n\n'
              'Это проверка одной шкалы, а не калибровка: коэффициенты '
              'не изменятся и предупреждение останется. Для проверки всего '
              'диапазона нужны эталонные сигналы.',
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Закрыть'),
          ),
          FilledButton(
            onPressed: canCapture ? () => Navigator.pop(context, true) : null,
            child: Text('Настроить CH${channel + 1}'),
          ),
          if (c.dirty)
            const Text('Сначала примените или отмените изменения настроек.'),
        ],
      ),
    );
    if (configure != true || !mounted || c.draft == null || !canCapture) return;
    c.edit((settings) {
      final channels = [...settings.channels];
      channels[channel] = channels[channel].copyWith(
        enabled: true,
        range: 5,
        probe: 1,
        offset: 0,
        coupling: 'DC',
      );
      return settings.copyWith(
        channels: channels,
        timebase: 17,
        triggerChannel: channel + 1,
        triggerMode: 1,
        triggerLevel: 0,
      );
    });
    await c.apply();
    if (c.error == null) {
      say('CH${channel + 1} настроен: DC, ×1, 0,5 В/дел.');
    }
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: c,
    builder: (context, _) {
      final wide = MediaQuery.sizeOf(context).width >= 720;
      return Scaffold(
        body: SafeArea(
          child: Column(
            children: [
              _header(),
              _toolbar(),
              if (c.error != null) _banner(c.error!, isError: true),
              if (c.settings?.calibrationRequired == true)
                _banner(
                  c.calibration?.baselineCompleted == true &&
                          c.calibration?.gainUnverified == true
                      ? 'Таблица нуля загружена. Усиление не проверено. Напряжения приблизительные.'
                      : 'Прибор сообщает, что требуется калибровка. Значения напряжения могут быть неточными.',
                  action: c.calibration?.gainUnverified == true
                      ? TextButton(
                          onPressed: showGainCheck,
                          child: const Text('Как проверить'),
                        )
                      : null,
                ),
              Expanded(
                child: wide
                    ? Row(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Expanded(child: _workspace()),
                          Container(width: 1, color: border),
                          SizedBox(width: 310, child: _tools()),
                        ],
                      )
                    : SingleChildScrollView(
                        child: Column(
                          children: [
                            SizedBox(height: 500, child: _workspace()),
                            Container(height: 1, color: border),
                            _tools(scroll: false),
                          ],
                        ),
                      ),
              ),
              _history(),
              _footer(),
            ],
          ),
        ),
      );
    },
  );
  Widget _header() => Container(
    height: 60,
    padding: const EdgeInsets.symmetric(horizontal: 20),
    decoration: const BoxDecoration(
      color: panel,
      border: Border(bottom: BorderSide(color: border)),
    ),
    child: Row(
      children: [
        Image.asset('assets/branding/scope-icon.png', width: 33, height: 33),
        const SizedBox(width: 10),
        const Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Осциллограф',
              style: TextStyle(fontSize: 14, fontWeight: FontWeight.w700),
            ),
            Text('FNIRSI 2C53T', style: TextStyle(fontSize: 11, color: muted)),
          ],
        ),
        const Spacer(),
        Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(
            color: c.connected ? green : muted,
            shape: BoxShape.circle,
          ),
        ),
        const SizedBox(width: 7),
        Text(
          c.connected ? 'USB подключён' : 'Нет подключения',
          style: const TextStyle(fontSize: 11, color: muted),
        ),
        const SizedBox(width: 12),
        _button(
          c.connected ? 'Отключить' : 'Подключить',
          c.busy ? null : (c.connected ? c.disconnect : _connect),
          icon: c.connected ? Icons.usb_off : Icons.usb,
        ),
        const SizedBox(width: 7),
        IconButton(
          tooltip: 'Обновление прошивки',
          onPressed: c.busy || c.calibrating ? null : _updateFirmware,
          icon: const Icon(Icons.system_update_alt, size: 17),
        ),
        PopupMenuButton<_FileAction>(
          tooltip: 'Опыт',
          enabled: !fileBusy,
          onSelected: _fileAction,
          position: PopupMenuPosition.under,
          itemBuilder: (context) => [
            const PopupMenuItem(
              value: _FileAction.open,
              child: Text('Открыть опыт…'),
            ),
            PopupMenuItem(
              value: _FileAction.save,
              enabled: c.recordings.isNotEmpty,
              child: const Text('Сохранить опыт…'),
            ),
            const PopupMenuDivider(),
            PopupMenuItem(
              value: _FileAction.png,
              enabled: visibleFrame?.verified == true,
              child: const Text('Экспорт графика PNG…'),
            ),
            PopupMenuItem(
              value: _FileAction.csv,
              enabled: visibleFrame?.verified == true,
              child: const Text('Экспорт кадра CSV…'),
            ),
          ],
          child: const Padding(
            padding: EdgeInsets.symmetric(horizontal: 8, vertical: 9),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.folder_outlined, size: 15),
                SizedBox(width: 5),
                Text('Опыт', style: TextStyle(fontSize: 11)),
              ],
            ),
          ),
        ),
      ],
    ),
  );
  Widget _toolbar() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 9),
      decoration: const BoxDecoration(
        color: panel,
        border: Border(bottom: BorderSide(color: border)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Wrap(
              spacing: 7,
              runSpacing: 7,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                _button(
                  running ? 'Стоп' : 'Запуск',
                  !available
                      ? null
                      : running
                      ? c.stop
                      : c.settings?.liveSnapshotSupported == true
                      ? _start
                      : null,
                  icon: running
                      ? Icons.pause_rounded
                      : Icons.play_arrow_rounded,
                  primary: !running,
                ),
                _button(
                  'Один кадр',
                  canCapture ? _single : null,
                  icon: Icons.center_focus_weak,
                ),
                _button(
                  'Автомасштаб',
                  canCapture && c.frame != null ? _autoScale : null,
                  icon: Icons.fit_screen,
                ),
              ],
            ),
          ),
          const SizedBox(width: 10),
          Flexible(
            fit: FlexFit.loose,
            child: Text(
              _acquisition(),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 11, color: muted),
            ),
          ),
        ],
      ),
    );
  }

  Widget _banner(String text, {bool isError = false, Widget? action}) =>
      Container(
        width: double.infinity,
        color: isError ? const Color(0xff4a2925) : const Color(0xff493b25),
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
        child: Row(
          children: [
            Expanded(
              child: Text(
                text,
                style: const TextStyle(fontSize: 12, color: ink),
              ),
            ),
            if (action != null) ...[const SizedBox(width: 12), action],
          ],
        ),
      );
  Widget _workspace() {
    final frame = visibleFrame;
    final reference = frame == null || frame == referenceFrame?.frame
        ? null
        : referenceFrame?.frame;
    final cursorValueA = frame?.volts[cursorChannelA]?[a];
    final cursorValueB = frame?.volts[cursorChannelB]?[b];
    final sameSetting =
        reference == null ||
        (frame!.triggerChannel == reference.triggerChannel &&
            frame.falling == reference.falling);
    return Padding(
      padding: const EdgeInsets.fromLTRB(18, 15, 18, 0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Осциллограмма',
                      style: TextStyle(
                        fontSize: 14,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    SizedBox(height: 3),
                    Text(
                      'Экранные отсчёты · 301 точка',
                      style: TextStyle(fontSize: 11, color: muted),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Flexible(
                child: Text(
                  selectedFrame == null
                      ? 'Текущий кадр'
                      : 'Кадр №${frame!.id} из истории',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 11, color: muted),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Expanded(
            child: RepaintBoundary(
              key: plotKey,
              child: Stack(
                children: [
                  Positioned.fill(
                    child: WaveformView(
                      frame: frame,
                      reference: reference,
                      decoded: decodedFrame == frame
                          ? decoded?.bytes ?? const []
                          : const [],
                      decodeChannel: decodeChannel,
                      cursors: cursors,
                      cursorA: a,
                      cursorB: b,
                      cursorChannelA: cursorChannelA,
                      cursorChannelB: cursorChannelB,
                      onCursors: (aa, bb) {
                        setState(() {
                          a = aa;
                          b = bb;
                        });
                      },
                    ),
                  ),
                  Positioned(
                    top: 9,
                    left: 45,
                    right: 12,
                    child: Wrap(
                      spacing: 10,
                      runSpacing: 3,
                      children: [
                        for (var i = 0; i < 2; i++)
                          if (frame?.volts[i] != null)
                            _channelLegend(i, frame!.scales[i]),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 14,
            runSpacing: 3,
            children: [
              Text(
                frame != null
                    ? '${formatTime(frame.dt * screenCodesPerDivision)}/дел'
                    : c.settings == null
                    ? '—'
                    : '${formatTime(c.settings!.secondsPerDivision)}/дел',
                style: const TextStyle(fontSize: 11),
              ),
              Text(
                frame != null
                    ? 'Триггер CH${frame.triggerChannel} ${frame.falling ? '↘' : '↗'}'
                    : c.settings == null
                    ? '—'
                    : 'Триггер CH${c.settings!.triggerChannel} ${c.settings!.falling ? '↘' : '↗'}',
                style: const TextStyle(fontSize: 11),
              ),
              if (reference != null)
                Text(
                  '– – Опорный №${reference.id} · по триггеру'
                  '${sameSetting ? '' : ' · разные фронты или каналы'}',
                  style: TextStyle(
                    fontSize: 11,
                    color: sameSetting ? ink : orange,
                  ),
                ),
              if (cursors && frame != null)
                Text(
                  'Δt  ${formatTime((frame.timeAt(b) - frame.timeAt(a)).abs())}'
                  '${cursorValueA == null || cursorValueB == null ? '' : '   ΔU  ${frame.calibrationRequired ? '≈ ' : ''}${formatVoltage(cursorValueB - cursorValueA)}'}',
                  style: const TextStyle(fontSize: 11, color: ink),
                ),
            ],
          ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }

  Widget _channelLegend(int i, double scale) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Container(
        width: 18,
        height: 18,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: channelColors[i],
          borderRadius: BorderRadius.circular(3),
        ),
        child: Text(
          '${i + 1}',
          style: const TextStyle(
            color: base,
            fontSize: 10,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
      const SizedBox(width: 5),
      Text(
        '${formatVoltage(scale)}/дел',
        style: const TextStyle(fontSize: 11, color: ink),
      ),
    ],
  );
  String _acquisition() => switch (c.acquisition) {
    Acquisition.disconnected => 'Прибор не подключён',
    Acquisition.stopped =>
      c.settings?.running == true ? 'Прибор работает' : 'Остановлен',
    Acquisition.live => 'Просмотр',
    Acquisition.waiting =>
      c.triggerArmed ? 'Ожидание фронта' : 'Взведение захвата',
    Acquisition.reading => 'Чтение кадра',
    Acquisition.interrupted => 'Просмотр прерван · проверьте прибор',
  };
  Widget _footer() => Container(
    height: 30,
    padding: const EdgeInsets.symmetric(horizontal: 20),
    decoration: const BoxDecoration(
      color: panel,
      border: Border(top: BorderSide(color: border)),
    ),
    child: Row(
      children: [
        Text(
          c.connected
              ? '2C53T · USB'
              : 'USB Sharing на приборе должен быть включён',
          style: const TextStyle(fontSize: 10, color: muted),
        ),
        const Spacer(),
        if (c.busy)
          const SizedBox(
            width: 12,
            height: 12,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
        if (visibleFrame case final frame?)
          Flexible(
            child: Text(
              frame.verified
                  ? 'Кадр ${frame.id} · CRC проверен'
                  : 'Кадр ${frame.id} · CRC не подтверждён',
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 10, color: muted),
            ),
          ),
      ],
    ),
  );
  Future<void> _start() async {
    setState(() => selectedFrame = null);
    await c.start();
  }

  Future<void> _single() async {
    setState(() {
      tool = _ScopePanel.capture;
      selectedFrame = null;
    });
    await c.single(timeoutSeconds: timeoutSeconds);
  }

  Future<void> _series() async {
    setState(() {
      historyOpen = true;
      selectedFrame = null;
    });
    await c.captureSeries(count: seriesCount, timeoutSeconds: timeoutSeconds);
  }

  void _autoScale() {
    final frame = c.frame;
    if (!canCapture || frame == null || !frame.verified) return;
    final current = c.settings!;
    final bytes = frame.deviceBytes;
    if (bytes == null ||
        bytes[0x2d] != current.timebase ||
        frame.filterMask != current.filterMask) {
      say('Снимите свежий кадр с текущими настройками.');
      return;
    }
    final channels = [...current.channels];
    for (var ch = 0; ch < 2; ch++) {
      final setting = current.channels[ch];
      if (setting.enabled != (frame.volts[ch] != null) ||
          setting.range != bytes[2 + ch] ||
          setting.offset != frame.offsets[ch] ||
          setting.coupling != (bytes[ch] == 0 ? 'AC' : 'DC') ||
          setting.probe != (bytes[6 + ch] == 0 ? 1 : 10)) {
        say('Снимите свежий кадр с текущими настройками.');
        return;
      }
      if (!setting.enabled) continue;
      final measurements = ChannelMeasurements.fromFrame(frame, ch);
      if (measurements.clipped) {
        channels[ch] = setting.copyWith(range: math.min(9, setting.range + 1));
        continue;
      }
      final peak = math.max(
        measurements.minimum.abs(),
        measurements.maximum.abs(),
      );
      if (measurements.smallSignal && peak < setting.voltsPerDivision) continue;
      final probeIndex = setting.probe == 10 ? 3 : 0;
      final range = voltageScales
          .sublist(probeIndex, probeIndex + 10)
          .indexWhere((scale) => peak <= 3 * scale);
      channels[ch] = setting.copyWith(
        range: range == -1 ? 9 : range,
        offset: 0,
      );
    }
    if (List.generate(
      2,
      (i) =>
          channels[i].range == current.channels[i].range &&
          channels[i].offset == current.channels[i].offset,
    ).every((same) => same)) {
      say('По этому кадру менять масштаб не требуется.');
      return;
    }
    final source = current.triggerChannel - 1;
    final voltage =
        (current.triggerLevel - current.channels[source].offset) *
        current.channels[source].voltsPerDivision /
        screenCodesPerDivision;
    final level =
        (voltage * screenCodesPerDivision / channels[source].voltsPerDivision)
            .round() +
        channels[source].offset;
    if (level.abs() > 100) {
      say('Подберите масштаб вручную: текущий порог выйдет за пределы канала.');
      return;
    }
    setState(() => tool = _ScopePanel.signal);
    c.edit((s) => s.copyWith(channels: channels, triggerLevel: level));
    say('Масштаб предложен по кадру. Проверьте и нажмите «Применить».');
  }

  Widget _tools({bool scroll = true}) {
    final contents = Padding(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 14),
      child: switch (tool) {
        _ScopePanel.signal => _settings(),
        _ScopePanel.measurements => _measurements(),
        _ScopePanel.capture => _capturePanel(),
        _ScopePanel.decoder => _decoderPanel(),
      },
    );
    final body = SingleChildScrollView(
      key: ValueKey(tool),
      primary: false,
      child: contents,
    );
    return Container(
      color: panel,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(8, 8, 8, 6),
            child: Row(
              children: [
                for (final p in _ScopePanel.values)
                  Expanded(
                    child: Semantics(
                      selected: p == tool,
                      child: TextButton(
                        onPressed: () => setState(() => tool = p),
                        style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(horizontal: 2),
                          foregroundColor: p == tool ? ink : muted,
                          backgroundColor: p == tool
                              ? const Color(0xff344641)
                              : panel,
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(4),
                          ),
                          textStyle: const TextStyle(fontSize: 11),
                        ),
                        child: Text(p.label, maxLines: 1),
                      ),
                    ),
                  ),
              ],
            ),
          ),
          const Divider(height: 1, color: border),
          if (scroll) Expanded(child: body) else contents,
          if (c.dirty)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 5, 14, 10),
              child: Wrap(
                spacing: 7,
                runSpacing: 5,
                children: [
                  _button('Применить', canEdit ? c.apply : null, primary: true),
                  _button('Отменить', canEdit ? c.discardEdits : null),
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _measurements() {
    final frame = visibleFrame;
    if (frame == null) return const Text('Снимите кадр для измерений.');
    final values = frame.volts[measurementChannel];
    final result = values == null
        ? null
        : ChannelMeasurements.fromFrame(frame, measurementChannel);
    String frequency(double? hz) {
      if (hz == null) return '—';
      final scale = hz >= 1e6
          ? 1e6
          : hz >= 1000
          ? 1000.0
          : 1.0;
      final units = scale == 1e6
          ? 'МГц'
          : scale == 1000
          ? 'кГц'
          : 'Гц';
      return '${(hz / scale).toStringAsPrecision(3).replaceAll('.', ',')} $units';
    }

    String valueOf(MeasurementKind kind) {
      if (result == null) return '—';
      final value = switch (kind) {
        MeasurementKind.span => formatVoltage(result.span),
        MeasurementKind.minimum => formatVoltage(result.minimum),
        MeasurementKind.maximum => formatVoltage(result.maximum),
        MeasurementKind.mean => formatVoltage(result.mean),
        MeasurementKind.frequency => frequency(result.frequency),
        MeasurementKind.period =>
          result.period == null ? '—' : formatTime(result.period!),
        MeasurementKind.pulseWidth =>
          result.pulseWidth == null ? '—' : formatTime(result.pulseWidth!),
        MeasurementKind.dutyCycle =>
          result.dutyCycle == null
              ? '—'
              : '${(result.dutyCycle! * 100).toStringAsFixed(1).replaceAll('.', ',')} %',
      };
      if ((frame.calibrationRequired || result.clipped) &&
          value != '—' &&
          (kind == MeasurementKind.span ||
              kind == MeasurementKind.minimum ||
              kind == MeasurementKind.maximum ||
              kind == MeasurementKind.mean)) {
        return '≈ $value';
      }
      return value;
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _channelPicker(
          measurementChannel,
          (ch) => setState(() => measurementChannel = ch),
        ),
        const SizedBox(height: 8),
        if (result == null) const Text('Этот канал выключен в кадре.'),
        for (final kind in measures)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 5),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    kind.label,
                    style: const TextStyle(fontSize: 11, color: muted),
                  ),
                ),
                const SizedBox(width: 6),
                Text(
                  valueOf(kind),
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
          ),
        if (result?.clipped == true)
          const Text(
            'Сигнал обрезан по вертикали.',
            style: TextStyle(fontSize: 11, color: orange),
          )
        else if (result?.smallSignal == true)
          const Text(
            'Малый размах для измерения времени.',
            style: TextStyle(fontSize: 11, color: muted),
          ),
        if (result != null &&
            !result.clipped &&
            !result.smallSignal &&
            result.period == null &&
            measures.any(
              (kind) =>
                  kind == MeasurementKind.frequency ||
                  kind == MeasurementKind.period ||
                  kind == MeasurementKind.dutyCycle,
            )) ...[
          const SizedBox(height: 4),
          const Text(
            'Для частоты нужны повторяющиеся периоды в кадре.',
            style: TextStyle(fontSize: 11, color: muted),
          ),
        ],
        const SizedBox(height: 4),
        Text(
          'Шаг экранных отсчётов  ${formatTime(frame.dt)}',
          style: const TextStyle(fontSize: 11, color: muted),
        ),
        const Divider(height: 20, color: border),
        Wrap(
          spacing: 7,
          runSpacing: 4,
          children: [
            _button(
              'Курсоры',
              () => setState(() {
                cursors = !cursors;
                if (cursors) {
                  cursorChannelA = cursorChannelB = measurementChannel;
                }
              }),
              icon: Icons.swap_horiz,
              selected: cursors,
            ),
            PopupMenuButton<MeasurementKind>(
              tooltip: 'Выбрать измерения',
              onSelected: (kind) => setState(() {
                if (!measures.remove(kind)) measures.add(kind);
              }),
              itemBuilder: (context) => [
                for (final kind in MeasurementKind.values)
                  CheckedPopupMenuItem(
                    value: kind,
                    checked: measures.contains(kind),
                    child: Text(kind.label),
                  ),
              ],
              child: const Padding(
                padding: EdgeInsets.all(8),
                child: Text('Изменить список…', style: TextStyle(fontSize: 11)),
              ),
            ),
          ],
        ),
        if (cursors) ...[
          const SizedBox(height: 8),
          _cursorInput(
            frame,
            'A',
            a,
            cursorChannelA,
            (index) => setState(() => a = index),
            (ch) => setState(() => cursorChannelA = ch),
          ),
          _cursorInput(
            frame,
            'B',
            b,
            cursorChannelB,
            (index) => setState(() => b = index),
            (ch) => setState(() => cursorChannelB = ch),
          ),
        ],
      ],
    );
  }

  Widget _cursorInput(
    ScopeFrame frame,
    String label,
    int index,
    int channel,
    ValueChanged<int> move,
    ValueChanged<int> choose,
  ) => Row(
    children: [
      Expanded(
        child: _slider(
          label,
          index,
          0,
          screenSampleCount - 1,
          formatTime(frame.timeAt(index)),
          move,
        ),
      ),
      const SizedBox(width: 8),
      DropdownButton<int>(
        value: channel,
        isDense: true,
        style: const TextStyle(fontSize: 11, color: ink),
        items: [
          for (var ch = 0; ch < 2; ch++)
            DropdownMenuItem(value: ch, child: Text('CH${ch + 1}')),
        ],
        onChanged: (ch) {
          if (ch != null) choose(ch);
        },
      ),
    ],
  );

  Widget _channelPicker(int current, ValueChanged<int> change) => Row(
    children: [
      for (var ch = 0; ch < 2; ch++)
        Expanded(
          child: Padding(
            padding: EdgeInsets.only(right: ch == 0 ? 6 : 0),
            child: _button(
              'CH${ch + 1}',
              () => change(ch),
              selected: ch == current,
              channel: ch,
            ),
          ),
        ),
    ],
  );

  Widget _capturePanel() {
    final s = c.draft;
    final progress = c.captureTarget > 1
        ? ' · ${c.capturedCount} из ${c.captureTarget}'
        : '';
    final status = c.capturing
        ? _acquisition() + progress
        : switch (c.captureOutcome) {
            CaptureOutcome.complete =>
              (c.captureTarget > 1 ? 'Серия завершена' : 'Кадр пойман') +
                  progress,
            CaptureOutcome.stopped => 'Захват остановлен$progress',
            CaptureOutcome.timedOut => 'Время ожидания истекло$progress',
            CaptureOutcome.failed => 'Захват прерван$progress',
            null => 'Захват по фронту',
          };
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Semantics(
          liveRegion: true,
          child: Text(
            status,
            style: const TextStyle(fontSize: 12, color: green),
          ),
        ),
        const SizedBox(height: 10),
        if (s != null) ..._triggerSettings(s, canEdit),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: _dropdown<int>(
                'Ожидание кадра',
                timeoutSeconds,
                const [
                  DropdownMenuItem(value: 5, child: Text('5 с')),
                  DropdownMenuItem(value: 15, child: Text('15 с')),
                  DropdownMenuItem(value: 30, child: Text('30 с')),
                  DropdownMenuItem(value: 60, child: Text('1 мин')),
                  DropdownMenuItem(value: 120, child: Text('2 мин')),
                  DropdownMenuItem(value: 600, child: Text('10 мин')),
                ],
                canEdit ? (v) => setState(() => timeoutSeconds = v) : null,
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _dropdown<int>(
                'Кадров в серии',
                seriesCount,
                [
                  for (final n in [2, 3, 5, 10, 20, 50])
                    DropdownMenuItem(value: n, child: Text('$n')),
                ],
                canEdit ? (v) => setState(() => seriesCount = v) : null,
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        _button(
          'Запустить серию',
          canCapture ? _series : null,
          primary: true,
          icon: Icons.layers_outlined,
        ),
        const SizedBox(height: 8),
        const Text(
          'Между захватами возможны пропуски.',
          style: TextStyle(fontSize: 11, color: muted),
        ),
      ],
    );
  }

  Widget _decoderPanel() {
    final frame = visibleFrame;
    final result = frame == decodedFrame ? decoded : null;
    final signal = frame?.volts[decodeChannel];
    void clearResult() {
      decodedFrame = null;
      decoded = null;
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _channelPicker(
          decodeChannel,
          (ch) => setState(() {
            decodeChannel = ch;
            clearResult();
          }),
        ),
        const SizedBox(height: 10),
        _dropdown<SerialProtocol>(
          'Протокол',
          protocol,
          [
            for (final choice in SerialProtocol.values)
              DropdownMenuItem(value: choice, child: Text(choice.label)),
          ],
          (v) => setState(() {
            protocol = v;
            clearResult();
          }),
        ),
        const SizedBox(height: 8),
        _dropdown<int>(
          'Скорость',
          baud,
          [
            for (final n in [9600, 19200, 38400, 57600, 115200])
              DropdownMenuItem(value: n, child: Text('$n бит/с')),
          ],
          (v) => setState(() {
            baud = v;
            clearResult();
          }),
        ),
        CheckboxListTile(
          contentPadding: EdgeInsets.zero,
          dense: true,
          title: const Text(
            'Инвертировать уровень',
            style: TextStyle(fontSize: 11),
          ),
          value: inverted,
          onChanged: (v) => setState(() {
            inverted = v ?? false;
            clearResult();
          }),
        ),
        _button(
          'Декодировать кадр',
          signal == null || frame == null || !canDecode
              ? null
              : () {
                  final min = signal.reduce(math.min),
                      max = signal.reduce(math.max);
                  setState(() {
                    decodedFrame = frame;
                    decoded = SerialDecode.fromFrame(
                      frame,
                      channel: decodeChannel,
                      protocol: protocol,
                      baud: baud,
                      inverted: inverted,
                      threshold: (max + min) / 2,
                    );
                  });
                },
          icon: Icons.code,
        ),
        const SizedBox(height: 8),
        if (result?.bytes.isNotEmpty == true)
          SelectableText(
            result!.bytes.map((byte) => byte.hex).join(' '),
            style: const TextStyle(fontFamily: 'monospace', fontSize: 14),
          )
        else if (result != null)
          Text(
            result.message,
            style: const TextStyle(fontSize: 11, color: muted),
          )
        else
          const Text(
            'Выберите канал и остановите кадр с посылкой.',
            style: TextStyle(fontSize: 11, color: muted),
          ),
      ],
    );
  }

  Future<void> _note(RecordedFrame record) async {
    var note = record.note;
    final result = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Заметка к кадру'),
        content: TextFormField(
          initialValue: note,
          autofocus: true,
          maxLength: 120,
          onChanged: (value) => note = value,
          onFieldSubmitted: (value) => Navigator.pop(context, value),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Отменить'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, note),
            child: const Text('Сохранить'),
          ),
        ],
      ),
    );
    if (result != null && mounted) setState(() => record.note = result.trim());
  }

  Widget _history() {
    final records = c.recordings;
    final canSave =
        c.frame?.verified == true &&
        !running &&
        !c.busy &&
        selectedFrame == null;
    return Container(
      decoration: const BoxDecoration(
        color: panel,
        border: Border(top: BorderSide(color: border)),
      ),
      padding: const EdgeInsets.fromLTRB(14, 6, 14, 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 6,
            runSpacing: 5,
            children: [
              _button(
                'Кадры ${records.length}',
                () => setState(() => historyOpen = !historyOpen),
                icon: Icons.layers_outlined,
                selected: historyOpen,
              ),
              if (selectedFrame != null)
                _button(
                  'К текущему',
                  () => setState(() => selectedFrame = null),
                  icon: Icons.undo,
                ),
              if (referenceFrame != null)
                _button(
                  'Убрать наложение',
                  () => setState(() => referenceFrame = null),
                ),
              _button(
                'Сохранить кадр',
                canSave
                    ? () {
                        final record = c.recordCurrentFrame();
                        if (record != null) {
                          setState(() {
                            selectedFrame = record;
                            historyOpen = true;
                          });
                        }
                      }
                    : null,
                icon: Icons.bookmark_add_outlined,
              ),
            ],
          ),
          if (historyOpen) ...[
            const SizedBox(height: 7),
            if (records.isEmpty)
              const Text(
                'Сохранённых кадров пока нет.',
                style: TextStyle(fontSize: 11, color: muted),
              )
            else
              SizedBox(
                height: 56,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  reverse: true,
                  itemCount: records.length,
                  separatorBuilder: (context, index) =>
                      const SizedBox(width: 7),
                  itemBuilder: (context, index) {
                    final record = records[records.length - 1 - index];
                    final frame = record.frame;
                    final time = TimeOfDay.fromDateTime(
                      frame.capturedAt.toLocal(),
                    );
                    final timestamp =
                        '${time.hour.toString().padLeft(2, '0')}:${time.minute.toString().padLeft(2, '0')}';
                    return Container(
                      width: 242,
                      padding: const EdgeInsets.all(5),
                      decoration: BoxDecoration(
                        border: Border.all(
                          color: record == selectedFrame ? muted : border,
                        ),
                        borderRadius: BorderRadius.circular(5),
                      ),
                      child: Row(
                        children: [
                          Expanded(
                            child: InkWell(
                              onTap: () =>
                                  setState(() => selectedFrame = record),
                              onLongPress: () => _note(record),
                              borderRadius: BorderRadius.circular(4),
                              child: Padding(
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 5,
                                ),
                                child: Column(
                                  mainAxisAlignment: MainAxisAlignment.center,
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      '№${frame.id} · $timestamp',
                                      style: const TextStyle(
                                        fontSize: 11,
                                        fontWeight: FontWeight.w600,
                                      ),
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                    Text(
                                      record.note.isEmpty
                                          ? '${formatTime(frame.dt * screenCodesPerDivision)}/дел'
                                          : record.note,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        fontSize: 10,
                                        color: muted,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                            ),
                          ),
                          const SizedBox(width: 4),
                          _button(
                            record == referenceFrame ? 'Опорный' : 'Наложить',
                            () => setState(() {
                              referenceFrame = record == referenceFrame
                                  ? null
                                  : record;
                            }),
                            selected: record == referenceFrame,
                          ),
                        ],
                      ),
                    );
                  },
                ),
              ),
            if (selectedFrame case final record?)
              Align(
                alignment: Alignment.centerLeft,
                child: _button(
                  'Заметка…',
                  () => _note(record),
                  icon: Icons.edit_outlined,
                ),
              ),
          ],
        ],
      ),
    );
  }

  Widget _settings() {
    final s = c.draft, enabled = canEdit && s != null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Row(
          children: [
            Text(
              'Настройки',
              style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700),
            ),
            Spacer(),
            Icon(Icons.tune, size: 17, color: muted),
          ],
        ),
        const SizedBox(height: 12),
        _channelPicker(selected, (ch) => setState(() => selected = ch)),
        if (s == null) ...[
          const SizedBox(height: 20),
          const Text(
            'Подключите прибор, чтобы увидеть настройки.',
            style: TextStyle(color: muted, fontSize: 12),
          ),
        ] else ...[
          const SizedBox(height: 10),
          Row(
            children: [
              SizedBox(
                width: 20,
                height: 20,
                child: Checkbox(
                  value: s.channels[selected].enabled,
                  onChanged: enabled
                      ? (value) {
                          if (value == false &&
                              s.channels.where((x) => x.enabled).length == 1) {
                            say('Нужен хотя бы один включённый канал');
                            return;
                          }
                          final channels = [...s.channels];
                          channels[selected] = channels[selected].copyWith(
                            enabled: value,
                          );
                          final trigger = channels[s.triggerChannel - 1].enabled
                              ? s.triggerChannel
                              : (selected == 0 ? 2 : 1);
                          c.edit(
                            (x) => x.copyWith(
                              channels: channels,
                              triggerChannel: trigger,
                            ),
                          );
                        }
                      : null,
                ),
              ),
              const SizedBox(width: 8),
              const Text('Показывать канал', style: TextStyle(fontSize: 12)),
            ],
          ),
          const SizedBox(height: 10),
          _dropdown<int>(
            'Масштаб',
            s.channels[selected].range,
            List.generate(
              10,
              (i) => DropdownMenuItem(
                value: i,
                child: Text(
                  '${formatVoltage(voltageScales[i + (s.channels[selected].probe == 10 ? 3 : 0)])}/дел',
                ),
              ),
            ),
            enabled ? (v) => _editChannel((ch) => ch.copyWith(range: v)) : null,
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: _dropdown<String>(
                  'Связь',
                  s.channels[selected].coupling,
                  const [
                    DropdownMenuItem(value: 'AC', child: Text('AC')),
                    DropdownMenuItem(value: 'DC', child: Text('DC')),
                  ],
                  enabled
                      ? (v) => _editChannel((ch) => ch.copyWith(coupling: v))
                      : null,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _dropdown<int>(
                  'Щуп',
                  s.channels[selected].probe,
                  const [
                    DropdownMenuItem(value: 1, child: Text('×1')),
                    DropdownMenuItem(value: 10, child: Text('×10')),
                  ],
                  enabled
                      ? (v) => _editChannel((ch) => ch.copyWith(probe: v))
                      : null,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          _slider(
            'Смещение',
            s.channels[selected].offset,
            -100,
            100,
            '${(s.channels[selected].offset / 25).toStringAsFixed(2)} дел',
            enabled
                ? (v) => _editChannel((ch) => ch.copyWith(offset: v))
                : null,
          ),
          const SizedBox(height: 8),
          Tooltip(
            message:
                'Цифровой фильтр экранных данных, не аналогового входа или триггера. На 1 мкс/дел и медленнее — обход.',
            child: Row(
              children: [
                SizedBox(
                  width: 20,
                  height: 20,
                  child: Checkbox(
                    value: (s.filterMask & (1 << selected)) != 0,
                    onChanged: enabled
                        ? (value) => c.edit(
                            (x) => x.copyWith(
                              filterMask: value == true
                                  ? x.filterMask | (1 << selected)
                                  : x.filterMask & ~(1 << selected),
                            ),
                          )
                        : null,
                  ),
                ),
                const SizedBox(width: 8),
                const Expanded(
                  child: Text(
                    'Цифровой ФНЧ 20 МГц',
                    style: TextStyle(fontSize: 11),
                  ),
                ),
                if ((s.filterMask & (1 << selected)) != 0 && s.timebase >= 6)
                  const Text(
                    'Обход',
                    style: TextStyle(fontSize: 10, color: muted),
                  ),
              ],
            ),
          ),
          const Divider(height: 24, color: border),
          _smallHeading('Развёртка'),
          const SizedBox(height: 9),
          _dropdown<int>(
            'Время на деление',
            s.timebase,
            List.generate(
              timeScales.length,
              (i) => DropdownMenuItem(
                value: i,
                child: Text(
                  '${formatTime(timeScales[i])}/дел${i >= captureTimeScaleCount ? ' *' : ''}',
                ),
              ),
            ),
            enabled ? (v) => c.edit((x) => x.copyWith(timebase: v)) : null,
          ),
          if (s.timebase >= captureTimeScaleCount)
            const Padding(
              padding: EdgeInsets.only(top: 5),
              child: Text(
                '* Для чтения кадра выберите до 20 мс/дел.',
                style: TextStyle(color: orange, fontSize: 10),
              ),
            ),
        ],
        const Divider(height: 24, color: border),
        _smallHeading('Калибровка нуля'),
        const SizedBox(height: 5),
        Text(
          c.calibration?.description ??
              (s?.liveSnapshotSupported == true
                  ? 'Оба щупа нужно отсоединить от прибора.'
                  : 'Для управления нужна прошивка r6j.'),
          style: const TextStyle(color: muted, fontSize: 11),
        ),
        const SizedBox(height: 7),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            _button(
              'Калибровать',
              enabled && s.liveSnapshotSupported ? confirmCalibration : null,
            ),
            _button(
              'Прервать',
              c.connected && !c.busy && c.calibrating
                  ? c.cancelCalibration
                  : null,
            ),
          ],
        ),
      ],
    );
  }

  List<Widget> _triggerSettings(ScopeSettings s, bool enabled) => [
    Row(
      children: [
        Expanded(
          child: _dropdown<int>(
            'Канал',
            s.triggerChannel,
            [
              for (var i = 0; i < 2; i++)
                if (s.channels[i].enabled)
                  DropdownMenuItem(value: i + 1, child: Text('CH${i + 1}')),
            ],
            enabled
                ? (v) => c.edit((x) => x.copyWith(triggerChannel: v))
                : null,
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: _dropdown<bool>(
            'Фронт',
            s.falling,
            const [
              DropdownMenuItem(value: false, child: Text('↗ Рост')),
              DropdownMenuItem(value: true, child: Text('↘ Спад')),
            ],
            enabled ? (v) => c.edit((x) => x.copyWith(falling: v)) : null,
          ),
        ),
      ],
    ),
    const SizedBox(height: 8),
    _dropdown<int>(
      'Режим',
      s.triggerMode,
      const [
        DropdownMenuItem(value: 0, child: Text('Обычный')),
        DropdownMenuItem(value: 1, child: Text('Авто')),
        DropdownMenuItem(value: 2, child: Text('Один кадр')),
      ],
      enabled ? (v) => c.edit((x) => x.copyWith(triggerMode: v)) : null,
    ),
    const SizedBox(height: 10),
    _slider(
      'Порог',
      s.triggerLevel,
      -100,
      100,
      formatVoltage(
        (s.triggerLevel - s.channels[s.triggerChannel - 1].offset) *
            s.channels[s.triggerChannel - 1].voltsPerDivision /
            25,
      ),
      enabled ? (v) => c.edit((x) => x.copyWith(triggerLevel: v)) : null,
    ),
    const SizedBox(height: 10),
    _slider(
      'До триггера',
      ((s.triggerPosition + 150) / 3).round().clamp(0, 100),
      0,
      100,
      '${((s.triggerPosition + 150) / 3).round().clamp(0, 100)} %',
      enabled
          ? (v) => c.edit((x) => x.copyWith(triggerPosition: v * 3 - 150))
          : null,
    ),
  ];

  void _editChannel(ChannelSettings Function(ChannelSettings) fn) {
    c.edit((s) {
      final channels = [...s.channels];
      channels[selected] = fn(channels[selected]);
      return s.copyWith(channels: channels);
    });
  }

  Widget _smallHeading(String t) => Text(
    t.toUpperCase(),
    style: const TextStyle(
      fontSize: 10,
      letterSpacing: .9,
      color: muted,
      fontWeight: FontWeight.w700,
    ),
  );
  Widget _dropdown<T>(
    String label,
    T value,
    List<DropdownMenuItem<T>> items,
    void Function(T)? onChange,
  ) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(label, style: const TextStyle(fontSize: 10, color: muted)),
      const SizedBox(height: 4),
      Container(
        height: 34,
        padding: const EdgeInsets.symmetric(horizontal: 8),
        decoration: BoxDecoration(
          border: Border.all(color: border),
          borderRadius: BorderRadius.circular(5),
        ),
        child: DropdownButtonHideUnderline(
          child: DropdownButton<T>(
            value: value,
            isExpanded: true,
            dropdownColor: panel,
            style: const TextStyle(fontSize: 11, color: ink),
            items: items,
            onChanged: onChange == null
                ? null
                : (v) {
                    if (v != null) onChange(v);
                  },
          ),
        ),
      ),
    ],
  );
  Widget _slider(
    String label,
    int value,
    int min,
    int max,
    String display,
    void Function(int)? change,
  ) => Column(
    children: [
      Row(
        children: [
          Text(label, style: const TextStyle(fontSize: 10, color: muted)),
          const Spacer(),
          Text(display, style: const TextStyle(fontSize: 10)),
        ],
      ),
      SizedBox(
        height: 23,
        child: SliderTheme(
          data: SliderTheme.of(context).copyWith(
            trackHeight: 2,
            thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 5),
            overlayShape: const RoundSliderOverlayShape(overlayRadius: 10),
          ),
          child: Slider(
            value: value.clamp(min, max).toDouble(),
            min: min.toDouble(),
            max: max.toDouble(),
            onChanged: change == null ? null : (v) => change(v.round()),
          ),
        ),
      ),
    ],
  );
  Widget _button(
    String text,
    VoidCallback? action, {
    IconData? icon,
    bool primary = false,
    bool selected = false,
    int? channel,
  }) => SizedBox(
    height: 33,
    child: TextButton.icon(
      onPressed: action,
      icon: icon != null
          ? Icon(icon, size: 15)
          : channel != null
          ? Container(
              width: 8,
              height: 8,
              decoration: BoxDecoration(
                color: channelColors[channel],
                borderRadius: BorderRadius.circular(2),
              ),
            )
          : const SizedBox.shrink(),
      label: Text(
        text,
        style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w600),
      ),
      style: TextButton.styleFrom(
        foregroundColor: primary ? base : ink,
        backgroundColor: primary
            ? green
            : (selected ? const Color(0xff344641) : panel),
        disabledForegroundColor: muted.withValues(alpha: .55),
        disabledBackgroundColor: primary ? const Color(0xff344641) : panel,
        side: BorderSide(color: selected ? muted : border),
        padding: const EdgeInsets.symmetric(horizontal: 10),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(5)),
      ),
    ),
  );
}
