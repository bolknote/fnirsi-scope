import 'dart:convert';

import 'protocol.dart';

class RecordedFrame {
  final ScopeFrame frame;
  String note;
  RecordedFrame(this.frame, {this.note = ''});
}

/// Projects keep the original instrument payload, including trigger position,
/// channel scales and offsets. Reopening goes through the same CRC and decoder
/// as a USB read.
class ScopeRecording {
  static const extension = 'fnirsi';
  static const maxFileBytes = 4 * 1024 * 1024;
  static const _format = 'fnirsi-scope';

  static String encode(List<RecordedFrame> records) {
    return jsonEncode({
      'format': _format,
      'version': 1,
      'frames': records.map((record) {
        final frame = record.frame;
        final bytes = frame.deviceBytes;
        if (!frame.verified || bytes == null) {
          throw const ScopeException(
            'Для сохранения нужен кадр прибора с CRC.',
          );
        }
        return {
          'id': frame.id,
          'crc': frame.crc,
          'acquiredAt': frame.capturedAt.toUtc().toIso8601String(),
          'preview': frame.preview,
          'calibrationRequired': frame.calibrationRequired,
          'filterMask': frame.filterMask,
          'note': record.note,
          'data': base64Encode(bytes),
        };
      }).toList(),
    });
  }

  static List<RecordedFrame> decode(String source) {
    if (source.length > maxFileBytes) {
      throw const ScopeException('Файл опыта превышает 4 МБ.');
    }
    final data = jsonDecode(source);
    if (data is! Map<String, dynamic> ||
        data['format'] != _format ||
        data['version'] != 1 ||
        data['frames'] is! List ||
        (data['frames'] as List).isEmpty) {
      throw const ScopeException('Файл не содержит кадров FNIRSI Scope.');
    }
    return List.unmodifiable((data['frames'] as List).map(_readFrame));
  }

  static RecordedFrame _readFrame(Object? item) {
    if (item is! Map<String, dynamic>) {
      throw const ScopeException('Некорректная запись кадра.');
    }
    final raw = item['data'],
        id = item['id'],
        crc = item['crc'],
        time = item['acquiredAt'],
        preview = item['preview'],
        calibrationRequired = item['calibrationRequired'],
        filterMask = item['filterMask'],
        note = item['note'];
    final capturedAt = time is String ? DateTime.tryParse(time) : null;
    if (raw is! String ||
        raw.length != 4800 ||
        id is! int ||
        crc is! int ||
        capturedAt == null ||
        preview is! bool ||
        calibrationRequired is! bool ||
        filterMask is! int ||
        filterMask < 0 ||
        filterMask > 3 ||
        note is! String) {
      throw const ScopeException('Некорректные параметры кадра в файле.');
    }
    return RecordedFrame(
      ScopeFrame.fromDeviceBytes(
        base64Decode(raw),
        id: id,
        crc: crc,
        preview: preview,
        calibrationRequired: calibrationRequired,
        filterMask: filterMask,
        capturedAt: capturedAt.toLocal(),
      ),
      note: note,
    );
  }
}
