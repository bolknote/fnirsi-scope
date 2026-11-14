import 'dart:async';
import 'dart:collection';
import 'dart:math' as math;
import 'package:flutter/foundation.dart';
import 'protocol.dart';
import 'recording.dart';
import 'usb.dart';

enum Acquisition { disconnected, stopped, live, waiting, reading, interrupted }

enum CaptureOutcome { complete, stopped, timedOut, failed }

/// Owns one USB connection and serializes all operations, including STOP.
class ScopeController extends ChangeNotifier {
  final ScopeLink link;
  ScopeController(this.link);
  bool connected = false, busy = false, dirty = false;
  String? error;
  ScopeSettings? settings, draft;
  CaptureStatus? status;
  CalibrationStatus? calibration;
  ScopeFrame? frame;
  final _recordings = <RecordedFrame>[];
  late final List<RecordedFrame> recordings = UnmodifiableListView(_recordings);
  int captureTarget = 0, capturedCount = 0;
  CaptureOutcome? captureOutcome;
  Acquisition acquisition = Acquisition.disconnected;
  Timer? _timer, _idleTimer, _calibrationTimer;
  int _epoch = 0, _singleBefore = 0;
  bool _disposed = false, _calibrationPolling = false;
  bool get calibrating =>
      calibration?.active == true || settings?.calibrationActive == true;
  bool get controlAvailable => acquisition != Acquisition.interrupted;
  bool get capturing =>
      acquisition == Acquisition.waiting || acquisition == Acquisition.reading;
  bool get triggerArmed =>
      acquisition == Acquisition.waiting && status?.phaseName == 'armed';
  Future<void> _tail = Future<void>.value();

  RecordedFrame? recordCurrentFrame() {
    final current = frame;
    if (current == null ||
        !current.verified ||
        busy ||
        capturing ||
        acquisition == Acquisition.live ||
        settings?.running == true) {
      return null;
    }
    for (final item in _recordings) {
      if (identical(item.frame, current)) return item;
    }
    final record = _record(current);
    _notify();
    return record;
  }

  RecordedFrame _record(ScopeFrame frame) {
    final record = RecordedFrame(frame);
    _recordings.add(record);
    return record;
  }

  void openRecording(List<RecordedFrame> records) {
    _recordings.addAll(records);
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  Future<T> _serial<T>(Future<T> Function() fn) {
    final c = Completer<T>();
    _tail = _tail.then((_) async {
      try {
        c.complete(await fn());
      } catch (e, st) {
        c.completeError(e, st);
      }
    });
    return c.future;
  }

  Future<void> _action(Future<void> Function() fn) async {
    busy = true;
    error = null;
    _notify();
    try {
      await _serial(fn);
    } catch (e) {
      if (e is ScopeException &&
          e.message == 'Прибор не подтвердил USB-команду.') {
        acquisition = Acquisition.interrupted;
      }
      error = acquisition == Acquisition.interrupted
          ? '$e Состояние прибора не подтверждено. Если кнопки не отвечают, нажмите RESET и переподключите USB.'
          : e.toString();
    } finally {
      busy = false;
      _notify();
    }
  }

  void _sync(ScopeSettings next, CaptureStatus nextStatus) {
    settings = next;
    status = nextStatus;
    if (!dirty) {
      draft = next.copyWith(triggerPosition: nextStatus.pretrigger * 3 - 150);
    }
    if (next.liveSnapshotSupported &&
        next.calibrationActive &&
        !(_calibrationTimer?.isActive ?? false)) {
      _watchCalibration();
    }
    _notify();
  }

  void _requireConnected() {
    if (!connected) {
      throw const ScopeException('Сначала подключите осциллограф.');
    }
  }

  void _checkAcquisition() {
    final s = settings!;
    if (s.calibrationActive || s.spiFault) {
      throw const ScopeException('Идёт калибровка или обнаружена ошибка SPI.');
    }
    if (s.mathMode || s.channels.any((c) => c.fft)) {
      throw const ScopeException(
        'Выключите FFT и математический режим на приборе.',
      );
    }
    if (s.timebase >= captureTimeScaleCount) {
      throw const ScopeException('Для чтения выберите развёртку до 20 мс/дел.');
    }
  }

  Future<void> connect() => _action(() async {
    if (connected) return;
    await link.connect();
    try {
      final s = await link.settings(), st = await link.status();
      connected = true;
      frame =
          null; // Never label a previous device's frame as the new connection.
      acquisition = Acquisition.stopped;
      _sync(s, st);
      if (s.liveSnapshotSupported) {
        calibration = await link.calibration();
        if (calibration!.active) _watchCalibration();
      }
      // A previously frozen frame can be read without changing instrument state.
      if (st.phaseName == 'ready' &&
          st.frameId != 0 &&
          st.frameBytes == 0xe10) {
        try {
          frame = await link.frame(st);
          _notify();
        } on ScopeException catch (e) {
          error = 'Подключено. Последний кадр не прочитан: ${e.message}';
        }
      }
      _idleTimer?.cancel();
      _idleTimer = Timer.periodic(const Duration(seconds: 4), (_) {
        if (!connected ||
            busy ||
            calibrating ||
            acquisition == Acquisition.live ||
            acquisition == Acquisition.waiting ||
            acquisition == Acquisition.reading) {
          return;
        }
        _serial(() async {
          if (!connected) return;
          try {
            _sync(await link.settings(), await link.status());
          } catch (e) {
            await link.close();
            connected = false;
            settings = null;
            draft = null;
            status = null;
            acquisition = Acquisition.disconnected;
            error = e.toString();
            _notify();
          }
        });
      });
    } catch (_) {
      await link.close();
      rethrow;
    }
  });
  Future<void> disconnect() async {
    if (busy) return;
    if (capturing) captureOutcome = CaptureOutcome.stopped;
    ++_epoch;
    _timer?.cancel();
    _idleTimer?.cancel();
    _calibrationTimer?.cancel();
    await _action(() async {
      if (connected &&
          (acquisition == Acquisition.live ||
              acquisition == Acquisition.waiting ||
              acquisition == Acquisition.reading)) {
        try {
          await link.command(26);
        } catch (_) {
          /* still release a removed device */
        }
      }
      await link.close();
      connected = false;
      settings = null;
      draft = null;
      status = null;
      calibration = null;
      dirty = false;
      acquisition = Acquisition.disconnected;
    });
  }

  void edit(ScopeSettings Function(ScopeSettings) change) {
    if (draft == null || busy || capturing) return;
    draft = change(draft!);
    dirty = true;
    error = null;
    _notify();
  }

  void discardEdits() {
    if (settings != null) {
      draft = settings!.copyWith(
        triggerPosition: (status?.pretrigger ?? 50) * 3 - 150,
      );
      dirty = false;
      _notify();
    }
  }

  void _watchCalibration() {
    _calibrationTimer?.cancel();
    _calibrationTimer = Timer.periodic(const Duration(seconds: 1), (_) async {
      if (!connected || _calibrationPolling || busy) return;
      _calibrationPolling = true;
      try {
        await _serial(() async {
          if (!connected) return;
          calibration = await link.calibration();
          if (calibration?.active != true) {
            _sync(await link.settings(), await link.status());
            _calibrationTimer?.cancel();
          }
        });
      } catch (e) {
        _calibrationTimer?.cancel();
        error = 'Не удалось прочитать состояние калибровки: $e';
      } finally {
        _calibrationPolling = false;
        _notify();
      }
    });
  }

  /// Caller must first confirm that both BNC inputs are disconnected.
  Future<void> startCalibration() async {
    if (!connected || busy || capturing || calibrating || !controlAvailable) {
      return;
    }
    ++_epoch;
    _timer?.cancel();
    await _action(() async {
      _requireConnected();
      final current = await link.settings();
      if (!current.liveSnapshotSupported) {
        throw const ScopeException(
          'Для управления калибровкой нужна прошивка r6j.',
        );
      }
      if (current.spiFault || current.calibrationActive) {
        throw const ScopeException('Прибор не готов к калибровке.');
      }
      try {
        if (current.running) await link.command(26);
        acquisition = Acquisition.stopped;
        frame = null;
        await link.command(35);
        calibration = await link.calibration();
        if (calibration?.active != true) {
          throw ScopeException(
            calibration?.description ?? 'Калибровка не запустилась.',
          );
        }
        _watchCalibration();
      } catch (_) {
        acquisition = Acquisition.interrupted;
        rethrow;
      }
    });
  }

  Future<void> cancelCalibration() async {
    if (!connected || !calibrating) return;
    await _action(() async {
      await link.command(36);
      calibration = await link.calibration();
      if (calibration?.active == true) {
        _watchCalibration();
      } else {
        _calibrationTimer?.cancel();
        _sync(await link.settings(), await link.status());
      }
    });
  }

  Future<void> _checkField(
    int operation,
    int argument,
    bool Function(ScopeSettings) correct,
  ) async {
    await link.command(operation, argument);
    final state = await link.settings();
    if (!correct(state) || state.running) {
      throw ScopeException(
        'Прибор не подтвердил настройку (команда $operation).',
      );
    }
  }

  Future<void> apply() async {
    if (!connected ||
        busy ||
        capturing ||
        calibrating ||
        !controlAvailable ||
        draft == null ||
        !dirty) {
      return;
    }
    final target = draft!;
    if (target.channelMask == 0 ||
        !target.channels[target.triggerChannel - 1].enabled) {
      error =
          'Нужен хотя бы один канал, а источник триггера должен быть включён.';
      _notify();
      return;
    }
    if (target.calibrationActive ||
        target.spiFault ||
        target.mathMode ||
        target.channels.any((c) => c.fft)) {
      error =
          'Дождитесь окончания калибровки и выключите FFT/математический режим.';
      _notify();
      return;
    }
    final resume = acquisition == Acquisition.live;
    ++_epoch;
    _timer?.cancel();
    await _action(() async {
      _requireConnected();
      var before = await link.settings();
      if (before.calibrationActive ||
          before.spiFault ||
          before.mathMode ||
          before.channels.any((c) => c.fft)) {
        throw const ScopeException(
          'Прибор сейчас не готов к изменению настроек.',
        );
      }
      await link.command(26);
      acquisition = Acquisition.stopped;
      if (target.channelMask != before.channelMask) {
        await _checkField(
          16,
          target.channelMask,
          (s) => s.channelMask == target.channelMask,
        );
      }
      for (var i = 0; i < 2; i++) {
        final old = before.channels[i], c = target.channels[i];
        if (c.probe != old.probe) {
          await _checkField(
            19 + i,
            c.probe == 1 ? 0 : 1,
            (s) => s.channels[i].probe == c.probe,
          );
        }
        if (c.range != old.range) {
          await _checkField(
            17 + i,
            c.range,
            (s) => s.channels[i].range == c.range,
          );
        }
        if (c.coupling != old.coupling) {
          await _checkField(
            21 + i,
            c.coupling == 'AC' ? 0 : 1,
            (s) => s.channels[i].coupling == c.coupling,
          );
        }
        if (c.offset != old.offset) {
          await _checkField(
            23 + i,
            c.offset,
            (s) => s.channels[i].offset == c.offset,
          );
        }
      }
      if (target.timebase != before.timebase) {
        await _checkField(
          28,
          target.timebase,
          (s) => s.timebase == target.timebase,
        );
      }
      if (target.triggerMode != before.triggerMode) {
        await _checkField(
          29,
          target.triggerMode,
          (s) => s.triggerMode == target.triggerMode,
        );
      }
      // Channel-mask updates may have changed the source; send the desired source explicitly.
      await _checkField(
        30,
        target.triggerChannel - 1,
        (s) => s.triggerChannel == target.triggerChannel,
      );
      if (target.falling != before.falling) {
        await _checkField(
          31,
          target.falling ? 1 : 0,
          (s) => s.falling == target.falling,
        );
      }
      if (target.triggerLevel != before.triggerLevel) {
        await _checkField(
          32,
          target.triggerLevel,
          (s) => s.triggerLevel == target.triggerLevel,
        );
      }
      if (target.filterMask != before.filterMask) {
        await _checkField(
          8,
          target.filterMask,
          (s) => s.filterMask == target.filterMask,
        );
      }
      final pre = ((target.triggerPosition + 150) / 3).round().clamp(0, 100);
      if (pre != (status?.pretrigger ?? 50)) {
        await link.command(7, pre);
        if ((await link.status()).pretrigger != pre) {
          throw const ScopeException(
            'Прибор не подтвердил положение триггера.',
          );
        }
      }
      dirty = false;
      _sync(await link.settings(), await link.status());
      if (resume) {
        try {
          await link.command(25, settings!.triggerMode == 0 ? 0 : 1);
          acquisition = Acquisition.live;
          final epoch = ++_epoch;
          _schedulePreview(epoch);
        } catch (_) {
          // A timed-out command may still be queued in the firmware. Do not
          // automatically issue a second command into an unknown state.
          acquisition = Acquisition.interrupted;
          rethrow;
        }
      }
    });
  }

  Future<void> start() async {
    if (!connected || busy || capturing || !controlAvailable) return;
    if (calibrating) return;
    if (acquisition == Acquisition.live) return;
    if (dirty) {
      error = 'Сначала примените изменения настроек.';
      _notify();
      return;
    }
    final epoch = ++_epoch;
    _timer?.cancel();
    await _action(() async {
      _requireConnected();
      _checkAcquisition();
      if (!settings!.liveSnapshotSupported) {
        throw const ScopeException('Для живого просмотра нужна прошивка r6j.');
      }
      try {
        captureTarget = 0;
        captureOutcome = null;
        await link.command(25, settings!.triggerMode == 0 ? 0 : 1);
        final s = await link.settings();
        if (!s.running) {
          throw const ScopeException('Прибор не подтвердил запуск.');
        }
        acquisition = Acquisition.live;
        _sync(s, await link.status());
        if (epoch == _epoch) _schedulePreview(epoch);
      } catch (_) {
        acquisition = Acquisition.interrupted;
        rethrow;
      }
    });
  }

  void _schedulePreview(int epoch) {
    if (epoch != _epoch || acquisition != Acquisition.live || !connected) {
      return;
    }
    final seconds = settings?.secondsPerDivision ?? .001;
    final wait = math.max(500, (12 * seconds * 1000 + 50).ceil());
    _timer = Timer(Duration(milliseconds: wait), () => _preview(epoch));
  }

  Future<void> _preview(int epoch) async {
    if (epoch != _epoch || !connected) return;
    try {
      await _serial(() async {
        if (epoch != _epoch || !connected) return;
        final next = await link.preview();
        if (epoch != _epoch || !connected) return;
        if (next != null) frame = next;
        final current = await link.settings();
        _sync(current, await link.status());
        if (!current.running) acquisition = Acquisition.stopped;
      });
      _notify();
      if (epoch == _epoch) _schedulePreview(epoch);
    } catch (e) {
      if (epoch == _epoch) {
        acquisition = Acquisition.interrupted;
        error =
            '$e Просмотр прерван. Переподключите прибор; если кнопки не отвечают, нажмите RESET.';
        _notify();
      }
    }
  }

  Future<void> stop() async {
    if (!connected || busy || calibrating || !controlAvailable) return;
    final stoppingCapture = capturing;
    ++_epoch;
    _timer?.cancel();
    await _action(() async {
      try {
        await link.command(26);
        final stopped = await link.settings();
        if (stopped.running) {
          throw const ScopeException('Прибор не подтвердил остановку.');
        }
        if (stoppingCapture) captureOutcome = CaptureOutcome.stopped;
        acquisition = Acquisition.stopped;
        _sync(stopped, await link.status());
      } catch (_) {
        if (stoppingCapture) captureOutcome = CaptureOutcome.failed;
        acquisition = Acquisition.interrupted;
        rethrow;
      }
    });
  }

  Future<void> single({int timeoutSeconds = 30}) =>
      captureSeries(count: 1, timeoutSeconds: timeoutSeconds);

  /// Rearm only after the host has read and verified the preceding frame.
  Future<void> captureSeries({
    required int count,
    required int timeoutSeconds,
  }) async {
    if (!connected || busy || capturing || calibrating || !controlAvailable) {
      return;
    }
    if (count < 1 || count > 50 || timeoutSeconds < 1 || timeoutSeconds > 600) {
      error = 'Выберите от 1 до 50 кадров и ожидание от 1 до 600 секунд.';
      _notify();
      return;
    }
    if (dirty) {
      error = 'Сначала примените изменения настроек.';
      _notify();
      return;
    }
    try {
      _checkAcquisition();
    } catch (e) {
      error = e.toString();
      _notify();
      return;
    }
    final epoch = ++_epoch;
    _timer?.cancel();
    await _action(() async {
      captureTarget = count;
      capturedCount = 0;
      captureOutcome = null;
      try {
        final stopped = await link.command(26);
        _singleBefore = stopped.frameId;
        await link.command(3, 0);
        await link.command(6, timeoutSeconds);
        await _armCapture(epoch);
        _scheduleCapture(epoch);
      } catch (_) {
        captureOutcome = CaptureOutcome.failed;
        acquisition = Acquisition.interrupted;
        rethrow;
      }
    });
  }

  Future<void> _armCapture(int epoch) async {
    if (epoch != _epoch) return;
    final armed = await link.command(33);
    final nextSettings = await link.settings();
    if (epoch != _epoch) return;
    acquisition = Acquisition.waiting;
    _sync(nextSettings, armed);
  }

  void _scheduleCapture(int epoch) {
    if (epoch != _epoch || !connected || !capturing) return;
    _timer = Timer(
      const Duration(milliseconds: 200),
      () => _pollCapture(epoch),
    );
  }

  Future<void> _pollCapture(int epoch) async {
    if (epoch != _epoch || !connected) return;
    try {
      await _serial(() async {
        if (epoch != _epoch || !connected) return;
        final next = await link.status();
        if (epoch != _epoch) return;
        status = next;
        if (next.phaseName == 'ready') {
          if (next.frameId == _singleBefore) {
            throw const ScopeException('Прибор не выдал новый кадр.');
          }
          acquisition = Acquisition.reading;
          _notify();
          final newFrame = await link.frame(next);
          if (epoch != _epoch) return;
          if (newFrame.preview ||
              !newFrame.verified ||
              newFrame.id != next.frameId) {
            throw const ScopeException(
              'Прибор не подтвердил новый кадр по триггеру.',
            );
          }
          frame = newFrame;
          _singleBefore = newFrame.id;
          capturedCount += 1;
          if (captureTarget > 1) _record(newFrame);
          if (capturedCount == captureTarget) {
            final stopped = await link.settings();
            if (epoch != _epoch) return;
            if (stopped.running) {
              throw const ScopeException(
                'Прибор не подтвердил остановку после захвата.',
              );
            }
            captureOutcome = CaptureOutcome.complete;
            acquisition = Acquisition.stopped;
            _sync(stopped, next);
          } else {
            _notify();
            await _armCapture(epoch);
          }
        } else if (next.phaseName == 'error' || next.phaseName == 'cancelled') {
          final stopped = await link.settings();
          if (epoch != _epoch) return;
          if (stopped.running) {
            throw const ScopeException(
              'Прибор не подтвердил остановку захвата.',
            );
          }
          captureOutcome = next.phaseName == 'cancelled'
              ? CaptureOutcome.stopped
              : next.error == 3
              ? CaptureOutcome.timedOut
              : CaptureOutcome.failed;
          if (captureOutcome == CaptureOutcome.failed) {
            error = next.errorMessage;
          }
          acquisition = Acquisition.stopped;
          _sync(stopped, next);
        } else if (next.phaseName == 'idle') {
          final stopped = await link.settings();
          if (epoch != _epoch) return;
          if (!stopped.running) {
            captureOutcome = CaptureOutcome.stopped;
            acquisition = Acquisition.stopped;
            _sync(stopped, next);
          }
        }
      });
      _notify();
      _scheduleCapture(epoch);
    } catch (e) {
      if (epoch == _epoch) {
        captureOutcome = CaptureOutcome.failed;
        acquisition = Acquisition.interrupted;
        error =
            '$e Захват прерван. Переподключите прибор; если кнопки не отвечают, нажмите RESET.';
        _notify();
      }
    }
  }

  @override
  void dispose() {
    _disposed = true;
    ++_epoch;
    _timer?.cancel();
    _idleTimer?.cancel();
    _calibrationTimer?.cancel();
    // Do not block Flutter's UI thread on native USB shutdown.
    unawaited(
      _serial(() async {
        try {
          if (connected &&
              (acquisition == Acquisition.live ||
                  acquisition == Acquisition.waiting ||
                  acquisition == Acquisition.reading)) {
            await link.command(26);
          }
        } catch (_) {}
        await link.close();
      }),
    );
    super.dispose();
  }
}
