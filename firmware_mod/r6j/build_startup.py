#!/usr/bin/env python3
"""Embed the 2C53T startup picture in a stock 1.2.0-derived application."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from build_patch import BASE, SOURCE, align, elf_link, load_stock, thumb_branch

ASSET = HERE / 'startup.png'
WIDTH, HEIGHT = 320, 240
IAP_LIMIT = 0x080ff800  # Factory IAP completion-flag page.
BOOT_ENTRY = 0x0802b414
LOGO_CLEANUP = 0x0802c126
BOOT_RESUME = 0x0802c19a


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pixels_rgb565(asset):
    from PIL import Image

    with Image.open(asset) as picture:
        if picture.size != (WIDTH, HEIGHT):
            raise ValueError(f'Startup picture must be {WIDTH} × {HEIGHT}')
        pixels = picture.convert('RGB').tobytes()
    return b''.join(
        struct.pack('<H', ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3))
        for red, green, blue in struct.iter_unpack('BBB', pixels)
    )


def embed_startup(parent, stock, asset=ASSET, clang='clang'):
    """Return (final app bytes, manifest data) without changing the input."""
    if len(parent) < len(stock):
        raise ValueError('Incomplete 2C53T application')

    # Guard the vector table, LCD setup, BOTH logo branches and the native
    # blitter used by this payload. Other r6j code can be rebuilt independently.
    for start, end in ((BASE, BASE + 8), (0x0802ade2, 0x0802b118),
                       (BOOT_ENTRY, 0x0802b45a), (0x0802c04a, 0x0802c20c),
                       (0x0803a23c, 0x0803a310)):
        if parent[start - BASE:end - BASE] != stock[start - BASE:end - BASE]:
            raise ValueError(f'Unexpected application bytes at 0x{start:08x}..0x{end:08x}')

    pixels = pixels_rgb565(asset)
    address = align(BASE + len(parent), 16)
    with TemporaryDirectory(prefix='2c53t-startup-') as temp:
        temp = Path(temp)
        (temp / 'startup.rgb565').write_bytes(pixels)
        obj = temp / 'startup.o'
        subprocess.run([clang, '--target=arm-none-eabi', '-mcpu=cortex-m4',
                        '-mthumb', '-c', str(HERE / 'startup.S'), '-o', str(obj)],
                       cwd=temp, check=True)
        payload, symbols, relocations = elf_link(obj.read_bytes(), address)

    image = bytearray(parent)
    changes = []
    for at, replacement, reason in (
        (BOOT_ENTRY, thumb_branch(BOOT_ENTRY, symbols['draw_startup'], False),
         'Draw startup pixels from application flash after native LCD setup'),
        (LOGO_CLEANUP, thumb_branch(LOGO_CLEANUP, BOOT_RESUME, False) + b'\x00\xbf',
         'Continue native boot after the logo delay without closing or freeing a file'),
    ):
        offset = at - BASE
        before = image[offset:offset + len(replacement)]
        image[offset:offset + len(replacement)] = replacement
        changes.append(dict(address=hex(at), fileOffset=hex(offset), before=before.hex(),
                            after=replacement.hex(), reason=reason))

    image.extend(b'\xff' * (address - BASE - len(image)))
    image.extend(payload)
    image.extend(b'\xff' * (align(len(image), 128) - len(image)))
    if align(BASE + len(image), 2048) > IAP_LIMIT:
        raise ValueError('Startup image would touch the factory IAP completion-flag page')
    report = dict(
        width=WIDTH, height=HEIGHT, format='RGB565 little-endian',
        sourceFile=asset.name, sourceSHA256=sha256(asset.read_bytes()),
        pixelsAddress=hex(symbols['startup_pixels']), pixelsBytes=len(pixels),
        pixelsSHA256=sha256(pixels), codeAddress=hex(symbols['draw_startup'] & ~1),
        payloadAddress=hex(address), payloadBytes=len(payload),
        parentBytes=len(parent), parentSHA256=sha256(parent),
        imageBytes=len(image), imageSHA256=sha256(image),
        changes=changes, relocations=relocations,
    )
    return bytes(image), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Unmodified r6j application binary')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, help='Optional JSON build report')
    parser.add_argument('--stock', type=Path, default=SOURCE)
    parser.add_argument('--artwork', type=Path, default=ASSET)
    parser.add_argument('--clang', default='clang')
    args = parser.parse_args()
    paths = [args.input.resolve(), args.output.resolve(), args.stock.resolve(), args.artwork.resolve()]
    if args.report:
        paths.append(args.report.resolve())
    if len(set(paths)) != len(paths):
        parser.error('Input, output and report must be separate files')
    image, report = embed_startup(args.input.read_bytes(), load_stock(args.stock), args.artwork, args.clang)
    args.output.write_bytes(image)
    rendered = json.dumps(report, indent=2) + '\n'
    if args.report:
        args.report.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
