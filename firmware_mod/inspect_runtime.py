from pathlib import Path
import struct
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
from unicorn.arm_const import *
from build_patch import BASE, HERE as ROOT, SOURCE as FW

def runtime(blob=None):
 b=blob or FW.read_bytes()
 u=Uc(UC_ARCH_ARM, UC_MODE_THUMB|UC_MODE_MCLASS)
 u.mem_map(0x08000000,0x200000);u.mem_write(BASE,b)
 u.mem_map(0x20000000,0x80000)
 u.reg_write(UC_ARM_REG_SP,0x20036f90)
 def stop(uc,address,size,user):
  if address==0x080072f0:uc.emu_stop()
 u.hook_add(UC_HOOK_CODE,stop)
 u.emu_start(0x08007185,0x080072f0,count=2000000)
 if u.reg_read(UC_ARM_REG_PC)!=0x080072f0:raise RuntimeError('Startup scatter load did not complete')
 return u

if __name__=='__main__':
 u=runtime()
 for at in [0x20001114,0x20001144,0x20001058]:
  print(hex(at),u.mem_read(at,48).hex(),[hex(x) for x in struct.unpack('<12I',u.mem_read(at,48))])
 (Path(__file__).resolve().parent/'runtime-initialized-ram.bin').write_bytes(u.mem_read(0x20000000,0x80000))

def call(u, address, args, count=2000000):
 u.reg_write(UC_ARM_REG_SP,0x20070000)
 u.reg_write(UC_ARM_REG_LR,0x081ff001)
 for register,value in zip([UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3],args):u.reg_write(register,value)
 u.emu_start(address|1,0x081ff000,count=count)
 if u.reg_read(UC_ARM_REG_PC)!=0x081ff000:raise RuntimeError('Function did not return')
 return u.reg_read(UC_ARM_REG_R0)

if __name__=='__main__':
 for font in [0x20001114,0x20001144]:
  callback=struct.unpack('<I',u.mem_read(font,4))[0]
  for c in 'AЯяЁёЖйБСЫ':
   u.mem_write(0x20050000,bytes(32))
   result=call(u,callback,[font,0x20050000,ord(c),0])
   print('GLYPH',hex(font),c,result,u.mem_read(0x20050000,16).hex())
