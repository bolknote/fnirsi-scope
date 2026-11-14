#!/usr/bin/env python3
"""Actual ARM adapters + native BOT state machine; USB/MMIO modeled locally.
No physical device, USB enumeration, or flash programming is performed.
"""
from pathlib import Path
import sys,struct,json,hashlib,zlib,subprocess
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inspect_runtime import *
from unicorn import UC_HOOK_CODE,UC_HOOK_MEM_READ,UC_HOOK_MEM_WRITE
from trace_calibration_flow import machine
HERE=Path(__file__).resolve().parent;BUILD=HERE/'build';S=0x200000f8;G=0x20033bc0
M=json.loads((BUILD/'manifest.json').read_text());BLOB=(BUILD/'APP_2C53T_V1.2.0_RU_REMOTE_r6j.bin').read_bytes();SYM={k:int(v,16) for k,v in M['symbols'].items()}
R3=FW.read_bytes() # Independent original stock SCSI baseline.
checks=[]
def verify(name,ok,**kw):
 assert ok,(name,kw)
 checks.append(dict(name=name,passed=True,**kw))
def u32(u,a):return struct.unpack('<I',u.mem_read(a,4))[0]
def p32(u,a,v):u.mem_write(a,struct.pack('<I',v))
def rawpattern(address,n):return bytes(((address+i)*13+((address+i)>>8)+7)&255 for i in range(n))
class VM:
 def __init__(self,blob=BLOB,raw_stub=True):
  self.u=u=machine(blob);self.packets=[];self.flash_reads=[];self.flash_writes=[];self.raw_failure=None;self.raw_stub=raw_stub
  self.ctx=0x20040000;self.desc=0x20040400;self.msc=0x20041000;self.req=0x20044000;self.out=0x20045000
  p=call(u,0x0803acfc,[32]);call(u,0x0803acbc,[p])
  p32(u,self.ctx+4,self.desc);p32(u,self.desc+0x24,self.msc)
  u.mem_write(self.ctx+0x134,struct.pack('<H',64));u.mem_write(self.ctx+0x13c,struct.pack('<H',31))
  u.mem_write(self.ctx+0x2c,b'\1');u.mem_write(self.ctx+0x12c,b'\1')
  p32(u,self.msc+8,0xe00);p32(u,self.msc+12,4096)
  self.cal=rawpattern(0,4096);u.mem_write(0x08006000,self.cal)
  u.hook_add(UC_HOOK_CODE,self.hook)
 def ret(self,v=0):self.u.reg_write(UC_ARM_REG_R0,v);self.u.reg_write(UC_ARM_REG_PC,self.u.reg_read(UC_ARM_REG_LR))
 def hook(self,u,a,size,data):
  if a==0x08040eb4:
   r=[u.reg_read(x) for x in (UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)]
   self.packets.append((r[1],bytes(u.mem_read(r[2],r[3]))));self.ret()
  elif self.raw_stub and a==(SYM['diagnostic_flash_read']&~1):
   p=u.reg_read(UC_ARM_REG_R0);address=u.reg_read(UC_ARM_REG_R1);n=u.reg_read(UC_ARM_REG_R2)
   self.flash_reads.append((address,n))
   if self.raw_failure is None:u.mem_write(p,rawpattern(address,n))
   self.ret(1 if self.raw_failure is None else self.raw_failure)
  elif a==0x08036048:
   p=u.reg_read(UC_ARM_REG_R0);address=u.reg_read(UC_ARM_REG_R1);n=u.reg_read(UC_ARM_REG_R2)
   self.flash_reads.append((address,n));u.mem_write(p,rawpattern(address,n));self.ret()
  elif a==0x0803616c:self.flash_writes.append(1);self.ret()
 def invoke(self,sym,args=[]):return call(self.u,SYM[sym],args,count=5000000)
 def cdb(self,op,lun=1,length=0,flags=0x80,cdb=None):
  m=self.msc;u=self.u;self.packets.clear();u.mem_write(m,bytes(0x1050));u.mem_write(m,b'\7')
  p32(u,m+8,0xe00);p32(u,m+12,4096)
  cdb=bytes([op])+bytes(11 if op in (0xa8,0xaa) else 9) if cdb is None else bytes(cdb)
  packet=struct.pack('<IIIBBB',0x43425355,0x12345678,length,flags,lun,len(cdb))+cdb
  u.mem_write(m+0x1020,packet+bytes(31-len(packet)))
  p32(u,m+0x1044,0x12345678);p32(u,m+0x1048,length)
 def dispatch(self):return call(self.u,0x0803020c,[self.ctx],count=5000000)
 def native_cbw(self):return call(self.u,0x08030e0c,[self.ctx],count=5000000)
 def ack(self):return call(self.u,0x08030cc4,[self.ctx],count=5000000)
 def response(self):return bytes(self.u.mem_read(self.msc+0x1c,u32(self.u,self.msc+0x18)))
 def sector(self,lba):
  self.u.mem_write(self.out,b'\xa5'*576)
  rc=self.invoke('diagnostic_sector',[lba,self.out]);assert rc==1,(lba,rc)
  assert self.u.mem_read(self.out+512,64)==b'\xa5'*64,'sector buffer overflow'
  return bytes(self.u.mem_read(self.out,512))

def main():
 v=VM();u=v.u
 for value in (0,99):
  p32(u,v.msc+4,value);u.mem_write(v.req,bytes.fromhex('a1fe000000000100'))
  call(u,0x08031078,[v.ctx,v.req],count=200000)
  verify('Native GET_MAX_LUN advertises two units after previous value '+str(value),v.packets[-1]==(0,b'\1'))
 # Baseline SCSI behavior is delegated exactly for the native volume.
 for op,length,cdb in [(0x12,36,bytes.fromhex('120000002400')),(0x25,8,None),(0x23,12,bytes.fromhex('23000000000000000c00')),(0x1a,8,bytes.fromhex('1a003f000800')),(0,0,None)]:
  old=VM(R3);new=VM()
  for x in (old,new):x.cdb(op,lun=0,length=length,cdb=cdb)
  a=old.dispatch();b=new.dispatch()
  verify('Original LUN0 SCSI behavior preserved: %02X'%op,a==b and old.response()==new.response())
 old=VM(R3);new=VM();c=bytearray(10);c[0]=0x28;c[5]=2;c[8]=1
 for x in (old,new):x.cdb(0x28,lun=0,length=4096,cdb=c)
 a=old.dispatch();b=new.dispatch()
 verify('Native LUN0 READ10 payload and external address are preserved',a==b and old.packets==new.packets and old.flash_reads==new.flash_reads==[(0x202000,4096)])
 # Direct inquiry/capacity/mode/sense calls exercise the linked C via native entry.
 v=VM();v.cdb(0x12,length=36,cdb=bytes.fromhex('120000002400'));rc=v.dispatch();r=v.response()
 verify('Diagnostic LUN identifies itself independently',rc==0 and len(r)==36 and r[16:32]==b'2C53T DIAGNOSTIC')
 v.cdb(0x25,length=8);v.dispatch();verify('Read capacity reports 32 MiB with 512-byte blocks',v.response()==struct.pack('>II',65535,512))
 for op,cdb,length,index in [(0x1a,bytes.fromhex('1a003f000400'),4,2),(0x5a,bytes.fromhex('5a003f00000000000800'),8,3)]:
  v.cdb(op,length=length,cdb=cdb);v.dispatch();verify('SCSI MODE SENSE write-protect bit: %02X'%op,bool(v.response()[index]&0x80))
 before=bytes(v.u.mem_read(0x20000018,18))
 for op in (0x04,0x15,0x2a,0x2e,0x3b,0x55,0xaa):
  v.cdb(op,length=512,flags=0);rc=v.dispatch()
  verify('Destructive SCSI command is rejected: %02X'%op,rc==1 and not v.flash_writes)
 v.cdb(0x03,length=18,cdb=bytes.fromhex('030000001200'));v.dispatch();r=v.response()
 verify('Rejected write reports DATA PROTECT, ASC 27',r[2]==7 and r[12]==0x27)
 verify('Diagnostic sense does not corrupt the native LUN sense',bytes(v.u.mem_read(0x20000018,18))==before)
 # Run the real native CBW and IN-completion state machines, not only wrappers.
 for blocks in (1,8,17):
  v=VM();c=bytearray(10);c[0]=0x28;c[7:9]=blocks.to_bytes(2,'big');v.cdb(0x28,length=blocks*512,cdb=c)
  v.native_cbw()
  while v.u.mem_read(v.msc,1)!=b'\7':v.ack()
  packets=[p for ep,p in v.packets];data=b''.join(p for p in packets if len(p)!=13)
  csw=packets[-1]
  verify('Native BOT streams %d block(s) and sends correct CSW'%blocks,len(data)==blocks*512 and len(csw)==13 and struct.unpack('<III',csw[:12])==(0x53425355,0x12345678,0) and csw[12]==0,packetSizes=list(map(len,packets)))
  verify('Native BOT data matches the virtual disk: '+str(blocks),data[:512]==v.sector(0))
 v=VM();c=bytearray(12);c[0]=0xa8;c[9]=1;v.cdb(0xa8,length=512,cdb=c);v.native_cbw();v.ack()
 verify('READ12 runs through the native CBW/CSW path',len(v.packets)==2 and len(v.packets[0][1])==512 and v.packets[-1][1][-1]==0)
 for op in (0x12,0x25,0x28,0xa8):
  v=VM();v.cdb(op,length=512,cdb=bytes([op]));verify('Short SCSI CDB refused: %02X'%op,v.dispatch()==1 and not v.packets and not v.flash_reads)
 v=VM();v.cdb(0x28,length=0,flags=0);verify('Zero-length READ10 accepts either transfer direction without reading',v.dispatch()==0 and not v.packets and not v.flash_reads)
 # Bound checks must refuse before reading arbitrary memory.
 for lba,blocks,host in [(65536,1,512),(65535,2,1024),(0xffffffff,1,512),(0,1,4096)]:
  v=VM();c=bytearray(10);c[0]=0x28;c[2:6]=lba.to_bytes(4,'big');c[7:9]=blocks.to_bytes(2,'big');v.cdb(0x28,length=host,cdb=c)
  verify('Invalid READ10 is refused: %d/%d/%d'%(lba,blocks,host),v.dispatch()==1 and not v.packets and not v.flash_reads)
 # Independently parse the FAT16 volume, walk every file chain and compare data.
 v=VM();boot=v.sector(0);bps=struct.unpack_from('<H',boot,11)[0];spc=boot[13];reserved=struct.unpack_from('<H',boot,14)[0];nfats=boot[16];roots=struct.unpack_from('<H',boot,17)[0];fatn=struct.unpack_from('<H',boot,22)[0];total=struct.unpack_from('<I',boot,32)[0]
 root=reserved+nfats*fatn;data_start=root+(roots*32+bps-1)//bps;clusters=(total-data_start)//spc
 verify('FAT16 BPB is internally consistent',boot[510:]==b'\x55\xaa' and bps==512 and spc==8 and nfats==2 and 4085<=clusters<65525 and fatn*512//2>=clusters+2)
 fat=b''.join(v.sector(reserved+i) for i in range(fatn))
 copy=b''.join(v.sector(reserved+fatn+i) for i in range(fatn));verify('Both FAT tables are identical',fat==copy)
 directory=b''.join(v.sector(root+i) for i in range((roots*32+511)//512));files={}
 for i in range(0,len(directory),32):
  e=directory[i:i+32]
  if e[0]==0:break
  if e[11]&8:continue
  name=e[:8].decode().rstrip()+'.'+e[8:11].decode().rstrip();cluster=struct.unpack_from('<H',e,26)[0];size=struct.unpack_from('<I',e,28)[0];chain=[]
  while cluster<0xfff8:
   assert 2<=cluster<clusters+2 and cluster not in chain
   chain.append(cluster);cluster=struct.unpack_from('<H',fat,cluster*2)[0]
  verify('File has a complete, correctly sized FAT chain: '+name,len(chain)==(size+4095)//4096 and bool(e[11]&1))
  files[name]=(size,chain)
 verify('Diagnostic directory contains the requested dumps',set(files)=={'CALIB.BIN','MCUFLASH.BIN','FLASH.BIN','RAM.BIN','STATE.BIN','STATUS.TXT','README.TXT'})
 all_clusters=[c for size,chain in files.values() for c in chain];verify('Diagnostic file chains never overlap',len(all_clusters)==len(set(all_clusters)))
 for name,(size,chain) in files.items():
  if name in ('STATUS.TXT','README.TXT'):continue
  for offset in sorted(set([0,(size-1)//512*512,min(4096,(size-1)//512*512)])):
   lba=data_start+(chain[offset//4096]-2)*spc+(offset%4096)//512;r=v.sector(lba);n=min(512,size-offset)
   if name=='FLASH.BIN':expected=rawpattern(offset,n)
   else:
    address={'CALIB.BIN':0x08006000,'MCUFLASH.BIN':0x08000000,'RAM.BIN':0x20000000,'STATE.BIN':S}[name]
    expected=bytes(v.u.mem_read(address+offset,n))
   verify('ARM sector data is exact: %s offset %X'%(name,offset),r[:n]==expected and r[n:]==bytes(512-n))
 status_lba=data_start+(files['STATUS.TXT'][1][0]-2)*spc;status=v.sector(status_lba).split(b'\0')[0].decode()
 verify('Status includes CRC32 of the exact saved calibration page',('calibration_page_crc32=%08X'%zlib.crc32(v.cal)) in status)
 (BUILD/'example-STATUS.TXT').write_text(status)
 # External read errors must not become apparently valid FF-filled file data.
 for failed in (0,2):
  v=VM();v.raw_failure=failed
  cluster=files['FLASH.BIN'][1][0];lba=data_start+(cluster-2)*spc;c=bytearray(10);c[0]=0x28;c[2:6]=lba.to_bytes(4,'big');c[8]=1;v.cdb(0x28,length=512,cdb=c)
  rc=v.dispatch();verify('SPI failure/busy is reported without a bulk data packet: '+str(failed),rc==1 and not v.packets)
 # Emit a sparse, independently checkable image. Data remains zero here; its
 # contents are checked above through the real ARM sector generator.
 image=BUILD/'diagnostic-volume-metadata.img'
 with image.open('wb') as f:
  f.truncate(total*bps)
  for lba in range(data_start):f.seek(lba*512);f.write(v.sector(lba))
 result=subprocess.run(['/sbin/fsck_msdos','-n',str(image)],capture_output=True,text=True)
 verify('Independent fsck_msdos accepts the generated FAT16 filesystem',result.returncode==0,output=result.stdout+result.stderr)
 (BUILD/'diagnostic-validation.json').write_text(json.dumps(dict(passed=True,checkCount=len(checks),checks=checks,patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hardwareTested=False,limitations=['USB packets intercepted at the stock endpoint send API','SPI payload fixture replaces raw flash reads except separate MMIO tests','No host USB enumeration or physical instrument test']),ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(dict(passed=True,checks=len(checks)),indent=2))
if __name__=='__main__':main()
