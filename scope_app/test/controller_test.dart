import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:fnirsi_scope/scope/controller.dart';
import 'package:fnirsi_scope/scope/protocol.dart';
import 'package:fnirsi_scope/scope/usb.dart';

ScopeSettings initialSettings({
  bool supportsLive = true,
  bool requiresCalibration = false,
}) => ScopeSettings(
  generation: 2,
  stage: 0,
  error: 0,
  channelMask: 3,
  timebase: 10,
  triggerChannel: 1,
  triggerMode: 1,
  triggerLevel: 0,
  triggerPosition: 0,
  filterMask: 0,
  running: false,
  falling: false,
  calibrationActive: false,
  spiFault: false,
  calibrationRequired: requiresCalibration,
  mathMode: false,
  preview: false,
  liveSnapshotSupported: supportsLive,
  channels: const [
    ChannelSettings(
      enabled: true,
      range: 2,
      probe: 1,
      offset: 0,
      coupling: 'AC',
    ),
    ChannelSettings(
      enabled: true,
      range: 6,
      probe: 1,
      offset: 0,
      coupling: 'DC',
    ),
  ],
);
CaptureStatus fakeStatus({
  int phase = 0,
  int id = 1,
  int pre = 50,
  int error = 0,
}) => CaptureStatus(
  phase: phase,
  error: error,
  commandResult: 0,
  frameId: id,
  frameCrc: 0,
  frameBytes: 3600,
  commandId: 1,
  commandDone: 1,
  pretrigger: pre,
);

class FakeLink implements ScopeLink {
  ScopeSettings current = initialSettings();
  CaptureStatus stat = fakeStatus();
  final commands = <String>[];
  bool opened = false;
  bool fakeCalibrationActive = false;
  bool fakeGainUnverified = false;
  bool frameIsPreview = true;
  int previewReads = 0;
  @override
  Future<void> connect() async {
    opened = true;
  }

  @override
  Future<void> close() async {
    opened = false;
  }

  @override
  Future<ScopeSettings> settings() async => current;
  @override
  Future<CaptureStatus> status() async => stat;
  @override
  Future<ScopeFrame?> preview() async {
    previewReads++;
    return frame(fakeStatus(phase: 4, id: previewReads));
  }

  @override
  Future<CalibrationStatus> calibration() async => CalibrationStatus(
    active: fakeCalibrationActive,
    seconds: 0,
    frames: 0,
    steps: 0,
    reason: 0,
    needsCalibration: false,
    baselineCompleted: true,
    gainUnverified: fakeGainUnverified,
    inStep: false,
    cancelRequested: false,
    spiFault: false,
  );

  @override
  Future<ScopeFrame> frame(CaptureStatus expected) async => ScopeFrame(
    id: expected.frameId,
    crc: 0,
    triggerIndex: 150,
    triggerChannel: 1,
    triggerLevel: 0,
    falling: false,
    dt: 1e-6,
    volts: [List.filled(301, 0.0), null],
    offsets: [0, 0],
    scales: [.05, 1],
    preview: frameIsPreview,
    capturedAt: DateTime.now(),
  );
  @override
  Future<CaptureStatus> command(int op, [int argument = 0]) async {
    commands.add('$op:$argument');
    final ch = [...current.channels];
    if (op == 16) {
      for (var i = 0; i < 2; i++) {
        ch[i] = ch[i].copyWith(enabled: (argument & (1 << i)) != 0);
      }
      current = current.copyWith(channels: ch);
    }
    if (op == 17 || op == 18) {
      final i = op - 17;
      ch[i] = ch[i].copyWith(range: argument);
      current = current.copyWith(channels: ch);
    }
    if (op == 19 || op == 20) {
      final i = op - 19;
      ch[i] = ch[i].copyWith(probe: argument == 0 ? 1 : 10);
      current = current.copyWith(channels: ch);
    }
    if (op == 21 || op == 22) {
      final i = op - 21;
      ch[i] = ch[i].copyWith(coupling: argument == 0 ? 'AC' : 'DC');
      current = current.copyWith(channels: ch);
    }
    if (op == 23 || op == 24) {
      final i = op - 23;
      ch[i] = ch[i].copyWith(offset: argument);
      current = current.copyWith(channels: ch);
    }
    if (op == 25) current = current.copyWith(running: true);
    if (op == 26 || op == 27) current = current.copyWith(running: false);
    if (op == 27) stat = fakeStatus(phase: 4, id: stat.frameId + 1);
    if (op == 28) current = current.copyWith(timebase: argument);
    if (op == 29) current = current.copyWith(triggerMode: argument);
    if (op == 30) current = current.copyWith(triggerChannel: argument + 1);
    if (op == 31) current = current.copyWith(falling: argument == 1);
    if (op == 32) current = current.copyWith(triggerLevel: argument);
    if (op == 7) stat = fakeStatus(pre: argument);
    if (op == 8) current = current.copyWith(filterMask: argument);
    if (op == 35) fakeCalibrationActive = true;
    if (op == 36) fakeCalibrationActive = false;
    return stat;
  }
}

class FailingStartLink extends FakeLink {
  @override
  Future<CaptureStatus> command(int op, [int argument = 0]) async {
    final result = await super.command(op, argument);
    if (op == 25) throw const ScopeException('Readback failed after RUN');
    return result;
  }
}

class BlockingPreviewLink extends FakeLink {
  final entered = Completer<void>(), release = Completer<void>();
  @override
  Future<ScopeFrame?> preview() async {
    entered.complete();
    await release.future;
    return super.preview();
  }
}

class TriggeredLink extends FakeLink {
  TriggeredLink() {
    frameIsPreview = false;
  }

  final reads = <int>[];
  Completer<void>? blockRead;
  final readEntered = Completer<void>();

  void trigger() {
    current = current.copyWith(running: false);
    stat = fakeStatus(phase: 4, id: stat.frameId + 1);
  }

  void timeout() {
    current = current.copyWith(running: false);
    stat = fakeStatus(phase: 6, id: stat.frameId, error: 3);
  }

  @override
  Future<CaptureStatus> command(int op, [int argument = 0]) async {
    await super.command(op, argument);
    if (op == 33) {
      current = current.copyWith(running: true, triggerMode: 2);
      stat = fakeStatus(phase: 2, id: stat.frameId);
    }
    if (op == 26) stat = fakeStatus(id: stat.frameId);
    return stat;
  }

  @override
  Future<ScopeFrame> frame(CaptureStatus expected) async {
    reads.add(expected.frameId);
    final blocked = blockRead;
    if (blocked != null) {
      readEntered.complete();
      await blocked.future;
    }
    return super.frame(expected);
  }
}

Future<void> waitUntil(bool Function() condition) async {
  final deadline = Stopwatch()..start();
  while (!condition() && deadline.elapsed < const Duration(seconds: 3)) {
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
  expect(condition(), isTrue);
}

void main() {
  test(
    'series rearms after reading each fresh frame and retains earlier records on timeout',
    () async {
      final link = TriggeredLink(), controller = ScopeController(link);
      await controller.connect();
      try {
        await controller.captureSeries(count: 3, timeoutSeconds: 5);
        expect(link.commands, ['26:0', '3:0', '6:5', '33:0']);
        expect(controller.triggerArmed, isTrue);
        link.trigger();
        await waitUntil(
          () => link.commands.length == 5 && controller.triggerArmed,
        );
        expect(link.reads, [2]);
        expect(controller.capturedCount, 1);
        expect(controller.recordings.single.frame.id, 2);
        link.timeout();
        await waitUntil(
          () => controller.captureOutcome == CaptureOutcome.timedOut,
        );
        expect(controller.error, isNull);
        expect(controller.controlAvailable, isTrue);
        await controller.captureSeries(count: 2, timeoutSeconds: 5);
        link.trigger();
        await waitUntil(
          () => controller.capturedCount == 1 && controller.triggerArmed,
        );
        link.trigger();
        await waitUntil(
          () => controller.captureOutcome == CaptureOutcome.complete,
        );
        expect(controller.recordings.map((record) => record.frame.id), [
          2,
          3,
          4,
        ]);
        expect(
          controller.recordCurrentFrame(),
          same(controller.recordings.last),
        );
        expect(controller.recordings, hasLength(3));
      } finally {
        await controller.disconnect();
        controller.dispose();
      }
    },
  );

  test(
    'STOP during a series read prevents rearm and excludes the pending frame',
    () async {
      final link = TriggeredLink()..blockRead = Completer<void>();
      final controller = ScopeController(link);
      await controller.connect();
      try {
        await controller.captureSeries(count: 2, timeoutSeconds: 10);
        link.trigger();
        await link.readEntered.future.timeout(const Duration(seconds: 3));
        final stopping = controller.stop();
        link.blockRead!.complete();
        await stopping;
        expect(link.commands, ['26:0', '3:0', '6:10', '33:0', '26:0']);
        expect(controller.acquisition, Acquisition.stopped);
        expect(controller.captureOutcome, CaptureOutcome.stopped);
        expect(controller.recordings, isEmpty);
      } finally {
        await controller.disconnect();
        controller.dispose();
      }
    },
  );
  test(
    'older firmware does not receive live or calibration commands',
    () async {
      final link = FakeLink()..current = initialSettings(supportsLive: false);
      final controller = ScopeController(link);
      await controller.connect();
      await controller.start();
      expect(controller.error, contains('r6j'));
      await controller.startCalibration();
      expect(controller.error, contains('r6j'));
      expect(link.commands, isEmpty);
      await controller.disconnect();
      controller.dispose();
    },
  );
  test('failed RUN never queues another command in an unknown state', () async {
    final link = FailingStartLink(), controller = ScopeController(link);
    await controller.connect();
    await controller.start();
    expect(link.commands, ['25:1']);
    expect(controller.acquisition, Acquisition.interrupted);
    expect(controller.error, contains('Readback failed'));
    await controller.disconnect();
    controller.dispose();
  });
  test(
    'reconnecting does not display a previous instrument frame as new',
    () async {
      final link = FakeLink(), controller = ScopeController(link);
      await controller.connect();
      controller.frame = await link.frame(link.stat);
      await controller.disconnect();
      expect(controller.frame, isNotNull);
      await controller.connect();
      expect(controller.frame, isNull);
      await controller.disconnect();
      controller.dispose();
    },
  );
  test('STOP during a live read stops once without restarting', () async {
    final link = BlockingPreviewLink(), controller = ScopeController(link);
    await controller.connect();
    await controller.start();
    await link.entered.future.timeout(const Duration(seconds: 3));
    final stopping = controller.stop();
    link.release.complete();
    await stopping;
    expect(controller.acquisition, Acquisition.stopped);
    expect(link.commands.where((x) => x.startsWith('25:')).length, 1);
    expect(link.commands.last, '26:0');
    await controller.disconnect();
    controller.dispose();
  });

  test('live preview does not repeatedly change RUN or STOP', () async {
    final link = FakeLink(), controller = ScopeController(link);
    await controller.connect();
    await controller.start();
    await Future<void>.delayed(const Duration(milliseconds: 1200));
    expect(link.previewReads, greaterThanOrEqualTo(2));
    expect(link.commands, ['25:1']);
    expect(controller.frame!.verified, isTrue);
    await controller.stop();
    expect(link.commands, ['25:1', '26:0']);
    expect(controller.frame!.verified, isTrue);
    await controller.disconnect();
    controller.dispose();
  });

  test(
    'baseline calibration stops acquisition, starts once and can be cancelled',
    () async {
      final link = FakeLink(), controller = ScopeController(link);
      await controller.connect();
      await controller.start();
      await controller.startCalibration();
      expect(link.commands, ['25:1', '26:0', '35:0']);
      expect(controller.calibrating, isTrue);
      await controller.cancelCalibration();
      expect(link.commands.last, '36:0');
      expect(controller.calibrating, isFalse);
      await controller.disconnect();
      controller.dispose();
    },
  );

  test(
    'connection reads device state without starting or changing it',
    () async {
      final link = FakeLink(), controller = ScopeController(link);
      await controller.connect();
      expect(controller.connected, isTrue);
      expect(controller.settings?.channels.length, 2);
      expect(link.commands, isEmpty);
      await controller.disconnect();
      expect(link.opened, isFalse);
      controller.dispose();
    },
  );
  test(
    'invalid channel combination is rejected before any device command',
    () async {
      final link = FakeLink(), controller = ScopeController(link);
      await controller.connect();
      controller.edit(
        (s) => s.copyWith(
          channels: s.channels.map((c) => c.copyWith(enabled: false)).toList(),
        ),
      );
      await controller.apply();
      expect(link.commands, isEmpty);
      expect(controller.error, contains('хотя бы один канал'));
      await controller.disconnect();
      controller.dispose();
    },
  );
  test(
    'applies settings with STOP and readback, including probe and trigger',
    () async {
      final link = FakeLink(), controller = ScopeController(link);
      await controller.connect();
      controller.edit((s) {
        final channels = [...s.channels];
        channels[0] = channels[0].copyWith(probe: 10, range: 4, coupling: 'DC');
        return s.copyWith(
          channels: channels,
          timebase: 7,
          triggerLevel: 25,
          triggerPosition: -90,
          filterMask: 1,
        );
      });
      await controller.apply();
      expect(controller.error, isNull);
      expect(controller.dirty, isFalse);
      expect(
        link.commands,
        containsAllInOrder([
          '26:0',
          '19:1',
          '17:4',
          '21:1',
          '28:7',
          '30:0',
          '32:25',
          '8:1',
          '7:20',
        ]),
      );
      expect(controller.settings!.channels[0].voltsPerDivision, 2);
      expect(controller.status!.pretrigger, 20);
      await controller.disconnect();
      controller.dispose();
    },
  );
  test('unapplied edits do not start or arm acquisition', () async {
    final link = FakeLink(), controller = ScopeController(link);
    await controller.connect();
    controller.edit((s) => s.copyWith(timebase: 7));
    await controller.start();
    expect(link.commands, isEmpty);
    expect(controller.error, contains('примените'));
    await controller.single();
    expect(link.commands, isEmpty);
    await controller.disconnect();
    controller.dispose();
  });
}
