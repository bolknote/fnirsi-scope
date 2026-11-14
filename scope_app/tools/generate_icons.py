#!/usr/bin/env python3
"""Regenerate desktop icons from the checked-in vector artwork.

Requires rsvg-convert and Pillow. The generated files are checked in, so end
users do not need these tools to build or run the application.
"""
from pathlib import Path
import subprocess
from PIL import Image

root = Path(__file__).resolve().parent.parent
source = root / 'assets/branding/scope-icon.svg'
master = root / 'assets/branding/scope-icon.png'
subprocess.run(['rsvg-convert', '-w', '1024', '-h', '1024', '-o', str(master), str(source)], check=True)
image = Image.open(master).convert('RGBA')
mac = root / 'macos/Runner/Assets.xcassets/AppIcon.appiconset'
for size in (16, 32, 64, 128, 256, 512, 1024):
    image.resize((size, size), Image.Resampling.LANCZOS).save(mac / f'app_icon_{size}.png')
image.save(root / 'windows/runner/resources/app_icon.ico', format='ICO',
           sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
image.resize((512, 512), Image.Resampling.LANCZOS).save(root / 'linux/assets/app_icon.png')
