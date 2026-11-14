package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	diskInterface = windows.GUID{Data1: 0x53f56307, Data2: 0xb6bf, Data3: 0x11d0,
		Data4: [8]byte{0x94, 0xf2, 0, 0xa0, 0xc9, 0x1e, 0xfb, 0x8b}}
	cfgmgr      = windows.NewLazySystemDLL("cfgmgr32.dll")
	getParent   = cfgmgr.NewProc("CM_Get_Parent")
	getDeviceID = cfgmgr.NewProc("CM_Get_Device_IDW")
)

const (
	storageNumber   = 0x002d1080 // IOCTL_STORAGE_GET_DEVICE_NUMBER
	diskGeometryEx  = 0x000700a0 // IOCTL_DISK_GET_DRIVE_GEOMETRY_EX
	volumeExtents   = 0x00560000 // IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS
	lockVolume      = 0x00090018 // FSCTL_LOCK_VOLUME
	dismountVolume  = 0x00090020 // FSCTL_DISMOUNT_VOLUME
	scsiPassThrough = 0x0004d004 // IOCTL_SCSI_PASS_THROUGH
	diskShare       = windows.FILE_SHARE_READ | windows.FILE_SHARE_WRITE
)

func windowsDevice(path string, access uint32) (windows.Handle, error) {
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return windows.InvalidHandle, err
	}
	return windows.CreateFile(name, access, diskShare, nil, windows.OPEN_EXISTING, 0, 0)
}

func ioctl(handle windows.Handle, code uint32, output []byte) (uint32, error) {
	var buffer *byte
	if len(output) != 0 {
		buffer = &output[0]
	}
	var count uint32
	err := windows.DeviceIoControl(handle, code, nil, 0, buffer, uint32(len(output)), &count, nil)
	return count, err
}

func diskIdentity(handle windows.Handle) (int64, int64, uint32, error) {
	var geometry [256]byte
	var number [12]byte
	if n, err := ioctl(handle, diskGeometryEx, geometry[:]); err != nil || n < 32 {
		return 0, 0, 0, errors.New("could not read disk geometry")
	}
	if n, err := ioctl(handle, storageNumber, number[:]); err != nil || n < 12 {
		return 0, 0, 0, errors.New("could not read disk number")
	}
	return int64(binary.LittleEndian.Uint32(geometry[20:])),
		int64(binary.LittleEndian.Uint64(geometry[24:])),
		binary.LittleEndian.Uint32(number[4:]), nil
}

func hasScopeUSB(node windows.DEVINST) bool {
	for range 16 {
		var name [256]uint16
		rc, _, _ := getDeviceID.Call(uintptr(node), uintptr(unsafe.Pointer(&name[0])), uintptr(len(name)), 0)
		if rc != 0 {
			return false
		}
		id := strings.ToUpper(windows.UTF16ToString(name[:]))
		if strings.HasPrefix(id, `USB\VID_`) {
			return strings.HasPrefix(id, `USB\VID_2E3C&PID_5720\`) ||
				strings.HasPrefix(id, `USB\VID_2E3C&PID_5720&`)
		}
		var parent windows.DEVINST
		rc, _, _ = getParent.Call(uintptr(unsafe.Pointer(&parent)), uintptr(node), 0)
		if rc != 0 {
			return false
		}
		node = parent
	}
	return false
}

func devicesInIAP() ([]iapDevice, error) {
	list, err := windows.SetupDiGetClassDevsEx(&diskInterface, "", 0,
		windows.DIGCF_PRESENT|windows.DIGCF_DEVICEINTERFACE, 0, "")
	if err != nil {
		return nil, err
	}
	defer windows.SetupDiDestroyDeviceInfoList(list)
	var matches []iapDevice
	for i := 0; ; i++ {
		info, err := windows.SetupDiEnumDeviceInfo(list, i)
		if errors.Is(err, windows.ERROR_NO_MORE_ITEMS) {
			break
		}
		if err != nil {
			return nil, err
		}
		if !hasScopeUSB(info.DevInst) {
			continue
		}
		instance, err := windows.SetupDiGetDeviceInstanceId(list, info)
		if err != nil {
			return nil, err
		}
		paths, err := windows.CM_Get_Device_Interface_List(instance, &diskInterface, 0)
		if err != nil {
			return nil, err
		}
		for _, path := range paths {
			handle, err := windowsDevice(path, 0)
			if err != nil {
				continue
			}
			block, size, number, err := diskIdentity(handle)
			_ = windows.CloseHandle(handle)
			if err == nil && iapGeometry(block, size) {
				label := fmt.Sprintf("Disk %d", number)
				matches = append(matches, iapDevice{
					id: checksum([]byte(instance + ":" + path + ":" + label)), label: label, path: path,
				})
			}
		}
	}
	return matches, nil
}

func holdVolumes(number uint32) ([]windows.Handle, error) {
	var volumes []windows.Handle
	var volume [1024]uint16
	search, err := windows.FindFirstVolume(&volume[0], uint32(len(volume)))
	if errors.Is(err, windows.ERROR_NO_MORE_FILES) {
		return volumes, nil
	}
	if err != nil {
		return nil, err
	}
	defer windows.FindVolumeClose(search)
	for {
		name := strings.TrimSuffix(windows.UTF16ToString(volume[:]), `\`)
		query, err := windowsDevice(name, 0)
		if err != nil {
			return volumes, fmt.Errorf("identify Windows volume %s: %w", name, err)
		}
		matches, err := volumeBelongsTo(query, number)
		_ = windows.CloseHandle(query)
		if err != nil {
			return volumes, err
		}
		if matches {
			handle, err := windowsDevice(name, windows.GENERIC_READ|windows.GENERIC_WRITE)
			if err != nil {
				return volumes, err
			}
			volumes = append(volumes, handle)
			if _, err := ioctl(handle, lockVolume, nil); err != nil {
				return volumes, fmt.Errorf("lock IAP volume: close Explorer windows using it: %w", err)
			}
			if _, err := ioctl(handle, dismountVolume, nil); err != nil {
				return volumes, fmt.Errorf("dismount IAP volume: %w", err)
			}
		}
		err = windows.FindNextVolume(search, &volume[0], uint32(len(volume)))
		if errors.Is(err, windows.ERROR_NO_MORE_FILES) {
			return volumes, nil
		}
		if err != nil {
			return volumes, err
		}
	}
}

func volumeBelongsTo(handle windows.Handle, number uint32) (bool, error) {
	// VOLUME_DISK_EXTENTS on x64. A volume may span more than one disk.
	const header, extentSize = 8, 24
	buf := make([]byte, header+extentSize)
	n, err := ioctl(handle, volumeExtents, buf)
	if errors.Is(err, windows.ERROR_MORE_DATA) {
		entries := binary.LittleEndian.Uint32(buf)
		if entries < 2 || entries > 4096 {
			return false, errors.New("could not identify the disks behind a Windows volume")
		}
		buf = make([]byte, header+extentSize*int(entries))
		n, err = ioctl(handle, volumeExtents, buf)
		if err != nil {
			return false, fmt.Errorf("read Windows volume extents: %w", err)
		}
	}
	if err == nil {
		entries := int(binary.LittleEndian.Uint32(buf))
		if n < header || n > uint32(len(buf)) || entries > (int(n)-header)/extentSize {
			return false, errors.New("incomplete Windows volume extent list")
		}
		for i := range entries {
			if binary.LittleEndian.Uint32(buf[header+extentSize*i:]) == number {
				return true, nil
			}
		}
		return false, nil
	}
	// Some removable or unrecognized filesystems only expose a disk number.
	var storage [12]byte
	n, err = ioctl(handle, storageNumber, storage[:])
	if err != nil || n < uint32(len(storage)) {
		if errors.Is(err, windows.ERROR_INVALID_FUNCTION) || errors.Is(err, windows.ERROR_NOT_READY) ||
			errors.Is(err, windows.ERROR_NOT_SUPPORTED) || errors.Is(err, windows.ERROR_NO_MEDIA_IN_DRIVE) {
			return false, nil
		}
		return false, fmt.Errorf("identify a Windows volume (bytes returned %d): %v", n, err)
	}
	const fileDeviceDisk = 7
	return binary.LittleEndian.Uint32(storage[:]) == fileDeviceDisk &&
		binary.LittleEndian.Uint32(storage[4:]) == number, nil
}

type windowsDisk struct {
	handle  windows.Handle
	volumes []windows.Handle
}

func (disk *windowsDisk) close() {
	_ = windows.CloseHandle(disk.handle)
	for _, volume := range disk.volumes {
		_ = windows.CloseHandle(volume)
	}
}

func openDevice(device iapDevice, write bool) (sectors, error) {
	handle, err := windowsDevice(device.path, windows.GENERIC_READ|windows.GENERIC_WRITE)
	if err != nil {
		return nil, err
	}
	disk := &windowsDisk{handle: handle}
	block, size, number, err := diskIdentity(handle)
	if err != nil || !iapGeometry(block, size) || device.label != fmt.Sprintf("Disk %d", number) {
		disk.close()
		return nil, errors.New("unexpected geometry for the open IAP handle")
	}
	if write {
		if _, err := readReady(disk); err != nil {
			disk.close()
			return nil, err
		}
		disk.volumes, err = holdVolumes(number)
		if err != nil {
			disk.close()
			return nil, err
		}
	}
	if _, err := selectDevice(device.id); err != nil {
		disk.close()
		return nil, err
	}
	return disk, nil
}

type passThrough struct {
	length                                                       uint16
	status, path, target, lun, cdbLength, senseLength, direction byte
	transferLength, timeout                                      uint32
	dataOffset                                                   uintptr
	senseOffset                                                  uint32
	cdb                                                          [16]byte
}

type scsiPacket struct {
	command passThrough
	sense   [32]byte
	data    [sectorBytes]byte
}

func (disk *windowsDisk) transfer(lba uint32, data []byte, writing bool) error {
	if len(data) != sectorBytes {
		return io.ErrShortBuffer
	}
	var packet scsiPacket
	packet.command.length = uint16(unsafe.Sizeof(packet.command))
	packet.command.cdbLength = 10
	packet.command.senseLength = byte(len(packet.sense))
	packet.command.senseOffset = uint32(unsafe.Offsetof(packet.sense))
	packet.command.dataOffset = unsafe.Offsetof(packet.data)
	packet.command.transferLength = sectorBytes
	packet.command.timeout = 10
	command := sectorCommand(lba, writing)
	copy(packet.command.cdb[:], command[:])
	packet.command.direction = 1
	if writing {
		packet.command.direction = 0
		copy(packet.data[:], data)
	}
	var count uint32
	p := (*byte)(unsafe.Pointer(&packet))
	size := uint32(unsafe.Sizeof(packet))
	if err := windows.DeviceIoControl(disk.handle, scsiPassThrough, p, size, p, size, &count, nil); err != nil {
		return err
	}
	if packet.command.status != 0 || packet.command.transferLength != sectorBytes {
		return fmt.Errorf("SCSI status %#x, transferred %d bytes", packet.command.status, packet.command.transferLength)
	}
	if !writing {
		if count < uint32(unsafe.Offsetof(packet.data))+sectorBytes {
			return io.ErrUnexpectedEOF
		}
		copy(data, packet.data[:])
	}
	return nil
}

func (disk *windowsDisk) read(lba uint32, data []byte) error {
	return disk.transfer(lba, data, false)
}

func (disk *windowsDisk) write(lba uint32, data []byte) error {
	return disk.transfer(lba, data, true)
}
