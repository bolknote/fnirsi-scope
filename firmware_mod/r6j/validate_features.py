#!/usr/bin/env python3
"""Execute actual r5 ARM code. No physical USB, ADC, flash or instrument.

The native allocator, native PLAY arm path, patched native SINGLE completion
fragment, extension state machine, USB setup adapter, fonts and UI run as ARM.
RTOS queues, waveform acquisition and LCD/DMA are modeled boundaries.
"""
from pathlib import Path
import sys,struct,json,hashlib,zlib,math
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inspect_runtime import *
from trace_calibration_flow import machine,parse
from unicorn import UC_HOOK_CODE,UC_HOOK_MEM_WRITE
from build_patch import SOURCE
from generate_resources import translations
import autocapture as host
HERE=Path(__file__).resolve().parent;BUILD=HERE/'build'
BLOB=(BUILD/'APP_2C53T_V1.2.0_RU_REMOTE_r6j.bin').read_bytes()
M=json.loads((BUILD/'manifest.json').read_text());SYM={k:int(v,16) for k,v in M['symbols'].items()}
S=0x200000f8;G=0x20033bc0
FIELDS='magic phase error mode count gap_ms timeout_ms pretrigger filter_mask cursor ui_open completed frame_id frame_crc last_file started next_arm pending pending_arg command_id command_done cancel_after_save filter_generation filter_crc0 filter_crc1 filter_scale frame_ready'.split()
checks=[]
def verify(name,ok,**kw):
 assert ok,(name,kw);checks.append(dict(name=name,passed=True,**kw))
def word(u,a):return struct.unpack('<I',u.mem_read(a,4))[0]
def p32(u,a,v):u.mem_write(a,struct.pack('<I',v&0xffffffff))

class VM:
 def __init__(self):
  self.u=u=machine(BLOB);self.queue=[];self.packets=[];self.draws=[];self.frames=[];self.flash_writes=[];self.native_text_calls=0
  self.acquire='none';self.queue_busy=0;self.native_key_calls=0;self.original_scope_calls=0;self.rebuilt_points=None
  self.stub_timebase=True;self.native_return=None;self.old_r10=None
  self.ctx=0x20040000;self.req=0x20045000;self.paintbuf=0x20046000
  self.pixels=bytearray(struct.pack('<H',0x18c3)*320*240)
  u.hook_add(UC_HOOK_CODE,self.hook)
  p=call(u,0x0803acfc,[32]);call(u,0x0803acbc,[p]);self.a=call(u,SYM['r5_get'],[])
  assert self.a and word(u,G)==0x33474e46
  u.hook_add(UC_HOOK_MEM_WRITE,lambda uc,access,address,size,value,data:self.flash_writes.append((address,size)),begin=0x08000000,end=0x080fffff)
  u.hook_add(UC_HOOK_MEM_WRITE,lambda uc,access,address,size,value,data:self.flash_writes.append((address,size)),begin=0x40022000,end=0x400227ff)
  coefs=parse(SOURCE.read_bytes(),bytes([255])*0x130)[0]
  u.mem_write(S+0x260,struct.pack('<120H',*coefs))
  u.mem_write(S+0x350,bytes([128]*4))
  for o,v in [(0x14,1),(0x15,1),(2,5),(3,5),(0x2d,13),(0xf68,2),(0xf60,3)]:self.byte(o,v)
  u.mem_write(0x40010c0c,struct.pack('<I',0x1040))
  for i in range(301):
   self.byte(0x356+i,128+i%20);self.byte(0x356+301+i,128-i%20)
  for i in range(1024):self.byte(0x5b0+i,(i*7+3)&255);self.byte(0x9b0+i,(i*11+9)&255)
  self.byte(0xe11,7)
 def byte(self,o,v):self.u.mem_write(S+o,bytes([v&255]))
 def set(self,name,v):p32(self.u,self.a+FIELDS.index(name)*4,v)
 def get(self,name):return word(self.u,self.a+FIELDS.index(name)*4)
 def ret(self,v=0):self.u.reg_write(UC_ARM_REG_R0,v);self.u.reg_write(UC_ARM_REG_PC,self.u.reg_read(UC_ARM_REG_LR))
 def cstr(self,p):return bytes(self.u.mem_read(p,512)).split(b'\0')[0].decode()
 def hook(self,u,a,size,unused):
  if a==0x08041cf0:
   p=u.reg_read(UC_ARM_REG_R1)
   self.queue.append((u.reg_read(UC_ARM_REG_R0),u.mem_read(p,1)[0] if p else None));self.ret(1)
  elif a==0x0804206c:self.ret(self.queue_busy)
  elif a==0x080423a8:self.ret(1)
  elif a==0x08042ee0:
   x,y,w,h,p=struct.unpack('<4HI',u.mem_read(0x20008350,12));b=bytes(u.mem_read(p,w*h*2));self.frames.append((x,y,w,h))
   for j in range(h):self.pixels[((y+j)*320+x)*2:((y+j)*320+x+w)*2]=b[j*w*2:(j+1)*w*2]
   self.ret()
  elif a==0x08040eb4:
   self.packets.append((u.reg_read(UC_ARM_REG_R1),bytes(u.mem_read(u.reg_read(UC_ARM_REG_R2),u.reg_read(UC_ARM_REG_R3)))));self.ret()
  elif a==0x0800bd60:self.native_key_calls+=1 # Execute the REAL native arm action.
  elif a==0x08020e98:
   self.original_scope_calls+=1
   if self.rebuilt_points is not None:u.mem_write(S+0x356,self.rebuilt_points)
   if self.acquire=='complete':
    self.acquire='none';self.native_return=u.reg_read(UC_ARM_REG_LR);self.old_r10=u.reg_read(UC_ARM_REG_R10)
    u.reg_write(UC_ARM_REG_R10,S);u.reg_write(UC_ARM_REG_PC,0x0802161d)
   else:
    if self.acquire=='manual-stop':self.byte(0x2e,0);self.acquire='none'
    self.ret()
  elif a==0x08021622 and self.native_return is not None:
   u.reg_write(UC_ARM_REG_R10,self.old_r10);u.reg_write(UC_ARM_REG_PC,self.native_return);self.native_return=None
  elif a==0x080242ec and self.stub_timebase:self.ret()
  elif a==(SYM['r5_paint_text']&~1):
   self.draws.append(self.cstr(word(u,u.reg_read(UC_ARM_REG_SP))))
  elif a==(SYM['native_image']&~1):
   # GUI artwork boundary: no filesystem/SPI in the font/caption test.
   self.ret()
  elif a==(SYM['native_text']&~1):self.native_text_calls+=1
 def invoke(self,name,args=(),count=30000000):return call(self.u,SYM[name],list(args),count=count)
 def command(self,op,v=0):
  rc=self.invoke('r5_command',[op,v&65535])
  if not rc:self.invoke('r5_gui')
  return rc
 def advance(self,n):p32(self.u,0x200062d4,word(self.u,0x200062d4)+n)
 def arm(self,mode=0):
  assert self.command(3,mode)==0
  self.invoke('r5_scope_service');assert self.command(1)==0
  self.advance(60);self.invoke('r5_scope_service');return self
 def complete(self):self.acquire='complete';self.invoke('r5_scope_service');return self
 def setup(self,op=0,arg=0,request=0x5a,length=64):
  self.packets=[];self.u.mem_write(self.req,struct.pack('<BBHHH',0xc0,request,op,arg&65535,length))
  call(self.u,0x08031078,[self.ctx,self.req],count=2000000)
  assert self.packets
  return self.packets[-1][1]
 def canvas(self):
  self.u.mem_write(0x20008350,struct.pack('<4HI',0,0,320,240,self.paintbuf))
  self.u.mem_write(self.paintbuf,struct.pack('<H',0x18c3)*320*240)
 def paint(self,text,rect=(0,0,320,60),size=18):
  self.canvas();p=self.paintbuf+320*240*2+64;self.u.mem_write(p,text.encode()+b'\0')
  self.u.mem_write(0x20070000,struct.pack('<4I',p,size,0xffff,0))
  self.invoke('r5_paint_text',rect)
  return bytes(self.u.mem_read(self.paintbuf,320*240*2))

def main():
 v=VM();u=v.u
 verify('Native pool reserves and initializes the extra capture context',v.a==0x20032bc0 and v.get('magic')==host.MAGIC and v.get('timeout_ms')==30000)
 p=call(u,0x0803acfc,[30000]);verify('Later native allocations cannot overlap either pinned context',p+30000<=v.a);call(u,0x0803acbc,[p])
 verify('Only the previously unused long CH1 event changes',BLOB[0x08046539-BASE]==24 and BLOB[0x08046528-BASE:0x08046537-BASE]==SOURCE.read_bytes()[0x08046528-BASE:0x08046537-BASE] and BLOB[0x0804653e-BASE]==0)
 # Full native arm/reset/queue path; not a Python pretend-arm.
 v.arm();verify('Native PLAY action arms SINGLE and resets capture phase',v.native_key_calls==1 and v.u.mem_read(S+0x17,1)==b'\2' and v.u.mem_read(S+0xdb0,1)==b'\3' and v.get('phase')==2 and any(e==8 for _,e in v.queue))
 v.acquire='manual-stop';v.invoke('r5_scope_service');verify('An arbitrary RUN to STOP transition is never accepted as a completed capture',v.get('frame_id')==0 and v.get('phase')==2)
 v=VM().arm().complete();u=v.u
 status=host.decode_status(v.setup());frame=bytes(u.mem_read(v.a+172,host.FRAME_BYTES))
 verify('Actual patched SINGLE completion fragment creates exactly one frozen frame',status['phase']=='ready' and status['completed']==1 and status['frame_id']==1 and not u.mem_read(S+0x2e,1)[0])
 verify('Snapshot bytes and independent CRC32 agree',frame==bytes(u.mem_read(S,host.FRAME_BYTES)) and zlib.crc32(frame)==status['frame_crc32'])
 v.invoke('r5_scope_service');verify('A stopped capture is not counted twice',v.get('frame_id')==1 and v.get('completed')==1)
 verify('Single without autosave never queues SAVE',not any(e==3 for _,e in v.queue))
 downloaded=b''.join(v.setup(off,1,0x5b,min(64,host.FRAME_BYTES-off)) for off in range(0,host.FRAME_BYTES,64))
 verify('USB setup downloads every byte of the frozen frame without an arbitrary memory read',downloaded==frame)
 for offset,token,n in [(0,2,64),(host.FRAME_BYTES+1,1,1),(host.FRAME_BYTES-1,1,2)]:
  verify('Snapshot rejects an invalid offset/id/length: '+str((offset,token,n)),v.setup(offset,token,0x5b,n)==b'')
 verify('USB control and snapshot download never write MCU flash or flash controller registers',not v.flash_writes)
 # Dispatch commands via the REAL patched class setup entry; effects are deferred.
 v=VM();s=host.decode_status(v.setup(4,5));verify('USB IRQ queues series count without changing it inline',s['command_id']==1 and s['command_done']==0 and v.get('count')==1)
 v.invoke('r5_gui');s=host.decode_status(v.setup());verify('GUI acknowledges the deferred USB command',s['command_done']==1 and v.get('count')==5)
 v=VM();v.queue.clear();v.u.reg_write(UC_ARM_REG_IPSR,16)
 s=host.decode_status(v.setup(4,5));verify('USB interrupt publishes the command without calling a task-only RTOS queue API',s['command_id']==1 and s['command_done']==0 and not v.queue)
 v.u.reg_write(UC_ARM_REG_IPSR,0);v.invoke('r5_scope_service')
 verify('Scope task wakes GUI for an interrupt-published command',any(e==126 for _,e in v.queue))
 v.invoke('r5_gui');verify('Interrupt-published command is executed and acknowledged in GUI context',v.get('count')==5 and v.get('command_done')==1)
 for op,arg in [(3,3),(4,0),(4,51),(5,10001),(6,601),(7,101),(8,4),(9,2),(10,2),(11,101),(12,20)]:
  v=VM();v.command(op,arg);verify('Malformed setting is refused in the GUI: '+str((op,arg)),v.get('error') in (2,8))
 v=VM();verify('Only one command can be outstanding',v.invoke('r5_command',[4,5])==0 and v.invoke('r5_command',[4,6])==1)
 v=VM().arm();verify('Configuration cannot race an armed capture',v.command(8,3)==1)
 v.command(2);verify('Cancel leaves the native scope stopped',v.get('phase')==7 and v.u.mem_read(S+0x2e,1)==b'\0')
 v=VM().arm();v.advance(30001);v.invoke('r5_scope_service');verify('Missing trigger times out instead of inventing a successful frame',v.get('phase')==6 and v.get('error')==3 and v.get('frame_id')==0)
 v=VM();v.byte(0x2d,20);v.command(1);verify('Unimplemented native slow-SINGLE path is explicitly refused',v.get('phase')==6 and v.get('error')==8)
 v=VM();v.byte(0xe1b,254);v.command(3,1);v.command(1);verify('Full native album refuses autosave before arming',v.get('error')==4 and v.native_key_calls==0)
 # Series: three separate completed captures, with SAVE acknowledgement required.
 v=VM();v.command(3,2);v.command(4,3);v.command(5,200);v.command(1)
 for index in range(3):
  v.advance(60);v.invoke('r5_scope_service');v.complete()
  verify('Series waits for a real SAVE result before rearming: '+str(index+1),v.get('phase')==3 and v.get('completed')==index+1)
  v.invoke('r5_gui');verify('Series uses the native SAVE preparation queue: '+str(index+1),v.u.mem_read(S+0xe10,1)==b'\1' and any(e==3 for _,e in v.queue))
  v.byte(0xe11,7+index);v.byte(0xe10,2);v.invoke('r5_after_save')
  if index<2:
   verify('Acknowledged series capture enters minimum pause: '+str(index+1),v.get('phase')==5)
   v.advance(199);v.invoke('r5_scope_service');verify('No early rearm before pause expires: '+str(index+1),v.get('phase')==5 and not v.get('pending'))
   v.advance(1);v.invoke('r5_scope_service');v.invoke('r5_gui');verify('Series rearms through native PLAY after the pause: '+str(index+1),v.get('phase')==1)
 verify('Series stops at the requested count',v.get('phase')==4 and v.get('completed')==3 and v.native_key_calls==3 and v.get('last_file')==9)
 v=VM().arm(1).complete();v.byte(0xe10,3);v.invoke('r5_after_save');verify('Failed SAVE halts the series and never reports success',v.get('phase')==6 and v.get('error')==4)
 v=VM().arm(1).complete();v.command(2);v.byte(0xe10,2);v.invoke('r5_after_save');verify('Cancel during SAVE finishes the file and prevents rearm',v.get('phase')==7)
 # The real UI dispatcher is patched before it indexes the stock table.
 v=VM();v.invoke('r5_key',[24]);verify('Long CH1 queues the control panel instead of indexing an invalid callback',v.get('pending')==14)
 u=v.u;u.reg_write(UC_ARM_REG_SP,0x2006f000);u.reg_write(UC_ARM_REG_R0,126);u.reg_write(UC_ARM_REG_R6,0x0804be74)
 def finish(uc,a,size,data):
  if a==0x0803da82:uc.emu_stop()
 h=u.hook_add(UC_HOOK_CODE,finish);u.emu_start(0x0803da7d,0x081ff000,count=60000000);u.hook_del(h)
 verify('Native GUI dispatch renders the new panel through ten bounded strips',u.reg_read(UC_ARM_REG_PC)==0x0803da82 and len(v.frames)==10 and all(h==24 for x,y,w,h in v.frames) and v.get('ui_open')==1)
 import validate_ui
 validate_ui.png565(BUILD/'preview-capture-panel.png',bytes(v.pixels))
 verify('Control panel contains real Cyrillic labels and no missing-glyph substitutions','Захват / фильтры r6' in v.draws and 'Число кадров' in v.draws and '?' not in ''.join(v.draws))
 v.invoke('r5_key',[12]);v.invoke('r5_key',[14]);verify('Panel arrows change series count',v.get('count')==2)
 v.invoke('r5_key',[10]);verify('MENU returns from the new panel to the oscilloscope',not v.get('ui_open') and v.u.mem_read(S+0xf68,1)==b'\2')
 v=VM();v.byte(0xf68,9);v.byte(0xf69,1);v.byte(0xf6a,3);v.byte(0xf6b,3)
 verify('Fifth channel row can actually be selected',v.invoke('r5_key',[12])==1 and v.u.mem_read(S+0xf6b,1)==b'\4')
 v.invoke('r5_key',[15]);verify('Fifth channel row changes CH1 filter state',v.get('filter_mask')==1)
 v.canvas();v.invoke('r5_scope_panel');validate_ui.png565(BUILD/'preview-channel-filter.png',bytes(v.u.mem_read(v.paintbuf,320*240*2)))
 verify('Fifth channel row is labeled as a digital filter','ФНЧ 20 МГц' in v.draws and 'Цифровой фильтр данных' in v.draws)
 # Complete alphabet, including letters absent in the stock fonts. Each
 # character must produce a nonempty shape distinct from fallback '?'.
 v=VM();question=v.paint('?',(0,0,32,32))
 alphabet='АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя'
 for char in alphabet:
  pixels=v.paint(char,(0,0,32,32));verify('Full Russian glyph '+char,pixels!=question and pixels!=struct.pack('<H',0x18c3)*320*240)
 v.paint(alphabet,(0,0,320,100));validate_ui.png565(BUILD/'preview-russian-alphabet.png',bytes(v.u.mem_read(v.paintbuf,320*240*2)))
 symbol_shapes=[v.paint(c,(0,0,32,32)) for c in '↑↓←→▶']
 verify('All five navigation symbols have distinct real glyphs',len(set(symbol_shapes))==5 and all(p!=question for p in symbol_shapes))
 mapping=translations(SOURCE.read_bytes());stock_ru=[]
 for raw in SOURCE.read_bytes()[0xb4000:].split(b'\0'):
  try:s=raw.decode()
  except UnicodeError:continue
  if s and any('\u0400'<=c<='\u04ff' for c in s) and all(c.isprintable() or c in '\r\n\t' for c in s):stock_ru.append(s)
 verify('Every stock Russian operational string has an audited replacement',len(stock_ru)==70 and all(s in mapping for s in stock_ru))
 for old,new in mapping.items():
  p=0x20069000;v.u.mem_write(p,old.encode()+b'\0');out=v.invoke('r5_translate',[p]);verify('ARM translation '+old,v.cstr(out)==new)
 for language in (1,2):
  v.byte(0xf60,language);v.canvas();p=0x20069000;v.u.mem_write(p,b'Channel:\0');v.u.mem_write(0x20070000,struct.pack('<4I',p,0x20001114,0xffff,1))
  count=v.native_text_calls
  v.invoke('r5_text',[0,0,100,30]);verify('Other language text still uses the stock renderer '+str(language),v.native_text_calls==count+1)
 # Numerical ARM execution of filter: gain, DC, bypass, raw preservation.
 for scale,frequency,expected in [(3,5e6,None),(3,20e6,None),(3,50e6,None),(5,20e6,None),(5,22e6,None)]:
  v=VM();v.byte(0x2d,scale);v.command(8,1);raw=bytes(v.u.mem_read(S+0x5b0,2048));fs=25e9/[10,20,50,100,200,500][scale]
  points=bytes(round(128+40*math.sin(2*math.pi*frequency*i/fs)) for i in range(301));v.u.mem_write(S+0x356,points)
  v.invoke('r5_timebase');result=bytes(v.u.mem_read(S+0x356,301))
  n=200;begin=101
  rms=lambda a:math.sqrt(sum((x-128)**2 for x in a[begin:])/n)
  measured=rms(result)/rms(points);k=math.tan(math.pi*frequency/fs)/math.tan(math.pi*20e6/fs);predicted=1/math.sqrt(1+k**4)
  verify('ARM 20 MHz response at %.1f MHz / %.0f MS/s'%(frequency/1e6,fs/1e6),abs(measured-predicted)<.04,measured=measured,expected=predicted)
  verify('Digital filtering never modifies the raw acquisition banks '+str((scale,frequency)),bytes(v.u.mem_read(S+0x5b0,2048))==raw)
  v.invoke('r5_filter_points');verify('Repeated redraw does not filter the same points twice '+str((scale,frequency)),bytes(v.u.mem_read(S+0x356,301))==result)
 for scale in range(6):
  v=VM();v.byte(0x2d,scale);v.command(8,3);v.u.mem_write(S+0x356,bytes([128])*602);v.invoke('r5_filter_points')
  verify('DC is preserved at supported scale '+str(scale),bytes(v.u.mem_read(S+0x356,602))==bytes([128])*602)
 v=VM();v.byte(0x2d,3);v.command(8,1)
 v.rebuilt_points=bytes(round(128+40*math.sin(2*math.pi*.2*i)) for i in range(301))
 v.invoke('r5_scope_service');filtered=bytes(v.u.mem_read(S+0x356,301))
 verify('Inline native STOP reconstruction is filtered after the scope FSM returns',filtered!=v.rebuilt_points)
 v.invoke('r5_scope_service');verify('A stopped redraw retains the same filtered waveform',bytes(v.u.mem_read(S+0x356,301))==filtered)
 v.command(8,0);v.invoke('r5_scope_service')
 verify('Disabling the filter restores reconstructed unfiltered points',bytes(v.u.mem_read(S+0x356,301))==v.rebuilt_points)
 for special in ('slow','calibration','off','math','fft'):
  v=VM();v.command(8,0 if special=='off' else 3)
  if special=='slow':v.byte(0x2d,6)
  else:v.byte(0x2d,3)
  if special=='calibration':v.byte(0x30,1)
  if special=='math':v.byte(0x34,1)
  if special=='fft':v.byte(10,1);v.byte(11,1)
  before=bytes(v.u.mem_read(S+0x356,602));v.invoke('r5_filter_points')
  verify('Filter bypass is exact for '+special,bytes(v.u.mem_read(S+0x356,602))==before)
 report=dict(passed=True,checkCount=len(checks),checks=checks,patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hardwareTested=False,
             limitations=['Native acquisition is a modeled boundary; actual patched SINGLE completion code is executed',
                          'USB packets intercepted at native endpoint send; host enumeration and on-device timing untested',
                          '20 MHz is digital display-data filtering; analog input and hardware trigger unchanged',
                          'No physical signal, storage result or bootloader acceptance test'])
 (BUILD/'features-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(dict(passed=True,checks=len(checks)),indent=2))
if __name__=='__main__':main()
