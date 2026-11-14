#!/usr/bin/env python3
"""Execute r6 USB/KEY hooks and stock control handlers as ARM, without hardware.

Native GPIO/DAC writes are observed, not replaced by Python setters. RTOS,
acquisition, LCD and USB endpoint completion remain modeled boundaries.
"""
from pathlib import Path
import sys, struct, json, hashlib, zlib
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_features import VM, S, G, BLOB, BUILD, SYM, word, p32, host
from inspect_runtime import *
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE

KEY, GUI, ACQ, SCOPE = 0x20006000, 0x20006020, 0x20006040, 0x20006060
TAIL = 172 + host.FRAME_BYTES
checks = []

def verify(name, ok, **details):
    assert ok, (name, details)
    checks.append(dict(name=name, passed=True, **details))

class RemoteVM(VM):
    def __init__(self):
        self.fail_key_queue = False
        self.executing = 'test'
        self.play_contexts = []
        self.idle_seen = []
        super().__init__()
        self.mmio = []
        for address, value in [(0x20002d70,KEY), (0x20002d6c,GUI),
                               (0x20002d78,ACQ), (0x20002d80,SCOPE)]:
            p32(self.u,address,value)
        p32(self.u,0x20002d68,0x20006080) # unrelated queue: must never receive KEY tokens
        self.u.hook_add(UC_HOOK_MEM_WRITE,
            lambda u,access,address,size,value,data:self.mmio.append((address,size,value)),
            begin=0x40000000,end=0x4002ffff)
    def hook(self,u,a,size,data):
        if a==SYM['native_second']&~1:self.idle_seen.append(u.mem_read(S+0xf65,1)[0])
        if a==0x08041cf0 and self.fail_key_queue and u.reg_read(UC_ARM_REG_R0)==KEY:
            self.ret(0)
        elif a==0x08009c78:self.ret() # battery/board boundary in one-second task
        else:
            if a==0x0800bd60:self.play_contexts.append(self.executing)
            super().hook(u,a,size,data)
    def remote_word(self,i):return word(self.u,self.a+TAIL+4*i)
    def key_fragment(self,key):
        u=self.u;sp=0x2006f000
        u.reg_write(UC_ARM_REG_SP,sp);u.mem_write(sp+7,bytes([key]))
        u.reg_write(UC_ARM_REG_R7,S)
        def stop(uc,a,size,data):
            if a in (0x08040038,0x08040044,0x08040060):uc.emu_stop()
        h=u.hook_add(UC_HOOK_CODE,stop);self.executing='KEY'
        try:u.emu_start(0x08040059,0x081ff000,count=60000000)
        finally:u.hook_del(h);self.executing='test'
        assert u.reg_read(UC_ARM_REG_PC) in (0x08040038,0x08040044,0x08040060)
        return u.reg_read(UC_ARM_REG_PC)
    def drain_key(self):
        """Run the ORIGINAL KEY task, including its real queue-handle load."""
        u=self.u;u.reg_write(UC_ARM_REG_SP,0x2006f008);self.key_received=[]
        def receive(uc,a,size,data):
            if a in (0x08040038,0x08040044) and self.key_received:uc.emu_stop();return
            if a!=0x080421d8:return
            queue=uc.reg_read(UC_ARM_REG_R0);pointer=uc.reg_read(UC_ARM_REG_R1)
            entry=next((i for i,(q,e) in enumerate(self.queue) if q==queue),None)
            assert entry is not None,('Native KEY task did not receive the USB token',queue,self.queue)
            q,event=self.queue.pop(entry);self.key_received.append((queue,event));uc.mem_write(pointer,bytes([event]));self.ret(1)
        h=u.hook_add(UC_HOOK_CODE,receive);self.executing='KEY'
        try:u.emu_start(0x08040009,0x081ff000,count=60000000)
        finally:u.hook_del(h);self.executing='test'
        assert u.reg_read(UC_ARM_REG_PC) in (0x08040038,0x08040044)
    def queue_command(self,op,arg=0):
        self.u.reg_write(UC_ARM_REG_IPSR,16)
        try:s=host.decode_status(self.setup(op,arg))
        finally:self.u.reg_write(UC_ARM_REG_IPSR,0)
        if s['command_result']:return s
        self.executing='GUI'
        try:self.invoke('r5_gui')
        finally:self.executing='test'
        return s
    def remote_command(self,op,arg=0):
        s=self.queue_command(op,arg)
        if not s['command_result'] and self.get('pending'):
            self.drain_key()
        return host.decode_status(self.setup())
    def settings(self):
        b=self.setup(request=0x5c)
        assert len(b)==64 and struct.unpack_from('<II',b)==(0x364c5443,0x00060001)
        return b
    def stopped(self):
        assert self.remote_command(26)['error']==0
        self.queue.clear();self.mmio.clear();return self

def main():
    v=RemoteVM();v.byte(0xf68,7);before=bytes(v.u.mem_read(S+0xf60,8))
    s=v.remote_command(26)
    verify('Remote STOP leaves the real USB Sharing display for scope without changing system/USB settings',
           not s['error'] and v.u.mem_read(S+0xf68,1)==b'\2' and not v.u.mem_read(S+0x2e,1)[0]
           and bytes(v.u.mem_read(S+0xf60,8))==before)
    verify('Settings readback remains available after leaving USB display',len(v.settings())==64)
    v=RemoteVM();before=bytes(v.u.mem_read(S,0x30));v.queue.clear()
    s=v.queue_command(26)
    verify('USB IRQ publishes one command; GUI forwards exactly one token to KEY',
           not s['command_result'] and v.get('pending')==26 and v.get('command_done')==0
           and sum(q==KEY for q,e in v.queue)==1 and not v.play_contexts)
    v.invoke('r5_gui')
    verify('Repeated GUI wakeups cannot duplicate a KEY token',sum(q==KEY for q,e in v.queue)==1)
    verify('Settings readback exposes queued state rather than claiming success',struct.unpack_from('<I',v.settings(),16)[0]==1 and bytes(v.u.mem_read(S,0x30))==before)
    v.drain_key()
    verify('Actual patched KEY prefix consumes token and acknowledges after native STOP',v.get('pending')==0 and v.get('command_done')==1 and v.u.mem_read(S+0x2e,1)==b'\0' and v.play_contexts==['KEY'])
    verify('Original KEY task reads the same queue to which USB tokens are sent',v.key_received==[(KEY,129)])
    verify('Completed settings packet has an even generation and the correct command ID',struct.unpack_from('<IIII',v.settings(),8)==(2,1,0,0))
    # Mid-function ABI: ordinary keys still replay both overwritten instructions.
    v=RemoteVM();u=v.u
    # Unicorn initializes the Cortex-M FP context on its first FP instruction;
    # execute VMRS once before injecting a nondefault live FPSCR for this test.
    u.mem_write(0x081fd000,bytes.fromhex('f1ee100a7047'));call(u,0x081fd000,[])
    regs=[UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_R4,UC_ARM_REG_R5,UC_ARM_REG_R6,
          UC_ARM_REG_R8,UC_ARM_REG_R9,UC_ARM_REG_R10,UC_ARM_REG_R11,UC_ARM_REG_R12,UC_ARM_REG_LR]
    expected=[0x12340001+n for n in range(len(regs))]
    for r,n in zip(regs,expected):u.reg_write(r,n)
    u.reg_write(UC_ARM_REG_FPSCR,0x01000000)
    floats=[UC_ARM_REG_S0,UC_ARM_REG_S1,UC_ARM_REG_S14,UC_ARM_REG_S15]
    for i,r in enumerate(floats):u.reg_write(r,0x3f000000+i)
    v.byte(0xe10,3);end=v.key_fragment(11)
    verify('Ordinary key hook replays native SAVE gate and retains SP/GPR/FPU state',
           end==0x08040060 and u.reg_read(UC_ARM_REG_R0)==3 and u.reg_read(UC_ARM_REG_R1)==2
           and u.reg_read(UC_ARM_REG_SP)==0x2006f000
           and [u.reg_read(r) for r in regs]==expected
           and [u.reg_read(r) for r in floats]==[0x3f000000+i for i in range(4)]
           and u.reg_read(UC_ARM_REG_FPSCR)==0x01000000)
    verify('Unknown high tokens never index the physical-key table',v.key_fragment(255)==0x08040044 and not v.play_contexts)
    v=RemoteVM().stopped();v.fail_key_queue=True;v.remote_command(17,6)
    verify('Full KEY queue reports BUSY and releases the mailbox',v.get('error')==1 and not v.get('pending') and v.u.mem_read(S+2,1)==b'\5')
    v=RemoteVM().stopped();v.queue_command(17,6);token=v.remote_word(3);v.advance(2001);v.invoke('r5_gui');v.key_fragment(token)
    verify('Expired queued command is never executed later',v.get('error')==3 and not v.get('pending') and v.u.mem_read(S+2,1)==b'\5')
    # Use native channel menus and range handlers, observing their hardware writes.
    v=RemoteVM().stopped()
    for op,arg,offset in [(16,3,0x14),(17,7,2),(18,3,3),(19,1,6),(20,1,7),
                          (21,1,0),(22,1,1),(23,10,4),(24,-10,5),(28,9,0x2d),
                          (30,1,0x16),(31,1,0x18)]:
        s=v.remote_command(op,arg)
        verify('Native control applies and reads back '+str((op,arg)),s['error']==0 and v.u.mem_read(S+offset,1)[0]==arg&255 and not v.u.mem_read(S+0x2e,1)[0])
    for mode in (0,2,1):
        s=v.remote_command(29,mode)
        verify('Trigger mode '+str(mode)+' finishes stopped despite native implicit RUN',not s['error'] and v.u.mem_read(S+0x17,1)[0]==mode and not v.u.mem_read(S+0x2e,1)[0])
    for level in (-100,0,100):
        s=v.remote_command(32,level)
        verify('Native signed trigger threshold '+str(level),not s['error'] and struct.unpack('<h',v.u.mem_read(S+0x1c,2))[0]==level)
    verify('Both coupling switches really use native PD12/PD13 GPIO writes',
           any(a in (0x40011410,0x40011414) and val&0x1000 for a,n,val in v.mmio)
           and any(a in (0x40011410,0x40011414) and val&0x2000 for a,n,val in v.mmio))
    verify('Both range/offset paths actually program DAC/PWM, beyond RAM readback',
           any(a==0x40007408 for a,n,val in v.mmio) and any(a==0x40001c34 for a,n,val in v.mmio))
    for mask in (2,1,3):
        s=v.remote_command(16,mask)
        verify('Channel transition never leaves an invalid trigger source: '+str(mask),not s['error'] and v.u.mem_read(S+0x14,1)[0]==mask and mask&(1<<v.u.mem_read(S+0x16,1)[0]))
    for op,arg in [(16,0),(16,4),(17,10),(18,10),(19,2),(20,2),(21,2),(22,2),
                   (23,101),(24,-101),(25,2),(26,1),(27,1),(28,20),(29,3),
                   (30,2),(31,2),(32,101),(33,1)]:
        v=RemoteVM().stopped();before=bytes(v.u.mem_read(S,0x30));v.remote_command(op,arg)
        verify('Malformed typed command has no input/trigger/acquisition effects '+str((op,arg)),v.get('error') in (2,8) and bytes(v.u.mem_read(S,0x30))==before)
    v=RemoteVM();s=v.remote_command(17,7)
    verify('Configuration while RUN is refused before mutation',s['error']==1 and v.u.mem_read(S+2,1)==b'\5')
    v=RemoteVM().stopped();v.byte(0x14,1);s=v.remote_command(30,1)
    verify('A disabled channel cannot become the trigger source',s['error']==2 and v.u.mem_read(S+0x16,1)==b'\0')
    for address in (G+4,G+24,S+0x2f,S+0x30,S+0x33):
        v=RemoteVM().stopped();v.u.mem_write(address,b'\1');s=v.remote_command(17,6)
        verify('Calibration/SPI interlock rejects control: '+hex(address),s['error']==5 and v.u.mem_read(S+2,1)==b'\5')
    v=RemoteVM().stopped();v.byte(0xf68,1);s=v.remote_command(17,6)
    verify('Scope control never reinterprets DMM state as scope settings',s['error']==9)
    for op in (15,37,65535):
        v=RemoteVM();verify('Internal/unknown opcode rejected at USB boundary '+str(op),host.decode_status(v.setup(op))['command_result']==2)
    # Live RUN/STOP and explicit freeze are different from a triggered SINGLE.
    v=RemoteVM().stopped();v.remote_command(25,0)
    verify('Native AUTO RUN starts without declaring a captured frame',v.u.mem_read(S+0x2e,1)==b'\1' and not v.get('frame_id') and not v.get('phase'))
    v.queue_command(27);v.key_fragment(v.remote_word(3));v.advance(49);v.invoke('r5_scope_service')
    verify('Preview freeze waits for settling and keeps mailbox outstanding',v.get('pending')==27 and not v.get('frame_id') and any(q==SCOPE for q,e in v.queue))
    v.advance(1);v.queue_busy=1;v.invoke('r5_scope_service')
    verify('Freeze cannot race pending native FPGA commands',v.get('pending')==27 and not v.get('frame_id'))
    v.queue_busy=0;p32(v.u,0x40010c0c,0);v.invoke('r5_scope_service')
    verify('Freeze also requires native SPI readiness GPIO',v.get('pending')==27 and not v.get('frame_id'))
    p32(v.u,0x40010c0c,0x40);v.u.mem_write(S+0xdb6,struct.pack('<H',301))
    p32(v.u,G+680,word(v.u,G+680)+1)
    v.invoke('r5_scope_service')
    s=host.decode_status(v.setup());f=bytes(v.u.mem_read(v.a+172,host.FRAME_BYTES))
    verify('Explicit preview publishes an immutable CRC frame without counting a SINGLE/SAVE',
           s['phase']=='ready' and s['frame_id']==1 and s['frame_crc32']==zlib.crc32(f)
           and not s['completed'] and not s['saved_number'] and v.settings()[63]>>4==1 and not v.get('pending'))
    v=RemoteVM().stopped();v.queue_command(27);v.key_fragment(v.remote_word(3));v.advance(3001);v.invoke('r5_scope_service')
    verify('Freeze timeout cannot become a false frame',v.get('error')==3 and not v.get('pending') and not v.get('frame_id'))
    v=RemoteVM().stopped();v.u.mem_write(S+0xdb6,b'\0\0');v.queue_command(27);v.key_fragment(v.remote_word(3));v.advance(60);v.invoke('r5_scope_service')
    verify('Freeze of an empty acquisition is refused',v.get('error')==7 and not v.get('frame_id'))
    v=RemoteVM().stopped();v.command(3,0);s=v.remote_command(33)
    verify('Remote SINGLE arm uses original KEY task, not recursive GUI calls',not s['error'] and v.get('phase')==1 and v.play_contexts[-1]=='KEY')
    v.advance(60);v.invoke('r5_scope_service');verify('Remote arm becomes ARMED only after native readiness checks',v.get('phase')==2)
    v.complete();s=host.decode_status(v.setup())
    verify('Real patched native SINGLE completion yields a new triggered frame',s['phase']=='ready' and s['frame_id']==1 and s['completed']==1 and v.settings()[63]>>4==0)
    verify('New protocol retains old capture status and frame CRC compatibility',host.decode_status(v.setup())['protocol']==0x00050001 and zlib.crc32(bytes(v.u.mem_read(v.a+172,host.FRAME_BYTES)))==s['frame_crc32'])
    verify('Settings requests with unknown page/length do not expose RAM',v.setup(1,0,0x5c)==b'' and len(v.setup(request=0x5c,length=8))==8)
    v=RemoteVM().stopped();v.byte(0xf65,3);v.setup(request=0x5c);v.invoke('guard_second')
    verify('USB keepalive suppresses only idle timeout while host polls',v.idle_seen[-1]==0 and v.u.mem_read(S+0xf65,1)==b'\3')
    v.advance(10000);v.invoke('guard_second')
    verify('USB keepalive expires without permanent power-setting changes',v.idle_seen[-1]==3 and v.u.mem_read(S+0xf65,1)==b'\3')
    v.queue_command(27);v.key_fragment(v.remote_word(3));v.queue.clear();v.invoke('guard_second')
    verify('One-second task wakes a stopped scope for pending freeze',any(q==SCOPE for q,e in v.queue))
    verify('Control and freeze tests never write MCU flash or its controller',not v.flash_writes)
    # The original task was only 128 words: the deepest native timebase path
    # plus the new hook reached that limit BEFORE an FP interrupt/context save.
    verify('Native KEY task creation reserves 240 words rather than the stock 128',BLOB[0x0802ccf2-0x08007000:0x0802ccf4-0x08007000]==b'\xf0\x22')
    peaks=[]
    for op,arg in [(23,-100),(24,100),(17,9),(28,0),(33,0)]:
        v=RemoteVM().stopped();v.command(3,0);v.queue_command(op,arg);minimum=[0x2006f008]
        h=v.u.hook_add(UC_HOOK_CODE,lambda u,a,n,d:minimum.__setitem__(0,min(minimum[0],u.reg_read(UC_ARM_REG_SP))))
        v.drain_key();v.u.hook_del(h);peak=0x2006f008-minimum[0];peaks.append(dict(operation=op,argument=arg,bytes=peak))
        verify('Native KEY stack keeps a 208-byte FP/RTOS context margin '+str((op,arg)),peak+208<=960,peakBytes=peak,reservedBytes=960)
    report=dict(passed=True,checkCount=len(checks),checks=checks,
                patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hardwareTested=False,
                keyStackBytes=960,keyStackPeaks=peaks,
                boundaries='Actual ARM USB/KEY hooks, stock control handlers and GPIO/DAC writes; RTOS, USB hardware, ADC and LCD modeled')
    (BUILD/'remote-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(passed=True,checks=len(checks))))

if __name__=='__main__':main()
