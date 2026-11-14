package main

import (
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

type rawDisk struct{ file *os.File }

func (d rawDisk) read(lba uint32, data []byte) error {
	n, err := d.file.ReadAt(data, int64(lba)*sectorBytes)
	if err == nil && n != len(data) {
		err = io.ErrUnexpectedEOF
	}
	return err
}

func (d rawDisk) write(lba uint32, data []byte) error {
	if len(data) != sectorBytes {
		return fmt.Errorf("expected one %d-byte IAP sector", sectorBytes)
	}
	// File.WriteAt completes partial writes with another syscall, which would
	// corrupt the bootloader's append-only transfer.
	n, err := unix.Pwrite(int(d.file.Fd()), data, int64(lba)*sectorBytes)
	if err == nil && n != len(data) {
		err = io.ErrShortWrite
	}
	return err
}

func (d rawDisk) close() { _ = d.file.Close() }
