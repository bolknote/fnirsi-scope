import 'protocol.dart';

enum SerialProtocol {
  uart('UART · 8N1'),
  sir('IrDA SIR · 8N1');

  final String label;
  const SerialProtocol(this.label);
}

class SerialByte {
  final int value;
  final double startSample, endSample;
  const SerialByte(this.value, this.startSample, this.endSample);

  String get hex => value.toRadixString(16).padLeft(2, '0').toUpperCase();
}

class SerialDecode {
  final List<SerialByte> bytes;
  final String message;

  const SerialDecode(this.bytes, this.message);

  factory SerialDecode.fromFrame(
    ScopeFrame frame, {
    required int channel,
    required SerialProtocol protocol,
    required int baud,
    required bool inverted,
    required double threshold,
  }) {
    final values = frame.volts[channel];
    if (values == null) {
      return const SerialDecode([], 'Канал выключен в кадре.');
    }
    final bit = 1 / (baud * frame.dt);
    final sir = protocol == SerialProtocol.sir;
    if (bit < (sir ? 16 : 8)) {
      return const SerialDecode(
        [],
        'Мало отсчётов. Выберите более быструю развёртку.',
      );
    }
    if (10 * bit > values.length - 1) {
      return const SerialDecode(
        [],
        'Байт не помещается. Увеличьте окно захвата.',
      );
    }
    final step = frame.scales[channel] / screenCodesPerDivision;
    final span =
        values.reduce((a, b) => a > b ? a : b) -
        values.reduce((a, b) => a < b ? a : b);
    if (span < 4 * step) {
      return const SerialDecode([], 'Малый размах для декодирования.');
    }
    final hysteresis = step / 2;
    final transitions = <double>[], active = <bool>[];
    var high = values.first >= threshold;
    for (var i = 0; i < values.length; i++) {
      if (values[i] > threshold + hysteresis) high = true;
      if (values[i] < threshold - hysteresis) high = false;
      // A UART start bit is low. A SIR zero produces a positive optical pulse.
      final next = (sir ? high : !high) != inverted;
      if (i > 0 && !active.last && next) {
        var j = i - 1;
        final rising = high;
        while (j > 0 &&
            (rising ? values[j] >= threshold : values[j] <= threshold)) {
          j--;
        }
        transitions.add(
          j + (threshold - values[j]) / (values[j + 1] - values[j]),
        );
      }
      active.add(next);
    }
    final bytes = <SerialByte>[];
    var nextByte = 0.0;
    for (final start in transitions) {
      final end = start + 10 * bit;
      // Include an idle interval before a candidate start bit.
      if (start < nextByte || start < bit || end > values.length - 1) continue;
      final before = active.sublist((start - bit).ceil(), start.ceil());
      if (before.any((sample) => sample)) continue;
      bool zero(int index) {
        if (!sir) return active[(start + (index + .5) * bit).round()];
        return transitions.any(
          (edge) => (edge - start - index * bit).abs() <= 1.6,
        );
      }

      if (!zero(0) || zero(9)) continue;
      if (sir &&
          transitions.any((edge) {
            if (edge <= start || edge >= end) return false;
            final index = ((edge - start) / bit).round();
            return (edge - start - index * bit).abs() > 1.6;
          })) {
        continue;
      }
      var value = 0;
      for (var i = 0; i < 8; i++) {
        if (!zero(i + 1)) value |= 1 << i;
      }
      bytes.add(SerialByte(value, start, end));
      nextByte = end - 1;
    }
    return SerialDecode(
      List.unmodifiable(bytes),
      bytes.isEmpty
          ? 'Целый байт не найден. Проверьте скорость, порог и полярность.'
          : '',
    );
  }
}
