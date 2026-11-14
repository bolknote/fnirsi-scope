#!/usr/bin/env python3
"""Check the tracked tree before publishing a firmware modifier or app."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SOURCE_FOLDERS = {'.github', 'firmware_mod', 'scope_app'}
ROOT_FILES = {'.gitignore', '.gitattributes', 'LICENSE', 'README.md', 'README.ru.md'}
LOCAL_FOLDERS = {'build', 'dist', 'input', 'dumps', 'captures', 'Original', 'ephemeral'}
TEXT_EXTENSIONS = {
    '.c', '.cc', '.cmake', '.cpp', '.entitlements', '.go', '.h', '.json', '.lock',
    '.manifest', '.md', '.plist', '.py', '.rc', '.s', '.sh', '.swift', '.svg',
    '.txt', '.xcconfig', '.xcsettings', '.xcscheme', '.yaml', '.yml', '.dart',
    '.mod', '.sum',
}
TEXT_FILES = {
    '.gitignore', '.gitattributes', '.metadata', 'LICENSE', 'COPYING',
    'CMakeLists.txt', 'Podfile', 'project.pbxproj', 'contents.xcworkspacedata',
    'MainMenu.xib',
}
ASSETS = {
    'firmware_mod/r6j/startup.png',
    'firmware_mod/r6j/font/NotoSans-Regular.ttf',
    'scope_app/assets/branding/scope-icon.png',
    'scope_app/linux/assets/app_icon.png',
    'scope_app/windows/runner/resources/app_icon.ico',
}
ASSETS.update(
    'scope_app/macos/Runner/Assets.xcassets/AppIcon.appiconset/'
    f'app_icon_{size}.png' for size in (16, 32, 64, 128, 256, 512, 1024)
)


def error_for(path, mode, object_id):
    name = path.as_posix()
    parts = path.parts
    if mode not in {b'100644', b'100755'}:
        return 'expected a regular file'
    if parts[0] not in SOURCE_FOLDERS and name not in ROOT_FILES:
        return 'outside the source directories'
    if LOCAL_FOLDERS.intersection(parts):
        return 'local input or generated output'
    if name in ASSETS:
        return None
    if path.name not in TEXT_FILES and path.suffix.lower() not in TEXT_EXTENSIONS:
        return 'unknown asset or binary file'
    data = subprocess.check_output(['git', 'cat-file', 'blob', object_id], cwd=ROOT)
    try:
        data.decode('utf-8')
    except UnicodeError:
        return 'expected UTF-8 text'
    if b'\0' in data:
        return 'NUL byte in a source file'
    return None


def main():
    result = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=ROOT)
    failed = []
    paths = [entry.split(b'\t', 1) for entry in result.split(b'\0') if entry]
    if not paths:
        raise SystemExit('Stage the repository files before running the source check.')
    for description, raw_name in paths:
        name = raw_name.decode('utf-8')
        mode, object_id, stage = description.split()
        reason = error_for(Path(name), mode, object_id.decode('ascii')) if stage == b'0' else 'unmerged file'
        if reason:
            failed.append(f'{name}: {reason}')
    if failed:
        sys.exit('\n'.join(failed))
    print(f'Checked {len(paths)} tracked files. Only source and listed assets.')


if __name__ == '__main__':
    main()
