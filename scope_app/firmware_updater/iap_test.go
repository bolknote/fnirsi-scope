package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"slices"
	"testing"
)

func fixtureMetadata() []byte {
	data := make([]byte, metadataSectors*sectorBytes)
	copy(data, "\xeb\x3c\x90MSDOS5.0")
	for offset, value := range map[int]uint16{11: sectorBytes, 14: 6, 17: 512, 19: 4095, 22: 1, 510: 0xaa55} {
		binary.LittleEndian.PutUint16(data[offset:], value)
	}
	data[13], data[16], data[21] = 4, 2, 248
	copy(data[0x3000:], []byte{0xf8, 0xff, 0xff, 0xff})
	copy(data[0x3800:], []byte{0xf8, 0xff, 0xff, 0xff})
	root := data[rootLBA*sectorBytes:]
	copy(root, "Ready   TXT")
	root[11] = 0x20
	copy(root[32:], "IAP        ")
	root[43] = 8
	return data
}

type writeCall struct {
	lba  uint32
	data []byte
}

type recordingDisk struct {
	metadata []byte
	got      []writeCall
	failAt   int
	result   string
}

func (d *recordingDisk) read(lba uint32, data []byte) error {
	if len(d.got) > 0 && lba == rootLBA {
		if d.result == "" {
			return io.ErrUnexpectedEOF
		}
		copy(data, d.result)
		return nil
	}
	if lba >= metadataSectors {
		return io.ErrUnexpectedEOF
	}
	copy(data, d.metadata[lba*sectorBytes:(lba+1)*sectorBytes])
	return nil
}

func (d *recordingDisk) write(lba uint32, data []byte) error {
	d.got = append(d.got, writeCall{lba, bytes.Clone(data)})
	if len(d.got) == d.failAt {
		return io.ErrShortWrite
	}
	return nil
}

func (d *recordingDisk) close() {}

func TestUploadWritesDirectoryAndOrderedSectors(t *testing.T) {
	disk := &recordingDisk{metadata: fixtureMetadata(), result: "SUCCESS "}
	ready, err := readReady(disk)
	if err != nil {
		t.Fatal(err)
	}
	data := bytes.Repeat([]byte{0xcd}, sectorBytes+5)
	var stages []string
	if err := upload(disk, data, ready.fingerprint, func(e event) {
		stages = append(stages, e.Kind)
		if e.Kind == "progress" && e.Total != 3 {
			t.Errorf("incorrect progress: %#v", e)
		}
	}); err != nil {
		t.Fatal(err)
	}
	if len(disk.got) != 3 || disk.got[0].lba != rootLBA || disk.got[1].lba != dataLBA || disk.got[2].lba != dataLBA+1 {
		t.Fatal("directory and data did not arrive in order")
	}
	directory := disk.got[0].data
	if !bytes.Equal(directory[:64], ready.root[:64]) || string(directory[64:75]) != "APP     BIN" ||
		binary.LittleEndian.Uint16(directory[90:]) != 3 || int(binary.LittleEndian.Uint32(directory[92:])) != len(data) {
		t.Fatalf("unexpected IAP directory entry: %x", directory[:96])
	}
	if !bytes.Equal(disk.got[1].data, data[:sectorBytes]) ||
		!bytes.Equal(disk.got[2].data[:5], data[sectorBytes:]) ||
		!bytes.Equal(disk.got[2].data[5:], bytes.Repeat([]byte{0xff}, sectorBytes-5)) {
		t.Fatal("last block was not padded correctly")
	}
	want := []string{"writing", "progress", "progress", "checking", "success"}
	if !slices.Equal(stages, want) {
		t.Errorf("stages = %v, want %v", stages, want)
	}
}

func TestUploadDoesNotStartIfTargetChanged(t *testing.T) {
	for _, offset := range []int{11, 0x3000, 0x3010, rootLBA*sectorBytes + 22, rootLBA*sectorBytes + 64} {
		data := fixtureMetadata()
		sum := checksum(data)
		data[offset] ^= 1
		disk := &recordingDisk{metadata: data}
		if err := upload(disk, []byte("firmware"), sum, func(event) {}); err == nil || len(disk.got) != 0 {
			t.Errorf("changed target at %x: writes %d, error %v", offset, len(disk.got), err)
		}
	}
}

func TestUploadStopsOnShortWrite(t *testing.T) {
	disk := &recordingDisk{metadata: fixtureMetadata(), failAt: 2}
	ready, _ := readReady(disk)
	err := upload(disk, make([]byte, 3*sectorBytes), ready.fingerprint, func(event) {})
	if !errors.Is(err, io.ErrShortWrite) || len(disk.got) != 2 || disk.got[1].lba != dataLBA {
		t.Fatalf("upload continued after a short write: writes %d, error %v", len(disk.got), err)
	}
}

func TestAClosedDeviceIsNotAnAcknowledgedUpdate(t *testing.T) {
	disk := &recordingDisk{metadata: fixtureMetadata()}
	ready, _ := readReady(disk)
	var last string
	err := upload(disk, make([]byte, sectorBytes), ready.fingerprint, func(e event) { last = e.Kind })
	if err != nil || last != "unconfirmed" || len(disk.got) != 2 {
		t.Errorf("writes %d, last event %s, error %v", len(disk.got), last, err)
	}
}

func TestSectorCommandUsesOneLogicalBlock(t *testing.T) {
	if got := sectorCommand(0x102030, true); got != ([10]byte{0x2a, 0, 0, 0x10, 0x20, 0x30, 0, 0, 1, 0}) {
		t.Fatalf("bad SCSI write command: %x", got)
	}
	if got := sectorCommand(8, false); got != ([10]byte{0x28, 0, 0, 0, 0, 8, 0, 0, 1, 0}) {
		t.Fatalf("bad SCSI read command: %x", got)
	}
}
