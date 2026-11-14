#!/usr/bin/env python3
"""Execute the new SPI2 reader against register models, without any USB/hardware."""
from validate_diagnostic import *
class SPI:
 def __init__(self,vm,mode='ok',cs_high=True,primask=0):
  self.v=vm;self.u=vm.u;self.mode=mode;self.tx=[];self.cs=[];self.reads=0
  p32(self.u,0x40010c0c,0x1000 if cs_high else 0);self.u.reg_write(UC_ARM_REG_PRIMASK,primask)
  self.u.hook_add(UC_HOOK_MEM_READ,self.read,begin=0x40003808,end=0x4000380f)
  self.u.hook_add(UC_HOOK_MEM_WRITE,self.write,begin=0x4000380c,end=0x4000380f)
  self.u.hook_add(UC_HOOK_MEM_WRITE,self.write,begin=0x40010c10,end=0x40010c17)
 def read(self,u,access,a,size,value,data):
  if a==0x40003808:
   self.reads+=1
   status=0 if self.mode=='tx-timeout' else 2 if self.mode=='rx-timeout' else 131 if self.mode=='busy-before' or (self.mode=='busy-after' and len(self.tx)>=20) else 3
   p32(u,a,status)
  elif a==0x4000380c:
   n=len(self.tx);address=int.from_bytes(bytes(self.tx[1:4]),'big') if n>=4 else 0
   v=rawpattern(address+n-5,1)[0] if n>4 else 0
   p32(u,a,v)
 def write(self,u,access,a,size,value,data):
  if a==0x4000380c:self.tx.append(value&255)
  else:
   self.cs.append((a,value));odr=u32(u,0x40010c0c)
   p32(u,0x40010c0c,odr|value if a==0x40010c10 else odr&~value)
 def transfer(self,address=0x123456,n=16):
  self.u.mem_write(self.v.out,b'\xa5'*520)
  rc=call(self.u,SYM['diagnostic_flash_read'],[self.v.out,address,n],count=3000000)
  return rc,bytes(self.u.mem_read(self.v.out,min(n,512)))

def main():
 for mask in (0,1):
  v=VM(raw_stub=False);s=SPI(v,primask=mask);rc,data=s.transfer()
  verify('SPI2 issues only READ 03 with the exact 24-bit address, PRIMASK='+str(mask),rc==1 and s.tx[:4]==[3,0x12,0x34,0x56] and s.tx[4:]==[255]*16)
  verify('SPI2 data bytes are not fabricated, PRIMASK='+str(mask),data==rawpattern(0x123456,16) and v.u.mem_read(v.out+16,504)==b'\xa5'*504)
  verify('SPI2 releases CS and restores entry IRQ state '+str(mask),s.cs==[(0x40010c14,0x1000),(0x40010c10,0x1000)] and v.u.reg_read(UC_ARM_REG_PRIMASK)==mask)
 for mask in (0,1):
  for mode,cs_high,expected in [('ok',False,2),('busy-before',True,2),('tx-timeout',True,0),('rx-timeout',True,0),('busy-after',True,0)]:
   v=VM(raw_stub=False);s=SPI(v,mode=mode,cs_high=cs_high,primask=mask);rc,data=s.transfer()
   verify('SPI2 refusal/timeout is finite and restores IRQ: %s/%s'%(mode,mask),rc==expected and v.u.reg_read(UC_ARM_REG_PRIMASK)==mask and s.reads<70000)
   if expected==2:verify('Owned/busy SPI bus is never disturbed: %s/%s'%(mode,mask),not s.tx and not s.cs and data==b'\xa5'*16)
   else:verify('Failed SPI transfer deasserts its own CS: %s/%s'%(mode,mask),s.cs[-1]==(0x40010c10,0x1000))
 for address,n,expected in [(0x1000000,0,1),(0x1000000,1,0),(0xffffff,2,0),(0xffffffff,1,0),(0,4097,0)]:
  v=VM(raw_stub=False);s=SPI(v);rc,data=s.transfer(address,n)
  verify('Raw flash bounds checked before touching SPI: %X/%d'%(address,n),rc==expected and not s.tx and not s.cs)
 report=dict(passed=True,checkCount=len(checks),checks=checks,patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hardwareTested=False,scope='Linked ARM SPI2 code; modeled MMIO, no actual electrical/USB test')
 (BUILD/'spi2-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(dict(passed=True,checks=len(checks)),indent=2))
if __name__=='__main__':main()
