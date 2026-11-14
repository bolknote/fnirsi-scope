import 'dart:math' as math;

import 'protocol.dart';

enum MeasurementKind {
  span('Размах'),
  minimum('Минимум'),
  maximum('Максимум'),
  mean('Среднее по кадру'),
  period('Период'),
  frequency('Частота'),
  pulseWidth('Положительный импульс'),
  dutyCycle('Заполнение');

  final String label;
  const MeasurementKind(this.label);
}

class ChannelMeasurements {
  final double minimum, maximum, mean;
  final bool clipped, smallSignal;
  final double? period, pulseWidth, dutyCycle;

  const ChannelMeasurements._({
    required this.minimum,
    required this.maximum,
    required this.mean,
    required this.clipped,
    required this.smallSignal,
    this.period,
    this.pulseWidth,
    this.dutyCycle,
  });

  double get span => maximum - minimum;
  double? get frequency => period == null ? null : 1 / period!;

  factory ChannelMeasurements.fromFrame(ScopeFrame frame, int channel) {
    final values = frame.volts[channel]!;
    final minimum = values.reduce(math.min), maximum = values.reduce(math.max);
    final center = (minimum + maximum) / 2;
    final mean = _average(values);
    final span = maximum - minimum;
    final step = frame.scales[channel] / screenCodesPerDivision;
    final clipped =
        frame.displayCode(channel, minimum) <= screenBottomCode + .01 ||
        frame.displayCode(channel, maximum) >= screenTopCode - .01;
    final smallSignal = span < 6 * step;
    if (clipped || smallSignal) {
      return ChannelMeasurements._(
        minimum: minimum,
        maximum: maximum,
        mean: mean,
        clipped: clipped,
        smallSignal: smallSignal,
      );
    }

    final hysteresis = math.max(step, span * .1);
    final edges = <({bool rising, double sample})>[];
    bool? wasHigh;
    for (var i = 0; i < values.length; i++) {
      final value = values[i];
      final high = value >= center + hysteresis
          ? true
          : value <= center - hysteresis
          ? false
          : wasHigh;
      if (high != null && wasHigh != null && high != wasHigh) {
        // After crossing the hysteresis band, interpolate at the midpoint.
        var j = i - 1;
        while (j > 0 && (high ? values[j] >= center : values[j] <= center)) {
          j--;
        }
        final fraction = (center - values[j]) / (values[j + 1] - values[j]);
        edges.add((rising: high, sample: j + fraction));
      }
      wasHigh = high;
    }

    final widths = <double>[];
    var unresolvedPulse = false;
    for (var i = 1; i < edges.length; i++) {
      final start = edges[i - 1], end = edges[i];
      final width = end.sample - start.sample;
      if (width < 3) {
        unresolvedPulse = true;
      } else if (start.rising && !end.rising) {
        widths.add(width);
      }
    }

    final rises = [
      for (final e in edges)
        if (e.rising) e.sample,
    ];
    final periods = [
      for (var i = 1; i < rises.length; i++) rises[i] - rises[i - 1],
    ];
    // Two complete periods and repeatable crossings reduce false readouts on
    // packet bursts and sporadic edges. A one-sample deviation is expected.
    double? periodSamples;
    if (periods.length >= 2 && !unresolvedPulse) {
      final average = _average(periods);
      if (average >= 8 &&
          periods.every(
            (p) => (p - average).abs() <= math.max(1, average * .05),
          )) {
        periodSamples = average;
      }
    }
    double? dutyCycle;
    if (periodSamples != null) {
      final ratios = <double>[];
      for (var i = 1; i < edges.length - 1; i++) {
        final start = edges[i - 1], end = edges[i], next = edges[i + 1];
        if (start.rising && !end.rising && next.rising) {
          ratios.add(
            (end.sample - start.sample) / (next.sample - start.sample),
          );
        }
      }
      if (ratios.length >= 2) dutyCycle = _average(ratios);
    }
    return ChannelMeasurements._(
      minimum: minimum,
      maximum: maximum,
      mean: mean,
      clipped: clipped,
      smallSignal: smallSignal,
      pulseWidth: unresolvedPulse || widths.isEmpty
          ? null
          : widths.first * frame.dt,
      period: periodSamples == null ? null : periodSamples * frame.dt,
      dutyCycle: dutyCycle,
    );
  }
}

double _average(List<double> values) =>
    values.fold(0.0, (sum, value) => sum + value) / values.length;
