#!/usr/bin/env python3
"""Check USB actions stay silent while an ordinary physical key still beeps."""
import struct
from validate_remote import KEY, RemoteVM, G, S, host, p32, word
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP

v=RemoteVM();tones=[]
v.byte(0xf62,4);v.u.mem_write(S+0xf6c,b'\xb0\x04')
v.u.hook_add(UC_HOOK_MEM_WRITE,lambda u,a,b,c,value,d:tones.append(value),begin=0x40015434,end=0x40015434)
def consume():
    u=v.u;u.reg_write(UC_ARM_REG_SP,0x2006f008);received=[]
    def receive(uc,pc,size,data):
        if pc==0x08040048 and received:uc.emu_stop();return
        if pc!=0x080421d8:return
        assert uc.reg_read(UC_ARM_REG_R0)==KEY
        n=next(i for i,(q,e) in enumerate(v.queue) if q==KEY)
        event=v.queue.pop(n)[1];received.append(event)
        uc.mem_write(uc.reg_read(UC_ARM_REG_R1),bytes([event]));v.ret(1)
    h=u.hook_add(UC_HOOK_CODE,receive)
    try:u.emu_start(0x08040009,0x081ff000,count=30000000)
    finally:u.hook_del(h)
    assert len(received)==1 and u.reg_read(UC_ARM_REG_PC)==0x08040048
    return received[0]
def operation(op,arg=0):
    s=v.queue_command(op,arg);assert not s['command_result']
    assert consume()>=0x80
    s=host.decode_status(v.setup());assert s['command_id']==s['command_done'] and not s['error']

for op,arg in [(26,0),(16,3),(17,4),(21,1),(28,12),(31,1),(25,1)]:operation(op,arg)
p32(v.u,G+680,word(v.u,G+680)+1);v.u.mem_write(S+0xdb6,struct.pack('<H',301))
assert not host.decode_status(v.setup(34))['command_result'];v.invoke('r5_scope_service')
assert host.decode_status(v.setup())['phase']=='ready'
for op in (26,35,36):operation(op)
assert not tones,tones
v.byte(0x2e,0);v.queue.append((KEY,15));assert consume()==15
assert tones==[4],tones
assert not v.flash_writes
print('ARM validation passed: USB controls, live snapshots and calibration are silent; physical PLAY retains the beep.')
