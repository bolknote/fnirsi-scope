#!/usr/bin/env python3
"""Execute native key debounce/long-press decoding and the patched KEY task.

GPIO sampling is replaced by a physical key bitmask. The native counter,
ROM key map, RTOS queue publication, KEY dispatch and guarded initializer run
as ARM. No physical instrument, ADC acquisition or calibration flash writes.
"""
import hashlib
import json
import struct
from validate_remote import RemoteVM, KEY, S, G, BLOB, BUILD, word
from inspect_runtime import *
from unicorn import UC_HOOK_CODE

checks = []


def verify(name, ok):
    assert ok, name
    checks.append(dict(name=name, passed=True))


def scan(v, mask):
    """Run the actual decoder after the GPIO matrix has produced its mask."""
    u = v.u
    sp = 0x2006e000
    u.reg_write(UC_ARM_REG_SP, sp)
    u.reg_write(UC_ARM_REG_R11, 0x20002d56)
    u.mem_write(0x20002d56, struct.pack('<H', mask))
    u.mem_write(sp + 10, b'\0')
    before = len(v.queue)

    def finish(uc, address, size, data):
        if address == 0x08040508:
            uc.emu_stop()

    hook = u.hook_add(UC_HOOK_CODE, finish)
    try:
        u.emu_start(0x08040375, 0x081ff000, count=20000)
    finally:
        u.hook_del(hook)
    assert u.reg_read(UC_ARM_REG_PC) == 0x08040508
    return v.queue[before:]


def hold_auto(v):
    for _ in range(69):
        assert not scan(v, 1 << 1)
    return scan(v, 1 << 1)


def main():
    v = RemoteVM().stopped()
    verify('Long AUTO ROM map remains native event 16', BLOB[0x08046538-0x08007000] == 16)
    verify('A held AUTO key publishes one long event at native count 70', hold_auto(v) == [(KEY, 16)])
    verify('The long event is queued while AUTO remains pressed',
           bytes(v.u.mem_read(0x20002d56, 2)) == b'\2\0'
           and v.u.mem_read(0x20002d59, 1) == b'\x46')
    # Queue delivery is a separate native task, exactly as on the instrument.
    v.drain_key()
    verify('Physical long AUTO reaches guarded initializer in STOP', word(v.u, G+4) == 1 and v.u.mem_read(S+0x30, 1) == b'\1')
    verify('Calibration enables both native channels even if CH2 was disabled', v.u.mem_read(S+0x14, 1) == b'\3')
    verify('Releasing a long hold cannot generate a short AUTO event', not scan(v, 0))
    verify('Decoder and initializer perform no flash writes', not v.flash_writes)

    v = RemoteVM().stopped()
    for _ in range(3):
        verify('Short AUTO is not published before release', not scan(v, 2))
    verify('Releasing a short AUTO publishes native event 2', scan(v, 0) == [(KEY, 2)])

    v = RemoteVM().stopped()
    v.byte(0xf68, 7)
    hold_auto(v)
    v.drain_key()
    verify('Long AUTO on USB Sharing cannot start calibration', word(v.u, G+4) == 0 and v.u.mem_read(S+0x30, 1) == b'\0')

    v = RemoteVM()
    v.byte(0xf68, 7)
    v.remote_command(26)
    v.queue.clear()
    hold_auto(v)
    v.drain_key()
    verify('After remote STOP leaves USB display, physical AUTO can start', word(v.u, G+4) == 1 and v.u.mem_read(S+0xf68, 1) == b'\2')
    report = dict(passed=True, hardwareTested=False, checkCount=len(checks),
                  patchedSHA256=hashlib.sha256(BLOB).hexdigest(), checks=checks,
                  boundaries='Native ARM key decoder and KEY task; GPIO matrix, RTOS scheduling and ADC are modeled.')
    (BUILD/'physical-auto-validation.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'{len(checks)} physical AUTO checks passed; no instrument accessed.')


if __name__ == '__main__':
    main()
