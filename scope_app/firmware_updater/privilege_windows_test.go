package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"os/exec"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

func TestWindowsFirmwarePipeClient(t *testing.T) {
	path := os.Getenv("FNIRSI_TEST_IAP_PIPE")
	if path == "" {
		t.Skip("pipe client is run by TestWindowsFirmwarePipe")
	}
	stream, err := openWindowsPipe(path)
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	var length uint32
	if err := binary.Read(stream, binary.LittleEndian, &length); err != nil || length > maxImageSize {
		t.Fatalf("pipe input length %d: %v", length, err)
	}
	data, err := io.ReadAll(io.LimitReader(stream, int64(length)))
	if err != nil || len(data) != int(length) {
		t.Fatalf("read pipe payload: %d/%d bytes, %v", len(data), length, err)
	}
	if _, err := fmt.Fprint(stream, checksum(data)); err != nil {
		t.Fatal(err)
	}
	if err := windows.FlushFileBuffers(windows.Handle(stream.Fd())); err != nil {
		t.Fatal(err)
	}
}

func TestWindowsFirmwarePipe(t *testing.T) {
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		t.Fatal(err)
	}
	path, handle, err := newWindowsPipe(user.User.Sid.String())
	if err != nil {
		t.Fatal(err)
	}
	stream := os.NewFile(uintptr(handle), "iap-test-pipe")
	defer stream.Close()
	cmd := exec.Command(os.Args[0], "-test.run=^TestWindowsFirmwarePipeClient$")
	cmd.Env = append(os.Environ(), "FNIRSI_TEST_IAP_PIPE="+path)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer cmd.Wait()
	timeout := time.AfterFunc(20*time.Second, func() { _ = cmd.Process.Kill() })
	defer timeout.Stop()
	defer cmd.Process.Kill()
	process, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION|windows.SYNCHRONIZE, false, uint32(cmd.Process.Pid))
	if err != nil {
		t.Fatal(err)
	}
	defer windows.CloseHandle(process)
	if err := waitWindowsPipe(handle, process); err != nil {
		t.Fatal(err)
	}
	data := bytes.Repeat([]byte{0xab, 0xcd}, maxImageSize/2)
	var result bytes.Buffer
	if err := sendWindowsImage(stream, data, &result); err != nil {
		t.Fatal(err)
	}
	if result.String() != checksum(data) {
		t.Fatalf("wrong digest received: %s", result.String())
	}
}
