#!/usr/bin/env python3
"""Check host trigger names against the actual native bottom-bar ARM lookup.

The old client assigned Auto to 0. Native code instead displays Normal for 0.
This test executes the native GS[17] -> string-pointer selection, so a Python
enumeration cannot validate itself by agreeing with a Python fake.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib,json,sys
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE))
from validate_control import EmulatedScope
from validate_remote import S,BLOB,BUILD
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_R7
import scope_control as h

checks=[]
def verify(name,ok):
 assert ok,name
 checks.append(dict(name=name,passed=True))
def native_label(scope):
 u=scope.v.u;u.mem_write(S+0xf60,b'\2');u.reg_write(UC_ARM_REG_R7,S)
 u.emu_start(0x0801eae7,0x0801eb0a,count=100)
 p=u.reg_read(UC_ARM_REG_R0)
 return bytes(u.mem_read(p,32)).split(b'\0')[0].decode()

def main():
 for name in ('normal','auto','single'):
  v=EmulatedScope();code=h.TRIGGER_MODE_NAMES.index(name)
  v.v.byte(0x17,code)
  verify('Client enumeration matches the native ARM label: '+name,native_label(v).lower()==name)
  v=EmulatedScope();v.configure(dict(trigger=dict(mode=name)))
  verify('Config name selects that actual native trigger mode: '+name,native_label(v).lower()==name and v.settings()['trigger_mode_name']==name)
 with TemporaryDirectory() as folder:
  for name in ('auto','normal'):
   v=EmulatedScope();p=Path(folder)/(name+'.json');p.write_text(json.dumps(dict(trigger=dict(mode=name))))
   with patch.object(h,'Scope',return_value=v),patch.object(v,'close'),patch('builtins.print'):
    h.main(['run','--config',str(p)])
   verify('CLI RUN uses the correct native mode: '+name,native_label(v).lower()==name)
  v=EmulatedScope()
  with patch.object(h.time,'sleep'):
   result=h.read_frame(v,Path(folder)/'preview',1,preview_s=.01)
  verify('Untriggered preview actually uses native Auto',native_label(v)=='Auto' and result['settings']['trigger_mode_name']=='auto')
 v=EmulatedScope();v.v.byte(0x17,255)
 try:v.settings()
 except h.ScopeError:verify('Unknown raw mode is refused before measurement',True)
 else:verify('Unknown raw mode is refused before measurement',False)
 report=dict(passed=True,checkCount=len(checks),checks=checks,
             patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hostSHA256=hashlib.sha256(Path(h.__file__).read_bytes()).hexdigest(),hardwareTested=False,
             boundaries='Actual native ARM text-table selection and host USB/KEY path; RTOS/ADC modeled')
 (BUILD/'trigger-mode-validation.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({k:report[k] for k in ('passed','checkCount','patchedSHA256')},indent=2))
if __name__=='__main__':main()
