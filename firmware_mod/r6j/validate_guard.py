#!/usr/bin/env python3
"""Execute linked ARM hooks and original firmware in Unicorn. No USB access.
Hardware, RTOS queues and drawing are boundaries; allocator/controller are real.
"""
from pathlib import Path
import sys,json,struct,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inspect_runtime import *
from unicorn import UC_HOOK_CODE,UC_HOOK_MEM_READ
from trace_calibration_flow import machine,parse as stock_parse
from diagnose_calibration import Controller
from build_patch import SOURCE,thumb_branch
HERE=Path(__file__).resolve().parent;BUILD=HERE/'build'
BLOB=(BUILD/'APP_2C53T_V1.2.0_RU_REMOTE_r6j.bin').read_bytes()
M=json.loads((BUILD/'manifest.json').read_text());SYM={k:int(v,16) for k,v in M['symbols'].items()}
S=0x200000f8;G=0x20033bc0
NOM=stock_parse(SOURCE.read_bytes(),bytes([255])*0x130)[0]
checks=[]
def verify(name,ok,**kw):
 assert ok,(name,kw)
 checks.append(dict(name=name,passed=True,**kw))
def words(u):return list(struct.unpack('<120H',u.mem_read(S+0x260,240)))
def word(u,a):return struct.unpack('<I',u.mem_read(a,4))[0]
def wb(u,o,v):u.mem_write(S+o,bytes([v&255]))
def gword(u,i):return word(u,G+4*i)
class Device:
 def __init__(self):
  self.u=machine(BLOB);self.events=[];self.draws=[]
  self.u.hook_add(UC_HOOK_CODE,self.hook)
  # Real main allocator invokes the patched pool initializer.
  p=call(self.u,0x0803acfc,[32]);call(self.u,0x0803acbc,[p])
  assert word(self.u,G)==0x33474e46
  self.u.mem_write(S+0x260,struct.pack('<120H',*NOM));self.u.mem_write(S+0x350,bytes([128]*4))
  for o,v in [(0,1),(1,0),(2,4),(3,5),(4,20),(5,236),(0x14,1),(0x15,1),(0x17,1),(0x2d,13),(0x2e,1),(0xf68,2),(0xf60,3)]:wb(self.u,o,v)
  self.u.mem_write(0x40001c2c,struct.pack('<I',4095))
 def ret(self,v=1):
  self.u.reg_write(UC_ARM_REG_R0,v);self.u.reg_write(UC_ARM_REG_PC,self.u.reg_read(UC_ARM_REG_LR))
 def hook(self,u,a,size,unused):
  if a in (0x080088a4,0x08008a58,0x08041cf0,0x08025fc0):self.events.append(a);self.ret()
  elif a==0x0800f154:
   sp=u.reg_read(UC_ARM_REG_SP);p=word(u,sp)
   self.draws.append(bytes(u.mem_read(p,100)).split(b'\0')[0].decode());self.ret()
 def start(self):
  self.initial=bytes(self.u.mem_read(S,0x30));self.coefs=words(self.u)
  call(self.u,SYM['guard_start'],[],count=100000)
  assert gword(self.u,1)==1
  return self
 def step(self,bank0=None,bank1=None):
  u=self.u
  for ch,b in enumerate((bank0,bank1)):
   if b is None:b=bytes([28 if u.mem_read(S+4+ch,1)[0]==156 else 228])*500
   u.mem_write(S+0x6aa+ch*1024,b)
  wb(u,0x32,40);u.reg_write(UC_ARM_REG_R10,S);u.reg_write(UC_ARM_REG_SP,0x2006ff00)
  def end(uc,a,size,data):
   if a==0x080240aa:uc.emu_stop()
  h=u.hook_add(UC_HOOK_CODE,end)
  u.emu_start(0x08022ae5,0x081ff000,count=300000);u.hook_del(h)
  assert u.reg_read(UC_ARM_REG_PC)==0x080240aa,hex(u.reg_read(UC_ARM_REG_PC))
 def invoke(self,sym,args=[]):return call(self.u,SYM[sym],args,count=2000000)
 def tick(self):
  # Execute guard + real one-second callback, with battery/board helper mocked.
  def skip(uc,a,size,data):
   if a==0x08009c78:self.ret()
  h=self.u.hook_add(UC_HOOK_CODE,skip)
  call(self.u,0x080406c8,[],count=100000);self.u.hook_del(h)

def parse(blob, record):
 u=machine(blob);u.mem_write(0x08006000,bytes(record)+bytes(512-len(record)))
 u.reg_write(UC_ARM_REG_R4,0x08006000);u.reg_write(UC_ARM_REG_SP,0x2006ff00)
 def end(uc,a,size,data):
  if a==0x0802d50a:uc.emu_stop()
 u.hook_add(UC_HOOK_CODE,end);u.emu_start(0x0802cd93,0x081ff000,count=200000)
 assert u.reg_read(UC_ARM_REG_PC)==0x0802d50a,hex(u.reg_read(UC_ARM_REG_PC))
 return words(u),list(u.mem_read(S+0x350,4))

def main():
 d=Device();u=d.u
 verify('Native allocator permanently reserves the fixed guard context',gword(u,0)==0x33474e46 and gword(u,1)==0)
 alloc=[]
 for n in (32,640,1024,0x102c,9000,30000):
  p=call(u,0x0803acfc,[n]);assert p;alloc.append(p)
  verify('Native pool allocation does not overlap guard: '+str(n),p+n<=G or p>=G+1024,address=hex(p))
 for p in alloc:call(u,0x0803acbc,[p])
 verify('Guard survives allocator reuse and frees',word(u,G)==0x33474e46)

 d=Device().start();u=d.u
 verify('Guarded initializer retains stock both-channel calibration setup',u.mem_read(S+0x14,1)==b'\3' and u.mem_read(S+4,2)==b'\x9c\x9c')
 for n in range(125):
  d.step()
  if not u.mem_read(S+0x30,1)[0]:break
 verify('Ideal samples complete all 120 original controller steps',n+1==121 and not gword(u,1) and words(u)==NOM,steps=n+1)
 # The first check is the native settling phase 1 -> 10.
 verify('Completed calibration retains the native measurement refresh',d.events.count(0x08025fc0)==1)
 verify('Successful calibration clears the needs-calibration flag',gword(u,5)==0)

 d=Device().start();u=d.u;d.step()
 for i in range(10):
  d.step(bytes([100])*500,bytes([100])*500)
  if not gword(u,1):break
 verify('Unresponsive ADC aborts before a coefficient wraps',not gword(u,1) and gword(u,4)==2 and words(u)==d.coefs,checks=i+1)
 verify('Abort restores scope ranges, offsets, coupling and timebase; leaves STOP',bytes(u.mem_read(S,0x2e))==d.initial[:0x2e] and u.mem_read(S+0x2e,1)==b'\0')
 verify('No old acquisition heap pointers are restored',u.mem_read(S+0xdfc,8)==bytes(8))

 d=Device().start();u=d.u
 for _ in range(10):d.tick()
 verify('Watchdog aborts when calibration receives no fresh ADC frame',gword(u,4)==3 and not gword(u,1) and words(u)==d.coefs)
 d=Device().start();u=d.u
 for _ in range(60):
  # Same phase and valid incoming frames; no progress.
  u.mem_write(G+4*8,struct.pack('<I',gword(u,7)))
  d.tick()
 verify('Fresh but non-progressing input has a finite deadline',gword(u,4)==4 and not gword(u,1))
 d=Device().start();u=d.u
 u.mem_write(G+7*4,struct.pack('<I',599));u.mem_write(G+8*4,struct.pack('<I',599));u.mem_write(G+9*4,struct.pack('<I',599));d.tick()
 verify('Total calibration is limited to ten minutes',gword(u,4)==5 and not gword(u,1))
 for key in (10,):
  d=Device().start();u=d.u
  result=d.invoke('guard_key',[key])
  verify('Cancellation restores table; MENU consumed / Power dispatched: '+str(key),not gword(u,1) and words(u)==d.coefs and result==(key==10))
 d=Device().start();u=d.u;u.mem_write(G+2*4,struct.pack('<I',1))
 d.invoke('guard_key',[10]);verify('MENU during a native step defers rollback',gword(u,1)==1 and gword(u,3)==1)
 d.invoke('guard_after_step');verify('Deferred MENU is honored at the safe step boundary',not gword(u,1) and words(u)==d.coefs)
 d=Device().start();u=d.u;wb(u,0xf65,1);u.mem_write(S+0xf6c,struct.pack('<H',901))
 d.tick();verify('Idle auto power-off is suppressed while calibrating and setting restored',gword(u,1)==1 and u.mem_read(S+0xf65,1)==b'\1')

 # Stop original writer before any flash unlock. Verify emitted record uses backup.
 d=Device().start();u=d.u;u.mem_write(S+0x260,struct.pack('<H',123))
 verify('Power bypasses calibration gate and dispatches guarded native shutdown',d.invoke('guard_key',[1])==2 and gword(u,1)==1)
 d.events.clear()
 hit=[]
 def preflash(uc,a,size,data):
  if a==0x08029a48:hit.append(a);uc.emu_stop()
 h=u.hook_add(UC_HOOK_CODE,preflash)
 u.reg_write(UC_ARM_REG_R0,0x55);u.reg_write(UC_ARM_REG_SP,0x2006ff00);u.emu_start(0x080293bd,0x081ff000,count=200000);u.hook_del(h)
 verify('Native shutdown writer restores the backup before flash operations',hit==[0x08029a48] and words(u)==d.coefs and not gword(u,1) and u.reg_read(UC_ARM_REG_PRIMASK)==1)

 verify('Shutdown rollback and record preparation contain no RTOS queue operation',0x08041cf0 not in d.events)

 # Boot records and native nominal fallback, including bad non-last coefficients.
 record=bytearray(0x130);record[0]=0x55
 packed=[NOM[i]|NOM[i+60]<<16 for i in range(60)];record[0x38:0x128]=struct.pack('<60I',*packed)
 record[0x128:0x12a]=b'\x80\x80';record[0x12c:0x12e]=b'\x80\x80'
 for index,value in [(0,0),(0,65535),(12,5000),(60,5000),(119,0)]:
  r=record.copy();struct.pack_into('<H',r,0x38+(index%60)*4+(index//60)*2,value)
  loaded,alignment=parse(BLOB,r)
  verify('Boot rejects invalid coefficient anywhere: %d=%d'%(index,value),loaded==NOM and alignment==[250,5,128,128])
 loaded,alignment=parse(BLOB,record);verify('Boot preserves a valid complete calibration table',loaded==NOM)

 # Nominal fallback warning is deferred until the normal scope UI is ready.
 d=Device();u=d.u;d.invoke('guard_nominal_loaded');d.tick()
 verify('Nominal fallback visibly requests calibration',gword(u,5)==1 and gword(u,4)==8 and u.mem_read(S+0xe10,1)==b'\3')
 d.invoke('guard_key',[10]);wb(u,0xe10,0);d.tick()
 verify('Nominal warning can be dismissed without repeated popups',u.mem_read(S+0xe10,1)==b'\0' and gword(u,5)==1)
 loaded,alignment=parse(BLOB,bytes([255])*0x130)
 verify('Erased settings retain the exact native nominal table',loaded==NOM)

 u=machine(BLOB);u.mem_write(0x200062d4,struct.pack('<I',123));u.mem_write(0x200061e4,bytes(4));u.reg_write(UC_ARM_REG_SP,0x2006ff00)
 u.emu_start(0x0802a995,0x080427aa,count=1000)
 verify('SPI deadline counter is the native SysTick/FreeRTOS tick',word(u,0x200062d4)==124 and u.reg_read(UC_ARM_REG_PC)==0x080427aa)

 # Every replaced SPI wait: ready returns to same native continuation and retains
 # all live registers/FP, except the original last status register and flags.
 for i,q in enumerate(M['spiWaits']):
  d=Device();u=d.u;u.mem_write(0x40003c08,struct.pack('<I',3))
  regs=[UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_R4,UC_ARM_REG_R5,UC_ARM_REG_R6,UC_ARM_REG_R7,UC_ARM_REG_R8,UC_ARM_REG_R9,UC_ARM_REG_R10,UC_ARM_REG_R11,UC_ARM_REG_R12,UC_ARM_REG_LR]
  vals=[0x12340000+j for j in range(len(regs))];vals[7]=0x40003c08
  for r,v in zip(regs,vals):u.reg_write(r,v)
  for j in range(16):u.reg_write(UC_ARM_REG_D0+j,0x123456789abcdef0+j)
  u.reg_write(UC_ARM_REG_CONTROL,4);u.reg_write(UC_ARM_REG_SP,0x2006f000);u.reg_write(UC_ARM_REG_FPSCR,0x40000000);u.reg_write(UC_ARM_REG_APSR,0x30000000)
  u.mem_write(0x081ff200,bytes.fromhex('03b44ff08040e1ee100a03bc')+thumb_branch(0x081ff20c,q['start'],False))
  u.emu_start(0x081ff201,q['ready'],count=10000)
  changed=int(q['reg'][1:]);expected=vals.copy();expected[changed]=(3<<(30 if q['bit'] else 31))&0xffffffff
  verify('SPI ready hook preserves live ABI and continuation: '+str(i),u.reg_read(UC_ARM_REG_PC)==q['ready'] and [u.reg_read(r) for r in regs]==expected and u.reg_read(UC_ARM_REG_SP)==0x2006f000 and all(u.reg_read(UC_ARM_REG_D0+j)==0x123456789abcdef0+j for j in range(16)) and u.reg_read(UC_ARM_REG_FPSCR)==0x40000000 and (u.reg_read(UC_ARM_REG_APSR)&0xf0000000)==(0x90000000 if q["bit"] else 0xb0000000))
 # Artificial ticker makes stuck hardware fail quickly without 1M iterations.
 for bit in (0,1):
  d=Device().start();u=d.u;ticks=[0]
  def clock(uc,access,a,size,value,data):
   ticks[0]+=1;uc.mem_write(0x200062d4,struct.pack('<I',ticks[0]))
  h=u.hook_add(UC_HOOK_MEM_READ,clock,begin=0x200062d4,end=0x20000003)
  result=d.invoke('guard_spi_wait',[bit]);u.hook_del(h)
  verify('Missing SPI ready bit is bounded and latched: '+str(bit),result==0 and gword(u,6)==1 and gword(u,3)==6)
  d.tick();verify('SPI fault aborts calibration and restores coefficients: '+str(bit),not gword(u,1) and words(u)==d.coefs)

 d=Device();u=d.u
 result=call(u,SYM['guard_spi_wait'],[0],count=25000000)
 verify('SPI wait is finite even if the SysTick counter stops',result==0 and gword(u,6)==1 and word(u,0x200062d4)==0)

 # Differential checks: identical float32 arithmetic and channel/bank mapping.
 # Compare the patched step with the native controller, allowing only a safe
 # rollback where native output would leave the hardware coefficient domain.
 for phase,mask,high in [(0x10,3,False),(0x50,3,True),(0x20,3,False),(0x60,3,True),(0x30,1,False),(0x30,2,False)]:
  for value in (0,27,28,29,100,227,228,229,255):
   native=Controller(SOURCE.read_bytes());d=Device().start();u=d.u
   for target in (native.u,u):
    target.mem_write(S+0x260,struct.pack('<120H',*NOM));wb(target,0x30,phase);wb(target,0x14,mask)
    target.mem_write(S+4,bytes([100 if high else 156])*2)
    wb(target,0x2d,5 if (phase&0x3f)<0x20 else 4 if (phase&0x3f)<0x30 else 3)
   banks=[bytes([value])*500,bytes([255-value])*500]
   native.check(banks);expected=words(native.u);safe=all(0<v<=4095 for v in expected)
   d.step(*banks)
   verify('Native/patched coefficient arithmetic %02X mask%d mean%d'%(phase,mask,value),
          (gword(u,1)==1 and words(u)==expected) if safe else (not gword(u,1) and words(u)==NOM),nativeWithinBounds=safe)

 # Album boundary: both old CSVs and unrelated files must be ignored, including
 # duplicate base numbers. Run the stock helper for BMP presence as a boundary.
 d=Device();u=d.u;files=iter(['7.bin','7.csv','7-raw.csv','7-meta.csv','2.bin','note.txt','0.bin','255.bin','2.BIN','3.bin']);freed=[];closed=[]
 folder=0x20050000;u.mem_write(folder,b'2:/Screenshot simple file\0')
 def directory(uc,a,size,data):
  if a==0x08034c40:d.ret(0)
  elif a==0x08034f20:
   name=next(files,'').encode()+b'\0';p=uc.reg_read(UC_ARM_REG_R1);uc.mem_write(p,bytes(0x118));uc.mem_write(p+0x16,name);d.ret(0)
  elif a==0x080341c4:closed.append(1);d.ret(0)
  elif a==0x080073b4:
   p=uc.reg_read(UC_ARM_REG_R0);n=uc.reg_read(UC_ARM_REG_R2);uc.mem_write(p,('2:/Screenshot file/%d.bmp'%n).encode()+b'\0');d.ret(0)
  elif a==0x08035a08:
   path=bytes(uc.mem_read(0x20008360,64)).split(b'\0')[0];d.ret(2 if path.endswith(b'/3.bmp') else 0)
 h=u.hook_add(UC_HOOK_CODE,directory);n=call(u,0x0803b878,[folder],count=100000);u.hook_del(h)
 verify('Album ignores CSV companions, duplicate IDs, invalid IDs and missing BMP',n==2 and list(u.mem_read(S+0xe25,2))==[2,7] and u.mem_read(S+0xe11,1)==b'\10' and closed==[1])

 # Exact keyboard trampoline / caller stack offset, not just C guard_key.
 d=Device().start();u=d.u;sp=0x2006f000;u.mem_write(sp+7,b'\12');u.reg_write(UC_ARM_REG_R7,S);u.reg_write(UC_ARM_REG_SP,sp)
 u.emu_start(0x08040075,0x08040038,count=100000)
 verify('Physical MENU key event passes through mid-task trampoline',u.reg_read(UC_ARM_REG_PC)==0x08040038 and not gword(u,1) and u.reg_read(UC_ARM_REG_SP)==sp)

 d=Device().start();u=d.u;sp=0x2006f000;u.mem_write(sp+7,b'\1');u.reg_write(UC_ARM_REG_R7,S);u.reg_write(UC_ARM_REG_SP,sp)
 u.emu_start(0x08040075,0x0804008c,count=100000)
 verify('Physical Power key bypasses native calibration gate',u.reg_read(UC_ARM_REG_PC)==0x0804008c and gword(u,1)==1 and u.reg_read(UC_ARM_REG_SP)==sp)

 # Runtime text hooks: genuine UTF-8 strings are captured at the native renderer.
 d=Device().start();u=d.u
 def draw(sym):
  sp=0x2006f000;u.mem_write(sp,struct.pack('<4I',0x080bc18b,0x20001174,0x55f,1));u.reg_write(UC_ARM_REG_SP,sp);u.reg_write(UC_ARM_REG_LR,0x081ff001)
  for reg,val in zip((UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3),(63,88,194,75)):u.reg_write(reg,val)
  u.emu_start(SYM[sym],0x081ff000,count=100000)
 draw('guard_progress_text');verify('Calibration popup contains actual progress, means and MENU hint',len(d.draws)==3 and 'MENU' in d.draws[-1] and '/3' in d.draws[0])
 d.invoke('guard_abort',[3]);d.draws.clear();draw('guard_notice_text');verify('Calibration error popup identifies missing samples and rollback','Нет отсчётов' in d.draws and 'Откат. СТОП' in d.draws)

 report=dict(passed=True,checkCount=len(checks),checks=checks,patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hardwareTested=False,scope='Real ARM code/allocator/controller. Synthetic ADC, MMIO and RTOS boundaries. No flash write or bootloader test.')
 (BUILD/'guard-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(dict(passed=True,checks=len(checks)),indent=2))
if __name__=='__main__':main()
