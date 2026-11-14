#!/usr/bin/env python3
"""Generate 4-bit Noto UI glyphs and exact-string Russian replacements.
The bitmap subset retains the Noto font's SIL Open Font License, in font/OFL.txt.
"""
from pathlib import Path
import json, math, hashlib
HERE=Path(__file__).resolve().parent
from PIL import Image,ImageDraw,ImageFont

def translations(stock):
    pairs = json.loads((HERE / 'translations.json').read_text(encoding='utf-8'))

    def resolve(entry):
        if isinstance(entry, str):
            return entry
        if type(entry) is not int or not 0 <= entry < len(stock):
            raise ValueError(f'Invalid translation string offset: {entry!r}')
        return stock[entry:stock.index(0, entry)].decode('utf-8')

    mapping = {resolve(key): resolve(value) for key, value in pairs}
    if len(mapping) != len(pairs):
        raise ValueError('Duplicate translation key')
    return mapping


def generate_resources(stock):
    build=HERE/'build';build.mkdir(exist_ok=True)
    mapping=translations(stock)
    chars=list(range(32,127))+[0x401]+list(range(0x410,0x450))+[0x451,0xb0,0xb5,0x3a9,0x221e,0x2014,0x2190,0x2191,0x2192,0x2193,0x25b6]
    output=['/* Bitmap font subset: Noto Sans, SIL OFL 1.1; see font/OFL.txt. */',
            'struct r5_glyph {unsigned short code,offset;unsigned char w,h,advance;signed char left,top;};']
    for size in [18,16,14,12]:
        font=ImageFont.truetype(str(HERE/'font/NotoSans-Regular.ttf'),size)
        data=bytearray();rows=[];ascent,descent=font.getmetrics()
        for code in chars:
            char=chr(code);left,top,right,bottom=font.getbbox(char,anchor='ls')
            w,h=right-left,bottom-top
            im=Image.new('L',(max(1,w),max(1,h)));ImageDraw.Draw(im).text((-left,-top),char,font=font,fill=255,anchor='ls')
            advance=round(font.getlength(char))
            if code in (0x2190,0x2191,0x2192,0x2193,0x25b6):
                # Noto Sans Regular has no arrow glyphs. These five UI symbols
                # are our own small geometric drawings, not a missing-glyph box.
                w=h=size-2;left=0;top=-h;advance=w+2
                im=Image.new('L',(w,h));d=ImageDraw.Draw(im);c=w//2
                if code==0x25b6:d.polygon([(2,1),(w-2,c),(2,h-2)],fill=255)
                elif code==0x2191:
                    d.line([(c,h-2),(c,1)],fill=255);d.line([(1,c),(c,1),(w-2,c)],fill=255)
                elif code==0x2193:
                    d.line([(c,1),(c,h-2)],fill=255);d.line([(1,c),(c,h-2),(w-2,c)],fill=255)
                elif code==0x2190:
                    d.line([(1,c),(w-2,c)],fill=255);d.line([(c,1),(1,c),(c,h-2)],fill=255)
                else:
                    d.line([(1,c),(w-2,c)],fill=255);d.line([(c,1),(w-2,c),(c,h-2)],fill=255)
            raw=[min(15,(v+8)//17) for v in im.get_flattened_data()] if w*h else []
            offset=len(data)
            for n in range(0,len(raw),2):data.append(raw[n]<<4|(raw[n+1] if n+1<len(raw) else 0))
            rows.append((code,offset,w,h,advance,left,top))
        assert len(data)<65536
        output += [f'static const unsigned char r5_font_{size}[]={{'+','.join(map(str,data))+'};',
                   f'static const struct r5_glyph r5_glyph_{size}[]={{'+','.join('{'+','.join(map(str,row))+'}' for row in rows)+'};']
    output+=['struct r5_translation {const char *old,*new;};',
             'static const struct r5_translation r5_translations[]={']
    for old,new in mapping.items():output.append('{'+json.dumps(old,ensure_ascii=False)+','+json.dumps(new,ensure_ascii=False)+'},')
    output+=['};']
    # Butterworth 2nd order at 20 MHz, indexed by screen point spacing. Slow
    # scales bypass: 20 MHz is above their displayed Nyquist limit. This is a
    # digital display-data filter, never advertised as an analog input filter.
    ns=[10,20,50,100,200,500]
    output+=['struct r5_biquad {float b0,b1,b2,a1,a2;};','static const struct r5_biquad r5_lpf[]={']
    coefficients=[]
    for t in ns:
        fs=25e9/t;k=math.tan(math.pi*20e6/fs);n=1/(1+math.sqrt(2)*k+k*k)
        co=[k*k*n,2*k*k*n,k*k*n,2*(k*k-1)*n,(1-math.sqrt(2)*k+k*k)*n]
        coefficients.append(dict(timebaseNS=t,screenSampleHz=fs,coefficients=co))
        output.append('{'+','.join(format(v,'.10g')+'f' if '.' in format(v,'.10g') or 'e' in format(v,'.10g') else str(v)+'.0f' for v in co)+'},')
    output+=['};']
    (build/'resources.c').write_text('\n'.join(output)+'\n', encoding='utf-8')
    (build/'resource-manifest.json').write_text(json.dumps(dict(fontSHA256=hashlib.sha256((HERE/'font/NotoSans-Regular.ttf').read_bytes()).hexdigest(),fontLicense='SIL OFL 1.1',glyphs=len(chars),fontSizes=[18,16,14,12],translations=len(mapping),filter='Digital screen-data 20 MHz Butterworth; raw banks and analog frontend unchanged',coefficients=coefficients),ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    print('Generated',len(chars),'glyphs × 4 sizes;',len(mapping),'translations')
