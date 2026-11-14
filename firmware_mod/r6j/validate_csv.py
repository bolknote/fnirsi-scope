#!/usr/bin/env python3
"""Run the linked ARM SAVE/CSV code against an in-memory filesystem boundary.

The real patched SAVE entry, CSV routines and stock BIN serializer execute.
Only heap, filesystem, existing BMP capture and filename formatting are mocked.
No USB, physical flash, ADC or device is accessed.
"""
from pathlib import Path
import csv, io, json, hashlib, struct
from decimal import Decimal
from unicorn import UC_HOOK_CODE
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inspect_runtime import *
BUILD=Path(__file__).resolve().parent/"build"

PAT=BUILD/'APP_2C53T_V1.2.0_RU_REMOTE_r6j.bin'
BLOB=PAT.read_bytes()
MANIFEST=json.loads((BUILD/'manifest.json').read_text())
checks=[]

def verify(name,ok,**details):
    assert ok,(name,details)
    checks.append({'name':name,'passed':True,**details})

def fixture(flags=3,timebase=13):
    s=bytearray(0xe10)
    s[2]=5;s[3]=6;s[4]=10;s[5]=246;s[6]=0;s[7]=1
    s[0x14]=flags;s[0x2d]=timebase
    s[0x17]=2;s[0x18]=1;s[0x16]=0
    struct.pack_into('<hh',s,0x1a,-12,34)
    struct.pack_into('<HH',s,0xdb4,128,301)
    s[0xdbe]=3;s[0xdbf]=2;s[0xdc0]=timebase
    s[0xdc1]=5;s[0xdc2]=6;s[0xdc3]=10;s[0xdc4]=246
    for i in range(301):
        s[0x356+i]=138+(i%11)-5
        s[0x356+301+i]=118+(i%17)-8
    for i in range(1024):
        s[0x5b0+i]=(i*3+7)&255
        s[0x9b0+i]=(255-i*5)&255
    return s

class Machine:
    def __init__(self,state=None,fail=None,n=7):
        self.u=runtime(BLOB);self.u.mem_map(0x21000000,0x100000)
        self.u.mem_write(0x20001080,b"\1")
        self.u.mem_write(0x20033bc0,struct.pack("<I",0x33474e46))
        self.state=bytes(fixture() if state is None else state)
        self.u.mem_write(0x200000f8,self.state)
        self.u.mem_write(0x20000f08,b'\1')
        self.u.mem_write(0x20000f09,bytes([n]))
        self.u.mem_write(0x20000f13,b'\2')
        self.u.mem_write(0x20001060,b'\5')
        self.heap=0x21000000;self.allocations={};self.handles={};self.fs={}
        self.events=[];self.fail=fail;self.alloc_count=0
        self.u.hook_add(UC_HOOK_CODE,self.hook)
    def text(self,p):return bytes(self.u.mem_read(p,256)).split(b'\0')[0].decode()
    def ret(self,value=0):
        self.u.reg_write(UC_ARM_REG_R0,value)
        self.u.reg_write(UC_ARM_REG_PC,self.u.reg_read(UC_ARM_REG_LR))
    def hook(self,u,a,size,unused):
        r=[u.reg_read(x) for x in [UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3]]
        if a==0x0803acfc:
            self.alloc_count+=1
            if self.fail=='alloc-csv' and self.alloc_count==2:self.ret(0);return
            p=self.heap;self.heap+=(r[0]+31)&~31
            assert self.heap<0x21100000
            self.allocations[p]=r[0];self.events.append(('allocate',p,r[0]));self.ret(p)
        elif a==0x08034534:
            self.events.append(("mkdir",self.text(r[0])));self.ret()
        elif a==0x0803acbc:
            assert r[0] in self.allocations,('invalid free',hex(r[0]))
            del self.allocations[r[0]];self.events.append(('free',r[0]));self.ret()
        elif a in (0x08007370,0x080073b4):
            value=self.text(r[1]).replace('%d',str(r[2])).encode()+b'\0'
            u.mem_write(r[0],value);self.ret(len(value)-1)
        elif a==0x0803d084:
            path=self.text(0x20008360);self.events.append(('bmp',path,self.flags()))
            self.fs[path]=bytearray(b'BM:existing-stock-capture')
            self.ret(1 if self.fail=='bmp' else 0)
        elif a==0x080348b8:
            path=self.text(r[1]);self.events.append(('open',path,r[2]))
            if self.fail=='open-raw' and path.endswith('-raw.csv'):self.ret(7);return
            if self.fail=='open-screen' and path.endswith('/7.csv'):self.ret(7);return
            if self.fail=='open-meta' and path.endswith('-meta.csv'):self.ret(7);return
            self.handles[r[0]]={'path':path,'position':0}
            self.fs[path]=bytearray();u.mem_write(r[0],bytes(0x102c));self.ret()
        elif a==0x08035530:
            h=self.handles[r[0]];path=h['path'];count=r[2];code=0
            if self.fail=='short-screen' and path.endswith('/7.csv'):count//=2
            if self.fail=='write-raw' and path.endswith('-raw.csv'):count=0;code=1
            if self.fail=='write-bin' and path.endswith('.bin'):count=0;code=1
            if self.fail=='write-meta' and path.endswith('-meta.csv'):count=0;code=1
            data=bytes(u.mem_read(r[1],count));pos=h['position'];f=self.fs[path]
            if len(f)<pos+count:f.extend(bytes(pos+count-len(f)))
            f[pos:pos+count]=data;h['position']+=count
            u.mem_write(r[0]+12,struct.pack('<I',h['position']))
            u.mem_write(r[3],struct.pack('<I',count))
            self.events.append(('write',path,r[2],count));self.ret(code)
        elif a==0x080341e8:
            self.handles[r[0]]['position']=r[1];self.events.append(('seek',r[1]));self.ret()
        elif a==0x08034014:
            h=self.handles.pop(r[0]);self.events.append(('close',h['path']));self.ret()
        elif a==0x0803512c:
            path=self.text(0x20008360);self.fs.pop(path,None)
            self.events.append(('unlink',path));self.ret()
    def save(self):
        call(self.u,0x08011660,[],count=12000000)
        assert not self.handles,'File not closed'
        assert not self.allocations,'Heap allocation leaked'
        return self
    def rows(self,name):
        p='2:/Waveform CSV/'+name
        return list(csv.DictReader(io.StringIO(self.fs[p].decode())))
    def flags(self):return bytes(self.u.mem_read(0x20000f08,1))[0]

def main():
    m=Machine().save()
    verify('Patched SAVE entry creates BMP/BIN and three CSV companions',set(m.fs)=={
        '2:/Screenshot file/7.bmp','2:/Screenshot simple file/7.bin',
        '2:/Waveform CSV/7.csv','2:/Waveform CSV/7-raw.csv',
        '2:/Waveform CSV/7-meta.csv'})
    verify('Existing BIN payload is retained byte-for-byte',
           m.fs['2:/Screenshot simple file/7.bin'][:602]==m.state[0x356:0x5b0] and
           m.fs['2:/Screenshot simple file/7.bin'][602:]==bytes.fromhex('030080012d'))
    verify('CSV uses its own root folder',('mkdir','2:/Waveform CSV') in m.events and not any(p.startswith('2:/Screenshot simple file/') and p.endswith('.csv') for p in m.fs))
    rows=m.rows('7.csv');raw=m.rows('7-raw.csv');meta={r['key']:r['value'] for r in m.rows('7-meta.csv')}
    verify('Screen CSV has 301 points and explicit units',len(rows)==301 and
           list(rows[0])==['index','time_s','ch1_v','ch2_v','ch1_screen_code','ch2_screen_code'])
    verify('Time scale is 200 us/div divided by 25',all(Decimal(r['time_s'])==Decimal(i)*Decimal('0.000008') for i,r in enumerate(rows)))
    verify('CH1 voltage includes its nonzero vertical position',all(Decimal(r['ch1_v'])==Decimal((i%11)-5)*Decimal('0.02') for i,r in enumerate(rows)))
    verify('CH2 voltage includes X10 probe and negative vertical position',all(Decimal(r['ch2_v'])==Decimal((i%17)-8)*Decimal('0.4') for i,r in enumerate(rows)))
    verify('Both complete 1024-byte acquisition banks are preserved',len(raw)==1024 and
           all(int(r['bank_index'])==i and int(r['bank0_code'])==(i*3+7)&255 and
               int(r['bank1_code'])==(255-i*5)&255 for i,r in enumerate(raw)))
    verify('Metadata identifies scale source, bank layout and unverified raw timing',
           meta['timebase_label']=='200uS' and meta['ch1_scale_label']=='500mV' and
           meta['ch2_scale_label']=='10V' and meta['raw_timing_verified']=='0' and
           meta['raw_bank_validity']=='not_inferred')
    verify('Numeric buffers and capture parameters remain unchanged',bytes(m.u.mem_read(0x200000f8,0xe10))==m.state)
    verify('Original album insertion and return to scope are retained',m.flags()==2 and
           m.u.mem_read(0x20000f13,1)==b'\3' and m.u.mem_read(0x20000f1f,1)==b'\7' and
           m.u.mem_read(0x20001060,1)==b'\2')
    verify('CSV writes use at most 512 bytes per filesystem operation',all(e[2]<=512 for e in m.events if e[0]=='write' and e[1].endswith('.csv')))
    verify('SAVE retains the original pending status during BMP capture',next(e for e in m.events if e[0]=='bmp')[2]==1)
    for suffix in ['7.csv','7-raw.csv','7-meta.csv']:
        content=m.fs['2:/Waveform CSV/'+suffix]
        verify('CSV is ASCII with CRLF and final newline: '+suffix,content.isascii() and content.endswith(b'\r\n') and b'\n' not in content.replace(b'\r\n',b''))
        (BUILD/('example-'+suffix)).write_bytes(content)

    for flags in [1,2]:
        x=Machine(fixture(flags=flags)).save();rs=x.rows('7.csv')
        off=2 if flags==1 else 1
        verify('Disabled channel is blank, not a stale waveform: flags='+str(flags),
               all(r[f'ch{off}_v']=='' and r[f'ch{off}_screen_code']=='' for r in rs))
    for idx,dt in [(0,'0.0000000004'),(28,'0.8')]:
        x=Machine(fixture(timebase=idx)).save()
        verify('Timebase extreme index '+str(idx),Decimal(x.rows('7.csv')[1]['time_s'])==Decimal(dt))
    s=fixture();s[2]=9;s[6]=0;x=Machine(s).save()
    verify('10 V/div frontend range uses the stock UI scale table',Decimal(x.rows('7.csv')[0]['ch1_v'])==Decimal('-2.0'))
    for fail in ['open-screen','open-raw','open-meta','short-screen','write-raw','write-meta','alloc-csv']:
        x=Machine(fail=fail)
        # Old files must not survive a failed overwrite under the same number.
        for suffix in ['7.csv','7-raw.csv','7-meta.csv']:x.fs['2:/Waveform CSV/'+suffix]=bytearray(b'old')
        x.save()
        verify('CSV failure preserves valid BMP/BIN and reports failure: '+fail,x.flags()==3 and
               '2:/Screenshot file/7.bmp' in x.fs and '2:/Screenshot simple file/7.bin' in x.fs)
        verify('Partial/old CSV set is removed after failure: '+fail,not any(p.endswith('.csv') for p in x.fs))
        stage=struct.unpack('<I',x.u.mem_read(0x20033bc0+17*4,4))[0]
        code=struct.unpack('<I',x.u.mem_read(0x20033bc0+18*4,4))[0]
        partial=struct.unpack('<I',x.u.mem_read(0x20033bc0+20*4,4))[0]
        expected=(4,0) if fail=='alloc-csv' else (6,7) if fail.startswith('open-') else (7,256) if fail=='short-screen' else (7,1)
        verify('SAVE identifies exact CSV stage/code and partial success: '+fail,(stage,code)==expected and partial==1)

    for fail in ['bmp','write-bin']:
        x=Machine(fail=fail).save()
        verify('Failure exits without false success or unclosed files: '+fail,x.flags()==3 and not any(p.endswith('.csv') for p in x.fs))
    x=Machine(n=255).save()
    verify('Full/invalid file index does not start a save',not x.fs and x.flags()==3)
    # Test snapshot interrupt-state restoration in both entry states.
    for primask in [0,1]:
        x=Machine();x.u.reg_write(UC_ARM_REG_PRIMASK,primask)
        call(x.u,int(MANIFEST['symbols']['export_csv'],16),[7],count=12000000)
        verify('Atomic snapshot restores PRIMASK='+str(primask),x.u.reg_read(UC_ARM_REG_PRIMASK)==primask)
        assert not x.allocations and not x.handles
    report={'passed':True,'checkCount':len(checks),'checks':checks,
            'patchedSHA256':hashlib.sha256(BLOB).hexdigest(),'hardwareTested':False,
            'filesystemBoundary':'in-memory mocks; real FAT/SPI flash not executed',
            'rawTimingVerified':False}
    (BUILD/'csv-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':True,'checks':len(checks),'exampleCSVBytes':len(m.fs['2:/Waveform CSV/7.csv'])},indent=2))

if __name__=='__main__':main()
