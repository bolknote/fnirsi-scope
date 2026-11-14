#!/usr/bin/env python3
"""Offline, stock-hash-locked build. Does not enumerate USB or flash hardware."""
from pathlib import Path
import argparse,sys,struct,json,hashlib,subprocess
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from build_patch import BASE,SOURCE,STOCK_SHA256,align,elf_link,load_stock,thumb_branch
from build_startup import ASSET,embed_startup
from capstone import Cs,CS_ARCH_ARM,CS_MODE_THUMB,CS_MODE_MCLASS
from generate_resources import generate_resources
HERE=Path(__file__).resolve().parent;BUILD=HERE/'build'
IMAGE_NAME='APP_2C53T_V1.2.0_RU_REMOTE_r6j.bin'


# Only straight-line, position-independent instructions can be replayed verbatim.
# PC-relative calls use explicit bridges in hooks.c.
REPLAY = {
    'POOL_INIT': (0x0803ab10, 6),
    'SETTINGS_SAVE': (0x080293bc, 6),
    'SECOND_PUSH': (0x080406c8, 2),
    'CAL_STEP': (0x08022ae4, 6),
    'CAL_AFTER_STEP': (0x080240a4, 6),
    'KEY': (0x08040074, 8),
    'REMOTE_KEY': (0x08040058, 8),
    'BOOT': (0x0802d1a8, 8),
    'SPI_BEGIN': (0x0803e4c2, 8),
    'SPI_BEGIN_OFFSET': (0x0803e4c6, 4),
    'GAIN_AUTO': (0x0800d970, 8),
    'GAIN_AUTO_R5_LOW': (0x0800d974, 4),
    'GAIN_AUTO_R5_HIGH': (0x0800d97c, 4),
    'GAIN_AUTO_R8_LOW': (0x0800d978, 4),
    'GAIN_AUTO_R8_HIGH': (0x0800d980, 4),
    'GAIN_ROLLING': (0x0803eb34, 8),
    'GAIN_ROLLING_STORE': (0x0803eb38, 4),
    'GAIN_CH1': (0x0803edc0, 8),
    'GAIN_CH1_FINISH': (0x0803edc4, 4),
    'GAIN_CH2': (0x0803f090, 8),
    'GAIN_CH2_FINISH': (0x0803f094, 4),
    'SPI_END_MASK': (0x0803e4a2, 2),
    'SPI_END_QUEUE': (0x0803e4a4, 4),
    'SPI_END_RELEASE': (0x0803e4a8, 2),
    'NOMINAL': (0x0802d1be, 8),
    'SCSI': (0x0803020c, 6),
    'USB_SETUP': (0x08031078, 6),
    'TEXT': (0x0800f154, 6),
    'SCOPE_PANEL': (0x08013338, 6),
    'IMAGE': (0x08039f6c, 6),
    'CAPTURE_COMPLETE': (0x0802161c, 6),
}


def write_replay_header(stock):
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    definitions = ['/* Generated locally from the verified input firmware. */']
    for name, (address, size) in REPLAY.items():
        original = stock[address - BASE:address - BASE + size]
        instructions = list(decoder.disasm(original, address))
        if sum(ins.size for ins in instructions) != size:
            raise ValueError(f'Incomplete Thumb instructions at {address:#010x}')
        assembly = []
        for ins in instructions:
            words = struct.unpack('<' + 'H' * (ins.size // 2), ins.bytes)
            if ins.size == 2:
                assembly.append(f'.inst.n 0x{words[0]:04x}\\n')
            else:
                assembly.append(f'.inst.w 0x{words[0]:04x}{words[1]:04x}\\n')
        definitions.append(f'#define STOCK_{name} "{"".join(assembly)}"')
    (BUILD / 'stock_replay.h').write_text('\n'.join(definitions) + '\n', encoding='ascii')

def spi_loops(blob):
 md=Cs(CS_ARCH_ARM,CS_MODE_THUMB|CS_MODE_MCLASS);out=[]
 allowed={'ldr','ldr.w','ldrpl','ldreq','lsls','lsls.w','lslspl.w','lslseq.w','it','itt','ittt','itttt','nop','bmi','bmi.w','bne','bne.w','bpl','bpl.w','beq','beq.w'}
 for a in range(0x0803e4c2,0x08040008,2):
  ins=list(md.disasm(blob[a-BASE:a-BASE+4],a,count=1))
  if not ins:continue
  i=ins[0]
  if i.mnemonic not in ('bpl','bpl.w','beq','beq.w'):continue
  try:t=int(i.op_str[1:],16)
  except ValueError:continue
  if not 6<=a-t<=70:continue
  body=list(md.disasm(blob[t-BASE:a+i.size-BASE],t))
  if not body or body[-1].address!=a or not all(z.mnemonic in allowed for z in body):continue
  shifts=[z for z in body if z.mnemonic.startswith('lsl')]
  if not shifts or not all('#0x1e' in z.op_str or '#0x1f' in z.op_str for z in shifts):continue
  if not all('[r7]' in z.op_str for z in body if z.mnemonic.startswith('ldr')):continue
  assert all(z.op_str.split(',')[0]==shifts[-1].op_str.split(',')[0] for z in shifts)
  ready=next(int(z.op_str[1:],16) for z in body if z.mnemonic in ('bmi','bmi.w','bne','bne.w'))
  out.append(dict(start=t,end=a+i.size,ready=ready,reg=shifts[-1].op_str.split(',')[0],bit=1 if '#0x1e' in shifts[-1].op_str else 0))
 assert len(out)==48,('SPI loop coverage changed',len(out))
 assert all(x['end']<=y['start'] for x,y in zip(out,out[1:]))
 return out

def build(stock, stock_path, clang, artwork):
 BUILD.mkdir(exist_ok=True)
 loops=spi_loops(stock)
 generated=['/* Generated from all 48 exact SPI3 ready loops. */']
 for i,q in enumerate(loops):
  shift=30 if q['bit'] else 31
  asm=(f'movs r0,#{q["bit"]}\\n bl guard_spi_wait\\n cmp r0,#0\\n beq 1f\\n')
  end=f'ldr {q["reg"]},[r7]\\n lsls {q["reg"]},{q["reg"]},#{shift}\\n ldr pc,=0x{q["ready"]|1:08x}\\n'
  # The common ready case must not push an FP context or call C per byte.
  # Replay the native status test first. The C deadline is only a fallback
  # while the peripheral is not ready; it still bounds a stuck SPI wait.
  branch='bmi' if q['bit'] else 'bne'
  fast=f'ldr {q["reg"]},[r7]\\n lsls {q["reg"]},{q["reg"]},#{shift}\\n {branch} 2f\\n'
  generated.append('NAKED void spi_wait_%d(void){__asm__ volatile("%s" SAVE "%s" RESTORE "%s1:\\n" RESTORE "ldr pc,=0x0803e49b\\n2:\\n ldr pc,=0x%08x\\n .ltorg\\n");}'%(i,fast,asm,end,q['ready']|1))
 (BUILD/'spi_hooks.c').write_text('\n'.join(generated)+'\n')
 combined=BUILD/'extension_all.c'
 write_replay_header(stock)
 generate_resources(stock)
 combined.write_text('#include "../../menu_extension.c"\n#include "resources.c"\n#include "../features.h"\n#include "../csv_extension.c"\n#include "../guard.c"\n#include "../hooks.c"\n#include "spi_hooks.c"\n#include "../localization.c"\n#include "../autocapture.c"\n#include "../control_usb.c"\n#include "../remote.c"\n#include "../diagnostic_usb.c"\n')
 obj=BUILD/'extension_all.o'
 subprocess.run([clang,'--target=arm-none-eabi','-mcpu=cortex-m4','-mthumb','-mfpu=fpv4-sp-d16','-mfloat-abi=softfp','-ffp-contract=off','-mno-movt','-Oz','-ffreestanding','-fno-builtin','-fno-unwind-tables','-fno-asynchronous-unwind-tables','-fstack-usage','-c',str(combined),'-o',str(obj)],check=True)
 tail=align(BASE+len(stock),16);payload,symbols,relocs=elf_link(obj.read_bytes(),tail)
 patched=bytearray(stock);changes=[]
 def patch(a,old,new,why):
  if patched[a-BASE:a-BASE+len(old)]!=old or len(old)!=len(new):
   raise ValueError(f'Unexpected original bytes or patch length at {a:#010x}')
  patched[a-BASE:a-BASE+len(old)]=new
  changes.append(dict(address=hex(a),fileOffset=hex(a-BASE),before=old.hex(),after=new.hex(),reason=why))
 def hook(a,n,sym,why):
  patch(a,stock[a-BASE:a-BASE+n],thumb_branch(a,symbols[sym],False)+b'\x00\xbf'*((n-4)//2),why)
 def call(a,target,sym,why):
  patch(a,thumb_branch(a,target),thumb_branch(a,symbols[sym]),why)
 patch(0x0800ab16,stock[0x0800ab16-BASE:0x0800ab18-BASE],b'\x02\x28','Third language ID')
 for a in (0x0800b1d8,0x0800b1f0):patch(a,stock[a-BASE:a-BASE+1],b'\2','Third language cursor')
 for a,old,sym in [(0x0804c150,0x0801cf51,'settings_extension'),(0x0804c17c,0x0801f325,'popup_extension'),(0x08046584,0x0800df59,'guard_start')]:
  patch(a,struct.pack('<I',old),struct.pack('<I',symbols[sym]),sym)
 call(0x080111da,0x0801cf50,'settings_extension','Live settings redraw')
 call(0x080121e6,0x0801f324,'popup_extension','Live language redraw')
 hook(0x08011660,6,'save_extension','SAVE diagnostics; CSV in separate directory')
 hook(0x0803ab10,6,'guard_pool_init','Reserve a permanent 1024-byte guard context from the main image pool')
 call(0x0802b144,0x08035f48,'hook_boot_pool','Reserve guard/capture contexts on the real inline cold-boot allocator path, before native hardware initialization')
 hook(0x08022ae4,6,'hook_step','Preflight next coefficient before native arithmetic; no unsigned wrap')
 hook(0x080240a4,6,'hook_after_step','Finish atomic calibration step and honor deferred cancellation')
 hook(0x08040074,8,'hook_key','Allow MENU cancellation and Power during calibration')
 hook(0x08040058,8,'hook_remote_key','Typed USB control executes on the original KEY task before the native key table')
 patch(0x0802ccf2,stock[0x0802ccf2-BASE:0x0802ccf4-BASE],b'\xf0\x22','KEY task stack: 240 words (960 bytes), including remote hook and Cortex-M4 FP context margin')
 hook(0x0802d1a8,8,'hook_boot','Validate all coefficients and low/high ordering at boot')
 hook(0x0802d1be,8,'hook_nominal','Nominal fallback after invalid settings header also requires calibration')
 hook(0x080293bc,6,'guard_before_settings_save','Restore backup before native shutdown writer; no partial calibration persisted')
 hook(0x080406c8,6,'guard_second','Calibration deadline/fresh-data/progress watchdog; suppress idle shutdown')
 call(0x08023ff6,0x08025fc0,'guard_finish','Validate complete table; original measurement refresh is NOT a settings save')
 call(0x0801c972,0x0800f154,'guard_progress_text','Calibration progress, means, elapsed time, MENU hint')
 call(0x0801c8b6,0x0800f154,'guard_notice_text','Display calibration reason or precise SAVE stage/code')
 hook(0x0803b878,6,'guard_scan_album','Only numeric BIN entries enter album; ignore legacy r2 CSV companions')
 hook(0x0803e4c2,8,'hook_spi_begin','SPI deadline and native trigger bypass when either gain is 0, 28 or 255')
 hook(0x0800d970,8,'hook_gain_auto','50% centering retains nominal gain instead of learning it from an unknown input')
 hook(0x0803eb34,8,'hook_gain_rolling','Extend native invalid-gain bypass to zero divisor 28 in rolling acquisition')
 hook(0x0803edc0,8,'hook_gain_ch1','Preserve CH1 samples instead of dividing by zero gain')
 hook(0x0803f090,8,'hook_gain_ch2','Preserve CH2 samples instead of dividing by zero gain')
 hook(0x0803e4a2,8,'hook_spi_end','Release native chip select, then count completed SPI commands and sample reads')
 hook(0x0803020c,6,'diagnostic_scsi','Second read-only diagnostic LUN; native LUN0 retained')
 hook(0x08031078,6,'diagnostic_usb_setup','Advertise two logical units through GET_MAX_LUN')
 patch(0x08046539,stock[0x08046539-BASE:0x0804653a-BASE],b'\x18','Unused long-CH1 event -> extension key 24; short press and all other keys retained')
 hook(0x0800f154,6,'r5_text','Correct and complete Russian text with a complete licensed glyph subset')
 hook(0x08013338,6,'r5_scope_panel','Five real channel settings including digital 20 MHz low-pass')
 hook(0x08039f6c,6,'r5_image','Translate embedded RUN/STOP/REF image captions')
 call(0x080400b2,0x08020e98,'r5_scope_service','Completed single-shot snapshot and bounded capture state machine')
 hook(0x0802161c,6,'hook_capture_complete','Mark actual native SINGLE completion, never a manual STOP')
 for a in (0x08021388,0x08021618,0x0802427e):call(a,0x080242ec,'r5_timebase','Digital low-pass after fresh screen-point reconstruction; native raw banks retained')
 patch(0x0803da7c,stock[0x0803da7c-BASE:0x0803da82-BASE],thumb_branch(0x0803da7c,symbols['r5_gui_dispatch'])+b'\0\xbf','Dedicated extension GUI event 126 serialized with the native UI and SAVE')
 for i,q in enumerate(loops):hook(q['start'],q['end']-q['start'],f'spi_wait_{i}','Bounded SPI3 TX/RX ready wait')
 patched.extend(b'\xff'*(tail-BASE-len(patched)));patched.extend(payload);patched.extend(b'\xff'*(align(len(patched),128)-len(patched)))
 patched,startup=embed_startup(patched,stock,artwork,clang)
 changes.extend(startup['changes'])
 if align(BASE+len(patched),2048)>0x080ff800:
  raise ValueError('Application would overlap the factory IAP completion page')
 output=BUILD/IMAGE_NAME
 manifest=dict(status='built-locally',revision='1.2.0-RU-REMOTE-r6j',baseAddress=hex(BASE),stockFile=str(stock_path),stockSHA256=STOCK_SHA256,stockBytes=len(stock),patchedFile=str(output),patchedSHA256=hashlib.sha256(patched).hexdigest(),patchedBytes=len(patched),payloadAddress=hex(tail),payloadBytes=len(payload),symbols={k:hex(v) for k,v in symbols.items()},changes=changes,relocations=relocs,spiWaits=loops,hardwareFlashed=False,bootloaderAcceptanceTested=False,csvDirectory='Waveform CSV',contextAddress=hex(0x20033bc0),contextReservationBytes=1024,policyTimeouts=dict(noFreshDataSeconds=10,noProgressSeconds=60,totalSeconds=600,spiReadyMilliseconds=100,spiIterationLimit=1000000,calibrationSPICommandMilliseconds=1000))
 manifest['features']={'autocapture':True,'completeRussianAlphabet':True,'translations':len(json.loads((HERE/'translations.json').read_text())),'bandwidthLimit':'20 MHz digital screen-data low-pass; analog input, raw banks and hardware trigger unchanged','captureTimebases':'10 ns/div to 20 ms/div; stock completed SINGLE path','usbControlVersion':0x00050001,'remoteControlVersion':0x00060001,'remoteControl':'Typed channel, timebase, trigger, RUN/STOP and freeze on native KEY task; 0x5C settings readback','contextExtraBytes':4096,'contextTotalBytes':5120,'longCH1Event':24,'controlPanel':'Hold CH1; arrows select/change; PLAY arms; MENU returns','keyTaskStackBytes':960,'extraKeyStackBytes':448}
 manifest['features'].update(spiReadyFastPath=True,completedSPIReadCounter=True,previewRequiresNewSampleRead=True,
                             fixedRegisterProbeRequest=0x5f,fixedRegisterProbeProtocol=0x00060002,
                             zeroGainDivisorGuard=True,invalidGainBytes=list(range(29))+[255],invalidGainPolicy='Native uncompensated path for zero/negative divisor; baseline calibration uses uncorrected endpoints; gain remains nominal until verified',
                             centeringCannotLearnGain=True,baselineCalibrationIndependentOfGain=True)
 manifest['startup']=startup
 manifest['diagnosticUSB']={'lun':1,'volume':'2C53T DIAG','logicalBlockBytes':512,'logicalBlocks':65536,'readOnly':True,'physicalUSBTested':False,'sourceInspiration':'https://github.com/maksidze/FNIRSI-2C53T-flash-dump/tree/acd86dbef1c63b158047219ffe135f0d6fe01dbb','implementation':'Independent FAT16 generator and adapter to the native stock BOT/SCSI/USB stack; no standalone USB stack transplanted'}
 # Replace the finished image only after compilation and validation succeed.
 temporary=output.with_suffix('.tmp')
 temporary.write_bytes(patched)
 temporary.replace(output)
 (BUILD/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
 print(json.dumps({k:manifest[k] for k in ('revision','patchedSHA256','patchedBytes','payloadBytes')},indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stock',nargs='?',default=SOURCE,type=Path,help='Official 2C53T 1.2.0 BIN from FNIRSI')
    parser.add_argument('--clang',default='clang',help='Clang executable with ARM target support')
    parser.add_argument('--artwork',default=ASSET,type=Path,help='Optional 320 × 240 startup image')
    args=parser.parse_args()
    try:
        stock_path=args.stock.resolve()
        if stock_path == (BUILD/IMAGE_NAME).resolve():
            raise ValueError('Choose the stock image as input, outside the build output')
        build(load_stock(stock_path), stock_path, args.clang, args.artwork.resolve())
    except (ValueError,OSError,subprocess.CalledProcessError) as error:
        parser.exit(1,f'Build failed: {error}\n')


if __name__=='__main__':
    main()
