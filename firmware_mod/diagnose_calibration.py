#!/usr/bin/env python3
"""Exercise the original 1.2.0 calibration controller on synthetic ADC buffers.

This is an offline ARM execution audit, not a capture from the user's scope.
GPIO range selection, RTOS queues, analog hardware and measurement refresh
are replaced with hooks. No instrument is accessed and no firmware is patched.
"""
import hashlib
import json
import struct
from pathlib import Path

from unicorn import UC_HOOK_CODE
from inspect_runtime import *

HERE = Path(__file__).resolve().parent
STATE = 0x200000f8
STEP = 0x08022ae4
AFTER_STEP = 0x080240a4
MEASURE = 0x08025fc0
MOCK_CALLS = {0x080088a4: 'CH1 range hardware',
              0x08008a58: 'CH2 range hardware',
              0x08041cf0: 'RTOS queue', MEASURE: 'measurement recalculation'}
STOCK_HASH = 'a17c5c35c97bb898f15672a1747bc1041d8ed507c16999ddba0d1e4e2ec0c760'


class Controller:
    def __init__(self, blob):
        self.u = u = runtime(blob)
        u.mem_map(0x40000000, 0x30000)
        u.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
        u.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        # Start after the settling phase: both channels, first range, low offset.
        # Coefficients are arbitrary fixture data, not read from an instrument.
        u.mem_write(STATE + 0x30, b'\x10')
        u.mem_write(STATE + 0x14, b'\x03')
        u.mem_write(STATE + 0x2d, b'\x05')
        u.mem_write(STATE + 4, b'\x9c\x9c')
        u.mem_write(STATE + 0x260, struct.pack('<120H', *([1000] * 120)))
        self.measurement_calls = 0
        self.completed_checks = 0
        self.mock_calls = 0
        self.tail = []
        self.reached_end = False

        def code(uc, pc, size, unused):
            self.tail.append(pc)
            self.tail = self.tail[-12:]
            if pc in MOCK_CALLS:
                self.mock_calls += 1
                if pc == MEASURE:
                    self.measurement_calls += 1
                uc.reg_write(UC_ARM_REG_R0, 1)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            elif pc == AFTER_STEP:
                self.reached_end = True
                uc.emu_stop()

        u.hook_add(UC_HOOK_CODE, code)

    def byte(self, offset):
        return self.u.mem_read(STATE + offset, 1)[0]

    def snapshot(self):
        return {'phase': f'{self.byte(0x30):02x}',
                'channelMask': self.byte(0x14),
                'timebaseIndex': self.byte(0x2d),
                'offsets': list(struct.unpack('bb', self.u.mem_read(STATE + 4, 2))),
                'firstCoefficient': struct.unpack('<H', self.u.mem_read(STATE + 0x260, 2))[0],
                'measurementRecalculationCalls': self.measurement_calls}

    def check(self, samples):
        assert len(samples) == 2 and all(len(bank) == 500 for bank in samples)
        for channel, bank in enumerate(samples):
            self.u.mem_write(STATE + 0x6aa + channel * 1024, bank)
        # Force only the cadence condition; elapsed real time is not modeled.
        self.u.mem_write(STATE + 0x32, b'\x28')
        self.u.reg_write(UC_ARM_REG_R10, STATE)
        self.u.reg_write(UC_ARM_REG_SP, 0x2006ff00)
        self.reached_end = False
        self.u.emu_start(STEP | 1, 0x081ff000, count=250000)
        assert self.reached_end, [hex(pc) for pc in self.tail]
        self.completed_checks += 1
        return self.snapshot()


def audit(blob):
    # A successful synthetic control: feed the current requested target exactly.
    c = Controller(blob)
    transitions = []
    for n in range(160):
        samples = [bytes([28 if c.byte(4 + ch) == 0x9c else 228]) * 500
                   for ch in range(2)]
        before = c.snapshot()
        after = c.check(samples)
        if any(before[key] != after[key] for key in
               ('offsets', 'channelMask', 'timebaseIndex')) or after['phase'] == '00':
            transitions.append({'check': n + 1, **after})
        if after['phase'] == '00':
            break
    assert c.completed_checks == 120 and c.measurement_calls == 1
    positive = {'forcedChecks': c.completed_checks, 'final': c.snapshot(),
                'majorTransitions': transitions}

    # Input is deliberately unresponsive to offset changes. The native controller
    # returns each time, but it keeps the same calibration phase, with no failure.
    c = Controller(blob)
    history = []
    for n in range(256):
        state = c.check([bytes([100]) * 500] * 2)
        assert state['phase'] == '10' and state['measurementRecalculationCalls'] == 0
        if n < 4 or n >= 252:
            history.append({'check': n + 1, **state})
    stalled = {'forcedChecks': c.completed_checks, 'allControllerChecksReturned': True,
               'final': c.snapshot(), 'selectedChecks': history,
               'fixtureCoefficientWrappedOnSecondCheck': history[1]['firstCoefficient'] == 65384}
    assert stalled['fixtureCoefficientWrappedOnSecondCheck']

    # The acceptance condition concerns the mean of 500 samples, not whether
    # the waveform itself is smooth. Test both sides and a high-ripple control.
    cases = [
        ('mean_27.8_outside', bytes([27]) * 100 + bytes([28]) * 400, False),
        ('mean_27.95_inside', bytes([27]) * 25 + bytes([28]) * 475, True),
        ('mean_28_exact', bytes([28]) * 500, True),
        ('mean_28.2_inside', bytes([28]) * 400 + bytes([29]) * 100, True),
        ('mean_28.4_outside', bytes([28]) * 300 + bytes([29]) * 200, False),
        ('same_mean_large_ripple', bytes([0, 56]) * 250, True),
    ]
    means = []
    for name, bank, accepted in cases:
        c = Controller(blob)
        after = c.check([bank, bank])
        # Identical in-window inputs set the initial dual phase's acceptance bit.
        assert after['phase'] == ('50' if accepted else '10'), (name, after)
        means.append({'case': name, 'sampleMean': sum(bank) / 500,
                      'identicalInputsInsideAcceptanceWindow': accepted, **after})
    return {'idealTargetControl': positive, 'unresponsiveInputModel': stalled,
            'meanAcceptanceControls': means}


def keyboard_gate(blob):
    """Test the native gate before any key callback, not the callbacks themselves."""
    u = runtime(blob)
    reached = []

    def code(uc, pc, size, unused):
        if pc in (0x08040038, 0x0804008c):
            reached.append('key_feedback_only' if pc == 0x08040038 else 'dispatch_key')
            uc.emu_stop()

    u.hook_add(UC_HOOK_CODE, code)
    controls = []
    for active in (False, True):
        decisions = []
        for key in range(1, 24):
            # Other modal flags are clear to isolate the calibration gate.
            for offset in (0x2f, 0x30, 0x33, 0xf2c):
                u.mem_write(STATE + offset, b'\x00')
            u.mem_write(STATE + 0x30, b'\x10' if active else b'\x00')
            u.mem_write(0x2006ff07, bytes([key]))
            u.reg_write(UC_ARM_REG_R7, STATE)
            u.reg_write(UC_ARM_REG_SP, 0x2006ff00)
            n = len(reached)
            u.emu_start(0x08040075, 0x081ff000, count=100)
            assert len(reached) == n + 1
            decisions.append(reached[-1])
        expected = 'key_feedback_only' if active else 'dispatch_key'
        assert all(decision == expected for decision in decisions)
        controls.append({'calibrationActive': active, 'keysTested': len(decisions),
                         'decisionForEveryKey': expected})
    return controls


def initializer_control(blob):
    results = []
    for mask in (0, 1, 2, 3):
        c = Controller(blob)
        c.u.mem_write(STATE + 0x14, bytes([mask]))
        c.u.mem_write(STATE + 0xf68, b'\x02')
        c.u.mem_write(STATE + 0x2e, b'\x01')
        c.u.mem_write(STATE + 0x2d, b'\x0d')
        call(c.u, 0x0800df58, [])
        result = {'startingChannelMask': mask, **c.snapshot(), 'run': c.byte(0x2e)}
        assert result['channelMask'] == 3
        assert result['phase'] == '01' and result['offsets'] == [-100, -100]
        assert result['timebaseIndex'] == 5 and result['run'] == 1
        results.append(result)
    return results


def main():
    images = {'stock': FW.read_bytes(),
              'r1': (HERE / 'build/APP_2C53T_V1.2.0_RU_EXPERIMENTAL.bin').read_bytes(),
              'r2': (HERE / 'build/APP_2C53T_V1.2.0_RU_CSV_r2.bin').read_bytes()}
    stock = images['stock']
    assert hashlib.sha256(stock).hexdigest() == STOCK_HASH
    ranges = [(0x0800df58, 0x0800e2f0), (0x08020e98, 0x08024300),
              (0x08040008, 0x0804009c)]
    identical = []
    for start, end in ranges:
        native = stock[start - BASE:end - BASE]
        assert all(blob[start - BASE:end - BASE] == native for blob in images.values())
        identical.append({'start': hex(start), 'endExclusive': hex(end),
                          'allThreeIdentical': True, 'sha256': hashlib.sha256(native).hexdigest()})
    results = {name: audit(blob) for name, blob in images.items()}
    assert results['stock'] == results['r1'] == results['r2']
    keys = {name: keyboard_gate(blob) for name, blob in images.items()}
    assert keys['stock'] == keys['r1'] == keys['r2']
    initial = {name: initializer_control(blob) for name, blob in images.items()}
    assert initial['stock'] == initial['r1'] == initial['r2']
    pointer = struct.unpack_from('<I', stock, 0x0804c078 - BASE)[0]
    end = stock.index(b'\x00', pointer - BASE)
    message = stock[pointer - BASE:end].decode('utf8')
    assert message == 'Ожидание автокоррекции...'
    upper, lower = struct.unpack_from('<dd', stock, 0x08023d40 - BASE)
    assert upper == 0.3 and lower == -0.1
    report = {
        'status': 'offline-calibration-nonconvergence-reproduced-not-incident-localized',
        'hardwareAccessed': False, 'firmwareModified': False,
        'imageSHA256': {name: hashlib.sha256(blob).hexdigest() for name, blob in images.items()},
        'identicalRegions': identical,
        'waitDialog': {'messageRU': message, 'nativeGatePC': '0x0801c93a',
                       'calibrationStateAddress': hex(STATE + 0x30)},
        'examinedController': {'entry': hex(STEP), 'end': hex(AFTER_STEP),
                               'sampleCountPerChannel': 500,
                               'lowTargetCode': 28, 'highTargetCode': 228,
                               'meanErrorAcceptanceConstants': [lower, upper]},
        'stockR1R2ExecutionIdentical': True,
        'results': results['stock'],
        'nativeKeyDispatchGate': {'entry': '0x08040074', 'controls': keys['stock']},
        'nativeCalibrationInitializer': {'entry': '0x0800df58', 'controls': initial['stock'],
            'result': 'Both channels are enabled for calibration, including when CH2 starts disabled.'},
        'userReportedObservation': {
            'description': 'Calibration wait dialog; probes disconnected; measurements update; keys beep without action; scales change.',
            'source': 'User report and photo, not an instrument memory capture.',
            'implication': 'Display activity alone cannot identify calibration progress. Native range routines can change hardware without changing the visible mV label; see calibration-flow-audit.json.'},
        'modeledNativeChecksPerImage': 120 + 256 + len(results['stock']['meanAcceptanceControls']) + len(initial['stock']),
        'limitations': [
            'The actual ADC samples, calibration phase and coefficients of the user instrument are unknown.',
            'The model starts after settling and forces each cadence check; it does not predict wall-clock duration.',
            'Range GPIO, RTOS queues, analog response and measurement recalculation are stubbed.',
            'Only the pre-dispatch keyboard gate is tested; key callbacks, display and scheduler are not executed.',
            'The stuck synthetic input demonstrates a possible endless calibration, not the cause of the observed incident.',
            'A moving waveform or beeping keys alone cannot show whether calibration is progressing.'
        ]}
    dest = HERE / 'build/calibration-incident-audit.json'
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(dest), 'images': len(images),
                      'nativeChecksPerImage': report['modeledNativeChecksPerImage'],
                      'idealTargetChecksUntilCompletion': 120,
                      'unresponsiveInputChecksWithoutPhaseProgress': 256,
                      'stockR1R2ExecutionIdentical': True}, indent=2))


if __name__ == '__main__':
    main()
