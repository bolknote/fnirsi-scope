#!/usr/bin/env python3
"""Link a small Cortex-M4 extension and encode Thumb branches."""
from pathlib import Path
import hashlib
import os
import struct

HERE = Path(__file__).resolve().parent
STOCK_NAME = 'APP_2C53T_V1.2.0_251015.bin'
SOURCE = Path(os.environ.get('FNIRSI_STOCK_IMAGE', HERE / 'input' / STOCK_NAME))
BASE = 0x08007000
STOCK_SHA256 = 'a17c5c35c97bb898f15672a1747bc1041d8ed507c16999ddba0d1e4e2ec0c760'


def load_stock(path=SOURCE):
    try:
        data = path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f'{path} was not found. Download and extract FNIRSI 2C53T firmware 1.2.0 first.') from error
    if hashlib.sha256(data).hexdigest() != STOCK_SHA256:
        raise ValueError(f'{path} does not match the official 2C53T firmware 1.2.0 SHA-256: {STOCK_SHA256}')
    return data

def align(value, boundary):
    return (value + boundary - 1) & -boundary

def thumb_branch(address, target, link=True):
    delta=(target & ~1)-(address+4)
    assert delta%2==0 and -(1<<24)<=delta<(1<<24)
    value=delta&0x1ffffff;s=value>>24
    j1=1^((value>>23)&1)^s;j2=1^((value>>22)&1)^s
    return struct.pack('<HH',0xf000 | s<<10 | ((value>>12)&1023),
                       (0xd000 if link else 0x9000) | j1<<13 | j2<<11 | ((value>>1)&2047))

def elf_link(data, start):
    """Minimal ELF32/ARM linker; reject every unsupported allocated relocation."""
    hdr = struct.unpack_from('<16sHHIIIIIHHHHHH', data)
    assert hdr[0][:6] == b'\x7fELF\x01\x01' and hdr[2] == 40
    sections = [struct.unpack_from('<10I', data, hdr[6] + n*hdr[11]) for n in range(hdr[12])]
    names = sections[hdr[13]]
    strings = data[names[4]:names[4]+names[5]]
    def cstr(buf, at):
        return buf[at:buf.index(0, at)].decode()
    snames = [cstr(strings, s[0]) for s in sections]
    positions = {}; output = bytearray(); cursor = start
    for n, sec in enumerate(sections):
        if not sec[2] & 2 or not sec[5] or snames[n].startswith('.ARM.exidx'):
            continue
        assert sec[1] == 1, (snames[n], 'unexpected allocated section')
        cursor = align(cursor, max(1, sec[8]))
        positions[n] = cursor
        offset = cursor-start
        output.extend(b'\xff' * (offset-len(output)))
        output.extend(data[sec[4]:sec[4]+sec[5]])
        cursor += sec[5]
    symbols = []; symbol_names = []
    for sec in sections:
        if sec[1] != 2: continue
        st = sections[sec[6]]; strs = data[st[4]:st[4]+st[5]]
        for at in range(sec[4], sec[4]+sec[5], sec[9]):
            name, value, size, info, other, section = struct.unpack_from('<IIIBBH', data, at)
            symbols.append((value, section))
            symbol_names.append(cstr(strs, name))
    relocations = []
    for sec in sections:
        if sec[1] != 9 or sec[7] not in positions: continue
        for at in range(sec[4], sec[4]+sec[5], sec[9]):
            offset, info = struct.unpack_from('<II', data, at)
            kind, index = info & 255, info >> 8
            value, section = symbols[index]
            assert section in positions, ('undefined symbol', symbol_names[index], section)
            target = positions[section] + value
            place = positions[sec[7]] + offset
            outat = place-start
            if kind == 2:  # R_ARM_ABS32, including the Thumb function bit.
                addend = struct.unpack_from('<I', output, outat)[0]
                struct.pack_into('<I', output, outat, (target+addend) & 0xffffffff)
            elif kind in (10, 30):  # R_ARM_THM_CALL / R_ARM_THM_JUMP24.
                h1, h2 = struct.unpack_from('<HH', output, outat)
                s = (h1 >> 10) & 1
                i1 = 1 ^ ((h2 >> 13) & 1) ^ s
                i2 = 1 ^ ((h2 >> 11) & 1) ^ s
                addend = s<<24 | i1<<23 | i2<<22 | (h1&1023)<<12 | (h2&2047)<<1
                if s: addend -= 1<<25
                delta = (target & ~1) + addend - place
                assert -(1<<24) <= delta < (1<<24) and delta % 2 == 0
                v = delta & 0x1ffffff; s = v>>24
                j1 = 1 ^ ((v>>23)&1) ^ s; j2 = 1 ^ ((v>>22)&1) ^ s
                h1 = (h1&0xf800) | s<<10 | ((v>>12)&1023)
                h2 = (h2&0xd000) | j1<<13 | j2<<11 | ((v>>1)&2047)
                struct.pack_into('<HH', output, outat, h1, h2)
            else:
                raise ValueError(('unsupported relocation', kind, symbol_names[index]))
            relocations.append({'address': hex(place), 'kind': kind, 'symbol': symbol_names[index], 'target': hex(target)})
    syms = {name: positions[section]+value for name, (value,section) in zip(symbol_names,symbols)
            if name and section in positions}
    return output, syms, relocations
