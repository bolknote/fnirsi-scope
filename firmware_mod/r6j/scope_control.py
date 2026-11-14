#!/usr/bin/env python3
"""Autonomous 2C53T r6 measurements: configure, arm, stimulate, read, assess.

USB Sharing must be enabled after the one-time firmware installation. Uses EP0
alongside native mass storage. No key prompts, arbitrary memory writes, AUTO,
calibration or firmware flashing. Stimuli are explicit argv, never shell code.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import statistics
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
import autocapture as legacy

ScopeError = legacy.ScopeError
SETTINGS_MAGIC, SETTINGS_VERSION = 0x364c5443, 0x00060001
STAGES = ('idle','queued-for-key','applying','freezing')
# Native UI table at 0804C7F0 and its GS[17] index at 0801EAF8:
# 0 Normal, 1 Auto, 2 Single. Order is not the manual's presentation order.
TRIGGER_MODE_NAMES = ('normal','auto','single')
TRIGGER_NORMAL, TRIGGER_AUTO, TRIGGER_SINGLE = range(3)
SCALE_V = (.01,.02,.05,.1,.2,.5,1,2,5,10,20,50,100)
TIME_S = (1e-8,2e-8,5e-8,1e-7,2e-7,5e-7,1e-6,2e-6,5e-6,1e-5,
          2e-5,5e-5,1e-4,2e-4,5e-4,.001,.002,.005,.01,.02)
HERE = Path(__file__).resolve().parent

class TriggerTimeout(ScopeError):pass
class EarlyTrigger(ScopeError):pass
class StimulusFailed(ScopeError):pass

def signed(b):return b-256 if b>127 else b
def timestamp():return datetime.now(timezone.utc).isoformat()
def write_json(path,value):
    path=Path(path);temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    temporary.replace(path)

def integer(value,low,high,name):
    if type(value) is not int or not low<=value<=high:
        raise ValueError(f'{name}: expected integer {low}..{high}')
    return value

def number(value,low,high,name):
    if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError(f'{name}: expected finite number {low}..{high}')
    return value

def keys(obj,allowed,name):
    if not isinstance(obj,dict):raise ValueError(name+': expected object')
    unknown=set(obj)-set(allowed)
    if unknown:raise ValueError(name+': unknown fields '+', '.join(sorted(unknown)))

def quantity(value,unit):
    if type(value) in (int,float):return number(value,1e-12,1e6,unit)
    if not isinstance(value,str):raise ValueError('Expected a scale with units')
    m=re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*([numµμ]?)'+unit+r'(?:/div)?\s*',value,re.I)
    if not m:raise ValueError('Invalid '+unit+' scale: '+str(value))
    return float(m[1])*{'':1,'m':1e-3,'u':1e-6,'µ':1e-6,'μ':1e-6,'n':1e-9}[m[2].lower()]

def index_of(value,values,name):
    for i,v in enumerate(values):
        if math.isclose(value,v,rel_tol=1e-8):return i
    raise ValueError('Unsupported '+name+': '+str(value))

def validate_config(config):
    keys(config,('channels','timebase','trigger','filter_mask','timeout_s'),'settings')
    if 'channels' in config:
        keys(config['channels'],('1','2'),'channels')
        for name,c in config['channels'].items():
            keys(c,('enabled','scale','probe','coupling','offset'),'CH'+name)
            if 'enabled' in c and type(c['enabled']) is not bool:raise ValueError('enabled must be boolean')
            if 'probe' in c and (type(c['probe']) is not int or c['probe'] not in (1,10)):raise ValueError('Native probe settings are 1 or 10')
            if 'scale' in c:index_of(quantity(c['scale'],'V'),SCALE_V,'volts/div')
            if 'coupling' in c and c['coupling'] not in ('AC','DC'):raise ValueError('coupling must be AC or DC')
            if 'offset' in c:integer(c['offset'],-100,100,'channel offset')
    if 'timebase' in config:index_of(quantity(config['timebase'],'s'),TIME_S,'timebase')
    if 'trigger' in config:
        t=config['trigger'];keys(t,('channel','edge','level','mode','pretrigger_percent'),'trigger')
        if 'channel' in t:integer(t['channel'],1,2,'trigger channel')
        if 'level' in t:integer(t['level'],-100,100,'trigger level')
        if 'pretrigger_percent' in t:integer(t['pretrigger_percent'],0,100,'pretrigger percentage')
        if 'edge' in t and t['edge'] not in ('rising','falling'):raise ValueError('edge must be rising or falling')
        if 'mode' in t and t['mode'] not in ('auto','normal','single'):raise ValueError('Invalid trigger mode')
    if 'filter_mask' in config:integer(config['filter_mask'],0,3,'filter mask')
    if 'timeout_s' in config:integer(config['timeout_s'],1,600,'trigger timeout')
    return config

def decode_settings(data):
    if len(data)!=64:raise ScopeError('Expected r6 settings; install RU REMOTE r6 and enable USB Sharing')
    magic,version,generation,completed,stage,error=struct.unpack_from('<6I',data)
    if (magic,version)!=(SETTINGS_MAGIC,SETTINGS_VERSION) or stage>=len(STAGES):
        raise ScopeError('Device does not provide the r6 remote-control protocol')
    raw=data[24:56];flags=data[62]
    if raw[0x14] not in (1,2,3) or data[56]>=len(legacy.TIMEBASE) or raw[0x16]>1 or raw[0x17]>2 or raw[0x18]>1:raise ScopeError('Invalid native scope configuration')
    channels=[]
    for c in range(2):
        probe=raw[6+c];r=raw[2+c]
        if probe>1 or r>9 or raw[c]>1:raise ScopeError('Invalid native channel configuration')
        channels.append(dict(enabled=bool(raw[0x14]&(1<<c)),range_index=r,probe=10**probe,
                             scale=legacy.SCALES[r+3*probe],volts_per_div=SCALE_V[r+3*probe],
                             coupling=('AC','DC')[raw[c]],offset=signed(raw[4+c]),fft=bool(raw[10+c])))
    return dict(protocol=version,generation=generation,completed_command=completed,stage=STAGES[stage],error=error,
                channel_mask=raw[0x14],channels=channels,timebase_index=data[56],timebase=legacy.TIMEBASE[data[56]],
                running=bool(data[57]),scope_mode=data[58],trigger_channel=raw[0x16]+1,
                trigger_mode=raw[0x17],trigger_mode_name=TRIGGER_MODE_NAMES[raw[0x17]] if raw[0x17]<3 else 'unknown',trigger_edge=raw[0x18],trigger_level=struct.unpack_from('<h',raw,0x1c)[0],
                trigger_position=struct.unpack_from('<h',raw,0x1a)[0],calibration_active=bool(flags&1),
                spi_fault=bool(flags&2),calibration_required=bool(flags&4),math_mode=bool(flags&8),
                filter_mask=data[63]&3,frame_kind='preview' if data[63]>>4 else 'triggered-single')

class Scope(legacy.Scope):
    def settings(self,stable=True,timeout=25):
        deadline=time.monotonic()+timeout
        while True:
            s=decode_settings(self.transfer(0x5c))
            if not stable or (not s['generation']&1 and s['stage']=='idle'):return s
            if time.monotonic()>=deadline:raise ScopeError('Settings did not reach a completed stable state')
            time.sleep(.05)
    def command(self,operation,argument=0,timeout=25):
        try:return super().command(operation,argument,timeout)
        except ScopeError as e:
            if operation>=16 and str(e)=='trigger timeout':raise ScopeError('Timed out applying scope operation '+str(operation)) from e
            raise
    def stop(self):
        self.command(26)
        s=self.settings()
        if s['running']:raise ScopeError('Native STOP did not stop acquisition')
        return s
    def configure(self,config):
        validate_config(config)
        # Validate combinations against current probe settings BEFORE stopping.
        before=self.settings();operations=[]
        channels=config.get('channels',{});mask=before['channel_mask']
        for name,c in channels.items():
            bit=1<<(int(name)-1)
            if 'enabled' in c:mask=(mask|bit) if c['enabled'] else (mask&~bit)
        if not mask:raise ValueError('At least one channel must remain enabled')
        target_channel=config.get('trigger',{}).get('channel',before['trigger_channel'])
        if not mask&(1<<(target_channel-1)):
            if 'channel' in config.get('trigger',{}):raise ValueError('Trigger channel is disabled')
            target_channel=1 if mask&1 else 2
        if mask!=before['channel_mask']:operations.append((16,mask,('channel_mask',mask)))
        for name,c in channels.items():
            ch=int(name)-1;current=before['channels'][ch];probe=c.get('probe',current['probe'])
            exponent=0 if probe==1 else 1
            if 'probe' in c:operations.append((19+ch,exponent,('channels',ch,'probe',probe)))
            if 'scale' in c:
                native_range=index_of(quantity(c['scale'],'V'),SCALE_V,'volts/div')-3*exponent
                if not 0<=native_range<=9:raise ValueError(f'CH{ch+1} scale is unavailable with a {probe}x probe')
                operations.append((17+ch,native_range,('channels',ch,'range_index',native_range)))
            if 'coupling' in c:operations.append((21+ch,('AC','DC').index(c['coupling']),('channels',ch,'coupling',c['coupling'])))
            if 'offset' in c:operations.append((23+ch,c['offset'],('channels',ch,'offset',c['offset'])))
        if 'timebase' in config:
            i=index_of(quantity(config['timebase'],'s'),TIME_S,'timebase');operations.append((28,i,('timebase_index',i)))
        t=config.get('trigger',{})
        if 'mode' in t:
            mode=TRIGGER_MODE_NAMES.index(t['mode']);operations.append((29,mode,('trigger_mode',mode)))
        operations.append((30,target_channel-1,('trigger_channel',target_channel)))
        if 'edge' in t:
            edge=('rising','falling').index(t['edge']);operations.append((31,edge,('trigger_edge',edge)))
        if 'level' in t:operations.append((32,t['level'],('trigger_level',t['level'])))
        if 'filter_mask' in config:operations.append((8,config['filter_mask'],('filter_mask',config['filter_mask'])))
        if before['calibration_active'] or before['spi_fault']:raise ScopeError('Calibration/SPI interlock is active')
        if before['math_mode'] or any(c['fft'] for c in before['channels']):
            raise ScopeError('Normal waveform mode is required; disable math/FFT before the unattended series')
        self.stop()
        applied=[]
        for op,arg,expected in operations:
            self.command(op,arg);s=self.settings();v=s
            for key in expected[:-1]:v=v[key]
            if v!=expected[-1] or s['running']:
                raise ScopeError(f'Readback mismatch after operation {op}: {expected}, actual {v}')
            applied.append(dict(operation=op,argument=arg))
        if 'pretrigger_percent' in t:
            self.command(7,t['pretrigger_percent'])
            if self.status()['pretrigger_percent']!=t['pretrigger_percent']:raise ScopeError('Pretrigger readback mismatch')
        if 'timeout_s' in config:self.command(6,config['timeout_s'])
        return dict(before=before,after=self.settings(),operations=applied)

def metrics(frame):
    if len(frame)!=legacy.FRAME_BYTES:raise ScopeError('Incorrect frozen frame size')
    if frame[0x34] or frame[10] or frame[11]:raise ScopeError('FFT/math samples cannot be analyzed as a normal waveform')
    scale=frame[0x2d]
    if scale>=len(TIME_S):raise ScopeError('Unsupported timebase in captured frame')
    dt=TIME_S[scale]/25;result=dict(sample_interval_s=dt,points=301,window_s=300*dt,
                                  time_origin='left screen edge',raw_adc_timing_verified=False,channels={})
    for ch in range(2):
        if not frame[0x14]&(1<<ch):continue
        codes=list(frame[0x356+ch*301:0x356+(ch+1)*301]);lo=min(codes);hi=max(codes)
        idx=frame[2+ch]+3*frame[6+ch]
        if idx>=len(SCALE_V):raise ScopeError('Invalid captured channel scale')
        volts=SCALE_V[idx]/25;zero=128+signed(frame[4+ch]);values=[(v-zero)*volts for v in codes]
        mean=statistics.fmean(values);pp=hi-lo;edges=[]
        mid=(lo+hi)/2;hysteresis=max(1,pp*.1);state=None
        if pp>=4:
            for i,v in enumerate(codes):
                next_state=0 if v<=mid-hysteresis else 1 if v>=mid+hysteresis else state
                if state is not None and next_state!=state:
                    # Interpolate the actual threshold crossing, then require
                    # hysteresis before accepting it as an edge.
                    j=i
                    while j>0 and ((codes[j-1]>=mid) if next_state else (codes[j-1]<=mid)):j-=1
                    fraction=(mid-codes[j-1])/(codes[j]-codes[j-1]) if j and codes[j]!=codes[j-1] else 0
                    edges.append(dict(edge='rising' if next_state else 'falling',time_s=((j-1+fraction) if j else 0)*dt))
                state=next_state
        widths={'high':[],'low':[]}
        for a,b in zip(edges,edges[1:]):widths['high' if a['edge']=='rising' else 'low'].append(b['time_s']-a['time_s'])
        rising=[e['time_s'] for e in edges if e['edge']=='rising'];periods=[b-a for a,b in zip(rising,rising[1:])]
        period=statistics.fmean(periods) if len(periods)>=2 and statistics.pstdev(periods)<=.05*statistics.fmean(periods) else None
        result['channels'][str(ch+1)]=dict(min_v=min(values),max_v=max(values),peak_to_peak_v=max(values)-min(values),
            mean_v=mean,rms_v=math.sqrt(statistics.fmean(v*v for v in values)),
            ac_rms_v=math.sqrt(statistics.fmean((v-mean)**2 for v in values)),span_codes=pp,
            clip_fraction=sum(v<=28 or v>=228 for v in codes)/len(codes),
            edge_threshold_v=(mid-zero)*volts,edge_count=len(edges),edges=edges,
            complete_pulse_widths_s=widths,period_s=period,frequency_hz=1/period if period else None,
            timing_resolution_s=dt,scale_index=frame[2+ch],probe=10**frame[6+ch])
    return result

def assess(measured,criteria,default_channel=1):
    keys(criteria,('channel','min_peak_to_peak_v','min_span_codes','min_edges','max_clip_fraction'),'accept')
    ch=integer(criteria.get('channel',default_channel),1,2,'accept channel')
    m=measured['channels'].get(str(ch))
    if m is None:return ['accept channel is disabled']
    errors=[]
    if 'min_span_codes' in criteria and m['span_codes']<integer(criteria['min_span_codes'],0,200,'minimum code span'):errors.append('signal span too small')
    if 'min_peak_to_peak_v' in criteria and m['peak_to_peak_v']<number(criteria['min_peak_to_peak_v'],0,1e6,'minimum signal'):errors.append('signal too small')
    if 'min_edges' in criteria and m['edge_count']<integer(criteria['min_edges'],0,1000,'minimum edges'):errors.append('too few edges')
    if 'max_clip_fraction' in criteria and m['clip_fraction']>number(criteria['max_clip_fraction'],0,1,'clipping limit'):errors.append('waveform clipped')
    return errors

def next_range(config,measured,channel):
    """Only adjust native volts/div, based on observed clipping/span. Bounded.

    Trigger timeouts provide no amplitude evidence and do not call this.
    """
    m=measured['channels'].get(str(channel))
    if m is None:return None
    delta=1 if m['clip_fraction']>.01 or m['span_codes']>180 else -1 if m['span_codes']<12 else 0
    r=m['scale_index']+delta
    if not delta or not 0<=r<=9:return None
    out=copy.deepcopy(config);c=out.setdefault('channels',{}).setdefault(str(channel),{})
    c['scale']=legacy.SCALES[r+(3 if m['probe']==10 else 0)];return out

def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

def read_frame(scope,directory,timeout_s,stimulus=None,preview_s=None):
    """No interactive step. A process can start only AFTER readiness readback."""
    directory=Path(directory);directory.mkdir(exist_ok=False,parents=True)
    report=dict(started_at=timestamp(),status='running',kind='preview' if preview_s is not None else 'triggered-single',stimulus=stimulus)
    process=None;log=None
    try:
        scope.stop();before=scope.status();scope.command(3,0);scope.command(6,timeout_s)
        if preview_s is None:
            scope.command(33)
            deadline=time.monotonic()+timeout_s+5
            while True:
                s=scope.status()
                if s['phase']=='armed':break
                if s['phase'] in ('ready','saving'):raise EarlyTrigger('Trigger fired before stimulus; adjust level/noise')
                if s['phase'] in ('error','cancelled'):raise TriggerTimeout('Instrument did not reach ARMED: '+s['phase'])
                if time.monotonic()>=deadline:raise TriggerTimeout('Host deadline waiting for ARMED')
                time.sleep(.05)
            report['armed_at']=timestamp()
        else:
            scope.command(25,TRIGGER_AUTO)
            if not scope.settings()['running']:raise ScopeError('Preview RUN was not applied')
        if stimulus:
            log=(directory/'stimulus.log').open('wb')
            process=subprocess.Popen(stimulus,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+(preview_s if preview_s is not None else timeout_s+5)
        while True:
            if process is not None and process.poll() not in (None,0):
                raise StimulusFailed('Stimulus exited with code '+str(process.returncode))
            if preview_s is None:
                s=scope.status()
                if s['phase']=='ready':break
                if s['phase'] in ('error','cancelled'):
                    if s['error']==3:raise TriggerTimeout('Native trigger timeout')
                    raise ScopeError('Capture failed: '+s['phase']+' / '+str(s['error']))
                if time.monotonic()>=deadline:raise TriggerTimeout('Host deadline waiting for completed SINGLE')
            else:
                if time.monotonic()>=deadline:
                    scope.command(27);s=scope.status();break
                scope.settings() # keepalive during continuous preview
            time.sleep(.05)
        stop_process(process)
        if process is not None and process.poll() not in (0,-15,-9):
            raise StimulusFailed('Stimulus failed at completion with code '+str(process.poll()))
        if s['phase']!='ready' or s['frame_id']==before['frame_id']:raise ScopeError('Instrument did not publish a new frame')
        frame=scope.frame(s);settings=scope.settings()
        if settings['frame_kind']!=report['kind']:raise ScopeError('Preview and triggered-SINGLE frame kinds disagree')
        measured=metrics(frame);files={}
        for suffix,data in legacy.exports(frame).items():
            p=directory/('waveform'+suffix);p.write_bytes(data)
            files[p.name]=dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        (directory/'state.bin').write_bytes(frame);write_json(directory/'metrics.json',measured)
        report.update(status='complete',finished_at=timestamp(),capture_status=s,settings=settings,files=files,metrics=measured)
        return report
    except BaseException as e:
        report.update(status='interrupted' if isinstance(e,KeyboardInterrupt) else 'error',error=str(e),error_type=type(e).__name__)
        try:scope.stop()
        except Exception as stop_error:report['stop_error']=str(stop_error)
        raise
    finally:
        try:stop_process(process)
        finally:
            if process is not None:report['stimulus_returncode']=process.poll()
            if log is not None:log.close()
            write_json(directory/'report.json',report)

def merge(a,b):
    out=copy.deepcopy(a)
    for k,v in b.items():out[k]=merge(out[k],v) if isinstance(v,dict) and isinstance(out.get(k),dict) else copy.deepcopy(v)
    return out

def validate_plan(plan):
    keys(plan,('version','settings','cases','continue_on_error'),'plan')
    if plan.get('version')!=1:raise ValueError('Plan version must be 1')
    validate_config(plan.get('settings',{}))
    if type(plan.get('continue_on_error',False)) is not bool:raise ValueError('continue_on_error must be boolean')
    cases=plan.get('cases')
    if not isinstance(cases,list) or not 1<=len(cases)<=1000:raise ValueError('Expected 1..1000 cases')
    for c in cases:
        keys(c,('name','settings','stimulus','captures','attempts','autorange','accept','gap_ms'),'case')
        if not isinstance(c.get('name'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',c['name']):raise ValueError('Case name must be 1..80 ASCII letters/digits/_/-')
        validate_config(merge(plan.get('settings',{}),c.get('settings',{})))
        if 'stimulus' in c and (not isinstance(c['stimulus'],list) or not c['stimulus'] or not all(isinstance(a,str) and a and '\0' not in a for a in c['stimulus'])):raise ValueError('stimulus must be a nonempty argv list')
        integer(c.get('captures',1),1,100,'captures');integer(c.get('attempts',1),1,5,'attempts');integer(c.get('gap_ms',0),0,60000,'gap')
        if type(c.get('autorange',False)) is not bool:raise ValueError('autorange must be boolean')
        assess(dict(channels={'1':dict(peak_to_peak_v=0,edge_count=0,clip_fraction=0,span_codes=0),'2':dict(peak_to_peak_v=0,edge_count=0,clip_fraction=0,span_codes=0)}),c.get('accept',{}))
    return plan

def experiment(scope,plan,output,printer=print):
    validate_plan(plan);directory=Path(output).expanduser().resolve();directory.mkdir(parents=True,exist_ok=False)
    report=dict(protocol=SETTINGS_VERSION,started_at=timestamp(),status='running',plan=plan,cases=[],
                host_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    try:
        for i,case in enumerate(plan['cases']):
            config=merge(plan.get('settings',{}),case.get('settings',{}))
            record=dict(name=case['name'],captures=[],status='running');report['cases'].append(record)
            for n in range(case.get('captures',1)):
                shot=dict(number=n+1,attempts=[],accepted=False);record['captures'].append(shot)
                for attempt in range(case.get('attempts',1)):
                    label=f'{i+1:03d}-{case["name"]}/{n+1:03d}-{attempt+1:02d}'
                    printer('MEASURING '+label,flush=True)
                    event=dict(directory=label,status='configuring');shot['attempts'].append(event)
                    try:
                        event['configuration']=scope.configure(config)
                        r=read_frame(scope,directory/label,config.get('timeout_s',10),case.get('stimulus'))
                    except (TriggerTimeout,EarlyTrigger) as e:
                        event.update(status='retryable-error',error=str(e))
                        write_json(directory/'report.json',report)
                        continue
                    except BaseException as e:
                        event.update(status='error',error=str(e));raise
                    errors=assess(r['metrics'],case.get('accept',{}),r['settings']['trigger_channel'])
                    event.update(status='accepted' if not errors else 'quality-rejected',criteria_errors=errors)
                    if not errors:shot['accepted']=True;break
                    if case.get('autorange'):
                        updated=next_range(config,r['metrics'],case.get('accept',{}).get('channel',r['settings']['trigger_channel']))
                        if updated is not None:config=updated;event['next_settings']=copy.deepcopy(updated)
                if not shot['accepted']:
                    record['status']='failed'
                    if not plan.get('continue_on_error',False):raise ScopeError('Capture criteria not met: '+case['name'])
                write_json(directory/'report.json',report)
                if n+1<case.get('captures',1):
                    until=time.monotonic()+case.get('gap_ms',0)/1000
                    while time.monotonic()<until:scope.settings();time.sleep(min(.5,max(0,until-time.monotonic())))
            if record['status']=='running':record['status']='complete'
        report['status']='complete' if all(c['status']=='complete' for c in report['cases']) else 'failed'
        return report
    except BaseException as e:
        report.update(status='interrupted' if isinstance(e,KeyboardInterrupt) else 'error',error=str(e));raise
    finally:
        try:scope.stop()
        except Exception as e:report['stop_error']=str(e)
        report['finished_at']=timestamp();write_json(directory/'report.json',report)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['status','configure','run','stop','preview','capture','experiment'])
    p.add_argument('plan',nargs='?',type=Path)
    p.add_argument('--config',type=Path);p.add_argument('--output',type=Path,default=HERE/'captures'/datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    p.add_argument('--count',type=int,default=1);p.add_argument('--attempts',type=int,default=1)
    p.add_argument('--timeout',type=int,default=10);p.add_argument('--duration',type=float,default=.5)
    p.add_argument('--autorange',action='store_true');p.add_argument('--stimulus-command',help='Quoted argv string; no shell')
    p.add_argument('--dry-run',action='store_true',help='Validate settings/plan without opening USB')
    args=p.parse_args(argv)
    config=validate_config(json.loads(args.config.read_text()) if args.config else {})
    if args.action=='run' and config.get('trigger',{}).get('mode')=='single':p.error('Use capture for triggered SINGLE; run supports auto/normal')
    integer(args.count,1,100,'count');integer(args.attempts,1,5,'attempts');integer(args.timeout,1,600,'timeout');number(args.duration,.05,60,'preview duration')
    if args.action=='experiment':
        if not args.plan:p.error('experiment requires a plan JSON file')
        plan=validate_plan(json.loads(args.plan.read_text()))
    else:
        if args.plan:p.error('A positional plan is accepted only with experiment')
        if 'timeout_s' not in config:config['timeout_s']=args.timeout
        case=dict(name='capture',captures=args.count,attempts=args.attempts,autorange=args.autorange)
        if args.autorange:case['accept']=dict(min_span_codes=12,max_clip_fraction=.02,min_edges=2)
        if args.stimulus_command:case['stimulus']=shlex.split(args.stimulus_command)
        plan=validate_plan(dict(version=1,settings=config,cases=[case]))
    if args.dry_run:
        print(json.dumps(dict(usb_opened=False,plan=plan),ensure_ascii=False,indent=2));return 0
    if args.action in ('preview','capture','experiment') and args.output.exists():raise FileExistsError('Refusing existing output directory: '+str(args.output))
    scope=Scope()
    try:
        scope.settings() # Refuse old firmware BEFORE any control command.
        if args.action=='status':result=dict(settings=scope.settings(False),capture=scope.status())
        elif args.action=='stop':result=scope.stop()
        elif args.action=='configure':result=scope.configure(config)
        elif args.action=='run':
            scope.configure(config);scope.command(25,TRIGGER_MODE_NAMES.index(config.get('trigger',{}).get('mode','auto')));result=scope.settings()
        elif args.action=='preview':
            scope.configure(config);result=read_frame(scope,args.output,config['timeout_s'],plan['cases'][0].get('stimulus'),args.duration)
        else:result=experiment(scope,plan,args.output)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.action in ('preview','capture','experiment'):print('SAVED:',args.output.resolve())
        return 0 if result.get('status')!='failed' else 2
    finally:scope.close()

if __name__=='__main__':
    try:sys.exit(main())
    except (ScopeError,OSError,ValueError) as e:raise SystemExit('ERROR: '+str(e))
