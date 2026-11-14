package main

import (
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

func peerUID(c *net.UnixConn) (uint32, error) {
	raw, err := c.SyscallConn()
	if err != nil {
		return 0, err
	}
	var cred *unix.Xucred
	var queryErr error
	if err := raw.Control(func(fd uintptr) {
		cred, queryErr = unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
	}); err != nil {
		return 0, err
	}
	if queryErr != nil {
		return 0, queryErr
	}
	return cred.Uid, nil
}

func privateMode(args []string) (int, bool) {
	if len(args) == 0 || args[0] != "--socket" {
		return 0, false
	}
	if len(args) < 4 || os.Geteuid() != 0 {
		return 1, true
	}
	expected, err := strconv.Atoi(args[2])
	if err != nil || expected <= 0 {
		return 1, true
	}
	conn, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: args[1], Net: "unix"})
	if err != nil {
		return 1, true
	}
	defer conn.Close()
	owner, err := peerUID(conn)
	if err != nil || owner != uint32(expected) {
		return 1, true
	}
	return run(args[3:], conn, conn), true
}

func forwardPrivileged(args []string, input io.Reader, output io.Writer) (bool, error) {
	if len(args) == 0 || (args[0] != "probe" && args[0] != "flash") || os.Geteuid() == 0 {
		return false, nil
	}
	exe, err := os.Executable()
	if err != nil {
		return true, err
	}
	dir, err := os.MkdirTemp("/tmp", "fnirsi-iap-")
	if err != nil {
		return true, err
	}
	defer os.RemoveAll(dir)
	name := filepath.Join(dir, "update.sock")
	server, err := net.ListenUnix("unix", &net.UnixAddr{Name: name, Net: "unix"})
	if err != nil {
		return true, err
	}
	defer server.Close()
	// osascript receives literal argv. AppleScript quotes every argument
	// before handing the fixed command to the system authorization dialog.
	script := `on run argv
set commandLine to quoted form of (item 1 of argv)
repeat with i from 2 to (count of argv)
set commandLine to commandLine & " " & quoted form of (item i of argv)
end repeat
do shell script commandLine with administrator privileges
end run`
	argv := append([]string{"-e", script, "--", exe, "--socket", name, strconv.Itoa(os.Getuid())}, args...)
	cmd := exec.Command("/usr/bin/osascript", argv...)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return true, err
	}
	done := make(chan error, 1)
	go func() {
		done <- cmd.Wait()
		_ = server.Close() // Also wakes AcceptUnix when the user cancels.
	}()
	connection, err := server.AcceptUnix()
	if err != nil {
		<-done
		return true, fmt.Errorf("administrator authorization did not complete: %s", strings.TrimSpace(stderr.String()))
	}
	defer connection.Close()
	uid, credErr := peerUID(connection)
	if credErr != nil || uid != 0 {
		return true, errors.New("the connected updater is not the authorized macOS process")
	}
	// Fork a writer so the parent can stream the device result while draining
	// the reply. The helper reads firmware data to the end of this stream.
	writeDone := make(chan error, 1)
	go func() {
		_ = connection.SetWriteDeadline(time.Now().Add(15 * time.Second))
		_, writeErr := io.Copy(connection, input)
		_ = connection.CloseWrite()
		writeDone <- writeErr
	}()
	_, readErr := io.Copy(output, connection)
	<-writeDone
	<-done
	if readErr != nil {
		return true, fmt.Errorf("read updater response: %w", readErr)
	}
	return true, nil
}
