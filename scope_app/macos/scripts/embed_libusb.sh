#!/bin/sh
set -eu

# Bundle the matching system libusb so the sandboxed app can load it. A
# universal dylib can be provided for a universal build via LIBUSB_DYLIB.
source_file="${LIBUSB_DYLIB:-}"
if [ -z "$source_file" ]; then
  for candidate in /opt/homebrew/lib/libusb-1.0.dylib /usr/local/lib/libusb-1.0.dylib; do
    if [ -f "$candidate" ]; then source_file="$candidate"; break; fi
  done
fi
if [ -z "$source_file" ] || [ ! -f "$source_file" ]; then
  echo 'error: libusb-1.0.dylib not found. Install libusb with Homebrew or set LIBUSB_DYLIB.' >&2
  exit 1
fi
for arch in ${ARCHS:-}; do
  if ! /usr/bin/lipo -verify_arch "$arch" "$source_file"; then
    echo "error: libusb does not contain architecture $arch; provide a compatible or universal LIBUSB_DYLIB." >&2
    exit 1
  fi
done
frameworks_dir="$TARGET_BUILD_DIR/$FRAMEWORKS_FOLDER_PATH"
mkdir -p "$frameworks_dir"
rm -f "$frameworks_dir/libusb-1.0.dylib"
cp -L "$source_file" "$frameworks_dir/libusb-1.0.dylib"
chmod u+w "$frameworks_dir/libusb-1.0.dylib"
/usr/bin/install_name_tool -id '@rpath/libusb-1.0.dylib' "$frameworks_dir/libusb-1.0.dylib"
/usr/bin/codesign --force --sign "${EXPANDED_CODE_SIGN_IDENTITY:--}" --timestamp=none "$frameworks_dir/libusb-1.0.dylib"

license_dir="$TARGET_BUILD_DIR/$UNLOCALIZED_RESOURCES_FOLDER_PATH/licenses/libusb"
mkdir -p "$license_dir"
cp "$PROJECT_DIR/../third_party/libusb/COPYING" "$license_dir/COPYING"
cp "$PROJECT_DIR/../third_party/libusb/README.md" "$license_dir/README.md"
