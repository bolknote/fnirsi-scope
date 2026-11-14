#!/usr/bin/env python3
"""Host controller against real emulated ARM firmware, plus analytical signals.

Only RTOS scheduling, ADC completion and child processes are synthetic. USB
requests go through the actual patched native class Setup and KEY hook.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import copy,json,hashlib,struct,sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import scope_control as h
from validate_remote import RemoteVM,TAIL,S,BLOB,BUILD,host

checks=[]
def verify(name,ok):
    assert ok,name
    checks.append(dict(name=name,passed=True))
def refuses(name,fn,types=(ValueError,h.ScopeError)):
    try:fn()
    except types:verify(name,True)
    else:verify(name,False)

def frame():
    f=bytearray(host.FRAME_BYTES);f[0x14]=3;f[2]=f[3]=5;f[0x2d]=9
    for i in range(301):
        f[0x356+i]=168 if 20<=i%50<30 else 128
        f[0x356+301+i]=88 if 20<=i%50<30 else 128
    return bytes(f)

class EmulatedScope(h.Scope):
    def __init__(self,failure=None):
        self.v=RemoteVM();self.calls=[];self.failure=failure;self.stimulated=False;self.last_operation=0;self.stimuli=0
    def pump(self):
        v=self.v
        if v.get('pending'):
            v.executing='GUI';v.invoke('r5_gui');v.executing='test'
            if v.remote_word(2)==1:v.drain_key()
        if v.u.mem_read(S+0x2e,1)==b'\1' and v.get('phase')==0:
            v.u.mem_write(S+0xdb6,struct.pack('<H',301))
            v.u.mem_write(S+0x356,frame()[0x356:0x5b0])
            # This class models ADC completion; publish its generation too.
            # Separate native-SPI tests exercise the actual completion hook.
            from validate_remote import G, word, p32
            p32(v.u,G+680,word(v.u,G+680)+1)
        v.advance(60);v.invoke('r5_scope_service')
        if v.get('phase')==2 and (self.stimulated or self.failure=='early'):
            if self.failure=='timeout':v.advance(10001);v.invoke('r5_scope_service')
            else:
                f=bytearray(frame())
                if self.failure=='clipped-once' and self.stimuli==1:
                    f[0x356:0x356+301]=bytes(228 if value>128 else 128 for value in f[0x356:0x356+301])
                v.u.mem_write(S+0x356,bytes(f[0x356:0x5b0]));v.complete()
    def transfer(self,request,value=0,index=0,length=64):
        self.calls.append((request,value,index,length))
        if request==0x5a and not value:self.pump()
        if request==0x5a and value:
            self.last_operation=value
            if value==33:self.stimulated=False
        data=self.v.setup(value,index,request,length)
        if self.failure=='crc' and request==0x5b and value==64:data=bytes([data[0]^1])+data[1:]
        if self.failure=='readback' and request==0x5c and self.last_operation==17:
            b=bytearray(data);b[26]=(b[26]+1)%10;data=bytes(b)
        return data

CURRENT=None
class Process:
    instances=[]
    def __init__(self,argv,**kw):
        self.argv=argv;self.kw=kw;self.log=kw['stdout'];self.returncode=None;self.stopped=False
        self.was_armed=CURRENT.status()['phase']=='armed'
        self.was_running=CURRENT.settings()['running']
        CURRENT.stimulated=True;CURRENT.stimuli+=1;Process.instances.append(self)
        self.log.write(b'synthetic stimulus process\n')
    def poll(self):
        if CURRENT.failure=='process' and not self.stopped:self.returncode=9
        return self.returncode
    def terminate(self):self.stopped=True;self.returncode=-15
    def kill(self):self.stopped=True;self.returncode=-9
    def wait(self,timeout=None):return self.returncode

def main():
    global CURRENT
    bad=bytes(64);refuses('Old/missing r6 settings is refused before configuration',lambda:h.decode_settings(bad))
    for config in [dict(timebase='21us'),dict(channels={'1':{'probe':100}}),dict(channels={'1':{'coupling':'GD'}}),dict(trigger={'level':101}),dict(trigger={'channel':True}),dict(timeout_s=float('nan')),dict(typo=1)]:
        refuses('Config syntax rejected before device operations '+str(config),lambda c=config:h.validate_config(c))
    v=EmulatedScope();s=v.settings()
    verify('Host decodes both native channel scales, offsets and calibration flags',s['channels'][0]['offset']==50 and s['channels'][1]['offset']==-50 and s['channels'][0]['volts_per_div']==.5 and s['protocol']==h.SETTINGS_VERSION)
    config=dict(channels={'1':dict(enabled=True,probe=10,scale='2V',coupling='DC',offset=0),
                          '2':dict(enabled=True,probe=1,scale='200mV',coupling='AC',offset=0)},
                timebase='10us',trigger=dict(channel=2,edge='falling',level=-5,pretrigger_percent=30),filter_mask=0)
    applied=v.configure(config);s=applied['after']
    verify('Host configures real ARM handlers for both channels and checks their readback',
           s['channel_mask']==3 and s['channels'][0]['probe']==10 and s['channels'][0]['scale']=='2V'
           and s['channels'][1]['scale']=='200mV' and s['channels'][0]['offset']==s['channels'][1]['offset']==0
           and s['timebase_index']==9 and s['trigger_channel']==2 and s['trigger_edge']==1 and s['trigger_level']==-5 and not s['running'])
    verify('Configure routes all native controls through KEY, including STOP',v.v.play_contexts and set(v.v.play_contexts)=={'KEY'})
    v=EmulatedScope();calls=list(v.calls)
    refuses('Impossible probe/scale combination is refused before STOP',lambda:v.configure(dict(channels={'1':dict(probe=10,scale='10mV')})))
    verify('Invalid combined scale produced only read-only settings requests',all(c[0]==0x5c for c in v.calls[len(calls):]))
    refuses('Both inputs cannot be disabled',lambda:v.configure(dict(channels={'1':dict(enabled=False),'2':dict(enabled=False)})))
    v=EmulatedScope('readback')
    refuses('USB acknowledgement alone cannot pass a mismatching native readback',lambda:v.configure(dict(channels={'1':dict(scale='1V')})))
    v=EmulatedScope();v.v.byte(10,1)
    refuses('FFT data cannot enter a normal waveform experiment',lambda:v.configure({}))
    m=h.metrics(frame());one=m['channels']['1'];two=m['channels']['2']
    verify('Known optical-like pulse widths use screen-point timing, not USB timestamps',
           all(abs(w-4e-6)<1e-12 for w in one['complete_pulse_widths_s']['high'])
           and abs(one['frequency_hz']-50000)<1e-5 and abs(one['peak_to_peak_v']-.8)<1e-9)
    verify('Both enabled channels have independent measured polarity and edges',one['max_v']>0 and one['min_v']==0 and two['min_v']<0 and two['max_v']==0 and one['edge_count']==two['edge_count'])
    flat=bytearray(frame());flat[0x356:0x5b0]=bytes([128])*602;fm=h.metrics(flat)
    verify('A flat trace never invents pulses, period or frequency',fm['channels']['1']['edge_count']==0 and fm['channels']['1']['frequency_hz'] is None)
    irregular=bytearray(frame())
    for i in range(301):irregular[0x356+i]=168 if any(a<=i<b for a,b in [(20,30),(75,85),(160,170),(220,230)]) else 128
    verify('Irregular IR pulse trains are not given a false single frequency',h.metrics(irregular)['channels']['1']['frequency_hz'] is None)
    clipped=bytearray(frame());clipped[0x356:0x5b0]=bytes([228])*602;cm=h.metrics(clipped)
    verify('Clipping is measured and can reject a completed capture',h.assess(cm,dict(max_clip_fraction=.02))==['waveform clipped'])
    adjusted=h.next_range({},cm,1)
    verify('Autorange chooses a coarser native range from actual clipping',adjusted['channels']['1']['scale']=='1V')
    small=bytearray(frame());small[0x356:0x5b0]=bytes([128])*602;sm=h.metrics(small)
    verify('Autorange chooses a finer range for a tiny span',h.next_range({},sm,1)['channels']['1']['scale']=='200mV')
    sm['channels']['1']['scale_index']=0
    verify('Autorange stops at native range limits',h.next_range({},sm,1) is None)
    verify('Measured criteria distinguish small signal, missing edges and clipping',len(h.assess(fm,dict(min_span_codes=12,min_edges=2)))==2)
    with TemporaryDirectory(prefix='2c53t-control-') as tmp:
        out=Path(tmp)
        CURRENT=EmulatedScope();CURRENT.configure(dict(channels={'1':dict(offset=0,enabled=True)},timebase='10us'));Process.instances=[]
        with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
            report=h.read_frame(CURRENT,out/'single',1,['python3','test with spaces.py'])
        verify('Full host -> actual ARM -> ARMED -> stimulus -> frozen frame path completes',report['status']=='complete' and Process.instances[0].was_armed and report['kind']=='triggered-single')
        verify('Stimulus is argv without a shell, with no inherited console input',Process.instances[0].argv==['python3','test with spaces.py'] and Process.instances[0].kw['stdin']==h.subprocess.DEVNULL and 'shell' not in Process.instances[0].kw)
        verify('Completed measurement stops its stimulus and closes its log',Process.instances[0].stopped and Process.instances[0].log.closed)
        verify('Host exports frozen state, BIN, screen/raw/meta CSV and metrics',all((out/'single'/n).exists() for n in ('state.bin','waveform.bin','waveform.csv','waveform-raw.csv','waveform-meta.csv','metrics.json','report.json')))
        for failure,error_type in [('early',h.EarlyTrigger),('timeout',h.TriggerTimeout),('crc',h.ScopeError),('process',h.StimulusFailed)]:
            CURRENT=EmulatedScope(failure);CURRENT.configure({});Process.instances=[]
            with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
                refuses('Failed capture is surfaced: '+failure,lambda f=failure:h.read_frame(CURRENT,out/f,1,['stimulus']),types=(error_type,))
            r=json.loads((out/failure/'report.json').read_text())
            verify('Failed capture retains its error and no false waveform: '+failure,r['status']=='error' and not (out/failure/'waveform.csv').exists() and not CURRENT.settings()['running'])
            verify('Failed capture cleans up any stimulus: '+failure,all(p.poll() is not None and p.log.closed for p in Process.instances))
            if failure=='early':verify('A trigger before ARMED prevents stimulus launch',not Process.instances)
        CURRENT=EmulatedScope();CURRENT.configure({});Process.instances=[]
        with patch.object(h.time,'sleep'):
            report=h.read_frame(CURRENT,out/'preview',1,preview_s=.1)
        verify('Untriggered preview uses native Auto and does not count as a SINGLE',report['kind']=='preview' and report['capture_status']['completed']==0 and report['settings']['frame_kind']=='preview' and report['settings']['trigger_mode']==1 and report['settings']['trigger_mode_name']=='auto')
        CURRENT=EmulatedScope();Process.instances=[]
        plan=dict(version=1,settings=dict(timebase='10us',channels={'1':dict(offset=0)}),cases=[dict(name='ir00',captures=2,stimulus=['stimulus'],attempts=1)])
        with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
            r=h.experiment(CURRENT,plan,out/'series',printer=lambda *a,**k:None)
        verify('Plan takes two measurements without console input or physical key events',r['status']=='complete' and len(r['cases'][0]['captures'])==2 and CURRENT.stimuli==2)
        verify('Every plan capture retains configuration readback and its own attempt directory',all(s['accepted'] and len(s['attempts'])==1 and 'after' in s['attempts'][0]['configuration'] for s in r['cases'][0]['captures']) and (out/'series'/'001-ir00'/'002-01'/'waveform.csv').exists())
        calls=list(CURRENT.calls)
        refuses('Existing evidence directory is refused before device operations',lambda:h.experiment(CURRENT,plan,out/'series'),types=(FileExistsError,))
        verify('Refusing output overwrite did not send a new USB command',CURRENT.calls==calls)
        # First timeout is retained; the next attempt is explicitly a NEW capture.
        CURRENT=EmulatedScope('timeout');real_configure=CURRENT.configure;configured=[]
        def configure(c):
            configured.append(copy.deepcopy(c))
            if len(configured)>1:CURRENT.failure=None;CURRENT.stimulated=False
            return real_configure(c)
        CURRENT.configure=configure;plan['cases'][0]['captures']=1;plan['cases'][0]['attempts']=2
        with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
            r=h.experiment(CURRENT,plan,out/'retry',printer=lambda *a,**k:None)
        attempts=r['cases'][0]['captures'][0]['attempts']
        verify('Trigger retry preserves the failed attempt and arms a fresh capture',r['status']=='complete' and [a['status'] for a in attempts]==['retryable-error','accepted'] and (out/'retry'/'001-ir00'/'001-01'/'report.json').exists())
        CURRENT=EmulatedScope('process')
        with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
            refuses('Stimulus failures are fatal rather than silently retried',lambda:h.experiment(CURRENT,plan,out/'fatal',printer=lambda *a,**k:None))
        verify('Fatal failure still stops acquisition and retains the experiment report',CURRENT.stimuli==1 and json.loads((out/'fatal'/'report.json').read_text())['status']=='error' and not CURRENT.settings()['running'])
        CURRENT=EmulatedScope('clipped-once');adaptive=copy.deepcopy(plan)
        adaptive['cases'][0].update(autorange=True,accept=dict(max_clip_fraction=.02))
        with patch.object(h.subprocess,'Popen',Process),patch.object(h.time,'sleep'):
            r=h.experiment(CURRENT,adaptive,out/'adaptive',printer=lambda *a,**k:None)
        attempts=r['cases'][0]['captures'][0]['attempts']
        verify('Autonomous quality loop retains clipped trace, changes native range and recaptures',
               r['status']=='complete' and [a['status'] for a in attempts]==['quality-rejected','accepted']
               and attempts[1]['configuration']['after']['channels'][0]['range_index']==6
               and (out/'adaptive'/'001-ir00'/'001-01'/'waveform.csv').exists()
               and (out/'adaptive'/'001-ir00'/'001-02'/'waveform.csv').exists())
        verify('Adaptive settings do not mutate the user plan',adaptive['settings']==plan['settings'])
    # Pure plan validation/dry run must never instantiate libusb.
    with patch.object(h,'Scope',side_effect=AssertionError('USB opened')):
        with patch('builtins.print'):verify('Dry-run validates a plan without opening USB',h.main(['capture','--dry-run'])==0)
    report=dict(passed=True,checkCount=len(checks),checks=checks,patchedSHA256=hashlib.sha256(BLOB).hexdigest(),
                hostSHA256=hashlib.sha256((Path(__file__).parent/'scope_control.py').read_bytes()).hexdigest(),
                hardwareTested=False,boundaries='Real host and emulated ARM firmware; ADC completion, RTOS scheduling, child processes modeled')
    (BUILD/'control-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(passed=True,checks=len(checks))))

if __name__=='__main__':main()
