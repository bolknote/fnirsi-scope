import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:fnirsi_scope/scope/decoder.dart';
import 'package:fnirsi_scope/scope/measurements.dart';
import 'package:fnirsi_scope/scope/protocol.dart';
import 'package:fnirsi_scope/scope/recording.dart';
import 'package:fnirsi_scope/ui/waveform.dart';

Uint8List settingsBytes({int timebase = 7, int flags = 0, int preview = 0}) {
  final b = Uint8List(64), d = ByteData.sublistView(b);
  d.setUint32(0, 0x364c5443, Endian.little);
  d.setUint32(4, 0x00060001, Endian.little);
  d.setUint32(8, 8, Endian.little);
  b[24] = 0;
  b[25] = 1;
  b[26] = 0;
  b[27] = 1;
  b[24 + 0x14] = 1;
  b[24 + 0x16] = 0;
  b[24 + 0x17] = 2;
  b[56] = timebase;
  d.setInt16(24 + 0x1a, -90, Endian.little);
  d.setInt16(24 + 0x1c, 8, Endian.little);
  b[62] = flags;
  b[63] = preview << 4;
  return b;
}

Uint8List statusBytes({required int crc, int id = 716}) {
  final b = Uint8List(64), d = ByteData.sublistView(b);
  void put(int index, int value) =>
      d.setUint32(index * 4, value, Endian.little);
  put(0, 0x35465043);
  put(1, 0x00050001);
  put(2, 4);
  put(4, id);
  put(5, crc);
  put(8, 2755);
  put(9, 2755);
  put(12, 3600);
  put(15, 20);
  return b;
}

// A complete wire-format fixture with known voltage and time scales. The
// unused regions of the frozen device state are zero, including calibration.
Uint8List testFrameBytes() {
  final bytes = Uint8List(0xe10);
  bytes[0x14] = 1;
  bytes[0x2d] = 7;
  final data = ByteData.sublistView(bytes);
  data.setInt16(0x1a, -90, Endian.little);
  data.setInt16(0x1c, 8, Endian.little);
  for (var i = 0; i < screenSampleCount; i++) {
    bytes[0x356 + i] = 124 + (i + i ~/ 50) % 6;
  }
  bytes[0x356] = 125;
  return bytes;
}

ScopeFrame sampleFrame(int Function(int) code, {int timebase = 10}) {
  final bytes = testFrameBytes();
  bytes[0x2d] = timebase;
  bytes[4] = 0;
  for (var i = 0; i < screenSampleCount; i++) {
    bytes[0x356 + i] = code(i);
  }
  return ScopeFrame.decode(
    bytes,
    CaptureStatus.decode(statusBytes(crc: crc32(bytes))),
    ScopeSettings.decode(settingsBytes(timebase: timebase)),
  );
}

void main() {
  test(
    'measures adequately sampled PWM and refuses clipped or unresolved timing',
    () {
      final frame = sampleFrame((i) => i % 50 < 20 ? 168 : 108);
      final result = ChannelMeasurements.fromFrame(frame, 0);
      expect(result.span, closeTo(.024, 1e-12));
      expect(result.period, closeTo(40e-6, 1e-12));
      expect(result.frequency, closeTo(25000, 1e-6));
      expect(result.pulseWidth, closeTo(16e-6, 1e-12));
      expect(result.dutyCycle, closeTo(.40, 1e-12));

      final clipped = ChannelMeasurements.fromFrame(
        sampleFrame((i) => i % 50 < 20 ? 228 : 108),
        0,
      );
      expect(clipped.clipped, isTrue);
      expect(clipped.period, isNull);
      final narrow = ChannelMeasurements.fromFrame(
        sampleFrame((i) => i % 50 < 2 ? 168 : 108),
        0,
      );
      expect(narrow.pulseWidth, isNull);
      expect(narrow.frequency, isNull);
    },
  );

  test('samples complete UART and SIR bytes and checks screen resolution', () {
    const baseBaud = 115200, target = 0xa5;
    for (final protocol in SerialProtocol.values) {
      final scale = protocol == SerialProtocol.sir ? 9 : 10;
      final samplesPerBit = 1 / (timeScales[scale] / 25 * baseBaud);
      int code(int i) {
        final cells = (i - 33.5) / samplesPerBit;
        final cell = cells.floor();
        final value = cell == 0
            ? 0
            : cell > 0 && cell < 9
            ? (target >> (cell - 1)) & 1
            : 1;
        if (protocol == SerialProtocol.uart) return value == 1 ? 168 : 108;
        return value == 0 && cells - cell < 3 / 16 ? 168 : 108;
      }

      final result = SerialDecode.fromFrame(
        sampleFrame(code, timebase: scale),
        channel: 0,
        protocol: protocol,
        baud: baseBaud,
        inverted: false,
        threshold: .004,
      );
      expect(result.bytes.map((b) => b.value), [
        target,
      ], reason: result.message);
      final tooSlow = SerialDecode.fromFrame(
        sampleFrame(code, timebase: 14),
        channel: 0,
        protocol: protocol,
        baud: baseBaud,
        inverted: false,
        threshold: .004,
      );
      expect(tooSlow.message, contains('Мало отсчётов'));
    }
  });

  test('projects round-trip samples and reject a corrupted wire payload', () {
    final bytes = testFrameBytes();
    final frame = ScopeFrame.decode(
      bytes,
      CaptureStatus.decode(statusBytes(crc: crc32(bytes))),
      ScopeSettings.decode(settingsBytes(flags: 4)),
    );
    final encoded = ScopeRecording.encode([
      RecordedFrame(frame, note: 'Сигнал на CH1'),
    ]);
    final reopened = ScopeRecording.decode(encoded).single;
    expect(reopened.note, 'Сигнал на CH1');
    expect(reopened.frame.volts, frame.volts);
    expect(reopened.frame.timeAt(0), frame.timeAt(0));
    expect(reopened.frame.indexAtTime(0), frame.triggerIndex.toDouble());
    expect(reopened.frame.calibrationRequired, isTrue);
    expect(reopened.frame.capturedAt, frame.capturedAt);
    final damaged = jsonDecode(encoded) as Map<String, dynamic>;
    final item = (damaged['frames'] as List).single as Map<String, dynamic>;
    final raw = base64Decode(item['data'] as String);
    raw[0x356] ^= 1;
    item['data'] = base64Encode(raw);
    expect(
      () => ScopeRecording.decode(jsonEncode(damaged)),
      throwsA(isA<ScopeException>()),
    );
  });
  test('live capability and calibration status are version checked', () {
    final s = settingsBytes();
    expect(ScopeSettings.decode(s).liveSnapshotSupported, isFalse);
    s[63] = 4;
    expect(ScopeSettings.decode(s).liveSnapshotSupported, isTrue);
    expect(ScopeSettings.decode(s).preview, isFalse);
    final raw = Uint8List(64), d = ByteData.sublistView(raw);
    d.setUint32(0, 0x364c4143, Endian.little);
    d.setUint32(4, 1, Endian.little);
    d.setUint32(8, 1, Endian.little);
    d.setUint32(12, 123, Endian.little);
    d.setUint32(20, 9, Endian.little);
    final calibration = CalibrationStatus.decode(raw);
    expect(calibration.active, isTrue);
    expect(calibration.description, contains('123 с'));
    d.setUint32(8, 0, Endian.little);
    d.setUint32(24, 8, Endian.little);
    d.setUint32(32, 1, Endian.little);
    d.setUint32(36, 1, Endian.little);
    final nominal = CalibrationStatus.decode(raw);
    expect(nominal.description, contains('Таблица нуля загружена'));
    expect(nominal.description, contains('усиление не проверено'));
    expect(nominal.description, isNot(contains('Требуется калибровка нуля')));
    raw[0] = 0;
    expect(() => CalibrationStatus.decode(raw), throwsA(isA<ScopeException>()));
  });
  test('decodes a 2C53T wire fixture with a precomputed CRC', () {
    final bytes = testFrameBytes();
    final status = CaptureStatus.decode(statusBytes(crc: 0x18c0d310));
    final settings = ScopeSettings.decode(settingsBytes());
    final frame = ScopeFrame.decode(bytes, status, settings);
    expect(crc32(bytes), 0x18c0d310);
    expect(frame.triggerIndex, 60);
    expect(frame.dt, closeTo(8e-8, 1e-15));
    expect(frame.volts[0]![0], closeTo(-.0012, 1e-10));
    expect(frame.volts[0]![100], closeTo(-.0016, 1e-10));
    expect(frame.peakToPeak(1), closeTo(.002, 1e-10));
    expect(frame.volts[1], isNull);
    final csv = frame.csv().split('\r\n');
    expect(csv.length, 303);
    expect(csv[1], startsWith('0,-0.00000480000000000,'));
  });
  test(
    'rejects corrupted and mismatched snapshots rather than presenting them',
    () {
      final bytes = testFrameBytes();
      final status = CaptureStatus.decode(statusBytes(crc: crc32(bytes))),
          settings = ScopeSettings.decode(settingsBytes());
      bytes[0x356] ^= 1;
      expect(
        () => ScopeFrame.decode(bytes, status, settings),
        throwsA(isA<ScopeException>()),
      );
    },
  );
  test(
    'accepts slow native settings but does not invent frame support for them',
    () {
      expect(ScopeSettings.decode(settingsBytes(timebase: 28)).timebase, 28);
      final bytes = testFrameBytes();
      bytes[0x2d] = 28;
      final status = CaptureStatus.decode(statusBytes(crc: crc32(bytes)));
      expect(
        () => ScopeFrame.decode(
          bytes,
          status,
          ScopeSettings.decode(settingsBytes(timebase: 28)),
        ),
        throwsA(isA<ScopeException>()),
      );
    },
  );
  test('rejects unknown versions and malformed responses', () {
    final settings = settingsBytes();
    settings[4] = 0;
    expect(
      () => ScopeSettings.decode(settings),
      throwsA(isA<ScopeException>()),
    );
    expect(
      () => CaptureStatus.decode(Uint8List(5)),
      throwsA(isA<ScopeException>()),
    );
  });
  test('formats whole quantities without truncating zeros', () {
    expect(formatTime(100e-6), '100 мкс');
    expect(formatTime(20e-6), '20 мкс');
    expect(formatTime(.001), '1 мс');
    expect(formatVoltage(.05), '50,0 мВ');
  });
}
