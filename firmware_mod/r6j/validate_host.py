#!/usr/bin/env python3
"""Exercise host protocol/orchestration with synthetic USB and process boundaries.
No libusb initialization, attached instrument, firmware upload or IR stimulus.
"""
from pathlib import Path
from argparse import Namespace
from unittest.mock import patch
from tempfile import TemporaryDirectory
import sys,json,hashlib,zlib,struct
import autocapture as h

HERE=Path(__file__).resolve().parent;BUILD=HERE/'build';checks=[]
def verify(name,ok):
    assert ok,name
    checks.append(dict(name=name,passed=True))
def fixture():
    f=bytearray(h.FRAME_BYTES);f[0x14]=3;f[2]=5;f[3]=6;f[0x2d]=13
    for i in range(301):f[0x356+i]=128+i%20;f[0x356+301+i]=128-i%20
    for i in range(1024):f[0x5b0+i]=i%256;f[0x9b0+i]=255-i%256
    return bytes(f)
def status(phase='ready',frame_id=1):
    return dict(phase=phase,frame_id=frame_id,frame_crc32=zlib.crc32(fixture()),frame_bytes=h.FRAME_BYTES,saved_number=7,error=0)
def packed(done=0,command_id=0,error=0,rc=0):
    return struct.pack('<16I',h.MAGIC,h.VERSION,0,error|rc<<16,1,zlib.crc32(fixture()),0,1,command_id,done,0,0,h.FRAME_BYTES,0,0,50)
def refuses(name,action):
    try:action()
    except h.ScopeError:verify(name,True)
    else:verify(name,False)

class Wire(h.Scope):
    def __init__(self):self.calls=[];self.command_id=0;self.done=0;self.error=0;self.rc=0;self.frame_id=1;self.corrupt=False;self.short=False
    def transfer(self,request,value=0,index=0,length=64):
        self.calls.append((request,value,index,length))
        if request==0x5a:
            if value:self.command_id+=1
            else:self.done=self.command_id
            return packed(self.done,self.command_id,self.error,self.rc)
        assert request==0x5b and index==1 and 0<=value<h.FRAME_BYTES and length<=64
        b=fixture()[value:value+length]
        if self.corrupt and value==64:b=bytes([b[0]^1])+b[1:]
        return b[:-1] if self.short else b
    def status(self):
        if any(c[0]==0x5b for c in self.calls):return status(frame_id=self.frame_id)
        return h.decode_status(self.transfer(0x5a))

class Process:
    instances=[]
    def __init__(self,argv,**kwargs):
        self.argv=argv;self.stopped=False;self.waited=False
        self.log=kwargs['stdout'];self.log.write(b'synthetic stimulus\n');Process.instances.append(self)
        assert CURRENT.armed_seen,'Stimulus started before ARMED'
    def poll(self):return 0 if self.stopped else None
    def terminate(self):self.stopped=True
    def kill(self):self.stopped=True
    def wait(self,timeout=None):self.waited=True;return 0

class CaptureDevice:
    def __init__(self,failure=None):self.calls=[];self.frame_id=0;self.stage=0;self.armed_seen=False;self.failure=failure
    def command(self,op,arg=0):
        self.calls.append(('command',op,arg))
        if op==1:self.stage=1;self.armed_seen=False
        return status('arming',self.frame_id)
    def status(self):
        if self.stage==1:
            self.stage=2
            if self.failure=='early':return status('saving',self.frame_id+1)
            self.armed_seen=True;return status('armed',self.frame_id)
        if self.stage==2:
            if self.failure=='interrupt':raise KeyboardInterrupt()
            if self.failure=='timeout':return dict(status('error',self.frame_id),error=3)
            self.stage=0;self.frame_id+=1
        return status('ready' if self.frame_id else 'idle',self.frame_id)
    def frame(self,s):
        self.calls.append(('download',s['frame_id']))
        if self.failure=='download':raise h.ScopeError('synthetic CRC error')
        return fixture()

def main():
    global CURRENT
    refuses('Rejects a firmware with the wrong USB protocol',lambda:h.decode_status(bytes(64)))
    refuses('Rejects truncated USB status',lambda:h.decode_status(bytes(63)))
    w=Wire()
    with patch.object(h.time,'sleep'):
        s=w.command(7,25)
    verify('Commands wait for GUI acknowledgement and retain their exact argument',s['command_done']==s['command_id']==1 and w.calls[:2]==[(0x5a,7,25,64),(0x5a,0,0,64)])
    w=Wire();w.rc=1;refuses('A busy mailbox is never reported as an accepted command',lambda:w.command(4,2))
    w=Wire();w.error=2;refuses('An invalid device setting is reported to the host',lambda:w.command(4,0))
    w=Wire();f=w.frame(status())
    verify('Snapshot is downloaded in bounded 64-byte blocks',f==fixture() and sum(c[3] for c in w.calls if c[0]==0x5b)==h.FRAME_BYTES and max(c[3] for c in w.calls if c[0]==0x5b)==64)
    w=Wire();w.corrupt=True;refuses('A single corrupt snapshot byte fails CRC verification',lambda:w.frame(status()))
    w=Wire();w.frame_id=2;refuses('Changed frame identifier invalidates a download',lambda:w.frame(status()))
    w=Wire();w.short=True;refuses('Short snapshot transfers are refused',lambda:w.frame(status()))
    with TemporaryDirectory(prefix='2c53t-host-check-') as tmp:
        args=Namespace(output=str(Path(tmp)/'ok'),mode='save',count=2,timeout=1,gap_ms=0,pretrigger=25,channel=1,edge='falling',level=-5,timebase_index=13,stimulus_command='python3 "argument with spaces.py"')
        CURRENT=CaptureDevice();Process.instances=[]
        with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
            report=h.capture(CURRENT,args,printer=lambda *a,**k:None)
        out=Path(args.output)
        verify('Arm -> ARMED -> stimulus -> complete -> download runs twice',report['status']=='complete' and len(report['frames'])==2 and [c[1] for c in CURRENT.calls if c[0]=='download']==[1,2] and len(Process.instances)==2)
        verify('Stimulus argv is parsed without shell interpretation',all(p.argv==['python3','argument with spaces.py'] for p in Process.instances))
        verify('Every stimulus is stopped and its parent log descriptor is closed',all(p.stopped and p.waited and p.log.closed for p in Process.instances))
        verify('Each capture writes BIN, three CSVs and the frozen state',all((out/(f'{n:03d}'+s)).exists() for n in (1,2) for s in ('.bin','.csv','-raw.csv','-meta.csv','-state.bin')))
        verify('Report never claims a physical storage test or a firmware flash',not report['physicalStorageVerified'] and not report['hardwareFlashed'])
        before=list(CURRENT.calls)
        try:h.capture(CURRENT,args)
        except FileExistsError:pass
        else:raise AssertionError('Existing output directory was overwritten')
        verify('Existing output directory is refused before any device command',before==CURRENT.calls)
        for failure in ('early','timeout','download','interrupt'):
            args.output=str(Path(tmp)/failure);args.count=1;CURRENT=CaptureDevice(failure);Process.instances=[]
            try:
                with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
                    h.capture(CURRENT,args,printer=lambda *a,**k:None)
            except (h.ScopeError,KeyboardInterrupt):pass
            else:raise AssertionError('Expected failure '+failure)
            r=json.loads((Path(args.output)/'report.json').read_text())
            verify('Failed capture is cancelled and reported: '+failure,r['status'] in ('error','interrupted') and CURRENT.calls[-1][:2]==('command',2) and not r['frames'])
            verify('Failed capture leaves no false successful data files: '+failure,not list(Path(args.output).glob('*.csv')))
            if failure=='early':verify('An early trigger never launches the stimulus',not Process.instances)
            else:verify('Failure stops any running stimulus: '+failure,all(p.stopped and p.waited and p.log.closed for p in Process.instances))
    report=dict(passed=True,checkCount=len(checks),checks=checks,hostSHA256=hashlib.sha256((HERE/'autocapture.py').read_bytes()).hexdigest(),patchedSHA256=json.loads((BUILD/'manifest.json').read_text())['patchedSHA256'],hardwareTested=False,boundaries='USB responses and child processes are synthetic; host orchestration and CRC run unmodified')
    (BUILD/'host-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(passed=True,checks=len(checks)),indent=2))
if __name__=='__main__':main()
