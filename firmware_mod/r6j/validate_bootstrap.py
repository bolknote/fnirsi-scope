"""Native, bounded read-only diagnostics with the optional context absent."""
from pathlib import Path
import hashlib,json,struct
from unicorn import UC_HOOK_MEM_WRITE
from validate_diagnostic import VM,BLOB,BUILD,G,SYM,call,p32

def main():
    v=VM();u=v.u;checks=[];writes=[]
    p32(u,G,0)
    u.hook_add(UC_HOOK_MEM_WRITE,lambda uc,access,address,size,value,data:writes.append((address,size)),begin=0x08000000,end=0x080fffff)
    u.hook_add(UC_HOOK_MEM_WRITE,lambda uc,access,address,size,value,data:writes.append((address,size)),begin=0x40022000,end=0x400227ff)
    def test(name,ok):
        assert ok,name
        checks.append(dict(name=name,passed=True))
    def setup(request,offset,n,bm=0xc0):
        v.packets=[];u.mem_write(v.req,struct.pack('<BBHHH',bm,request,offset&65535,offset>>16,n))
        return call(u,SYM['r5_usb_setup'],[v.ctx,v.req])
    for req,base,size in ((0x5d,0x08000000,0x100000),(0x5e,0x20000000,0x38000)):
        for offset,n in ((0,1),(0x7000,64),(0xffff,64),(size-64,64),(size-1,1)):
            test(f'Context-free {req:02x} read {offset:x}+{n}',setup(req,offset,n)==1 and v.packets==[(0,bytes(u.mem_read(base+offset,n)))])
        for offset,n in ((size,1),(size-1,2),(0,0),(0,65),(0,65535),(0xffffffff,64)):
            test(f'Out-of-range {req:02x} {offset:x}+{n} rejected',setup(req,offset,n)==0 and not v.packets)
        test(f'OUT {req:02x} refused',setup(req,0,64,0x40)==0 and not v.packets)
    test('No flash or FMC writes during bootstrap reads',not writes)
    # The existing diagnostic disk must also be usable for pure memory reads
    # when the optional acquisition context is unavailable.
    c=bytearray(10);c[0]=0x28;c[8]=1;v.cdb(0x28,length=512,cdb=c)
    test('Diagnostic boot sector readable without guard',v.dispatch()==0 and v.packets[-1][1][510:512]==b'\x55\xaa')
    report=dict(passed=True,checkCount=len(checks),checks=checks,patchedSHA256=hashlib.sha256(BLOB).hexdigest(),hardwareTested=False)
    (BUILD/'bootstrap-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(passed=True,checks=len(checks))))
if __name__=='__main__':main()
