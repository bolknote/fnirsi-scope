import 'dart:math' as math;
import 'dart:typed_data';

/// Remote-control protocol implemented by the 2C53T RU REMOTE r6 firmware.
/// These are screen samples, not an unverified stream of raw ADC samples.
const List<double> voltageScales = [
  .01,
  .02,
  .05,
  .1,
  .2,
  .5,
  1,
  2,
  5,
  10,
  20,
  50,
  100,
];
const List<double> timeScales = [
  1e-8,
  2e-8,
  5e-8,
  1e-7,
  2e-7,
  5e-7,
  1e-6,
  2e-6,
  5e-6,
  1e-5,
  2e-5,
  5e-5,
  1e-4,
  2e-4,
  5e-4,
  .001,
  .002,
  .005,
  .01,
  .02,
  .05,
  .1,
  .2,
  .5,
  1,
  2,
  5,
  10,
  20,
];
const int captureTimeScaleCount = 20;
const int screenSampleCount = 301;
const int screenCenterCode = 128;
const int screenCodesPerDivision = 25;
const int screenBottomCode = 28;
const int screenTopCode = 228;
const List<String> capturePhases = [
  'idle',
  'arming',
  'armed',
  'saving',
  'ready',
  'gap',
  'error',
  'cancelled',
];
const List<String> deviceErrors = [
  'Нет ошибки',
  'Прибор занят',
  'Недопустимая настройка',
  'Время ожидания триггера истекло',
  'Не удалось сохранить кадр в приборе',
  'Идёт калибровка или ошибка SPI',
  'Нет памяти для кадра',
  'Нет сохранённого кадра',
  'Неподдерживаемая развёртка',
  'На приборе не открыт режим осциллографа',
];

class ScopeException implements Exception {
  final String message;
  const ScopeException(this.message);
  @override
  String toString() => message;
}

int signedByte(int n) => n > 127 ? n - 256 : n;

class CaptureStatus {
  final int phase, error, commandResult, frameId, frameCrc, frameBytes;
  final int commandId, commandDone, pretrigger, filterMask;
  const CaptureStatus({
    required this.phase,
    required this.error,
    required this.commandResult,
    required this.frameId,
    required this.frameCrc,
    required this.frameBytes,
    required this.commandId,
    required this.commandDone,
    required this.pretrigger,
    this.filterMask = 0,
  });

  factory CaptureStatus.decode(Uint8List bytes) {
    if (bytes.length != 64) {
      throw const ScopeException('Неверный ответ статуса USB.');
    }
    final d = ByteData.sublistView(bytes);
    int word(int n) => d.getUint32(4 * n, Endian.little);
    if (word(0) != 0x35465043 || word(1) != 0x00050001) {
      throw const ScopeException(
        'Прибор не поддерживает протокол захвата RU REMOTE r6.',
      );
    }
    if (word(2) >= capturePhases.length) {
      throw const ScopeException('Неизвестная фаза захвата.');
    }
    return CaptureStatus(
      phase: word(2),
      error: word(3) & 0xffff,
      commandResult: word(3) >> 16,
      frameId: word(4),
      frameCrc: word(5),
      commandId: word(8),
      commandDone: word(9),
      frameBytes: word(12),
      pretrigger: word(15),
      filterMask: word(14) & 3,
    );
  }
  String get phaseName => capturePhases[phase];
  String get errorMessage => errorText(error);
  static String errorText(int code) =>
      code < deviceErrors.length ? deviceErrors[code] : 'Ошибка прибора $code';
}

class ChannelSettings {
  final bool enabled;
  final int range, probe, offset;
  final String coupling;
  final bool fft;
  const ChannelSettings({
    required this.enabled,
    required this.range,
    required this.probe,
    required this.offset,
    required this.coupling,
    this.fft = false,
  });
  double get voltsPerDivision => voltageScales[range + (probe == 10 ? 3 : 0)];
  ChannelSettings copyWith({
    bool? enabled,
    int? range,
    int? probe,
    int? offset,
    String? coupling,
  }) => ChannelSettings(
    enabled: enabled ?? this.enabled,
    range: range ?? this.range,
    probe: probe ?? this.probe,
    offset: offset ?? this.offset,
    coupling: coupling ?? this.coupling,
    fft: fft,
  );
}

class CalibrationStatus {
  final bool active, needsCalibration, baselineCompleted, gainUnverified;
  final bool inStep, cancelRequested, spiFault;
  final int seconds, frames, steps, reason;
  const CalibrationStatus({
    required this.active,
    required this.seconds,
    required this.frames,
    required this.steps,
    required this.reason,
    required this.needsCalibration,
    required this.baselineCompleted,
    required this.gainUnverified,
    required this.inStep,
    required this.cancelRequested,
    required this.spiFault,
  });
  factory CalibrationStatus.decode(Uint8List data) {
    if (data.length != 64) {
      throw const ScopeException('Неверный статус калибровки.');
    }
    final d = ByteData.sublistView(data);
    int word(int i) => d.getUint32(4 * i, Endian.little);
    if (word(0) != 0x364c4143 ||
        word(1) != 1 ||
        [2, 7, 8, 9, 10, 12].any((i) => word(i) > 1)) {
      throw const ScopeException('Неизвестный статус калибровки.');
    }
    return CalibrationStatus(
      active: word(2) != 0,
      seconds: word(3),
      frames: word(4),
      steps: word(5),
      reason: word(6),
      needsCalibration: word(7) != 0,
      baselineCompleted: word(8) != 0,
      gainUnverified: word(9) != 0,
      inStep: word(10) != 0,
      cancelRequested: word(11) != 0,
      spiFault: word(12) != 0,
    );
  }
  String get description {
    if (active) {
      return cancelRequested
          ? 'Останавливаем калибровку…'
          : 'Калибровка нуля: $seconds с · шагов: $steps';
    }
    switch (reason) {
      case 1:
        return 'Калибровка прервана, прежние коэффициенты восстановлены.';
      case 2:
        return 'Калибровка остановлена: предел коэффициентов.';
      case 3:
        return 'Калибровка остановлена: нет новых отсчётов.';
      case 4:
        return 'Калибровка остановлена: нет продвижения.';
      case 5:
        return 'Калибровка остановлена по времени.';
      case 6:
        return 'Калибровка остановлена: ошибка SPI.';
      case 7:
        return 'Калибровка остановлена: неверная таблица.';
      case 8:
        if (baselineCompleted && gainUnverified) {
          return 'Таблица нуля загружена; усиление не проверено.';
        }
        if (baselineCompleted) {
          return 'Таблица нуля загружена; требуется проверка калибровки.';
        }
        return 'Требуется калибровка нуля.';
      case 10:
        return 'Нуль откалиброван; усиление номинальное, точность не проверена.';
      default:
        return baselineCompleted
            ? (gainUnverified
                  ? 'Таблица нуля загружена; усиление не проверено.'
                  : 'Калибровка нуля доступна.')
            : 'Калибровка нуля доступна.';
    }
  }
}

class ScopeSettings {
  final int generation, stage, error, channelMask, timebase, triggerChannel;
  final int triggerMode, triggerLevel, triggerPosition, filterMask;
  final bool running,
      falling,
      calibrationActive,
      spiFault,
      calibrationRequired,
      mathMode,
      preview,
      liveSnapshotSupported;
  final List<ChannelSettings> channels;
  const ScopeSettings({
    required this.generation,
    required this.stage,
    required this.error,
    required this.channelMask,
    required this.timebase,
    required this.triggerChannel,
    required this.triggerMode,
    required this.triggerLevel,
    required this.triggerPosition,
    required this.filterMask,
    required this.running,
    required this.falling,
    required this.calibrationActive,
    required this.spiFault,
    required this.calibrationRequired,
    required this.mathMode,
    required this.preview,
    this.liveSnapshotSupported = false,
    required this.channels,
  });
  bool get stable => generation.isEven && stage == 0;
  double get secondsPerDivision => timeScales[timebase];

  factory ScopeSettings.decode(Uint8List bytes) {
    if (bytes.length != 64) {
      throw const ScopeException('Неверный ответ настроек USB.');
    }
    final d = ByteData.sublistView(bytes);
    if (d.getUint32(0, Endian.little) != 0x364c5443 ||
        d.getUint32(4, Endian.little) != 0x00060001) {
      throw const ScopeException(
        'Нужна прошивка RU REMOTE r6 с USB-управлением.',
      );
    }
    final mask = bytes[24 + 0x14],
        src = bytes[24 + 0x16],
        mode = bytes[24 + 0x17],
        edge = bytes[24 + 0x18];
    if (mask < 1 ||
        mask > 3 ||
        bytes[56] >= timeScales.length ||
        src > 1 ||
        mode > 2 ||
        edge > 1 ||
        d.getUint32(16, Endian.little) > 3) {
      throw const ScopeException(
        'Прибор вернул некорректные настройки осциллографа.',
      );
    }
    final channels = List.generate(2, (i) {
      final coupling = bytes[24 + i],
          range = bytes[26 + i],
          probe = bytes[30 + i];
      if (coupling > 1 || range > 9 || probe > 1) {
        throw const ScopeException(
          'Прибор вернул некорректные настройки канала.',
        );
      }
      return ChannelSettings(
        enabled: mask & (1 << i) != 0,
        range: range,
        probe: probe == 0 ? 1 : 10,
        coupling: coupling == 0 ? 'AC' : 'DC',
        offset: signedByte(bytes[28 + i]),
        fft: bytes[34 + i] != 0,
      );
    });
    final flags = bytes[62];
    return ScopeSettings(
      generation: d.getUint32(8, Endian.little),
      stage: d.getUint32(16, Endian.little),
      error: d.getUint32(20, Endian.little),
      channelMask: mask,
      timebase: bytes[56],
      triggerChannel: src + 1,
      triggerMode: mode,
      falling: edge == 1,
      triggerLevel: d.getInt16(24 + 0x1c, Endian.little),
      triggerPosition: d.getInt16(24 + 0x1a, Endian.little),
      running: bytes[57] != 0,
      calibrationActive: flags & 1 != 0,
      spiFault: flags & 2 != 0,
      calibrationRequired: flags & 4 != 0,
      mathMode: flags & 8 != 0,
      filterMask: bytes[63] & 3,
      preview: bytes[63] >> 4 != 0,
      liveSnapshotSupported: bytes[63] & 4 != 0,
      channels: channels,
    );
  }
  ScopeSettings copyWith({
    List<ChannelSettings>? channels,
    int? timebase,
    int? triggerChannel,
    int? triggerMode,
    int? triggerLevel,
    int? triggerPosition,
    int? filterMask,
    bool? falling,
    bool? running,
  }) {
    final next = channels ?? this.channels;
    return ScopeSettings(
      generation: generation,
      stage: stage,
      error: error,
      channelMask: (next[0].enabled ? 1 : 0) | (next[1].enabled ? 2 : 0),
      channels: next,
      timebase: timebase ?? this.timebase,
      triggerChannel: triggerChannel ?? this.triggerChannel,
      triggerMode: triggerMode ?? this.triggerMode,
      triggerLevel: triggerLevel ?? this.triggerLevel,
      triggerPosition: triggerPosition ?? this.triggerPosition,
      running: running ?? this.running,
      falling: falling ?? this.falling,
      calibrationActive: calibrationActive,
      spiFault: spiFault,
      calibrationRequired: calibrationRequired,
      mathMode: mathMode,
      filterMask: filterMask ?? this.filterMask,
      preview: preview,
      liveSnapshotSupported: liveSnapshotSupported,
    );
  }
}

class ScopeFrame {
  final int id, crc, triggerIndex, triggerChannel, triggerLevel;
  final double dt;
  final List<List<double>?> volts;
  final List<int> offsets;
  final List<double> scales;
  final bool preview, falling;
  final bool calibrationRequired;
  final int filterMask;
  final Uint8List? deviceBytes;

  /// Only frames that passed the device's CRC may be exported.
  final bool verified;
  final DateTime capturedAt;
  const ScopeFrame({
    required this.id,
    required this.crc,
    required this.triggerIndex,
    required this.triggerChannel,
    required this.triggerLevel,
    required this.falling,
    required this.dt,
    required this.volts,
    required this.offsets,
    required this.scales,
    required this.preview,
    this.verified = true,
    this.calibrationRequired = false,
    this.filterMask = 0,
    this.deviceBytes,
    required this.capturedAt,
  });

  factory ScopeFrame.decode(
    Uint8List frame,
    CaptureStatus status,
    ScopeSettings settings,
  ) {
    if (status.frameBytes != 0xe10) {
      throw const ScopeException('Прибор вернул неполный кадр.');
    }
    return ScopeFrame.fromDeviceBytes(
      frame,
      id: status.frameId,
      crc: status.frameCrc,
      preview: settings.preview,
      calibrationRequired: settings.calibrationRequired,
      filterMask: status.filterMask,
      capturedAt: DateTime.now(),
    );
  }

  factory ScopeFrame.fromDeviceBytes(
    Uint8List frame, {
    required int id,
    required int crc,
    required bool preview,
    required bool calibrationRequired,
    required int filterMask,
    required DateTime capturedAt,
  }) {
    if (frame.length != 0xe10 || id <= 0 || id > 0xffffffff) {
      throw const ScopeException('Неполный кадр осциллографа.');
    }
    if (crc32(frame) != crc) {
      throw const ScopeException('Контрольная сумма кадра не совпала.');
    }
    if (frame[0x34] != 0 || frame[10] != 0 || frame[11] != 0) {
      throw const ScopeException(
        'Выключите FFT и математический режим на приборе.',
      );
    }
    final t = frame[0x2d];
    if (t >= captureTimeScaleCount) {
      throw const ScopeException(
        'Для чтения кадра нужна развёртка не медленнее 20 мс/дел.',
      );
    }
    final mask = frame[0x14];
    if (mask < 1 || mask > 3) {
      throw const ScopeException('В кадре некорректный набор каналов.');
    }
    final offsets = List.generate(2, (i) => signedByte(frame[4 + i]));
    final scales = List.generate(2, (i) {
      final range = frame[2 + i], probe = frame[6 + i];
      if (range > 9 || probe > 1) {
        throw const ScopeException('В кадре некорректный масштаб канала.');
      }
      return voltageScales[range + 3 * probe];
    });
    final volts = List<List<double>?>.generate(
      2,
      (c) => mask & (1 << c) == 0
          ? null
          : List<double>.unmodifiable(
              List<double>.generate(
                screenSampleCount,
                (i) =>
                    (frame[0x356 + c * screenSampleCount + i] -
                        screenCenterCode -
                        offsets[c]) *
                    scales[c] /
                    screenCodesPerDivision,
              ),
            ),
    );
    final pos = ByteData.sublistView(frame).getInt16(0x1a, Endian.little);
    final source = frame[0x16];
    if (source > 1) {
      throw const ScopeException('В кадре некорректные параметры триггера.');
    }
    return ScopeFrame(
      id: id,
      crc: crc,
      triggerIndex: 150 + pos,
      triggerChannel: source + 1,
      triggerLevel: ByteData.sublistView(frame).getInt16(0x1c, Endian.little),
      falling: frame[0x18] == 1,
      dt: timeScales[t] / screenCodesPerDivision,
      volts: List.unmodifiable(volts),
      offsets: List.unmodifiable(offsets),
      scales: List.unmodifiable(scales),
      preview: preview,
      calibrationRequired: calibrationRequired,
      filterMask: filterMask,
      capturedAt: capturedAt,
      deviceBytes: Uint8List.fromList(frame).asUnmodifiableView(),
    );
  }

  double timeAt(num index) => (index - triggerIndex) * dt;
  double indexAtTime(double seconds) => seconds / dt + triggerIndex;
  double displayCode(int channelIndex, double voltage) =>
      screenCenterCode +
      offsets[channelIndex] +
      voltage * screenCodesPerDivision / scales[channelIndex];

  double? peakToPeak(int channel) {
    final values = volts[channel - 1];
    if (values == null || values.isEmpty) return null;
    return values.reduce(math.max) - values.reduce(math.min);
  }

  String csv() {
    if (!verified) {
      throw const ScopeException('Остановите просмотр для проверенного кадра.');
    }
    final b = StringBuffer('index,time_from_trigger_s,ch1_v,ch2_v\r\n');
    for (var i = 0; i < screenSampleCount; i++) {
      final a = volts[0]?[i], c = volts[1]?[i];
      b.write(
        '$i,${timeAt(i).toStringAsPrecision(12)},${a?.toStringAsPrecision(12) ?? ''},${c?.toStringAsPrecision(12) ?? ''}\r\n',
      );
    }
    return b.toString();
  }
}

int crc32(Uint8List data) {
  var c = 0xffffffff;
  for (final b in data) {
    c ^= b;
    for (var bit = 0; bit < 8; bit++) {
      c = (c >> 1) ^ ((c & 1) == 1 ? 0xedb88320 : 0);
    }
  }
  return (c ^ 0xffffffff) & 0xffffffff;
}
