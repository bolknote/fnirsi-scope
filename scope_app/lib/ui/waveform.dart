import 'dart:math' as math;
import 'package:flutter/material.dart';
import '../scope/decoder.dart';
import '../scope/protocol.dart';

const channelColors = [Color(0xfff3ce71), Color(0xff76c9e7)];
const plotBackground = Color(0xff101b1b);
const plotMuted = Color(0xff819997);

String formatVoltage(double? value) {
  if (value == null || !value.isFinite) return '—';
  final abs = value.abs();
  final factor = abs >= 1
      ? 1.0
      : abs >= .001
      ? .001
      : .000001;
  final unit = factor == 1
      ? 'В'
      : factor == .001
      ? 'мВ'
      : 'мкВ';
  final v = value / factor;
  final digits = v.abs() >= 100
      ? 0
      : v.abs() >= 10
      ? 1
      : 2;
  return '${v.toStringAsFixed(digits).replaceAll('.', ',')} $unit';
}

String formatTime(double value) {
  if (!value.isFinite) return '—';
  final abs = value.abs();
  final factor = abs >= 1
      ? 1.0
      : abs >= .001
      ? .001
      : abs >= .000001
      ? .000001
      : .000000001;
  final unit = factor == 1
      ? 'с'
      : factor == .001
      ? 'мс'
      : factor == .000001
      ? 'мкс'
      : 'нс';
  final v = value / factor;
  var n = v.toStringAsFixed(
    v.abs() >= 100
        ? 0
        : v.abs() >= 10
        ? 1
        : 2,
  );
  if (n.contains('.')) n = n.replaceAll(RegExp(r'\.?0+$'), '');
  return '${n.replaceAll('.', ',')} $unit';
}

class WaveformView extends StatelessWidget {
  final ScopeFrame? frame;
  final ScopeFrame? reference;
  final List<SerialByte> decoded;
  final int decodeChannel;
  final bool cursors;
  final int cursorA, cursorB;
  final int cursorChannelA, cursorChannelB;
  final void Function(int a, int b)? onCursors;
  const WaveformView({
    super.key,
    required this.frame,
    this.reference,
    this.decoded = const [],
    this.decodeChannel = 0,
    required this.cursors,
    required this.cursorA,
    required this.cursorB,
    this.cursorChannelA = 0,
    this.cursorChannelB = 0,
    required this.onCursors,
  });
  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        void move(Offset local) {
          if (!cursors ||
              frame == null ||
              onCursors == null ||
              constraints.maxWidth <= 52) {
            return;
          }
          const left = 34.0, right = 18.0;
          final x =
              ((local.dx - left) /
                      (constraints.maxWidth - left - right) *
                      (screenSampleCount - 1))
                  .round()
                  .clamp(0, screenSampleCount - 1);
          if ((x - cursorA).abs() <= (x - cursorB).abs()) {
            onCursors!(x, cursorB);
          } else {
            onCursors!(cursorA, x);
          }
        }

        return GestureDetector(
          onPanDown: (e) => move(e.localPosition),
          onPanUpdate: (e) => move(e.localPosition),
          child: ClipRRect(
            borderRadius: BorderRadius.circular(5),
            child: CustomPaint(
              painter: _WavePainter(
                frame: frame,
                reference: reference,
                decoded: decoded,
                decodeChannel: decodeChannel,
                cursors: cursors,
                a: cursorA,
                b: cursorB,
                cursorChannelA: cursorChannelA,
                cursorChannelB: cursorChannelB,
              ),
              size: Size.infinite,
              child: frame == null
                  ? Center(
                      child: Text(
                        'Кадра пока нет',
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: .4),
                          fontSize: 13,
                        ),
                      ),
                    )
                  : null,
            ),
          ),
        );
      },
    );
  }
}

class _WavePainter extends CustomPainter {
  final ScopeFrame? frame;
  final ScopeFrame? reference;
  final List<SerialByte> decoded;
  final int decodeChannel;
  final bool cursors;
  final int a, b;
  final int cursorChannelA, cursorChannelB;
  _WavePainter({
    required this.frame,
    required this.reference,
    required this.decoded,
    required this.decodeChannel,
    required this.cursors,
    required this.a,
    required this.b,
    required this.cursorChannelA,
    required this.cursorChannelB,
  });
  @override
  void paint(Canvas canvas, Size size) {
    canvas.drawColor(plotBackground, BlendMode.src);
    final grid = Rect.fromLTRB(
      34,
      20,
      math.max(35, size.width - 18),
      math.max(21, size.height - (decoded.isEmpty ? 30 : 53)),
    );
    if (grid.width <= 1 || grid.height <= 1) return;
    final minor = Paint()
      ..color = const Color(0xff233533)
      ..strokeWidth = .5;
    final major = Paint()
      ..color = const Color(0xff344c47)
      ..strokeWidth = .8;
    for (var i = 0; i <= 60; i++) {
      final x = grid.left + grid.width * i / 60;
      canvas.drawLine(
        Offset(x, grid.top),
        Offset(x, grid.bottom),
        i % 5 == 0 ? major : minor,
      );
    }
    for (var i = 0; i <= 40; i++) {
      final y = grid.top + grid.height * i / 40;
      canvas.drawLine(
        Offset(grid.left, y),
        Offset(grid.right, y),
        i % 5 == 0 ? major : minor,
      );
    }
    final f = frame;
    if (f == null) return;
    double xAt(double index) =>
        grid.left + grid.width * index / (screenSampleCount - 1);
    double yAt(double code) =>
        grid.top +
        (screenTopCode - code) /
            (screenTopCode - screenBottomCode) *
            grid.height;
    void text(
      String value,
      Offset at, {
      Color color = plotMuted,
      TextAlign align = TextAlign.center,
      double maxWidth = 100,
    }) {
      final t = TextPainter(
        text: TextSpan(
          text: value,
          style: TextStyle(color: color, fontSize: 10, fontFamily: 'monospace'),
        ),
        textDirection: TextDirection.ltr,
        textAlign: align,
        maxLines: 1,
      )..layout(maxWidth: maxWidth);
      t.paint(canvas, Offset(at.dx - t.width / 2, at.dy));
    }

    canvas.save();
    canvas.clipRect(grid);
    final trigX = xAt(f.triggerIndex.toDouble());
    final trig = Paint()
      ..color = const Color(0xffdf9b73).withValues(alpha: .7)
      ..strokeWidth = 1;
    void dashed(Offset p, Offset q, Paint paint) {
      final d = q - p, length = d.distance;
      if (length == 0) return;
      final v = d / length;
      for (var z = 0.0; z < length; z += 8) {
        canvas.drawLine(p + v * z, p + v * math.min(z + 4, length), paint);
      }
    }

    dashed(Offset(trigX, grid.top), Offset(trigX, grid.bottom), trig);
    {
      final y = yAt((128 + f.triggerLevel).toDouble());
      dashed(
        Offset(grid.left, y),
        Offset(grid.right, y),
        trig..color = trig.color.withValues(alpha: .36),
      );
    }
    final ref = reference;
    if (ref != null) {
      // Use the active frame's coordinate system for both traces. A striped
      // clip distinguishes the reference even when the traces overlap.
      final stripes = Path();
      for (var x = grid.left; x < grid.right; x += 9) {
        stripes.addRect(Rect.fromLTWH(x, grid.top, 5, grid.height));
      }
      canvas.save();
      canvas.clipPath(stripes);
      drawTraces(canvas, f, ref, xAt, yAt, opacity: .62);
      canvas.restore();
    }
    drawTraces(canvas, f, f, xAt, yAt);
    if (cursors) {
      final p = Paint()
        ..color = const Color(0xffc7e6da)
        ..strokeWidth = 1;
      for (final (index, ch) in [(a, cursorChannelA), (b, cursorChannelB)]) {
        final x = xAt(index.toDouble());
        dashed(Offset(x, grid.top), Offset(x, grid.bottom), p);
        final values = f.volts[ch];
        if (values != null) {
          canvas.drawCircle(
            Offset(x, yAt(f.displayCode(ch, values[index]))),
            3.5,
            Paint()..color = channelColors[ch],
          );
        }
      }
    }
    canvas.restore();
    final tickCount = grid.width < 315 ? 2 : 4;
    for (var i = 0; i <= tickCount; i++) {
      final index = (screenSampleCount - 1) * i / tickCount;
      text(
        formatTime(f.timeAt(index)),
        Offset(xAt(index), grid.bottom + 8),
        maxWidth: 80,
      );
    }
    if (decoded.isNotEmpty) {
      final markPaint = Paint()
        ..color = channelColors[decodeChannel].withValues(alpha: .8)
        ..strokeWidth = 1;
      final y = grid.bottom + 32;
      for (final byte in decoded) {
        final left = xAt(byte.startSample), right = xAt(byte.endSample);
        canvas.drawLine(Offset(left, y), Offset(right, y), markPaint);
        for (final x in [left, right]) {
          canvas.drawLine(Offset(x, y - 3), Offset(x, y + 3), markPaint);
        }
        if (right - left > 24) {
          text(
            byte.hex,
            Offset((left + right) / 2, y + 3),
            color: markPaint.color,
          );
        }
      }
    }
    text(
      'T',
      Offset(trigX.clamp(grid.left, grid.right), 3),
      color: const Color(0xffdf9b73),
    );
    if (cursors) {
      text(
        'A',
        Offset(xAt(a.toDouble()) + 7, grid.top + 5),
        color: Colors.white70,
      );
      text(
        'B',
        Offset(xAt(b.toDouble()) + 7, grid.top + 5),
        color: Colors.white70,
      );
    }
    for (var c = 0; c < 2; c++) {
      if (f.volts[c] == null) continue;
      final y = yAt(
        (128 + f.offsets[c]).toDouble(),
      ).clamp(grid.top, grid.bottom);
      text('${c + 1}', Offset(18, y - 6), color: channelColors[c]);
    }
  }

  void drawTraces(
    Canvas canvas,
    ScopeFrame display,
    ScopeFrame source,
    double Function(double) xAt,
    double Function(double) yAt, {
    double opacity = 1,
  }) {
    for (var channel = 0; channel < 2; channel++) {
      final points = source.volts[channel];
      if (points == null || display.volts[channel] == null) continue;
      final line = Path();
      for (var i = 0; i < points.length; i++) {
        final index = display.indexAtTime(source.timeAt(i));
        final y = yAt(display.displayCode(channel, points[i]));
        if (i == 0) {
          line.moveTo(xAt(index), y);
        } else {
          line.lineTo(xAt(index), y);
        }
      }
      canvas.drawPath(
        line,
        Paint()
          ..color = channelColors[channel].withValues(alpha: opacity)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.6
          ..strokeJoin = StrokeJoin.round,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _WavePainter old) =>
      old.frame != frame ||
      old.reference != reference ||
      old.decoded != decoded ||
      old.decodeChannel != decodeChannel ||
      old.cursors != cursors ||
      old.a != a ||
      old.b != b ||
      old.cursorChannelA != cursorChannelA ||
      old.cursorChannelB != cursorChannelB;
}
