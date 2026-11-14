package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"time"
)

const (
	sectorBytes     = 2048
	rootLBA         = 8
	dataLBA         = 20
	metadataSectors = 16
)

type sectors interface {
	read(lba uint32, data []byte) error
	write(lba uint32, data []byte) error
	close()
}

// All platforms match a present device by USB ancestry and exact geometry
// before opening it. The ID passed back by the UI pins that attachment.
type iapDevice struct {
	id    string
	label string
	path  string
}

func sectorCommand(lba uint32, writing bool) [10]byte {
	command := [10]byte{0x28} // READ(10)
	if writing {
		command[0] = 0x2a // WRITE(10)
	}
	binary.BigEndian.PutUint32(command[2:], lba)
	binary.BigEndian.PutUint16(command[7:], 1)
	return command
}

func iapGeometry(blockBytes, size int64) bool {
	return blockBytes == sectorBytes &&
		(size == 4080*sectorBytes || size == 4095*sectorBytes || size == 4096*sectorBytes)
}

func selectDevice(expected string) (iapDevice, error) {
	devices, err := devicesInIAP()
	if err != nil {
		return iapDevice{}, err
	}
	if len(devices) == 0 {
		return iapDevice{}, errors.New("no 2C53T IAP disk found")
	}
	if len(devices) != 1 {
		return iapDevice{}, errors.New("leave just one 2C53T in IAP connected")
	}
	if expected != "" && devices[0].id != expected {
		return iapDevice{}, errors.New("the detected IAP device changed. Check the device again")
	}
	return devices[0], nil
}

type readySnapshot struct {
	root        []byte
	fingerprint string
}

func readReady(disk sectors) (readySnapshot, error) {
	metadata := make([]byte, metadataSectors*sectorBytes)
	for i := uint32(0); i < metadataSectors; i++ {
		if err := disk.read(i, metadata[i*sectorBytes:(i+1)*sectorBytes]); err != nil {
			return readySnapshot{}, fmt.Errorf("read IAP metadata at sector %d: %w", i, err)
		}
	}
	boot := metadata[:sectorBytes]
	u16 := func(offset int) uint16 { return binary.LittleEndian.Uint16(boot[offset:]) }
	if !bytes.Equal(boot[:11], []byte("\xeb\x3c\x90MSDOS5.0")) ||
		u16(510) != 0xaa55 || u16(11) != sectorBytes || boot[13] != 4 ||
		u16(14) != 6 || boot[16] != 2 || u16(17) != 512 ||
		u16(19) != 4095 || boot[21] != 248 || u16(22) != 1 {
		return readySnapshot{}, errors.New("the disk does not have the expected 2C53T IAP geometry")
	}
	reserved := []byte{0xf8, 0xff, 0xff, 0xff}
	if !bytes.Equal(metadata[0x3000:0x3004], reserved) || !bytes.Equal(metadata[0x3800:0x3804], reserved) {
		return readySnapshot{}, errors.New("unexpected IAP FAT signatures")
	}
	root := metadata[rootLBA*sectorBytes:]
	if !bytes.EqualFold(root[:11], []byte("READY   TXT")) || root[11] != 0x20 ||
		binary.LittleEndian.Uint32(root[28:32]) != 0 ||
		string(root[32:43]) != "IAP        " || root[43] != 8 ||
		bytes.Count(root[64:], []byte{0}) != len(root)-64 {
		return readySnapshot{}, errors.New("the IAP is not at Ready. Power off and enter it again with MENU held")
	}
	return readySnapshot{root[:sectorBytes], checksum(metadata)}, nil
}

func upload(disk sectors, data []byte, fingerprint string, report func(event)) error {
	ready, err := readReady(disk)
	if err != nil {
		return err
	}
	if ready.fingerprint != fingerprint {
		return errors.New("the IAP session changed. Check the device again before writing")
	}
	blocks := (len(data) + sectorBytes - 1) / sectorBytes
	if blocks == 0 || appStart+blocks*sectorBytes > appLimit {
		return errors.New("the transfer would cross the application boundary")
	}
	// The loader appends each data sector in arrival order. The directory
	// entry gives it the byte count and selects the default application area.
	root := ready.root
	copy(root[64:75], "APP     BIN")
	root[75] = 0x20
	binary.LittleEndian.PutUint16(root[90:], 3)
	binary.LittleEndian.PutUint32(root[92:], uint32(len(data)))
	report(event{Kind: "writing", Total: blocks + 1})
	if err := disk.write(rootLBA, root); err != nil {
		return fmt.Errorf("write IAP directory: %w", err)
	}
	var tail [sectorBytes]byte
	for i := range blocks {
		start := i * sectorBytes
		chunk := data[start:min(start+sectorBytes, len(data))]
		if len(chunk) < sectorBytes {
			for j := range tail {
				tail[j] = 0xff
			}
			copy(tail[:], chunk)
			chunk = tail[:]
		}
		if err := disk.write(uint32(dataLBA+i), chunk); err != nil {
			return fmt.Errorf("write data block %d/%d: %w", i+1, blocks, err)
		}
		report(event{Kind: "progress", Done: i + 2, Total: blocks + 1})
	}
	report(event{Kind: "checking"})
	for i := range 5 {
		if i != 0 {
			time.Sleep(250 * time.Millisecond)
		}
		if err := disk.read(rootLBA, root); err != nil {
			break
		}
		if bytes.EqualFold(root[:8], []byte("SUCCESS ")) ||
			bytes.EqualFold(root[:8], []byte("SUCCESS\x00")) {
			report(event{Kind: "success"})
			return nil
		}
		if bytes.EqualFold(root[:4], []byte("FAIL")) {
			return errors.New("the bootloader reported an update failure")
		}
	}
	report(event{Kind: "unconfirmed"})
	return nil
}
