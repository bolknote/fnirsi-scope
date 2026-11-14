#!/usr/bin/env python3
"""Native-code controls for calibration readiness, settings and offset output.

No USB or physical instrument is accessed. MCU flash is an emulator memory map.
The settings serializer is stopped BEFORE flash unlock/erase/programming, and
the boot parser is stopped before hardware setup. ADC buffers are synthetic.
"""
import hashlib
import json
import struct
from pathlib import Path

from unicorn import UC_HOOK_CODE
from inspect_runtime import *
from diagnose_calibration import Controller, STATE, STOCK_HASH, STEP

HERE = Path(__file__).resolve().parent
CONFIG = 0x08006000
COEFFICIENTS = STATE + 0x260
COUNT = 120


def machine(blob):
    u = runtime(blob)
    u.mem_map(0x40000000, 0x30000)
    u.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
    u.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
    return u


def coefficients(u):
    return list(struct.unpack('<120H', u.mem_read(COEFFICIENTS, COUNT * 2)))


def serialize(blob, mode, words):
    u = machine(blob)
    u.mem_write(COEFFICIENTS, struct.pack('<120H', *words))
    u.mem_write(STATE + 0x350, bytes([128, 129, 130, 131]))
    u.mem_write(STATE + 0xf60, b'\x03')
    # This allocation exists only in the emulator, not the instrument's heap.
    allocated = 0x20050000
    stopped = []

    def code(uc, pc, size, unused):
        if pc == 0x0803acfc:
            assert uc.reg_read(UC_ARM_REG_R0) == 512
            uc.reg_write(UC_ARM_REG_R0, allocated)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
        elif pc == 0x08029a48:
            stopped.append(pc)
            uc.emu_stop()

    u.hook_add(UC_HOOK_CODE, code)
    u.reg_write(UC_ARM_REG_SP, 0x2006ff00)
    u.reg_write(UC_ARM_REG_R0, mode)
    u.emu_start(0x080293bd, 0x081ff000, count=20000)
    assert stopped == [0x08029a48]
    record = bytes(u.mem_read(allocated, 0x130))
    assert record[0] == (0x55 if mode == 0x55 else 0xaa)
    packed = [words[i] | words[i + 60] << 16 for i in range(60)]
    assert record[0x38:0x128] == struct.pack('<60I', *packed)
    assert record[0x128:0x12a] == bytes([128, 129])
    assert record[0x12c:0x12e] == bytes([130, 131])
    assert coefficients(u) == words
    return record, {'inputMode': mode, 'recordMarker': hex(record[0]),
                    'all120CoefficientsPreserved': True,
                    'languageAfterCall': u.mem_read(STATE + 0xf60, 1)[0],
                    'stoppedBeforeFlashProgramming': True,
                    'serializedRecordSHA256': hashlib.sha256(record).hexdigest()}


def parse(blob, record):
    u = machine(blob)
    u.mem_write(CONFIG, bytes(record) + bytes(512 - len(record)))
    u.reg_write(UC_ARM_REG_R4, CONFIG)
    u.reg_write(UC_ARM_REG_SP, 0x2006ff00)
    stop = []

    def code(uc, pc, size, unused):
        if pc == 0x0802d50a:
            stop.append(pc)
            uc.emu_stop()

    u.hook_add(UC_HOOK_CODE, code)
    u.emu_start(0x0802cd93, 0x081ff000, count=10000)
    assert stop == [0x0802d50a]
    return coefficients(u), list(u.mem_read(STATE + 0x350, 4))


def settings_controls(blob):
    initial = list(range(1500, 1560)) + list(range(3000, 3060))
    normal, normal_report = serialize(blob, 0x55, initial)
    factory, factory_report = serialize(blob, 0, initial)
    assert normal_report['languageAfterCall'] == 3
    assert factory_report['languageAfterCall'] == 1
    assert parse(blob, normal)[0] == initial
    assert parse(blob, factory)[0] == initial
    corrupt = initial.copy()
    corrupt[0] = 0xffff
    reset_corrupt, _ = serialize(blob, 0, corrupt)
    assert parse(blob, reset_corrupt)[0] == corrupt
    cases = []
    for label, ch, index, value, survives in [
        ('CH1 first coefficient is FFFF', 0, 0, 0xffff, True),
        ('CH1 first coefficient is zero', 0, 0, 0, True),
        ('CH2 first coefficient exceeds 12 bits', 1, 0, 5000, True),
        ('CH2 last coefficient is FFFF', 1, 59, 0xffff, False),
        ('CH2 last coefficient is zero', 1, 59, 0, False),
        ('CH2 last coefficient is 5000', 1, 59, 5000, True),
    ]:
        altered = bytearray(normal)
        struct.pack_into('<H', altered, 0x38 + index * 4 + ch * 2, value)
        loaded, alignment = parse(blob, altered)
        expected = initial.copy()
        expected[ch * 60 + index] = value
        assert (loaded == expected) == survives, label
        cases.append({'case': label, 'tableAcceptedUnchanged': survives,
                      'firstCH1': loaded[0], 'lastCH2': loaded[-1]})
    erased = bytes([255]) * 0x130
    defaults, alignment = parse(blob, erased)
    assert all(0 < word < 4096 for word in defaults)
    return normal, defaults, {
        'normalSave': normal_report, 'factoryReset': factory_report,
        'invalidFirstCoefficientSurvivesFactoryResetAndBoot': True,
        'parserChecks': cases, 'erasedRecordLoadsNominalTable': True,
        'nominalCoefficients': defaults, 'nominalAlignmentBytes': alignment,
        'parserGuard': 'Header must be 55/AA; only last CH2 coefficient is checked for 0/FFFF before nominal-table fallback.'}


def readiness_controls(blob):
    reports = []
    children = {0x080242ec, 0x080251e4, 0x08027930, 0x08025aac, 0x080266f8}
    cases = [('ready', 1, 0, 0, True), ('no-frame', 0, 0, 0, False),
             ('other-acquisition-state-2', 2, 0, 0, False),
             ('other-acquisition-state-3', 3, 0, 0, False),
             ('settling-A', 1, 1, 0, False), ('settling-B', 1, 0, 1, False)]
    for label, ready, a, b, expected in cases:
        c = Controller(blob)
        u = c.u
        for offset, value in [(0xf68, 2), (0x2e, 1), (0x17, 1), (0x2f, 0),
                              (0x32, 40), (0xdb0, ready), (0xdba, a), (0xdbb, b)]:
            u.mem_write(STATE + offset, bytes([value]))
        outcome = []

        def code(uc, pc, size, unused):
            if pc == STEP:
                outcome.append('calibration-step');uc.emu_stop()
            elif pc in (0x080224b2, 0x080224d4):
                outcome.append('outside-calibration-step');uc.emu_stop()
            elif pc in children:
                uc.reg_write(UC_ARM_REG_R0, 0)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

        u.hook_add(UC_HOOK_CODE, code)
        u.reg_write(UC_ARM_REG_SP, 0x2006ff00)
        u.reg_write(UC_ARM_REG_LR, 0x081ff001)
        u.emu_start(0x08020e99, 0x081ff000, count=100000)
        assert len(outcome) == 1, (label, hex(u.reg_read(UC_ARM_REG_PC)))
        assert (outcome[0] == 'calibration-step') == expected, (label, outcome)
        reports.append({'case': label, 'dataState': ready,
                        'settlingCounters': [a, b], 'calibrationStepReached': expected})
    return reports


def range_controls(blob, nominal):
    rows = []
    for ch, routine, register in [(0, 0x080088a4, 0x40007408),
                                  (1, 0x08008a58, 0x40001c34)]:
        for word in (1600, 0, 0xffff, 5000):
            u = machine(blob)
            u.mem_write(COEFFICIENTS, struct.pack('<120H', *nominal))
            u.mem_write(COEFFICIENTS + ch * 120 + 2 * 7, struct.pack('<H', word))
            u.mem_write(STATE + 0x2d, b'\x05')
            u.mem_write(STATE + 0x14, b'\x03')
            u.mem_write(STATE + 4 + ch, b'\x9c')
            # Existing visible scale is 10 mV; request hardware range 7.
            u.mem_write(STATE + 2 + ch, b'\x00')
            call(u, routine, [7])
            value = struct.unpack('<I', u.mem_read(register, 4))[0]
            assert value == (word & 4095 if ch == 0 else word)
            assert u.mem_read(STATE + 2 + ch, 1) == b'\x00'
            rows.append({'channel': ch + 1, 'requestedHardwareRange': 7,
                         'visibleRangeIndexStill': 0, 'coefficient': word,
                         'offsetRegister': hex(register), 'writtenValue': value})
    return rows


def overflow_control(blob, nominal):
    c = Controller(blob)
    c.u.mem_write(COEFFICIENTS, struct.pack('<120H', *nominal))
    rows = []
    for n in range(6):
        before = coefficients(c.u)
        after = c.check([bytes([100]) * 500] * 2)
        rows.append({'check': n + 1, 'sampleMean': 100, 'target': 28,
                     'previousCH1Coefficient': before[0],
                     'CH1Coefficient': coefficients(c.u)[0],
                     'CH2Coefficient': coefficients(c.u)[60],
                     'CH1OffsetRegister': struct.unpack('<I', c.u.mem_read(0x40007408, 4))[0],
                     'CH2OffsetRegister': struct.unpack('<I', c.u.mem_read(0x40001c34, 4))[0],
                     'calibrationPhase': after['phase']})
    assert all(row['calibrationPhase'] == '10' for row in rows)
    assert any(row['CH1Coefficient'] > 4095 for row in rows)
    return rows


def phase_latch_controls(blob):
    def level(n): return bytes([n]) * 500
    c = Controller(blob)
    stages = [c.check([level(100), level(28)])['phase'],
              c.check([level(100), level(28)])['phase'],
              c.check([level(28), level(100)])['phase']]
    assert stages == ['50', '50', '11']
    d = Controller(blob)
    reverse = [d.check([level(28), level(100)])['phase'],
               d.check([level(28), level(28)])['phase'],
               d.check([level(28), level(28)])['phase']]
    assert reverse == ['10', '50', '11']
    return {'bank1AcceptedWhileBank0Outside': stages,
            'bank0AcceptedWhileBank1Outside': reverse,
            'bit6Interpretation': 'Bank 1 acceptance is latched, then bank 0 is checked before advancing the range.'}


def auto_shutdown_controls(blob):
    rows = []
    for mode, idle, active, expected in [(0, 1000, True, False),
                                         (1, 899, True, False),
                                         (1, 900, True, True),
                                         (1, 900, False, True)]:
        u = machine(blob)
        u.mem_write(STATE + 0x30, b'\x10' if active else b'\x00')
        u.mem_write(STATE + 0xf65, bytes([mode]))
        u.mem_write(STATE + 0xf66, b'\x00')
        u.mem_write(STATE + 0xf68, b'\x02')
        u.mem_write(STATE + 0xf6c, struct.pack('<H', idle))
        captured = []

        def code(uc, pc, size, unused):
            if pc == 0x080293bc:
                captured.append(uc.reg_read(UC_ARM_REG_R0))
                uc.emu_stop()
            elif pc == 0x08041cf0:
                uc.reg_write(UC_ARM_REG_R0, 1)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

        u.hook_add(UC_HOOK_CODE, code)
        u.reg_write(UC_ARM_REG_SP, 0x2006ff00)
        u.reg_write(UC_ARM_REG_LR, 0x081ff001)
        u.emu_start(0x080406c9, 0x081ff000, count=100000)
        assert bool(captured) == expected
        assert captured == ([0x55] if expected else [])
        assert u.reg_read(UC_ARM_REG_PC) in (0x080293bc, 0x081ff000)
        rows.append({'autoShutdownMode': mode, 'previousIdleCounter': idle,
                     'calibrationActive': active, 'settingsSaveCalled': expected,
                     'stoppedBeforeSettingsSaveBody': bool(captured)})
    return rows


def audit(blob):
    record, nominal, settings = settings_controls(blob)
    return {'settingsAndCalibrationRecord': settings,
            'controllerReadinessGates': readiness_controls(blob),
            'realHardwareRangeRoutines': range_controls(blob, nominal),
            'unresponsiveInputStartingWithNominalCoefficients': overflow_control(blob, nominal),
            'dualBankAcceptanceLatch': phase_latch_controls(blob),
            'autoShutdownDuringCalibration': auto_shutdown_controls(blob)}


def main():
    images = {'stock': FW.read_bytes(),
              'r1': (HERE / 'build/APP_2C53T_V1.2.0_RU_EXPERIMENTAL.bin').read_bytes(),
              'r2': (HERE / 'build/APP_2C53T_V1.2.0_RU_CSV_r2.bin').read_bytes()}
    assert hashlib.sha256(images['stock']).hexdigest() == STOCK_HASH
    identical_regions = []
    for start, end in [(0x080088a4, 0x08008c60), (0x08020e98, 0x08024300),
                       (0x080293bc, 0x08029d78), (0x0802cd92, 0x0802d50a),
                       (0x0803e454, 0x08040008), (0x080406c8, 0x08040734)]:
        native = images['stock'][start - BASE:end - BASE]
        assert all(blob[start - BASE:end - BASE] == native for blob in images.values())
        identical_regions.append({'start': hex(start), 'endExclusive': hex(end),
                                  'allThreeIdentical': True,
                                  'sha256': hashlib.sha256(native).hexdigest()})
    results = {name: audit(blob) for name, blob in images.items()}
    assert results['stock'] == results['r1'] == results['r2']
    report = {'status': 'native-offline-controls-passed-not-instrument-dump',
              'hardwareAccessed': False, 'firmwareModified': False,
              'stockR1R2ResultsIdentical': True,
              'identicalCodeRegions': identical_regions,
              'imageSHA256': {name: hashlib.sha256(blob).hexdigest() for name, blob in images.items()},
              'results': results['stock'],
              'importantCorrection': '0x08025fc0 recalculates measurements; it is not calibration persistence. Coefficients are serialized with settings by 0x080293bc.',
              'limits': ['ADC buffers and coefficients are synthetic; the actual instrument values are unknown.',
                         'The settings serializer stops before MCU flash unlock/erase/programming.',
                         'The boot parser starts after hardware/task setup and stops before subsequent peripheral setup.',
                         'Readiness tests execute the native controller gates but stub measurement child functions.',
                         'Range-routine MMIO writes are emulator memory, not observed analog outputs.',
                         'No failing analog component, device-side table corruption or connection to the SAVE error is proven.']}
    out = HERE / 'build/calibration-flow-audit.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(out), 'stockR1R2ResultsIdentical': True,
                      'factoryResetPreservesCalibration': True,
                      'nonLastInvalidCoefficientsAccepted': True,
                      'nativeReadinessCases': len(results['stock']['controllerReadinessGates'])}, indent=2))


if __name__ == '__main__':
    main()
