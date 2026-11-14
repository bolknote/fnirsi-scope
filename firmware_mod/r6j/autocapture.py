#!/usr/bin/env python3
"""FNIRSI 2C53T r5: arm -> wait ready -> run stimulus -> read frozen frame.

Uses libusb control transfers only; never claims/detaches the mass-storage
interface, flashes firmware, resets USB, or sends arbitrary memory addresses.
USB Sharing must already be enabled on the instrument. macOS/Linux require
libusb-1.0 (the CasioWQV adapter tools already use it).
"""
import argparse
import csv
import ctypes as C
import ctypes.util
from datetime import datetime,timezone
import hashlib
import io
import json
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import time
import zlib

MAGIC=0x35465043
VERSION=0x00050001
FRAME_BYTES=0xe10
PHASES=('idle','arming','armed','saving','ready','gap','error','cancelled')
ERRORS=('ok','busy','invalid setting','trigger timeout','save failed/full album',
        'calibration/SPI busy or failed','no reserved memory','no frozen frame',
        'unsupported timebase','not in oscilloscope mode')
TIMEBASE=('10nS','20nS','50nS','100nS','200nS','500nS','1uS','2uS','5uS','10uS',
          '20uS','50uS','100uS','200uS','500uS','1mS','2mS','5mS','10mS','20mS',
          '50mS','100mS','200mS','500mS','1S','2S','5S','10S','20S')
SCALES=('10mV','20mV','50mV','100mV','200mV','500mV','1V','2V','5V','10V','20V','50V','100V')

class ScopeError(RuntimeError):pass

def decode_status(data):
    if len(data)!=64:raise ScopeError('Expected a 64-byte r5 status response; install the r5 firmware first')
    v=struct.unpack('<16I',data)
    if v[:2]!=(MAGIC,VERSION):raise ScopeError('USB device is not running the r5 capture-control protocol')
    if v[2]>=len(PHASES):raise ScopeError('Invalid capture phase')
    keys=('magic','protocol','phase_code','error_word','frame_id','frame_crc32','completed','target',
          'command_id','command_done','mode','saved_number','frame_bytes','device_tick','filter_mask','pretrigger_percent')
    s=dict(zip(keys,v));s['phase']=PHASES[v[2]];s['error']=v[3]&65535;s['command_result']=v[3]>>16
    return s

class Scope:
    def __init__(self,vid=0x2e3c,pid=0x5720,library=None):
        path=library or ctypes.util.find_library('usb-1.0')
        if not path and sys.platform=='darwin':path='/opt/homebrew/lib/libusb-1.0.dylib'
        if not path:raise ScopeError('libusb-1.0 was not found')
        self.usb=C.CDLL(path);self.ctx=C.c_void_p();self.handle=C.c_void_p()
        signatures={
            'init':([C.POINTER(C.c_void_p)],C.c_int),
            'exit':([C.c_void_p],None),
            'open_device_with_vid_pid':([C.c_void_p,C.c_uint16,C.c_uint16],C.c_void_p),
            'close':([C.c_void_p],None),
            'control_transfer':([C.c_void_p,C.c_uint8,C.c_uint8,C.c_uint16,C.c_uint16,
                                 C.POINTER(C.c_ubyte),C.c_uint16,C.c_uint],C.c_int),
            'error_name':([C.c_int],C.c_char_p),
        }
        for name,(args,result) in signatures.items():
            f=getattr(self.usb,'libusb_'+name);f.argtypes=args;f.restype=result
        rc=self.usb.libusb_init(C.byref(self.ctx))
        if rc<0:raise ScopeError('libusb initialization failed')
        self.handle=C.c_void_p(self.usb.libusb_open_device_with_vid_pid(self.ctx,vid,pid))
        if not self.handle:
            self.close();raise ScopeError('2C53T USB device was not found/opened. Enable USB Sharing and connect a data cable.')
    def close(self):
        if self.handle:self.usb.libusb_close(self.handle);self.handle=C.c_void_p()
        if self.ctx:self.usb.libusb_exit(self.ctx);self.ctx=C.c_void_p()
    def transfer(self,request,value=0,index=0,length=64):
        data=(C.c_ubyte*length)()
        n=self.usb.libusb_control_transfer(self.handle,0xc0,request,value,index,data,length,2000)
        if n<0:raise ScopeError('USB control: '+self.usb.libusb_error_name(n).decode()+'. Check r5 firmware and USB Sharing.')
        return bytes(data[:n])
    def status(self):return decode_status(self.transfer(0x5a))
    def command(self,operation,argument=0,timeout=5):
        s=decode_status(self.transfer(0x5a,operation,argument&65535))
        if s['command_result']:
            n=s['command_result'];raise ScopeError(ERRORS[n] if n<len(ERRORS) else 'Command rejected: '+str(n))
        expected=s['command_id'];deadline=time.monotonic()+timeout
        while s['command_done']!=expected:
            if time.monotonic()>=deadline:raise ScopeError('Instrument GUI did not acknowledge the queued command')
            time.sleep(.05);s=self.status()
        if s['error']:
            n=s['error'];raise ScopeError(ERRORS[n] if n<len(ERRORS) else 'Instrument error '+str(n))
        return s
    def frame(self,status):
        if status['frame_bytes']!=FRAME_BYTES or not status['frame_id']:raise ScopeError('No compatible frozen frame')
        frame=bytearray();frame_id=status['frame_id']
        for offset in range(0,FRAME_BYTES,64):
            n=min(64,FRAME_BYTES-offset)
            block=self.transfer(0x5b,offset,frame_id&65535,n)
            if len(block)!=n:raise ScopeError('Frozen frame changed or is not ready; snapshot download refused')
            frame.extend(block)
        after=self.status()
        if after['frame_id']!=frame_id or zlib.crc32(frame)!=status['frame_crc32']:
            raise ScopeError('Frozen frame identifier/CRC changed during download')
        return bytes(frame)

def wait_for(scope,phases,timeout):
    until=time.monotonic()+timeout
    while True:
        s=scope.status()
        if s['phase'] in ('error','cancelled'):
            n=s['error'];raise ScopeError('Capture '+s['phase']+': '+(ERRORS[n] if n<len(ERRORS) else str(n)))
        if s['phase'] in phases:return s
        if time.monotonic()>=until:raise ScopeError('Host timeout waiting for '+', '.join(phases))
        time.sleep(.05)

def csv_text(rows):
    out=io.StringIO(newline='');writer=csv.writer(out,lineterminator='\r\n');writer.writerows(rows);return out.getvalue()

def exports(frame):
    """Same screen/raw column layout and units as the existing r4 CSV export."""
    if len(frame)!=FRAME_BYTES:raise ScopeError('Unexpected native snapshot size')
    def signed(v):return v-256 if v>127 else v
    flags=4 if frame[0x34]==1 else frame[0x14]
    timebase=TIMEBASE[frame[0x2d]];time_n=int(timebase.rstrip('nuSm'))
    factor=1e-9 if 'n' in timebase else 1e-6 if 'u' in timebase else 1e-3 if 'm' in timebase else 1
    scales=[SCALES[frame[2+c]+3*frame[6+c]] for c in range(2)]
    volts=[float(s[:-2])/1000 if s.endswith('mV') else float(s[:-1]) for s in scales]
    screen=[['index','time_s','ch1_v','ch2_v','ch1_screen_code','ch2_screen_code']]
    for i in range(301):
        values=[frame[0x356+c*301+i] for c in range(2)]
        screen.append([i,format(i*time_n*factor/25,'.12g')]+
                      [format((values[c]-128-signed(frame[4+c]))*volts[c]/25,'.12g') if flags&(1<<c) else '' for c in range(2)]+
                      [values[c] if flags&(1<<c) else '' for c in range(2)])
    raw=[['bank_index','bank0_code','bank1_code']]+[[i,frame[0x5b0+i],frame[0x9b0+i]] for i in range(1024)]
    meta=[['key','value'],['format','fnirsi-2c53t-csv-v1'],['firmware','1.2.0-RU-REMOTE-r6'],
          ['screen_points',301],['calibration_required','unknown'],['last_calibration_error','unknown']]
    # The frozen state contains calibration words, but not the guard's live
    # history. Do not invent a successful calibration from missing information.
    meta += [['calibration_word_'+str(i),struct.unpack_from('<H',frame,0x260+2*i)[0]] for i in range(120)]
    meta += [['screen_time_origin','left_edge'],['screen_code_unit','1/25_division'],
          ['channel_flags',flags],['display_mode',frame[0x34]],['timebase_label',timebase],
          ['ch1_scale_label',scales[0]],['ch2_scale_label',scales[1]],
          ['ch1_zero_screen_code',128+signed(frame[4])],['ch2_zero_screen_code',128+signed(frame[5])],
          ['ch1_probe_exponent',frame[6]],['ch2_probe_exponent',frame[7]],['timebase_index',frame[0x2d]],
          ['trigger_mode',frame[0x17]],['trigger_edge',frame[0x18]],['trigger_channel',frame[0x16]],
          ['trigger_position_code',struct.unpack_from('<h',frame,0x1a)[0]],
          ['trigger_level_code',struct.unpack_from('<h',frame,0x1c)[0]],
          ['acquisition_mode',frame[0xdbe]],['acquisition_channel_config',frame[0xdbf]],
          ['acquisition_timebase_index',frame[0xdc0]],
          ['acquisition_ch1_range_index',frame[0xdc1]],['acquisition_ch2_range_index',frame[0xdc2]],
          ['acquisition_ch1_offset',signed(frame[0xdc3])],['acquisition_ch2_offset',signed(frame[0xdc4])],
          ['firmware_window_start',struct.unpack_from('<H',frame,0xdb4)[0]],
          ['firmware_window_count',struct.unpack_from('<H',frame,0xdb6)[0]],
          ['raw_bank_points',1024],['raw_layout','firmware_acquisition_banks'],
          ['raw_values','firmware_processed_uint8'],['raw_bank_validity','not_inferred'],['raw_timing_verified',0]]
    payload=frame[0x356:0x5b0]+bytes([flags])+frame[0xdb4:0xdb6][::-1]+frame[0xdb6:0xdb8][::-1]
    assert len(payload)==607
    return {'.bin':payload,'.csv':csv_text(screen).encode(),'-raw.csv':csv_text(raw).encode(),'-meta.csv':csv_text(meta).encode()}

def capture(scope,args,printer=print):
    directory=Path(args.output).expanduser().resolve();directory.mkdir(parents=True,exist_ok=False)
    report={'firmwareProtocol':VERSION,'startedAt':datetime.now(timezone.utc).isoformat(),
            'status':'running','frames':[],'stimulusCommand':args.stimulus_command,
            'physicalStorageVerified':False,'hardwareFlashed':False}
    active=None
    try:
        scope.command(3,0 if args.mode=='single' else 1)
        scope.command(6,args.timeout)
        scope.command(7,args.pretrigger)
        if args.channel is not None:scope.command(9,args.channel-1)
        if args.edge is not None:scope.command(10,0 if args.edge=='rising' else 1)
        if args.level is not None:scope.command(11,args.level)
        if args.timebase_index is not None:scope.command(12,args.timebase_index)
        for i in range(args.count):
            before=scope.status();scope.command(1)
            armed=wait_for(scope,{'armed','ready','saving'},args.timeout+5)
            if armed['phase']!='armed':raise ScopeError('Trigger fired before the stimulus; adjust the trigger level')
            printer(f'ARMED {i+1}/{args.count}: the instrument is ready for the signal.',flush=True)
            if args.stimulus_command:
                # Explicit user-supplied argv, never a shell. Each child is
                # stopped before another capture can be armed.
                with (directory/f'{i+1:03d}-stimulus.log').open('wb') as log:
                    active=subprocess.Popen(shlex.split(args.stimulus_command),stdin=subprocess.DEVNULL,
                                            stdout=log,stderr=subprocess.STDOUT)
            done=wait_for(scope,{'ready'},args.timeout+30)
            if done['frame_id']==before['frame_id']:raise ScopeError('No new completed frame')
            if active:
                if active.poll() is None:
                    active.terminate()
                    try:active.wait(timeout=5)
                    except subprocess.TimeoutExpired:active.kill();active.wait()
                active=None
            frame=scope.frame(done);files={}
            for suffix,data in exports(frame).items():
                name=f'{i+1:03d}'+suffix;(directory/name).write_bytes(data)
                files[name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            (directory/f'{i+1:03d}-state.bin').write_bytes(frame)
            report['frames'].append(dict(hostReadAt=datetime.now(timezone.utc).isoformat(),status=done,files=files))
            (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            printer(f"CAPTURED {done['frame_id']}; CRC32 {done['frame_crc32']:08X}; device SAVE #{done['saved_number'] or 'none'}",flush=True)
            if i+1<args.count:time.sleep(args.gap_ms/1000)
        report['status']='complete'
        return report
    except BaseException as error:
        report['status']='interrupted' if isinstance(error,KeyboardInterrupt) else 'error';report['error']=str(error)
        try:scope.command(2)
        except Exception:pass
        raise
    finally:
        if active and active.poll() is None:
            active.terminate()
            try:active.wait(timeout=5)
            except subprocess.TimeoutExpired:active.kill();active.wait()
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['status','cancel','capture','panel'],nargs='?',default='status')
    p.add_argument('--mode',choices=['single','save'],default='save')
    p.add_argument('--count',type=int,default=1);p.add_argument('--timeout',type=int,default=30)
    p.add_argument('--gap-ms',type=int,default=500);p.add_argument('--pretrigger',type=int,default=50)
    p.add_argument('--channel',type=int,choices=[1,2]);p.add_argument('--edge',choices=['rising','falling'])
    p.add_argument('--level',type=int);p.add_argument('--timebase-index',type=int)
    p.add_argument('--stimulus-command',help='Command and arguments to launch ONLY after ARMED (no shell expansion)')
    p.add_argument('--output',default='2c53t-capture-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    args=p.parse_args(argv)
    if not 1<=args.count<=50 or not 1<=args.timeout<=600 or not 0<=args.pretrigger<=100 or not 0<=args.gap_ms<=10000:p.error('Invalid count/timeout/pretrigger/gap')
    if args.level is not None and not -100<=args.level<=100:p.error('Level must be between -100 and 100')
    if args.timebase_index is not None and not 0<=args.timebase_index<=19:p.error('Capture timebase index must be 0..19')
    scope=Scope()
    try:
        if args.action=='capture':
            capture(scope,args);print('SAVED:',Path(args.output).resolve())
        else:print(json.dumps(scope.command(2 if args.action=='cancel' else 14) if args.action!='status' else scope.status(),ensure_ascii=False,indent=2))
    finally:scope.close()
if __name__=='__main__':
    try:main()
    except (ScopeError,OSError,ValueError) as error:raise SystemExit('ERROR: '+str(error))
