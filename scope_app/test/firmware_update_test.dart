import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:fnirsi_scope/main.dart';
import 'package:fnirsi_scope/scope/controller.dart';
import 'package:fnirsi_scope/scope/firmware_update.dart';
import 'package:fnirsi_scope/ui/firmware_update_dialog.dart';
import 'controller_test.dart' show FakeLink;

class TestUpdater extends FirmwareUpdater {
  bool rejectFile = false;
  bool flashed = false;

  @override
  Future<FirmwareFile?> select() async {
    if (rejectFile) {
      throw const FirmwareUpdateException('the image size is invalid');
    }
    return FirmwareFile('r6j.bin', 'RU REMOTE r6j', 'digest', Uint8List(42));
  }

  @override
  Future<IapDevice> probe() async => const IapDevice('disk', 'IAP', 'fresh');

  @override
  Stream<UpdateEvent> flash(FirmwareFile file, IapDevice device) async* {
    if (file.sha256 != 'digest' || device.fingerprint != 'fresh') {
      throw StateError('selection was not retained');
    }
    flashed = true;
    yield const UpdateEvent('writing', total: 2);
    yield const UpdateEvent('progress', done: 2, total: 2);
    yield const UpdateEvent('checking');
    yield const UpdateEvent('unconfirmed');
  }
}

void main() {
  testWidgets('firmware updater opens without connecting in USB Sharing', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1280, 850);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final link = FakeLink(), controller = ScopeController(link);
    await tester.pumpWidget(ScopeApp(controller: controller));
    await tester.tap(find.byTooltip('Обновление прошивки'));
    await tester.pumpAndSettle();
    expect(find.byType(FirmwareUpdateDialog), findsOneWidget);
    expect(find.textContaining('зажатой MENU'), findsOneWidget);
    expect(link.commands, isEmpty);
    expect(controller.connected, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
    controller.dispose();
  });

  testWidgets('requires a validated file and IAP, with no implicit retry', (
    tester,
  ) async {
    final updater = TestUpdater();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: FirmwareUpdateDialog(updater: updater)),
      ),
    );
    final writeButton = find.widgetWithText(FilledButton, 'Записать прошивку');
    bool canWrite() => tester.widget<FilledButton>(writeButton).enabled;
    expect(canWrite(), isFalse);
    await tester.tap(find.text('Выбрать файл .bin…'));
    await tester.pumpAndSettle();
    expect(canWrite(), isFalse);
    await tester.tap(find.text('Проверить загрузчик'));
    await tester.pumpAndSettle();
    expect(canWrite(), isTrue);

    updater.rejectFile = true;
    await tester.tap(find.text('Выбрать файл .bin…'));
    await tester.pumpAndSettle();
    expect(canWrite(), isFalse);
    updater.rejectFile = false;
    await tester.tap(find.text('Выбрать файл .bin…'));
    await tester.pumpAndSettle();
    await tester.tap(writeButton);
    await tester.pumpAndSettle();

    expect(updater.flashed, isTrue);
    expect(find.textContaining('Все блоки переданы'), findsOneWidget);
    expect(find.textContaining('SUCCESS'), findsNothing);
    expect(canWrite(), isFalse);
  });
}
