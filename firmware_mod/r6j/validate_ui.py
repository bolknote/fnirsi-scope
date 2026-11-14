#!/usr/bin/env python3
"""Offline ARM validation. No device, USB or firmware installation is used.

The stock UTF-8/text/font routines run as ARM code. JPEG filesystem loads are
stubbed (the device's SPI-flash images are not part of the application .bin).
An isolated test allocator replaces the stock application's heap allocator.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inspect_runtime import *
import hashlib, json, zlib, binascii
from unicorn import UC_HOOK_CODE
from build_patch import STOCK_SHA256
BUILD=Path(__file__).resolve().parent/"build"

PAT = BUILD/'APP_2C53T_V1.2.0_RU_REMOTE_r6j.bin'
original = FW.read_bytes(); patched = PAT.read_bytes()
report = {'hardwareTested':False, 'bootloaderAcceptanceTested':False, 'checks':[]}

def check(name, condition, **details):
    assert condition, (name, details)
    report['checks'].append({'name':name, 'passed':True, **details})

def png565(path, data, w=320, h=240):
    pixels = struct.unpack('<'+'H'*(w*h),data)
    rgb = bytearray()
    for y in range(h):
        rgb.append(0)
        for p in pixels[y*w:(y+1)*w]:
            rgb.extend((((p>>11)*255)//31, (((p>>5)&63)*255)//63, ((p&31)*255)//31))
    def chunk(tag, content):
        return struct.pack('>I',len(content))+tag+content+struct.pack('>I',binascii.crc32(tag+content)&0xffffffff)
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>2I5B',w,h,8,2,0,0,0))+
                     chunk(b'IDAT',zlib.compress(bytes(rgb)))+chunk(b'IEND',b''))

def cstr(u, pointer):
    return bytes(u.mem_read(pointer,512)).split(b'\0')[0].decode('utf-8')

def screen(blob, pointer, language, focus=1, cursor=0, category=0, clip=(0,0,320,240), mode=None):
    u = runtime(blob); u.mem_map(0x21000000,0x100000)
    u.mem_write(0x20001080,b'\1')  # suppress stock custom-heap release for test allocations
    u.mem_write(0x20001078,bytes(4))
    u.mem_write(0x20008350,struct.pack('<4HI',*clip,0x20040000))
    u.mem_write(0x20040000,struct.pack('<H',0x18c3)*320*240)
    u.mem_write(0x20001058,bytes([language]))
    u.mem_write(0x20001061,bytes([focus,category,cursor]))
    if mode is not None:u.mem_write(0x20001060,bytes([mode]))
    heap = [0x21000000]; records=[]
    def hook(uc,address,size,unused):
        if address == 0x0803acfc:
            n = uc.reg_read(UC_ARM_REG_R0); p = heap[0]; heap[0] += (n+7)&~7
            assert heap[0] < 0x21100000
            uc.reg_write(UC_ARM_REG_R0,p)
            uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
        elif address == 0x08039f6c:
            records.append({'kind':'jpeg', 'pointer':uc.reg_read(UC_ARM_REG_R0),
                            'x':uc.reg_read(UC_ARM_REG_R1), 'y':uc.reg_read(UC_ARM_REG_R2)})
            uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
        elif address == 0x0800f154:
            sp = uc.reg_read(UC_ARM_REG_SP)
            ptr,font,color,flags = struct.unpack('<4I',uc.mem_read(sp,16))
            records.append({'kind':'text', 'text':cstr(uc,ptr),
                            'rect':[uc.reg_read(r) for r in [UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3]],
                            'font':hex(font),'color':color,'flags':flags})
        elif address == 0x08042ee0:
            # Stock live-menu path submits its actual clipped framebuffer here.
            cx,cy,cw,ch,ptr=struct.unpack('<4HI',uc.mem_read(0x20008350,12))
            frame=bytes(uc.mem_read(ptr,cw*ch*2))
            records.append({'kind':'live-framebuffer','rect':[cx,cy,cw,ch],
                            'pixelsSHA256':hashlib.sha256(frame).hexdigest(),
                            'nonzeroBytes':sum(x!=0 for x in frame)})
            uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
        elif address == 0x080423a8:
            # Hardware DMA/semaphore is outside the offline UI test.
            uc.reg_write(UC_ARM_REG_R0,0)
            uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
    u.hook_add(UC_HOOK_CODE,hook)
    saved=[UC_ARM_REG_R4,UC_ARM_REG_R5,UC_ARM_REG_R6,UC_ARM_REG_R7,UC_ARM_REG_R8,UC_ARM_REG_R9,UC_ARM_REG_R10,UC_ARM_REG_R11]
    values=[0x12340000+n*0x101 for n in range(8)]
    for reg,value in zip(saved,values):u.reg_write(reg,value)
    call(u,pointer,[],count=10000000)
    assert [u.reg_read(reg) for reg in saved] == values, 'AAPCS register preservation'
    return records, bytes(u.mem_read(0x20040000,320*240*2))

def fragment(blob, start, ends, language=2, cursor=1, category=0, extra=None):
    u=runtime(blob)
    u.reg_write(UC_ARM_REG_R5,0x200000f8);u.reg_write(UC_ARM_REG_R6,0x200000f8)
    u.reg_write(UC_ARM_REG_R7,0x200000f8);u.reg_write(UC_ARM_REG_R10,0x200000f8)
    u.mem_write(0x20001058,bytes([language]));u.mem_write(0x20001062,bytes([category,cursor]))
    if extra: extra(u)
    def stop(uc,a,s,_):
        if a in ends:uc.emu_stop()
    u.hook_add(UC_HOOK_CODE,stop)
    u.emu_start(start|1,0x081ff000,count=10000)
    assert u.reg_read(UC_ARM_REG_PC) in ends
    return u

def main():
    check('Exact official source hash', hashlib.sha256(original).hexdigest()==STOCK_SHA256)
    a=runtime(original);b=runtime(patched)
    check('Stock startup scatter-loading is unchanged',bytes(a.mem_read(0x20000000,0x80000))==bytes(b.mem_read(0x20000000,0x80000)))
    manifest=json.loads((BUILD/'manifest.json').read_text())
    allowed=set()
    for change in manifest['changes']:
        at=int(change['fileOffset'],16);allowed.update(range(at,at+len(bytes.fromhex(change['before']))))
    differences=[n for n,(x,y) in enumerate(zip(original,patched)) if x!=y]
    check('Every change inside the original image is declared', set(differences)<=allowed,
          changedOriginalBytes=len(differences), appendedBytes=len(patched)-len(original))

    real_strings=[]
    for value in original[0xb4500:].split(b'\0'):
        try:s=value.decode('utf-8')
        except UnicodeError:continue
        if s and any('\u0400'<=c<='\u04ff' for c in s) and all(c.isprintable() or c in '\r\n\t' for c in s):
            real_strings.append(s)
    chars=set('Русский')|set(''.join(real_strings))
    for font in [0x20001114,0x20001144]:
        cb=struct.unpack('<I',b.mem_read(font,4))[0]
        missing=[c for c in sorted(chars) if c not in '\r\n\t' and call(b,cb,[font,0x20050000,ord(c),0])==0]
        check('All stock Russian glyphs exist in font '+hex(font),not missing,
              russianStrings=len(real_strings),characters=len(chars),missing=missing)

    settings=int(manifest['symbols']['settings_extension'],16)
    popup=int(manifest['symbols']['popup_extension'],16)
    for language in [1,2,3]:
        for cursor in [0,1,2]:
            before,pixels0=screen(original,0x0801cf51,language,cursor=cursor)
            after,pixels1=screen(patched,settings,language,cursor=cursor)
            check(f'Settings lang={language} cursor={cursor}: original rows retained',after[:len(before)]==before and (language==3 or pixels0[:190*640]==pixels1[:190*640]))
            ru=[r for r in after if r.get('text')=='Русский']
            check(f'Settings lang={language} cursor={cursor}: Russian row and focus',len(ru)==1 and ru[0]['color']==(0x55f if cursor==2 else 0xffff))
        before,pixels0=screen(original,0x0801f325,language)
        after,pixels1=screen(patched,popup,language)
        check(f'Popup lang={language}: old rows retained and Russian added',after[:len(before)]==before and after[-1].get('text')=='Русский' and (language==3 or pixels0[:170*640]==pixels1[:170*640]))
    # Exercise the real ARM routines with the narrow clipping rectangles as well.
    for pointer,clip in [(settings,(120,24,184,216)),(popup,(68,12,184,216))]:
        records,_=screen(patched,pointer,3,cursor=2,clip=clip)
        check('Russian row with clipped framebuffer '+hex(pointer),records[-1].get('text')=='Русский')

    live_records=[]
    # Regression for r1: invoke the LIVE GUI callbacks, not just draw functions
    # or the screenshot dispatcher. These calls were missed by linear disassembly.
    for address,mode in [(0x08011141,4),(0x0801215d,5)]:
        old,_=screen(original,address,3,cursor=2,mode=mode)
        new,_=screen(patched,address,3,cursor=2,mode=mode)
        check('Live GUI callback reaches Russian row '+hex(address),
              not any(x.get('text')=='Русский' for x in old) and
              any(x.get('text')=='Русский' for x in new))
        check('Live GUI callback submits visible framebuffer '+hex(address),
              len([x for x in new if x['kind']=='live-framebuffer' and x['nonzeroBytes']>0])==1)
        live_records.append({'callback':hex(address),'records':new})

    for language in [1,2,3]:
        u=fragment(patched,0x0800ab12,{0x0800ab22,0x0800b1b2},language=language)
        check('Direct selector language '+str(language),u.mem_read(0x20001058,1)[0]==min(3,language+1))
        u=fragment(patched,0x0800a340,{0x0800a350,0x0800a8ca},language=language)
        check('Direct selector decrement '+str(language),u.mem_read(0x20001058,1)[0]==max(1,language-1))
    for start,end in [(0x0800abc2,0x0800abe0),(0x0800ac9e,0x0800add4)]:
        # First branch exercises the whole increment; second checks its dispatch bound.
        for cursor in [0,1,2]:
            u=fragment(patched,start,{end,0x0800b1b2},cursor=cursor)
            check('Language cursor bound '+hex(start)+' cursor='+str(cursor),
                  (u.reg_read(UC_ARM_REG_PC)==0x0800b1b2)==(cursor==2))
    u=fragment(patched,0x0800beca,{0x0800bee0},cursor=2)
    check('Confirming row 3 sets original Russian language ID',u.mem_read(0x20001058,1)==b'\3')
    # The unchanged save/restore ARM instructions carry all four settings bytes.
    u=fragment(patched,0x0802962a,{0x08029632},language=3,
               extra=lambda u:u.reg_write(UC_ARM_REG_R10,0x20060000))
    check('Original settings save packs language ID 3',u.mem_read(0x2006002c,1)==b'\3')
    def restore(u):
        u.reg_write(UC_ARM_REG_R4,0x20060000)
        u.mem_write(0x2006002c,struct.pack('<II',0x00016403,0))
    u=fragment(patched,0x0802ce40,{0x0802ce4c},extra=restore)
    check('Original settings restore retains Russian ID',u.mem_read(0x20001058,1)==b'\3')

    records,pixels=screen(patched,settings,3,cursor=2)
    png565(BUILD/'russian-rows-emulated.png',pixels)
    (BUILD/'settings-render-calls.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
    (BUILD/'live-menu-render-calls.json').write_text(json.dumps(live_records,ensure_ascii=False,indent=2)+'\n')
    report['passed']=True;report['checkCount']=len(report['checks'])
    report['patchedSHA256']=hashlib.sha256(patched).hexdigest()
    report['limitations']=['No physical-device test or stock-bootloader acceptance test',
        'JPEG assets omitted from framebuffer preview; ARM text rendering is real',
        'Settings flash erase/program and hardware/ADC tasks are not emulated']
    (BUILD/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':True,'checks':len(report['checks']),'patchedSHA256':report['patchedSHA256']},indent=2))

if __name__=='__main__':main()
