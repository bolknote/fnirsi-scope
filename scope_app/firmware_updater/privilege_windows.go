package main

import (
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"runtime"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

type shellRequest struct {
	size, mask                        uint32
	window                            windows.Handle
	verb, file, parameters, directory *uint16
	display                           int32
	instance, idList                  uintptr
	class                             *uint16
	classKey                          windows.Handle
	hotKey                            uint32
	iconOrMonitor, process            windows.Handle
}

var shellExecuteEx = windows.NewLazySystemDLL("shell32.dll").NewProc("ShellExecuteExW")

func askWindowsAdmin(exe string, args []string) (windows.Handle, error) {
	verb, _ := windows.UTF16PtrFromString("runas")
	path, err := windows.UTF16PtrFromString(exe)
	if err != nil {
		return 0, err
	}
	flags, err := windows.UTF16PtrFromString(windows.ComposeCommandLine(args))
	if err != nil {
		return 0, err
	}
	info := shellRequest{mask: 0x40 | 0x100, verb: verb, file: path, parameters: flags}
	info.size = uint32(unsafe.Sizeof(info))
	r, _, err := shellExecuteEx.Call(uintptr(unsafe.Pointer(&info)))
	runtime.KeepAlive(verb)
	runtime.KeepAlive(path)
	runtime.KeepAlive(flags)
	if r == 0 {
		return 0, err
	}
	return info.process, nil
}

func privateMode(args []string) (int, bool) {
	if len(args) == 0 || args[0] != "--pipe" {
		return 0, false
	}
	if len(args) < 3 || !windows.GetCurrentProcessToken().IsElevated() {
		return 1, true
	}
	name, err := windows.UTF16PtrFromString(args[1])
	if err != nil {
		return 1, true
	}
	handle, err := windows.CreateFile(name, windows.GENERIC_READ|windows.GENERIC_WRITE, 0, nil,
		windows.OPEN_EXISTING, windows.SECURITY_SQOS_PRESENT|windows.SECURITY_IDENTIFICATION, 0)
	if err != nil {
		return 1, true
	}
	stream := os.NewFile(uintptr(handle), "iap-pipe")
	defer stream.Close()
	// A byte-count prefix lets the same pipe carry events back to the UI.
	var length uint32
	if err := binary.Read(stream, binary.LittleEndian, &length); err != nil || length > maxImageSize {
		return 1, true
	}
	code := run(args[2:], io.LimitReader(stream, int64(length)), stream)
	_ = windows.FlushFileBuffers(handle)
	return code, true
}

func forwardPrivileged(args []string, input io.Reader, output io.Writer) (bool, error) {
	token := windows.GetCurrentProcessToken()
	if len(args) == 0 || (args[0] != "probe" && args[0] != "flash") || token.IsElevated() {
		return false, nil
	}
	exe, err := os.Executable()
	if err != nil {
		return true, err
	}
	data, err := readImage(input)
	if err != nil || len(data) > maxImageSize {
		return true, errors.New("could not prepare the firmware stream")
	}
	user, err := token.GetTokenUser()
	if err != nil {
		return true, err
	}
	// Only this user's session and elevated administrators may open it.
	acl, err := windows.SecurityDescriptorFromString("D:P(A;;GA;;;BA)(A;;GA;;;" + user.User.Sid.String() + ")")
	if err != nil {
		return true, err
	}
	attr := windows.SecurityAttributes{Length: uint32(unsafe.Sizeof(windows.SecurityAttributes{})), SecurityDescriptor: acl}
	path := `\\.\pipe\fnirsi-iap-` + rand.Text()
	name, _ := windows.UTF16PtrFromString(path)
	handle, err := windows.CreateNamedPipe(name,
		windows.PIPE_ACCESS_DUPLEX|windows.FILE_FLAG_FIRST_PIPE_INSTANCE,
		windows.PIPE_NOWAIT|windows.PIPE_REJECT_REMOTE_CLIENTS, 1, 65536, 65536, 0, &attr)
	if err != nil {
		return true, err
	}
	stream := os.NewFile(uintptr(handle), "iap-pipe")
	defer stream.Close()
	process, err := askWindowsAdmin(exe, append([]string{"--pipe", path}, args...))
	if err != nil {
		return true, fmt.Errorf("administrator authorization: %w", err)
	}
	defer windows.CloseHandle(process)
	expectedPID, err := windows.GetProcessId(process)
	if err != nil {
		return true, err
	}
	for deadline := time.Now().Add(15 * time.Second); ; {
		err := windows.ConnectNamedPipe(handle, nil)
		if err == nil || errors.Is(err, windows.ERROR_PIPE_CONNECTED) {
			break
		}
		if !errors.Is(err, windows.ERROR_PIPE_LISTENING) {
			return true, err
		}
		state, _ := windows.WaitForSingleObject(process, 50)
		if state == windows.WAIT_OBJECT_0 || time.Now().After(deadline) {
			return true, errors.New("the authorized updater did not connect")
		}
	}
	var peer uint32
	if err := windows.GetNamedPipeClientProcessId(handle, &peer); err != nil || peer != expectedPID {
		return true, errors.New("the connection did not come from the authorized updater")
	}
	state := uint32(windows.PIPE_WAIT)
	if err := windows.SetNamedPipeHandleState(handle, &state, nil, nil); err != nil {
		return true, err
	}
	if err := binary.Write(stream, binary.LittleEndian, uint32(len(data))); err != nil {
		return true, err
	}
	if _, err := stream.Write(data); err != nil {
		return true, err
	}
	_, err = io.Copy(output, stream)
	if errors.Is(err, windows.ERROR_BROKEN_PIPE) {
		err = nil
	}
	return true, err
}
