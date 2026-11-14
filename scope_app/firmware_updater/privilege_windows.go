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
	stream, err := openWindowsPipe(args[1])
	if err != nil {
		return 1, true
	}
	defer stream.Close()
	// A byte-count prefix lets the same pipe carry events back to the UI.
	var length uint32
	if err := binary.Read(stream, binary.LittleEndian, &length); err != nil || length > maxImageSize {
		return 1, true
	}
	code := run(args[2:], io.LimitReader(stream, int64(length)), stream)
	_ = windows.FlushFileBuffers(windows.Handle(stream.Fd()))
	return code, true
}

func openWindowsPipe(path string) (*os.File, error) {
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	handle, err := windows.CreateFile(name, windows.GENERIC_READ|windows.GENERIC_WRITE, 0, nil,
		windows.OPEN_EXISTING, windows.SECURITY_SQOS_PRESENT|windows.SECURITY_IDENTIFICATION, 0)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(handle), "iap-pipe"), nil
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
	path, handle, err := newWindowsPipe(user.User.Sid.String())
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
	if err := waitWindowsPipe(handle, process); err != nil {
		return true, err
	}
	return true, sendWindowsImage(stream, data, output)
}

func newWindowsPipe(sid string) (string, windows.Handle, error) {
	// Only the calling user and elevated administrators may open it.
	acl, err := windows.SecurityDescriptorFromString("D:P(A;;GA;;;BA)(A;;GA;;;" + sid + ")")
	if err != nil {
		return "", windows.InvalidHandle, err
	}
	attr := windows.SecurityAttributes{Length: uint32(unsafe.Sizeof(windows.SecurityAttributes{})), SecurityDescriptor: acl}
	path := `\\.\pipe\fnirsi-iap-` + rand.Text()
	name, _ := windows.UTF16PtrFromString(path)
	handle, err := windows.CreateNamedPipe(name,
		windows.PIPE_ACCESS_DUPLEX|windows.FILE_FLAG_FIRST_PIPE_INSTANCE,
		windows.PIPE_NOWAIT|windows.PIPE_REJECT_REMOTE_CLIENTS, 1, 65536, 65536, 0, &attr)
	return path, handle, err
}

func waitWindowsPipe(handle, process windows.Handle) error {
	expectedPID, err := windows.GetProcessId(process)
	if err != nil {
		return err
	}
	for deadline := time.Now().Add(15 * time.Second); ; {
		err := windows.ConnectNamedPipe(handle, nil)
		if errors.Is(err, windows.ERROR_PIPE_CONNECTED) {
			break
		}
		if err != nil && !errors.Is(err, windows.ERROR_PIPE_LISTENING) {
			return err
		}
		state, _ := windows.WaitForSingleObject(process, 50)
		if state == windows.WAIT_OBJECT_0 || time.Now().After(deadline) {
			return errors.New("the authorized updater did not connect")
		}
	}
	var peer uint32
	if err := windows.GetNamedPipeClientProcessId(handle, &peer); err != nil || peer != expectedPID {
		return errors.New("the connection did not come from the authorized updater")
	}
	state := uint32(windows.PIPE_WAIT)
	return windows.SetNamedPipeHandleState(handle, &state, nil, nil)
}

func sendWindowsImage(stream *os.File, data []byte, output io.Writer) error {
	if err := binary.Write(stream, binary.LittleEndian, uint32(len(data))); err != nil {
		return err
	}
	if _, err := stream.Write(data); err != nil {
		return err
	}
	_, err := io.Copy(output, stream)
	if errors.Is(err, windows.ERROR_BROKEN_PIPE) {
		err = nil
	}
	return err
}
