#!/usr/bin/env python3
"""2C53T stock IAP on macOS: explicit, aligned, one-pass application upload.

probe/plan/verify are read-only. Only `flash` opens the verified IAP raw device
for writing. The factory bootloader is NOT replaced. No Finder/mtools/FAT driver
is used for writing. Firmware and bootloader readback remain separate checks.
"""
from __future__ import annotations
import argparse,hashlib,json,os,platform,plistlib,re,stat,struct,subprocess,sys,time
from pathlib import Path

SECTOR=2048
APP=0x08007000
FLAG=0x080ff800
BOOT_CODE_SHA='7a6201ee16ce155675f1d235e468edfdce7a736318f2591d54a221d4ad06566b'
STOCK_SHA='a17c5c35c97bb898f15672a1747bc1041d8ed507c16999ddba0d1e4e2ec0c760'
R5_SHA='99d47ada7a67f02cfaa52dcc195a61c3ed49904d31e838a1607b69a3a720b9d6'
R6_SHA='9733115928fb56bbf65c7afefcce6fa19e1ed1bfd6908b0bdbb57e3c5b91202f'
R6D_SHA='a285603c8713000309bd761cc133f04b9d1048cbcce4509d03fe9995a3122037'
R6E_SHA='494259f15ece0ceabbb8dd881931be0ac2f38a513e585d77047f6204329e0e65'
R6F_SHA='049b7899daa69bb13b1b79eb3f125e992ac16b69db38d9472d29035d4693c0ac'
R6G_SHA='ed59c5c1cf3c074541f408fe68d7378c8061a0186bf6198ffe965c3f6ea2526d'
R6H_SHA='e3a440f582ff9b41f6b3b28a92c683558b87e01a9da389e7e8ea8fe7f9c7a18b'
R6I_SHA='a0ae2e9eb312d3e1551c2ae08a4ae3a53133456834047f36e90ce87518c1ca3c'
R6J_SHA='8cd9a9bf391924aac9a9942dcec865ef0c694889fd366ecf5dcaff79ee37c7bf'
KNOWN_IMAGES={STOCK_SHA:'official 1.2.0',R5_SHA:'RU CAPTURE r5',R6_SHA:'RU REMOTE r6',R6D_SHA:'RU REMOTE r6d diagnostics',R6E_SHA:'RU REMOTE r6e boot fix',R6F_SHA:'RU REMOTE r6f acquisition diagnostics',R6G_SHA:'RU REMOTE r6g stock-SPI A/B diagnostic',R6H_SHA:'RU REMOTE r6h zero gain divisor fix',R6I_SHA:'RU REMOTE r6i independent baseline calibration and nominal gain',R6J_SHA:'RU REMOTE r6j with community startup screen'}
ROOT_LBA=8
DATA_LBA=20
IAP_DEVICE_BYTES=(4080*SECTOR,4095*SECTOR,4096*SECTOR)

class Refused(RuntimeError):pass
def require(condition,message):
    if not condition:raise Refused(message)
def digest(b):return hashlib.sha256(b).hexdigest()
def json_out(p,value):
    Path(p).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def image_bytes(path):
    b=Path(path).read_bytes();d=digest(b)
    require(d in KNOWN_IMAGES,'Unreviewed image SHA-256. Only official 1.2.0 or our exact r5/r6 binaries are accepted.')
    require(8<=len(b)<=FLAG-APP,'Image overlaps the factory completion-flag page or is empty.')
    sp,reset=struct.unpack_from('<II',b)
    require(0x20000000<sp<=0x20038000 and sp%8==0 and reset&1 and APP<=reset&~1<APP+len(b),'Invalid 2C53T application vector table.')
    return b
def backup_bytes(path):
    b=Path(path).read_bytes()
    require(len(b)==0x100000,'MCU backup must be the full 1 MiB MCUFLASH.BIN from this instrument.')
    require(digest(b[:0x6000])==BOOT_CODE_SHA,'Different factory bootloader: this transfer path has not been reviewed for that code.')
    return b
def entries(root):
    return [root[o:o+32] for o in range(0,len(root),32) if root[o] not in (0,0xe5)]
def validate_snapshot(snapshot,total_size=None):
    """Require the actual bootloader's generated geometry, not a volume name."""
    require(len(snapshot)>=0x8000,'IAP snapshot is shorter than its metadata.')
    b=snapshot[:SECTOR]
    fields=(struct.unpack_from('<H',b,11)[0],b[13],struct.unpack_from('<H',b,14)[0],b[16],
            struct.unpack_from('<H',b,17)[0],struct.unpack_from('<H',b,19)[0],b[21],struct.unpack_from('<H',b,22)[0])
    require(fields==(2048,4,6,2,512,4095,248,1),'Not the reviewed stock 2C53T IAP geometry.')
    require(b[:11]==b'\xeb\x3c\x90MSDOS5.0' and b[510:512]==b'\x55\xaa','IAP boot signature mismatch.')
    require(snapshot[0x3000:0x3004]==bytes.fromhex('f8ffffff') and snapshot[0x3800:0x3804]==bytes.fromhex('f8ffffff'),'IAP reserved FAT signatures differ.')
    root=snapshot[0x4000:0x8000];live=entries(root)
    require(len(live)==2 and root[:11].upper()==b'READY   TXT' and root[11]==0x20 and
            root[32:43]==b'IAP        ' and root[43]==8 and not any(root[64:]),
            'IAP is not fresh READY with its exact two initial entries. Re-enter MENU + Power.')
    require(not struct.unpack_from('<I',root,28)[0],'Unexpected READY file data.')
    # Real 2C53T reports 4080 sectors through SCSI, while its generated BPB
    # declares 4095. The live 32 KiB metadata is byte-identical to the native
    # bootloader emulation; do not infer an arbitrary size from the FAT header.
    if total_size is not None:require(total_size in IAP_DEVICE_BYTES,'IAP physical disk size differs.')
    return dict(sectorBytes=2048,totalSectorsBPB=4095,sectorsPerCluster=4,rootLBA=ROOT_LBA,dataLBA=DATA_LBA,
                metadataSHA256=digest(snapshot[:0x8000]))
def transfer_plan(snapshot,firmware):
    info=validate_snapshot(snapshot)
    require(digest(firmware) in KNOWN_IMAGES,'Firmware hash not approved for this plan.')
    # Native root parser skips the Ready and IAP entries and accepts the first
    # ordinary file. A special Axxxxxx name chooses a flash address; never use
    # it. APP     BIN deliberately fails that special address-name grammar.
    root=bytearray(snapshot[0x4000:0x4800]);root[64:]=bytes(SECTOR-64)
    root[64:75]=b'APP     BIN';root[75]=0x20
    struct.pack_into('<H',root,64+26,3);struct.pack_into('<I',root,64+28,len(firmware))
    # The reviewed IAP ignores FAT-chain writes and appends data-sector writes
    # to its internal app pointer. This is its transfer protocol, not a disk
    # image to mount. Never rewrite the whole fake disk or retry a data block.
    writes=[(ROOT_LBA,bytes(root))]
    for offset in range(0,len(firmware),SECTOR):
        writes.append((DATA_LBA+offset//SECTOR,firmware[offset:offset+SECTOR].ljust(SECTOR,b'\xff')))
    require(all(n>=DATA_LBA for n,_ in writes[1:]),'Invalid transfer LBA.')
    require(DATA_LBA+len(writes)-1<=4095,'Transfer exceeds IAP volume.')
    return writes,dict(**info,imageSHA256=digest(firmware),imageBytes=len(firmware),
                      imageName=KNOWN_IMAGES[digest(firmware)],fileNameOnIAP='APP.BIN',
                      applicationStart=hex(APP),applicationEndExclusive=hex(APP+len(firmware)),
                      preservedCalibrationPage='0x08006000..0x08006fff',
                      stockCompletionFlagPage='0x080ff800..0x080fffff',
                      writeSectors=len(writes),writeOrder='one root sector, then strictly increasing app data sectors',
                      bootloaderModified=False,hardwareTested=False)

def run_plist(argv):
    r=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    require(r.returncode==0,argv[0]+': '+r.stderr.decode(errors='replace').strip())
    try:return plistlib.loads(r.stdout)
    except Exception as e:raise Refused('Invalid plist from '+argv[0]) from e
def descendants(value):
    if isinstance(value,list):
        for x in value:yield from descendants(x)
    elif isinstance(value,dict):
        yield value;yield from descendants(value.get('IORegistryEntryChildren',[]))
def usb_disks(tree):
    """Only BSD whole disks descended from the exact vendor/product pair."""
    result={}
    for device in descendants(tree):
        if device.get('idVendor')!=0x2e3c or device.get('idProduct')!=0x5720:continue
        identity={k:device.get(k) for k in ('IORegistryEntryID','USB Serial Number','USB Product Name')}
        for child in descendants(device):
            name=child.get('BSD Name','')
            if isinstance(name,str) and re.fullmatch(r'disk\d+',name):result[name]=identity
    return result
def validate_info(name,info):
    require(re.fullmatch(r'disk\d+',name) is not None,'Not a whole-disk identifier.')
    whole=info.get('WholeDisk',info.get('Whole'))
    require(not ('WholeDisk' in info and 'Whole' in info and info['WholeDisk']!=info['Whole']),'Conflicting whole-disk properties.')
    require(info.get('DeviceIdentifier')==name and whole is True and
            info.get('Internal') is False and info.get('BusProtocol')=='USB',
            'Target is not an external USB whole disk.')
    require(info.get('TotalSize') in IAP_DEVICE_BYTES,'Target size is not the reviewed 8 MiB IAP.')
    require(info.get('DeviceBlockSize')==SECTOR,'Target logical sector size is not 2048 bytes.')
    require(not info.get('VirtualOrPhysical') or info.get('VirtualOrPhysical')=='Physical','Virtual disk refused.')
def candidates():
    require(platform.system()=='Darwin','Live probe/flash is implemented only for macOS; plan/verify are portable.')
    tree=run_plist(['/usr/sbin/ioreg','-a','-l','-r','-c','IOUSBHostDevice'])
    found=[]
    for name,identity in usb_disks(tree).items():
        info=run_plist(['/usr/sbin/diskutil','info','-plist','/dev/'+name])
        try:validate_info(name,info)
        except Refused:continue
        found.append(dict(device=name,identity=identity,info=info))
    return found
def choose(device=None):
    found=candidates()
    if device:
        name=device.removeprefix('/dev/')
        found=[d for d in found if d['device']==name]
    require(len(found)==1,'No single verified 2C53T IAP disk. Enter MENU + Power; for several devices specify --device diskN.')
    return found[0]
def read_exact(fd,n,offset):
    require(n%SECTOR==0 and offset%SECTOR==0,'Unaligned raw read.')
    b=os.pread(fd,n,offset);require(len(b)==n,'Short raw read.');return b
def read_snapshot(target):
    raw='/dev/r'+target['device']
    require(Path(raw).resolve()==Path(raw),'Raw device symlink refused.')
    fd=os.open(raw,os.O_RDONLY)
    try:
        require(stat.S_ISCHR(os.fstat(fd).st_mode),'Expected a raw character device.')
        snapshot=b''.join(read_exact(fd,SECTOR,o) for o in range(0,0x8000,SECTOR))
    finally:os.close(fd)
    validate_snapshot(snapshot,target['info']['TotalSize']);return snapshot
def verify_readback(image_path,mcu_path,backup_path=None):
    firmware=image_bytes(image_path);mcu=Path(mcu_path).read_bytes()
    require(len(mcu)==0x100000,'Readback must be a full 1 MiB MCUFLASH.BIN.')
    require(mcu[0x7000:0x7000+len(firmware)]==firmware,'Application readback differs from the selected firmware.')
    require(digest(mcu[:0x6000])==BOOT_CODE_SHA,'Bootloader readback differs from the reviewed factory code.')
    report=dict(status='application-readback-verified',imageSHA256=digest(firmware),bootloaderUnchanged=True)
    if backup_path:
        backup=backup_bytes(backup_path)
        require(mcu[0x6000:0x6130]==backup[0x6000:0x6130],'Calibration record changed: compare the before/after backups.')
        report['calibrationRecordUnchanged']=True;report['wholeSettingsPageUnchanged']=mcu[0x6000:0x7000]==backup[0x6000:0x7000]
    return report
def flash(args):
    # All validation, root metadata preparation and firmware bytes precede
    # the user's final confirmation. No source file is reread during upload.
    firmware=image_bytes(args.image)
    backup_path=getattr(args,'backup',None);without_backup=getattr(args,'without_backup',False)
    require(bool(backup_path)!=bool(without_backup),'Provide --backup, or explicitly choose --without-backup.')
    backup=backup_bytes(backup_path) if backup_path else None
    report_path=Path(args.report).expanduser().resolve()
    require(not str(report_path).startswith(('/Volumes/','/dev/')),'Report must be stored on the computer.')
    require(not report_path.exists(),'Report file already exists; use a new path.')
    target=choose(args.device);snapshot=read_snapshot(target)
    writes,plan=transfer_plan(snapshot,firmware)
    report=dict(plan,device=target['device'],usbIdentity=target['identity'],status='prepared',
                sectorsWritten=0,writeAcknowledged=False,applicationReadbackVerified=False,
                backupSHA256=digest(backup) if backup is not None else None,
                calibrationRecordSHA256=digest(backup[0x6000:0x6130]) if backup is not None else None,
                backupWaivedByUser=without_backup,bootloaderReadbackCheckedBeforeFlash=backup is not None)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    confirmation='FLASH '+target['device']
    confirmed_device=getattr(args,'confirm_device',None)
    if confirmed_device is not None:require(confirmed_device==target['device'],'Confirmed device differs from the detected IAP target.')
    if confirmed_device is None and input('Type '+confirmation+' to upload this exact image; anything else cancels: ').strip()!=confirmation:
        report['status']='cancelled-before-write';json_out(report_path,report);return report
    require(os.geteuid()==0,'Raw IAP writing needs root: run this command with sudo.')
    fd=None
    try:
        json_out(report_path,report)
        subprocess.run(['/usr/sbin/diskutil','unmountDisk','/dev/'+target['device']],check=True)
        again=choose(target['device'])
        require(again['identity']==target['identity'],'USB target changed after preparation.')
        require(not again['info'].get('Mounted') and not again['info'].get('MountPoint'),'IAP is still mounted.')
        require(read_snapshot(again)==snapshot,'IAP metadata changed after preparation. Re-enter upgrade mode.')
        raw='/dev/r'+target['device'];require(Path(raw).resolve()==Path(raw),'Raw device symlink refused.')
        fd=os.open(raw,os.O_RDWR)
        require(stat.S_ISCHR(os.fstat(fd).st_mode),'Expected a raw character device.')
        require(b''.join(read_exact(fd,SECTOR,o) for o in range(0,0x8000,SECTOR))==snapshot,'IAP metadata changed before writing.')
        report['status']='uploading';json_out(report_path,report)
        for index,(lba,data) in enumerate(writes):
            # A short/failed write is never retried: native IAP appends by an
            # internal byte counter and would treat a repeated block as new data.
            report['lastAttemptedLBA']=lba
            n=os.pwrite(fd,data,lba*SECTOR)
            require(n==SECTOR,'Short write. STOP: do not retry a block within this IAP session.')
            report['sectorsWritten']=index+1
            if index%32==0:print(f'USB sectors acknowledged: {index+1}/{len(writes)}',flush=True)
        report['writeAcknowledged']=True;report['status']='uploaded-unverified'
        try:
            root=read_exact(fd,SECTOR,ROOT_LBA*SECTOR)
            report['iapMarker']=root[:8].decode('ascii',errors='replace').strip()
            report['iapReportedSuccess']=report['iapMarker'].upper()=='SUCCESS'
        except OSError as e:report['markerReadError']=str(e)
        print('UPLOAD WRITES ACKNOWLEDGED. Firmware readback is still required; USB disappearance alone is not success.',flush=True)
    except BaseException as e:
        report['status']='upload-interrupted-unverified' if report['sectorsWritten'] else 'not-uploaded'
        report['error']=str(e);raise
    finally:
        if fd is not None:
            try:os.close(fd)
            except OSError as e:report['closeError']=str(e)
        json_out(report_path,report)
    return report
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    probe=sub.add_parser('probe');probe.add_argument('--device');probe.add_argument('--snapshot',help='Save read-only IAP metadata to a NEW local file')
    plan=sub.add_parser('plan');plan.add_argument('--snapshot',required=True);plan.add_argument('--image',required=True);plan.add_argument('--output',required=True)
    f=sub.add_parser('flash');f.add_argument('--image',required=True)
    backups=f.add_mutually_exclusive_group(required=True);backups.add_argument('--backup');backups.add_argument('--without-backup',action='store_true')
    f.add_argument('--device');f.add_argument('--report',required=True);f.add_argument('--confirm-device',help='Explicit diskN confirmation instead of an interactive prompt')
    v=sub.add_parser('verify');v.add_argument('--image',required=True);v.add_argument('--mcu',required=True);v.add_argument('--backup');v.add_argument('--report')
    args=p.parse_args(argv)
    if args.action=='probe':
        target=choose(args.device);snapshot=read_snapshot(target)
        if args.snapshot:
            with Path(args.snapshot).open('xb') as out:out.write(snapshot)
        print(json.dumps(dict(device=target['device'],usbIdentity=target['identity'],**validate_snapshot(snapshot,target['info']['TotalSize'])),ensure_ascii=False,indent=2))
    elif args.action=='plan':
        firmware=image_bytes(args.image);writes,result=transfer_plan(Path(args.snapshot).read_bytes(),firmware)
        result['status']='offline-plan-no-device-write'
        with Path(args.output).open('x') as out:out.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.action=='verify':
        result=verify_readback(args.image,args.mcu,args.backup)
        if args.report:
            with Path(args.report).open('x') as out:out.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    else:flash(args)
if __name__=='__main__':
    try:main()
    except (Refused,OSError,subprocess.CalledProcessError,EOFError,KeyboardInterrupt) as e:
        raise SystemExit('STOP: '+str(e))
