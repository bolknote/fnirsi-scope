# libusb

FNIRSI Scope loads libusb dynamically. Official macOS and Windows packages
use libusb 1.0.30. Its source archive, `libusb-1.0.30.tar.bz2`, is attached to
the same GitHub release. The upstream project also provides [source and Windows
builds](https://github.com/libusb/libusb/releases/tag/v1.0.30). The license is
[GNU LGPL 2.1 or later](COPYING).

On Windows, a compatible replacement DLL can be placed next to the executable.
On macOS, build the app with `LIBUSB_DYLIB` pointing to your replacement.
The build script signs the resulting local bundle. When building locally,
Homebrew's libusb is used by default. Linux uses the system's libusb.
