#!/usr/bin/env python3
"""Bounded physical r6j live-frame test: one RUN, many CRC reads, one STOP.

Never starts calibration, touches flash, sends IR, or retries a timed-out
control operation. Does not prove analog calibration or front-panel behavior.
"""
import argparse, json, struct, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'firmware_mod/r6j'))
import scope_control

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seconds',type=int,default=15,choices=range(1,61))
    args=p.parse_args()
    out=ROOT/'scope_app/build'/('physical-r6j-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    report={'kind':'r6j-live-usb','seconds':args.seconds,'runCommands':0,'stopCommands':0,
            'snapshots':[],'noFrame':0,'writesFlash':False,'startsCalibration':False,'complete':False}
    scope=scope_control.Scope();started=False
    try:
        settings=scope.settings();raw=scope.transfer(0x5c)
        if not raw[63]&4:raise RuntimeError('Device does not advertise r6j live snapshots')
        if settings['calibration_active'] or settings['spi_fault'] or settings['math_mode'] or any(c['fft'] for c in settings['channels']):
            raise RuntimeError('Device is in calibration, SPI fault, FFT or math mode')
        cal=scope.transfer(0x60)
        if len(cal)!=64 or struct.unpack_from('<II',cal)!=(0x364c4143,1):raise RuntimeError('Invalid calibration-status page')
        if struct.unpack_from('<I',cal,8)[0]:raise RuntimeError('Calibration is active')
        report['initialSettings']=settings
        scope.command(25,1,timeout=8);report['runCommands']=1;started=True
        if not scope.settings()['running']:raise RuntimeError('RUN not confirmed')
        deadline=time.monotonic()+args.seconds
        while time.monotonic()<deadline:
            before=scope.status()
            try:status=scope.command(34,0,timeout=6)
            except scope_control.ScopeError:
                status=scope.status()
                if status['error']==7 and status['command_id']==status['command_done']:
                    report['noFrame']+=1;continue
                raise
            if status['phase']!='ready' or status['frame_id']==before['frame_id']:
                raise RuntimeError('Live frame id did not advance')
            data=scope.frame(status)
            mode=scope.transfer(0x5c)
            if (mode[63]>>4)&3!=2 or not mode[57]:raise RuntimeError('Live snapshot is not in RUN mode')
            report['snapshots'].append({'id':status['frame_id'],'crc32':status['frame_crc32'],
                                        'screenChannel1Range':[min(data[0x356:0x356+301]),max(data[0x356:0x356+301])],
                                        'screenChannel2Range':[min(data[0x356+301:0x356+602]),max(data[0x356+301:0x356+602])]})
            time.sleep(.2)
        if len(report['snapshots'])<3:raise RuntimeError('Too few valid live snapshots')
        scope.command(26,0,timeout=8);report['stopCommands']=1;started=False
        report['finalSettings']=scope.settings()
        if report['finalSettings']['running']:raise RuntimeError('STOP not confirmed')
        report['complete']=True
    except Exception as e:
        report['error']=str(e)
        # A timed-out command may still be executing. Never enqueue a blind retry.
        if started:
            try:
                state=scope.status()
                if state['command_id']==state['command_done']:
                    scope.command(26,0,timeout=8);report['stopCommands']=1
            except Exception as stop_error:report['stopError']=str(stop_error)
    finally:
        scope.close();out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('complete','runCommands','stopCommands','noFrame')},ensure_ascii=False))
    print('SNAPSHOTS:',len(report['snapshots']),'REPORT:',out)
    if report.get('error'):print('ERROR:',report['error'],file=sys.stderr)
    return 0 if report['complete'] else 1

if __name__=='__main__':sys.exit(main())
