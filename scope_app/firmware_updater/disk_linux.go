package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"unsafe"

	"golang.org/x/sys/unix"
)

var scsiDisk = regexp.MustCompile(`^sd[a-z]+$`)

func sysValue(path, name string) string {
	data, _ := os.ReadFile(filepath.Join(path, name))
	return strings.TrimSpace(string(data))
}

func sysInt(path, name string) int64 {
	n, _ := strconv.ParseInt(sysValue(path, name), 10, 64)
	return n
}

func devicesInIAP() ([]iapDevice, error) {
	blocks, err := os.ReadDir("/sys/class/block")
	if err != nil {
		return nil, err
	}
	var matches []iapDevice
	for _, block := range blocks {
		name := block.Name()
		if !scsiDisk.MatchString(name) {
			continue
		}
		path, err := filepath.EvalSymlinks("/sys/class/block/" + name)
		if err != nil || !iapGeometry(sysInt(path, "queue/logical_block_size"), sysInt(path, "size")*512) {
			continue
		}
		for parent := path; parent != "/"; parent = filepath.Dir(parent) {
			vendor := sysValue(parent, "idVendor")
			if vendor == "" {
				continue
			}
			seq := sysValue(path, "diskseq")
			if vendor == "2e3c" && sysValue(parent, "idProduct") == "5720" && seq != "" {
				matches = append(matches, iapDevice{
					id: name + ":" + seq, label: name, path: "/dev/" + name,
				})
			}
			break
		}
	}
	return matches, nil
}

func linuxUnmount(device iapDevice) error {
	mounts, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		return err
	}
	majorMinor := sysValue("/sys/class/block/"+device.label, "dev")
	if majorMinor == "" {
		return errors.New("the IAP disk was removed")
	}
	for _, line := range strings.Split(string(mounts), "\n") {
		fields := strings.Fields(line)
		if len(fields) > 4 && fields[2] == majorMinor {
			// umount accepts a block-device path. Use the normal filesystem
			// unmount before taking out an exclusive block-device handle.
			cmd := exec.Command("/usr/bin/umount", "--", device.path)
			if output, err := cmd.CombinedOutput(); err != nil {
				return fmt.Errorf("unmount IAP: %s (%w)", strings.TrimSpace(string(output)), err)
			}
			break
		}
	}
	return nil
}

func openDevice(device iapDevice, write bool) (sectors, error) {
	flags := unix.O_RDONLY
	if write {
		if err := linuxUnmount(device); err != nil {
			return nil, err
		}
		flags = unix.O_RDWR | unix.O_EXCL
	}
	f, err := os.OpenFile(device.path, flags|unix.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	disk := linuxDisk{f}
	info, err := f.Stat()
	if err != nil || info.Mode()&(os.ModeDevice|os.ModeCharDevice) != os.ModeDevice {
		disk.close()
		return nil, errors.New("the IAP target is not a block device")
	}
	// Read the disk sequence of this open handle. A new USB disk may have
	// reused /dev/sdX after the scan, so a matching path alone is insufficient.
	var sequence uint64
	const blkGetDiskSeq = 0x80081280 // _IOR(0x12, 128, uint64), for amd64/arm64.
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, f.Fd(), blkGetDiskSeq, uintptr(unsafe.Pointer(&sequence)))
	if errno != 0 || device.id != fmt.Sprintf("%s:%d", device.label, sequence) {
		disk.close()
		return nil, errors.New("the open Linux block device does not match the IAP attachment")
	}
	if _, err := selectDevice(device.id); err != nil {
		disk.close()
		return nil, err
	}
	return disk, nil
}

type linuxDisk struct{ file *os.File }

func (disk linuxDisk) close() { _ = disk.file.Close() }

// Linux sg_io_hdr_t, with native pointer alignment. SG_IO sends one SCSI
// command directly through the open block device without the block cache.
type linuxSGIO struct {
	interfaceID, direction      int32
	commandBytes, maxSenseBytes uint8
	iovecCount                  uint16
	dataBytes                   uint32
	data, command, sense        unsafe.Pointer
	timeout, flags              uint32
	packID                      int32
	user                        unsafe.Pointer
	status, masked, msg, sensed uint8
	hostStatus, driverStatus    uint16
	residual                    int32
	duration, info              uint32
}

func (disk linuxDisk) transfer(lba uint32, data []byte, writing bool) error {
	if len(data) != sectorBytes {
		return io.ErrShortBuffer
	}
	command := sectorCommand(lba, writing)
	var sense [32]byte
	request := linuxSGIO{
		interfaceID: 'S', direction: -3, // SG_DXFER_FROM_DEV
		commandBytes: uint8(len(command)), maxSenseBytes: uint8(len(sense)),
		dataBytes: sectorBytes,
		data:      unsafe.Pointer(&data[0]), command: unsafe.Pointer(&command[0]), sense: unsafe.Pointer(&sense[0]),
		timeout: 10_000,
	}
	if writing {
		request.direction = -2 // SG_DXFER_TO_DEV
	}
	const sgIO = 0x2285
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, disk.file.Fd(), sgIO, uintptr(unsafe.Pointer(&request)))
	runtime.KeepAlive(disk.file)
	if errno != 0 {
		return errno
	}
	if request.status != 0 || request.hostStatus != 0 || request.driverStatus != 0 || request.residual != 0 || request.info&1 != 0 {
		return fmt.Errorf("SCSI status %#x, host %#x, driver %#x, residual %d",
			request.status, request.hostStatus, request.driverStatus, request.residual)
	}
	return nil
}

func (disk linuxDisk) read(lba uint32, data []byte) error {
	return disk.transfer(lba, data, false)
}

func (disk linuxDisk) write(lba uint32, data []byte) error {
	return disk.transfer(lba, data, true)
}
