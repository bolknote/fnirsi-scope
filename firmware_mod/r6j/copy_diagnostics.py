#!/usr/bin/env python3
"""Copy files FROM the mounted diagnostic disk. Never writes to the instrument."""
import argparse,datetime,hashlib,json,re,zlib
from pathlib import Path
FILES={'CALIB.BIN':4096,'MCUFLASH.BIN':1048576,'FLASH.BIN':16777216,'RAM.BIN':229376,'STATE.BIN':3968,'STATUS.TXT':512,'README.TXT':None}
def main():
 p=argparse.ArgumentParser(description='Скопировать диагностику 2C53T в новую локальную папку. Прибор только читается.')
 p.add_argument('volume',type=Path,help='Путь к диску 2C53T DIAG, например /Volumes/2C53T DIAG')
 p.add_argument('--output',type=Path,help='Новая папка для резервной копии')
 args=p.parse_args();src=args.volume.resolve()
 if not all((src/n).is_file() for n in ('CALIB.BIN','MCUFLASH.BIN','STATUS.TXT')):p.error('Выберите диск 2C53T DIAG с файлами CALIB.BIN и MCUFLASH.BIN.')
 dest=(args.output or Path.cwd()/('FNIRSI-backup-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S'))).resolve()
 if dest==src or src in dest.parents:p.error('Папка резервной копии должна находиться на компьютере, вне диска прибора.')
 dest.mkdir(parents=True,exist_ok=False)
 report={'source':str(src),'files':{},'complete':True,'deviceWrites':0,'ramAtomicSnapshot':False}
 for name,expected in FILES.items():
  target=dest/(name+'.partial');sha=hashlib.sha256();n=0;next_report=1048576
  print('Чтение '+name,flush=True)
  try:
   with (src/name).open('rb') as inp,target.open('xb') as out:
    while block:=inp.read(65536):
     out.write(block);sha.update(block);n+=len(block)
     if n>=next_report:
      print('  %s: %d / %s байт'%(name,n,expected if expected is not None else '?'),flush=True);next_report+=1048576
   if expected is not None and n!=expected:raise ValueError('Неожиданный размер: %d вместо %d'%(n,expected))
   target.rename(dest/name);report['files'][name]={'bytes':n,'sha256':sha.hexdigest(),'complete':True}
   print('Скопирован %s: %d байт'%(name,n),flush=True)
  except (OSError,ValueError) as e:
   report['complete']=False;report['files'][name]={'bytes':n,'complete':False,'error':str(e)};print('Ошибка %s: %s'%(name,e),flush=True)
 if (dest/'CALIB.BIN').exists() and (dest/'STATUS.TXT').exists():
  status=(dest/'STATUS.TXT').read_bytes().split(b'\0')[0].decode('ascii',errors='replace')
  expected=re.search(r'^calibration_page_crc32=([0-9A-Fa-f]{8})',status,re.M)
  actual=zlib.crc32((dest/'CALIB.BIN').read_bytes())
  report['calibrationCRC32']='%08X'%actual
  report['calibrationCRCMatchesStatus']=bool(expected and actual==int(expected[1],16))
  if not report['calibrationCRCMatchesStatus']:report['complete']=False
  print('CRC32 калибровки: %08X; совпадение с прибором: %s'%(actual,report['calibrationCRCMatchesStatus']),flush=True)
 (dest/'backup-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print('Отчёт: '+str(dest/'backup-report.json'))
 return 0 if report['complete'] else 1
if __name__=='__main__':raise SystemExit(main())
