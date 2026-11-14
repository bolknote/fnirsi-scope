package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"golang.org/x/sys/unix"
	"howett.net/plist"
)

const diskutil = "/usr/sbin/diskutil"

var wholeDisk = regexp.MustCompile(`^disk[0-9]+$`)

type diskInfo struct {
	DeviceIdentifier  string
	DeviceBlockSize   int64
	TotalSize         int64
	WholeDisk         *bool
	Whole             *bool
	Internal          *bool
	BusProtocol       string
	VirtualOrPhysical string
	MountPoint        string
	Mounted           bool
}

func osCommand(path string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	result, err := exec.CommandContext(ctx, path, args...).Output()
	if ctx.Err() != nil {
		return nil, fmt.Errorf("%s: %w", path, ctx.Err())
	}
	if e, ok := err.(*exec.ExitError); ok {
		return nil, fmt.Errorf("%s: %s", path, strings.TrimSpace(string(e.Stderr)))
	}
	return result, err
}

func (info diskInfo) matchesIAP(name string) bool {
	whole := info.WholeDisk
	if whole == nil {
		whole = info.Whole
	} else if info.Whole != nil && *info.Whole != *whole {
		return false
	}
	return info.DeviceIdentifier == name && whole != nil && *whole &&
		info.Internal != nil && !*info.Internal && info.BusProtocol == "USB" &&
		(info.VirtualOrPhysical == "" || info.VirtualOrPhysical == "Physical") &&
		iapGeometry(info.DeviceBlockSize, info.TotalSize)
}

func macDiskInfo(name string) (diskInfo, error) {
	var info diskInfo
	result, err := osCommand(diskutil, "info", "-plist", "/dev/"+name)
	if err != nil {
		return info, err
	}
	_, err = plist.Unmarshal(result, &info)
	return info, err
}

func usbNumber(value any) uint64 {
	switch n := value.(type) {
	case uint64:
		return n
	case int64:
		if n >= 0 {
			return uint64(n)
		}
	}
	return 0
}

func devicesInIAP() ([]iapDevice, error) {
	result, err := osCommand("/usr/sbin/ioreg", "-a", "-l", "-r", "-c", "IOUSBHostDevice")
	if err != nil {
		return nil, err
	}
	var tree []map[string]any
	if _, err := plist.Unmarshal(result, &tree); err != nil {
		return nil, err
	}
	disks := map[string]iapDevice{}
	var walk func(map[string]any, string)
	walk = func(node map[string]any, usb string) {
		if vendor, isDevice := node["idVendor"]; isDevice {
			usb = ""
			if usbNumber(vendor) == 0x2e3c && usbNumber(node["idProduct"]) == 0x5720 {
				if attachment := usbNumber(node["IORegistryEntryID"]); attachment != 0 {
					usb = fmt.Sprintf("%x", attachment)
				}
			}
		}
		if name, ok := node["BSD Name"].(string); usb != "" && ok && wholeDisk.MatchString(name) {
			disks[name] = iapDevice{id: usb + ":" + name, label: name, path: "/dev/r" + name}
		}
		if children, ok := node["IORegistryEntryChildren"].([]any); ok {
			for _, child := range children {
				if nested, ok := child.(map[string]any); ok {
					walk(nested, usb)
				}
			}
		}
	}
	for _, node := range tree {
		walk(node, "")
	}
	var matches []iapDevice
	for name, disk := range disks {
		info, err := macDiskInfo(name)
		if err == nil && info.matchesIAP(name) {
			matches = append(matches, disk)
		}
	}
	return matches, nil
}

func openDevice(device iapDevice, write bool) (sectors, error) {
	flags := unix.O_RDONLY
	if write {
		// Flush/unmount first and recheck both the attachment and the mount.
		if _, err := osCommand(diskutil, "unmountDisk", "/dev/"+device.label); err != nil {
			return nil, err
		}
		if _, err := selectDevice(device.id); err != nil {
			return nil, err
		}
		info, err := macDiskInfo(device.label)
		if err != nil {
			return nil, err
		}
		if info.Mounted || info.MountPoint != "" {
			return nil, errors.New("macOS still has the IAP volume mounted")
		}
		flags = unix.O_RDWR
	}
	f, err := os.OpenFile(device.path, flags|unix.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	disk := rawDisk{f}
	info, err := f.Stat()
	if err != nil || info.Mode()&os.ModeCharDevice == 0 {
		disk.close()
		return nil, errors.New("the IAP target is not a raw disk")
	}
	if _, err := selectDevice(device.id); err != nil {
		disk.close()
		return nil, err
	}
	return disk, nil
}
