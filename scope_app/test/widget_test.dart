import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:fnirsi_scope/main.dart';
import 'package:fnirsi_scope/scope/controller.dart';
import 'package:fnirsi_scope/ui/waveform.dart';
import 'controller_test.dart' show FakeLink, TriggeredLink, initialSettings;

void main() {
  testWidgets(
    'series places each new frame in the history and overlays a selected reference',
    (tester) async {
      tester.view.physicalSize = const Size(1000, 534);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final link = TriggeredLink(), controller = ScopeController(link);
      await tester.pumpWidget(ScopeApp(controller: controller));
      await tester.tap(find.text('Подключить'));
      await tester.pump();
      await tester.tap(find.text('Захват'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Запустить серию'));
      await tester.tap(find.text('Запустить серию'));
      await tester.pump();
      expect(controller.triggerArmed, isTrue);
      link.trigger();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pump();
      expect(find.text('Кадры 1'), findsOneWidget);
      link.trigger();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pump();
      expect(find.text('Кадры 2'), findsOneWidget);
      await tester.tap(find.text('Стоп'));
      await tester.pump();
      final first = controller.recordings.first.frame;
      await tester.tap(find.text('Наложить').last);
      await tester.pump();
      final plot = tester.widget<WaveformView>(find.byType(WaveformView));
      expect(plot.frame, same(controller.recordings.last.frame));
      expect(plot.reference, same(first));
      expect(find.textContaining('по триггеру'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await controller.disconnect();
      await tester.pumpWidget(const SizedBox.shrink());
      controller.dispose();
    },
  );

  testWidgets('calibration requires probe confirmation and exposes cancel', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1280, 850);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final link = FakeLink(), controller = ScopeController(link);
    await tester.pumpWidget(ScopeApp(controller: controller));
    await tester.tap(find.text('Подключить'));
    await tester.pump();
    await tester.tap(find.text('Калибровать'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Отсоедините оба щупа'), findsOneWidget);
    expect(link.commands, isEmpty);
    await tester.tap(find.text('Оба щупа сняты — начать'));
    await tester.pumpAndSettle();
    expect(link.commands, ['35:0']);
    expect(controller.calibrating, isTrue);
    await tester.tap(find.text('Прервать'));
    await tester.pump();
    expect(link.commands.last, '36:0');
    await controller.disconnect();
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });
  testWidgets(
    'desktop starts disconnected and shows actual device settings after connect',
    (tester) async {
      tester.view.physicalSize = const Size(1280, 850);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final link = FakeLink(), controller = ScopeController(link);
      await tester.pumpWidget(ScopeApp(controller: controller));
      expect(find.text('Нет подключения'), findsOneWidget);
      expect(find.text('Кадра пока нет'), findsOneWidget);
      expect(link.commands, isEmpty);
      await tester.tap(find.text('Подключить'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.text('USB подключён'), findsOneWidget);
      expect(find.text('Масштаб'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await controller.disconnect();
      await tester.pumpWidget(const SizedBox.shrink());
      controller.dispose();
    },
  );
  testWidgets('compact desktop keeps controls and settings usable', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(800, 650);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final link = FakeLink(), controller = ScopeController(link);
    await tester.pumpWidget(ScopeApp(controller: controller));
    await tester.tap(find.text('Подключить'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.text('Настройки'), findsOneWidget);
    expect(find.text('Запуск'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await controller.disconnect();
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });

  testWidgets('window fits at the requested screenshot height', (tester) async {
    // 1000 × 562 points including the macOS title bar gives a 2000 × 1124
    // pixel window on a 2× Retina display. Flutter gets the area below it.
    tester.view.physicalSize = const Size(1000, 534);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final link = FakeLink()
      ..current = initialSettings(requiresCalibration: true)
      ..fakeGainUnverified = true;
    final controller = ScopeController(link);
    await tester.pumpWidget(ScopeApp(controller: controller));
    await tester.tap(find.text('Подключить'));
    await tester.pump();
    expect(find.text('Настройки'), findsOneWidget);
    expect(find.text('Как проверить'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await controller.disconnect();
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });

  testWidgets('one control starts and stops acquisition', (tester) async {
    tester.view.physicalSize = const Size(1280, 850);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final link = FakeLink(), controller = ScopeController(link);
    await tester.pumpWidget(ScopeApp(controller: controller));
    await tester.tap(find.text('Подключить'));
    await tester.pump();
    expect(find.text('Запуск'), findsOneWidget);
    expect(find.text('Стоп'), findsNothing);
    await tester.tap(find.text('Запуск'));
    await tester.pump();
    expect(find.text('Стоп'), findsOneWidget);
    expect(find.text('Запуск'), findsNothing);
    expect(link.commands, ['25:1']);
    await tester.tap(find.text('Стоп'));
    await tester.pump();
    expect(find.text('Запуск'), findsOneWidget);
    expect(find.text('Стоп'), findsNothing);
    expect(link.commands, ['25:1', '26:0']);
    await controller.disconnect();
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });

  testWidgets('a running instrument can be stopped on older firmware', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1280, 850);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final link = FakeLink()
      ..current = initialSettings(supportsLive: false).copyWith(running: true);
    final controller = ScopeController(link);
    await tester.pumpWidget(ScopeApp(controller: controller));
    await tester.tap(find.text('Подключить'));
    await tester.pump();
    expect(find.text('Стоп'), findsOneWidget);
    await tester.tap(find.text('Стоп'));
    await tester.pump();
    expect(link.commands, ['26:0']);
    expect(find.text('Запуск'), findsOneWidget);
    await controller.disconnect();
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });

  testWidgets('gain warning offers instructions and configures the channel', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(800, 650);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final link = FakeLink()
      ..current = initialSettings(requiresCalibration: true)
      ..fakeGainUnverified = true;
    final controller = ScopeController(link);
    await tester.pumpWidget(ScopeApp(controller: controller));
    await tester.tap(find.text('Подключить'));
    await tester.pump();
    expect(find.text('Как проверить'), findsOneWidget);
    await tester.tap(find.text('Как проверить'));
    await tester.pumpAndSettle();
    expect(find.text('Как проверить точность напряжения'), findsOneWidget);
    expect(find.textContaining('не калибровка'), findsOneWidget);
    expect(link.commands, isEmpty);
    await tester.tap(find.text('Настроить CH1'));
    await tester.pumpAndSettle();
    expect(link.current.channels[0].range, 5);
    expect(link.current.channels[0].probe, 1);
    expect(link.current.channels[0].coupling, 'DC');
    expect(link.current.channels[0].offset, 0);
    expect(link.current.timebase, 17);
    expect(link.current.triggerMode, 1);
    expect(link.commands, containsAll(['17:5', '21:1', '28:17']));
    expect(link.commands.where((v) => v.startsWith('35:')), isEmpty);
    expect(link.current.calibrationRequired, isTrue);
    expect(find.text('Как проверить'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await controller.disconnect();
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });
}
