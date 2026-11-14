#!/usr/bin/env python3
"""Native USB-core routing and native filename/SAVE integration, all offline.

Actual ARM routing, album scan, BIN/CSV serialization and r5 SAVE callback
execute. RTOS queues, the ADC, BMP image capture, USB endpoint hardware and
FatFs calls are modeled boundaries. This never accesses the instrument.
"""
from pathlib import Path
import json,struct,hashlib
from unicorn import UC_HOOK_CODE
from validate_features import VM,S,G,SYM,BLOB,BUILD,word,p32,FIELDS,host
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from validate_csv import Machine
from inspect_runtime import *

checks=[]
def verify(name,ok,**details):
    assert ok,(name,details)
    checks.append(dict(name=name,passed=True,**details))

def fragment(u,start,ends,count=3000000):
    def stop(uc,pc,size,data):
        if pc in ends:uc.emu_stop()
    h=u.hook_add(UC_HOOK_CODE,stop)
    u.emu_start(start|1,0x081ff000,count=count);u.hook_del(h)
    assert u.reg_read(UC_ARM_REG_PC) in ends,hex(u.reg_read(UC_ARM_REG_PC))

def usb_core():
    v=VM().arm().complete();u=v.u;ctx=v.ctx;ops=0x20045400
    p32(u,ctx+4,ops);p32(u,ops+8,0x08031079)
    u.mem_write(ctx+0x14,struct.pack('<H',64));u.mem_write(ctx+0x224,b'\3')
    def setup(request,value=0,index=0,n=64):
        v.packets=[];u.mem_write(ctx+0x214,struct.pack('<BBHHH',0xc0,request,value,index,n))
        u.reg_write(UC_ARM_REG_R10,ctx);u.reg_write(UC_ARM_REG_R9,0x40005c00)
        fragment(u,0x0802ebf2,{0x0802f200})
        assert len(v.packets)==1
        return v.packets[-1][1]
    packet=setup(0x5a)
    verify('Real stock USB core routes vendor device IN to patched class Setup',host.decode_status(packet)['phase']=='ready')
    verify('EP0 data lengths and state are initialized for native IN completion',u.mem_read(ctx+0x26,4)==struct.pack('<HH',64,64) and u.mem_read(ctx+0x220,1)==b'\2')
    # The real endpoint hardware sets the actual completed packet length.
    u.mem_write(ctx+0x24,struct.pack('<H',64))
    u.reg_write(UC_ARM_REG_R10,ctx);u.reg_write(UC_ARM_REG_R8,ctx);u.reg_write(UC_ARM_REG_R9,0x40005c00)
    fragment(u,0x0802ec80,{0x0802eb28})
    verify('Real stock EP0 completion proceeds to status OUT without extra data',u.mem_read(ctx+0x220,1)==b'\5' and len(v.packets)==1)
    packet=setup(0x5b,0,1,64)
    verify('Snapshot transfer also traverses stock setup parser',packet==bytes(u.mem_read(v.a+172,64)))
    verify('Native setup parser retains the exact requested length',u.mem_read(ctx+0x222,2)==struct.pack('<H',64))
    # Inject an interrupt while GUI executes the native arm action. The
    # outstanding mailbox must remain busy until its completion is published.
    v=VM();u=v.u;events=[]
    def irq(uc,pc,size,data):
        if pc==0x0800bd60:
            # The USB request handler only enqueues: executing it nested would
            # clobber the ARM caller registers, so inspect the published mailbox.
            events.append((v.get('pending'),v.get('command_id'),v.get('command_done')))
    u.hook_add(UC_HOOK_CODE,irq)
    v.command(1)
    verify('Mailbox stays busy throughout the real native arm action',len(events)==1 and events[0][0]==1 and events[0][1]>events[0][2] and v.get('pending')==0 and v.get('command_done')==v.get('command_id'))

class SaveMachine(Machine):
    def __init__(self):
        super().__init__();self.u.mem_map(0x40000000,0x30000)
        self.u.reg_write(UC_ARM_REG_C1_C0_2,0xf00000);self.u.reg_write(UC_ARM_REG_FPEXC,0x40000000)
        self.a=0x20032bc0;p32(self.u,G+672,self.a);call(self.u,SYM['r5_defaults'],[self.a])
        self.directory=[];self.queue=[];self.u.mem_write(S+0xf60,b'\3');self.u.mem_write(S+0xf68,b'\2')
    def set(self,k,v):p32(self.u,self.a+FIELDS.index(k)*4,v)
    def get(self,k):return word(self.u,self.a+FIELDS.index(k)*4)
    def hook(self,u,a,size,data):
        r=[u.reg_read(x) for x in [UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3]]
        if a==0x08034c40:
            folder=self.text(r[1]);self.directory=[p.rsplit('/',1)[1] for p in self.fs if p.startswith(folder+'/')];self.ret()
        elif a==0x08034f20:
            u.mem_write(r[1],bytes(0x118))
            if self.directory:u.mem_write(r[1]+0x16,self.directory.pop(0).encode()+b'\0')
            self.ret()
        elif a==0x080341c4:self.ret()
        elif a==0x08035a08:self.ret(0 if self.text(0x20008360) in self.fs else 4)
        elif a==0x08041cf0:
            self.queue.append(u.mem_read(r[1],1)[0]);self.ret(1)
        else:super().hook(u,a,size,data)
    def prepare(self):
        self.u.reg_write(UC_ARM_REG_R9,S)
        fragment(self.u,0x0801c7c4,{0x0801c7f0},count=10000000)
        self.u.mem_write(S+0xe10,b'\xff');self.u.mem_write(S+0xf68,b'\x0a')
        return self.u.mem_read(S+0xe11,1)[0]

def save_series():
    m=SaveMachine()
    m.fs['2:/Screenshot simple file/9.bin']=bytearray(b'previous BIN')
    m.fs['2:/Screenshot file/9.bmp']=bytearray(b'previous BMP')
    m.fs['2:/Screenshot simple file/9.csv']=bytearray(b'legacy CSV')
    m.fs['2:/Screenshot simple file/9-raw.csv']=bytearray(b'legacy CSV')
    m.set('mode',2);m.set('count',3);m.set('gap_ms',500)
    ids=[]
    for index in range(3):
        # Acquisition completion is covered in validate_features. Here the
        # actual GUI/SAVE adapter must choose a new filename on every cycle.
        m.set('phase',3);m.set('completed',index+1);m.set('frame_ready',0)
        call(m.u,SYM['r5_gui'],[])
        verify('Auto-SAVE uses original preparation request '+str(index+1),m.flags()==1 and m.queue[-1]==3)
        ids.append(m.prepare());m.save()
        verify('Real SAVE callback advances the capture state '+str(index+1),m.get('phase')==(5 if index<2 else 4) and m.get('last_file')==ids[-1])
    verify('Native scan assigns three distinct filenames after an existing album',ids==[10,11,12])
    verify('Existing album entries and legacy files survive the series',m.fs['2:/Screenshot simple file/9.bin']==b'previous BIN' and m.fs['2:/Screenshot simple file/9.csv']==b'legacy CSV')
    verify('All three captures contain their own BMP/BIN/three CSV set',all(
        f'2:/Screenshot file/{n}.bmp' in m.fs and f'2:/Screenshot simple file/{n}.bin' in m.fs and
        all(f'2:/Waveform CSV/{n}{s}' in m.fs for s in ('.csv','-raw.csv','-meta.csv')) for n in ids))
    verify('Native album contains each new number exactly once',m.u.mem_read(S+0xe1b,1)==b'\4' and m.u.mem_read(S+0xe25,4)==bytes([9,10,11,12]))
    verify('All series filesystem handles and allocations are released',not m.handles and not m.allocations)
    # Host exporter uses the existing format and field names; calibration
    # history unavailable to the snapshot is explicitly unknown, never success.
    state=bytes(m.u.mem_read(S,host.FRAME_BYTES));payload=host.exports(state)
    import csv,io
    rows=lambda b:list(csv.DictReader(io.StringIO(b.decode())))
    screen_device=rows(m.fs['2:/Waveform CSV/12.csv']);screen_host=rows(payload['.csv'])
    verify('USB-downloaded BIN matches the native serializer byte for byte',payload['.bin']==m.fs['2:/Screenshot simple file/12.bin'])
    verify('Host and firmware screen CSV have identical columns and values',all(a.keys()==b.keys() and all(a[k]==b[k] or (a[k] and b[k] and abs(float(a[k])-float(b[k]))<1e-12) for k in a) for a,b in zip(screen_device,screen_host)) and len(screen_host)==len(screen_device)==301)
    verify('Host and firmware raw CSV are identical',payload['-raw.csv']==m.fs['2:/Waveform CSV/12-raw.csv'])
    md={r['key']:r['value'] for r in rows(m.fs['2:/Waveform CSV/12-meta.csv'])};mh={r['key']:r['value'] for r in rows(payload['-meta.csv'])}
    verify('Host metadata retains the schema and does not invent calibration history',md.keys()==mh.keys() and all(md[k]==mh[k] for k in md if k not in ('calibration_required','last_calibration_error')) and mh['calibration_required']==mh['last_calibration_error']=='unknown')

def main():
    usb_core();save_series()
    (BUILD/'integration-validation.json').write_text(json.dumps(dict(passed=True,checkCount=len(checks),checks=checks,patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hardwareTested=False,boundaries='ADC, RTOS, USB hardware, BMP capture and FatFs are simulated; native ARM routing/scan/BIN/CSV execute'),ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(passed=True,checks=len(checks)),indent=2))
if __name__=='__main__':main()
