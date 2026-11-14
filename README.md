# FNIRSI Scope

English · [Русский](README.ru.md)

FNIRSI Scope is a desktop app for the **FNIRSI 2C53T** oscilloscope. It shows both channels over USB, measures the waveform, captures a series of triggered frames, and lets you overlay a saved frame on another one. The app runs on macOS, Linux and Windows. You can update from the factory firmware in the app. Live viewing and control require the **r6j** firmware modification in this repository.

This is an independent community project by Evgeny Stepanischev.

## Get the app

Open [GitHub Releases](https://github.com/bolknote/fnirsi-scope/releases) and download the file for your computer.

| Computer | Download | Open |
| --- | --- | --- |
| Apple Silicon Mac | `macos-arm64.zip` | `fnirsi_scope.app` |
| Intel Mac | `macos-x64.zip` | `fnirsi_scope.app` |
| 64-bit Linux (x86-64) | `linux-x64.tar.gz` | `./fnirsi_scope` in the extracted folder |
| 64-bit Windows 10 or 11 (x86-64) | `windows-x64.zip` | `fnirsi_scope.exe` in the extracted folder |

Extract the entire archive. Keep the executable and its accompanying files together.

- **macOS 12 or later.** The app includes libusb. Releases use an ad hoc signature. If Gatekeeper blocks the first launch, check **System Settings → Privacy & Security → Open Anyway** after confirming the download came from this repository.
- **Linux.** The release is built on Ubuntu 24.04. Install GTK 3 and libusb 1.0. On Ubuntu, use `sudo apt install libgtk-3-0t64 libusb-1.0-0`. For USB access as a normal user, put the rule below in `/etc/udev/rules.d/70-fnirsi-2c53t.rules`, run `sudo udevadm control --reload-rules`, and reconnect the instrument.
- **Windows.** If the app cannot open USB while Windows sees the scope as a drive, the 2C53T needs a libusb-compatible driver such as WinUSB. In Zadig, select the device with USB ID **2e3c:5720**. Set up the driver after flashing. With WinUSB assigned, the instrument's normal USB drive may disappear until you restore the mass-storage driver. If Windows reports a missing Visual C++ runtime, install the [Microsoft x64 redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).

```udev
SUBSYSTEM=="usb", ATTR{idVendor}=="2e3c", ATTR{idProduct}=="5720", TAG+="uaccess"
```

USB control has been tested with a physical 2C53T on Apple Silicon. Linux and Windows packages are built in CI. Their USB setup still needs a hardware test.

## Build the firmware on your computer

Download **2C53T Firmware V1.2.0** from the [FNIRSI download page](https://www.fnirsi.com/pages/manuals-firmwares) and extract `APP_2C53T_V1.2.0_251015.bin`. The modifier checks the SHA-256 of that exact file before building. The original image and the result stay on your computer.

Install Python 3.13 and Clang with the ARM target. On macOS, Clang from the Xcode Command Line Tools works. On Ubuntu, install the `clang` package. On Windows you can use the same commands in WSL with Ubuntu.

Run these commands from the repository root, using the path to your downloaded file.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r firmware_mod/requirements.txt
.venv/bin/python firmware_mod/r6j/build_r6.py "/path/to/APP_2C53T_V1.2.0_251015.bin"
```

The result is `firmware_mod/r6j/build/APP_2C53T_V1.2.0_RU_REMOTE_r6j.bin`. The script prints its SHA-256 and writes a local `manifest.json` with the applied changes. The patch adds USB control, Russian menus, CSV export on the instrument, and a way to interrupt zero calibration.

## Install firmware from the app

1. Charge the scope and connect it with a USB data cable. Turn it off, then hold **MENU** while turning it on to enter **IAP**. This is the factory bootloader and works before any modification has been installed.
2. Click **Прошивка…** (Firmware) in the app toolbar. Pick the `.bin` file, then click **Проверить загрузчик** (Check bootloader).
3. Click **Записать прошивку** (Write firmware) once the bootloader is ready. Approve the system's administrator prompt, then leave the app open and the cable connected. When the bootloader reports **SUCCESS**, restart the scope and check its version. If all blocks were sent without a reply, check the scope's screen and version before trying again.

The updater accepts the official 2C53T V1.2.0 image and r6j. If a local r6j build has a different checksum, keep its generated `manifest.json` alongside the `.bin`. The app checks the file and identifies the factory IAP disk before writing.

On Linux, the updater requires `pkexec` (install with `sudo apt install pkexec` on Ubuntu) and a desktop Polkit authentication agent. On Windows, IAP must use the built-in USB mass-storage driver. If you assigned WinUSB in Zadig and the IAP drive no longer appears, restore the Windows mass-storage driver for the device in IAP mode first. FNIRSI also provides its [Windows update instructions](https://www.fnirsi.com/pages/manuals-firmwares).

## Take a measurement

1. Open oscilloscope mode on the 2C53T, enable **USB Sharing**, and plug in the USB data cable.
2. Open the app and click **Подключить** (Connect). In **Сигнал** (Signal), set the timebase, channel range and probe multiplier. Click **Применить** (Apply).
3. Click **Запуск** (Run) for live view, **Один кадр** (Single) for one trigger, or open **Захват** (Capture) to set a trigger and collect a series. Each frame is read and checked before the next one is armed.
4. Open **Измерения** (Measurements) for frequency, period, pulse width, duty cycle, min/max, mean and peak-to-peak. Use the cursors for time or voltage differences. **Декодер** (Decoder) can interpret UART and IrDA SIR on a stopped frame if the capture resolves the bits.
5. Use **Сохранить кадр** (Save frame) to keep a stopped frame in the bottom strip. Select a reference with **Наложить** (Overlay). The graph lines up both frames at their trigger points. The **Опыт** (Session) menu saves and reopens `.fnirsi` sessions and exports PNG or CSV.

The app's interface is currently in Russian. It works with 301 screen samples per channel, on timebases from 10 ns/div through 20 ms/div. There is a gap between frames in a series. The 20 MHz filter applies to the displayed waveform.

The **Калибровать** (Calibrate) button in **Сигнал** runs zero calibration. First unplug both probes at the BNC inputs. The instrument can still report unverified gain after the zero check. Treat voltage readings as approximate while that warning is present.

## Build and release the desktop app

Use [Flutter 3.41.9](https://docs.flutter.dev/install/archive), Go 1.26.11 for the firmware helper, and the desktop tools for your platform. From `scope_app`:

```sh
flutter pub get --enforce-lockfile
flutter analyze --no-pub
flutter test --no-pub
```

- **macOS.** Install Xcode, CocoaPods and libusb (`brew install libusb`), then run `bash tools/build_macos.sh`.
- **Linux.** Install the [Flutter Linux build dependencies](https://docs.flutter.dev/platform-integration/linux/setup) and libusb. Run `bash tools/build_updater.sh` before `flutter build linux --release`.
- **Windows.** Install [Visual Studio with Desktop development with C++](https://docs.flutter.dev/platform-integration/windows/setup). Run `bash tools/build_updater.sh` in Git Bash. Set `LIBUSB_DLL` to the full path of the x64 `libusb-1.0.dll`, then run `flutter build windows --release`.

Push a new tag such as `v0.2.0` to publish a release. The [Desktop release workflow](.github/workflows/release.yml) builds all four packages and publishes them with SHA-256 checksums when every build passes. You can also run the workflow from the **Actions** tab to try a build before tagging.

## License

The project's own code and artwork are under the [MIT License](LICENSE), copyright 2026 Evgeny Stepanischev. The bundled Noto Sans font is under [SIL OFL 1.1](firmware_mod/r6j/font/OFL.txt). macOS and Windows packages include [libusb under LGPL 2.1 or later](scope_app/third_party/libusb/COPYING). The corresponding libusb source is attached to each release.

Original and modified FNIRSI firmware images are absent from the repository and release downloads. The stock firmware remains subject to the manufacturer's terms. Keep local inputs, generated images, and device dumps out of commits and release assets.
