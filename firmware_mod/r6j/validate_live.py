#!/usr/bin/env python3
"""Exercise r6j live snapshots and guarded remote calibration as ARM code.

USB, native scheduling, analog and LCD are modelled: hardware testing remains
necessary. No test opens a device, writes flash, or saves calibration.
"""
import json, struct, zlib
from validate_remote import RemoteVM, G, S, p32, word, host
from validate_features import FIELDS
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_IPSR, UC_ARM_REG_PRIMASK

checks=[]
def verify(name, value):
    assert value, name
    checks.append(name)

def irq_command(v, op, arg=0):
    v.u.reg_write(UC_ARM_REG_IPSR,16)
    try: return host.decode_status(v.setup(op,arg))
    finally: v.u.reg_write(UC_ARM_REG_IPSR,0)

def main():
    v=RemoteVM().stopped()
    before=v.get('command_id')
    rejected=irq_command(v,34)
    verify('A live request while STOP is rejected without entering the mailbox',rejected['command_result']==7 and v.get('command_id')==before and not v.get('pending'))
    verify('Live capability is advertised without changing existing filter bits',v.settings()[63]&4 and not v.settings()[63]&3)

    v.remote_command(25,1)
    p32(v.u,G+680,word(v.u,G+680)+1)
    v.u.mem_write(S+0xdb6,struct.pack('<H',301))
    v.queue.clear()
    result=irq_command(v,34)
    verify('Live request is only queued by USB IRQ, with RUN unchanged',not result['command_result'] and v.get('pending')==34 and v.u.mem_read(S+0x2e,1)==b'\1' and not v.queue)
    v.invoke('r5_gui')
    verify('GUI does not dispatch the live request to the key task',v.get('pending')==34 and not v.queue)
    v.advance(1000);v.invoke('guard_second')
    verify('The watchdog does not flood the GUI queue for a live request',v.get('pending')==34 and not v.queue)
    expected=bytes(v.u.mem_read(S,host.FRAME_BYTES))
    critical=[0]
    events=[]
    def count_masked(uc,_address,_size,_data):
        if uc.reg_read(UC_ARM_REG_PRIMASK): critical[0]+=1
    hook=v.u.hook_add(UC_HOOK_CODE,count_masked)
    def record_publication(uc, access, address, size, value, data):
        for name in ('frame_ready','frame_id','command_done','pending'):
            if address==v.a+FIELDS.index(name)*4:events.append((name,value))
    memhook=v.u.hook_add(UC_HOOK_MEM_WRITE,record_publication,begin=v.a,end=v.a+108)
    v.invoke('r5_scope_service')
    v.u.hook_del(hook);v.u.hook_del(memhook)
    status=host.decode_status(v.setup())
    frame=bytes(v.u.mem_read(v.a+172,host.FRAME_BYTES))
    verify('Native scope task publishes the exact frozen frame with CRC, preserving RUN',status['phase']=='ready' and status['frame_id']==1 and status['frame_crc32']==zlib.crc32(frame) and frame==expected and v.u.mem_read(S+0x2e,1)==b'\1')
    verify('Publication acknowledges only after completion without a SINGLE count',not v.get('pending') and v.get('command_done')==v.get('command_id') and not status['completed'])
    verify('Buffer is readable before the USB command becomes acknowledged',events.index(('frame_ready',0))<next(i for i,x in enumerate(events) if x[0]=='command_done'))
    verify('IRQ-masked copy has a finite small instruction budget',0<critical[0]<100000)
    v.byte(0x356,0x33)
    downloaded=b''.join(v.setup(off,1,0x5b,min(64,host.FRAME_BYTES-off)) for off in range(0,host.FRAME_BYTES,64))
    verify('USB frame remains immutable after the live display changes',downloaded==frame and zlib.crc32(downloaded)==status['frame_crc32'])
    v.set('frame_ready',2)
    verify('USB refuses frame access during publication',v.setup(0,1,0x5b,64)==b'')
    v.set('frame_ready',0)
    verify('USB refuses a stale frame id',v.setup(0,2,0x5b,64)==b'')

    result=irq_command(v,34)
    verify('A second request uses the same RUN session',not result['command_result'] and v.get('pending')==34)
    v.invoke('r5_scope_service')
    newer=host.decode_status(v.setup())
    verify('Successive live snapshots advance id while RUN stays active',newer['frame_id']==2 and not v.get('pending') and v.u.mem_read(S+0x2e,1)==b'\1')
    verify('Live snapshots never write flash',not v.flash_writes)

    v=RemoteVM().stopped();v.remote_command(25,1)
    verify('A missing sample request can enter the mailbox',not irq_command(v,34)['command_result'])
    v.advance(3000)
    v.set('frame_ready',2);busy=host.decode_status(v.setup())
    verify('Timeout cannot cancel a snapshot already being published',v.get('pending')==34 and busy['command_done']!=busy['command_id'])
    v.set('frame_ready',0)
    expired=host.decode_status(v.setup())
    verify('No fresh sample expires without publishing or keeping the mailbox busy',expired['error']==7 and expired['command_done']==expired['command_id'] and not v.get('pending') and not expired['frame_id'])
    verify('The mailbox can be used after live timeout',not irq_command(v,34)['command_result'])

    v=RemoteVM().stopped();v.remote_command(25,1)
    p32(v.u,G+680,word(v.u,G+680)+1);v.u.mem_write(S+0xdb6,struct.pack('<H',301))
    p32(v.u,G+700,1) # spi_inflight
    irq_command(v,34);v.invoke('r5_scope_service')
    verify('Live snapshot waits while the SPI acquisition is in flight',v.get('pending')==34 and not v.get('frame_id'))
    p32(v.u,G+700,0);v.invoke('r5_scope_service')
    verify('Live snapshot proceeds after SPI completes',not v.get('pending') and v.get('frame_id')==1)

    v=RemoteVM().stopped();v.remote_command(25,1)
    p32(v.u,G+680,word(v.u,G+680)+1);v.u.mem_write(S+0xdb6,struct.pack('<H',301))
    irq_command(v,34);v.u.reg_write(UC_ARM_REG_PRIMASK,1);v.invoke('r5_scope_service')
    verify('Snapshot preserves an already masked interrupt state',v.u.reg_read(UC_ARM_REG_PRIMASK)==1 and v.get('frame_id')==1)

    v=RemoteVM().stopped()
    status=v.remote_command(35)
    cal=v.setup(request=0x60)
    fields=struct.unpack('<16I',cal)
    verify('Remote baseline start uses guarded calibration and exposes read-only status',not status['error'] and fields[0]==0x364c4143 and fields[1]==1 and fields[2]==1)
    status=v.remote_command(36)
    fields=struct.unpack('<16I',v.setup(request=0x60))
    verify('Remote cancellation uses guarded rollback and reports cancellation',not status['error'] and fields[2]==0 and fields[6]==1)
    verify('Calibration status refuses unsupported page and offset',v.setup(1,0,0x60)==b'' and v.setup(0,1,0x60)==b'')
    verify('Guarded calibration test never writes flash',not v.flash_writes)

    v=RemoteVM().stopped();v.remote_command(35)
    original=bytes(v.u.mem_read(S+0x260,240))
    v.u.mem_write(S+0x260,b'\x34\x12');p32(v.u,G+8,1)
    status=v.remote_command(36)
    verify('Cancellation during a coefficient step is deferred',not status['error'] and word(v.u,G+4)==1 and word(v.u,G+12)==1)
    v.invoke('guard_after_step')
    verify('Deferred cancellation restores the full prior coefficient table',word(v.u,G+4)==0 and bytes(v.u.mem_read(S+0x260,240))==original)
    print(json.dumps({'passed':True,'checks':len(checks),'masked_instructions':critical[0], 'hardwareTested':False}))

if __name__=='__main__':main()
