package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
)

func init() {
	// If the UI closes, a broken progress pipe must not interrupt the
	// elevated process in the middle of writing the application.
	signal.Ignore(syscall.SIGPIPE)
}

func privateMode(_ []string) (int, bool) { return 0, false }

func forwardPrivileged(args []string, input io.Reader, output io.Writer) (bool, error) {
	if len(args) == 0 || (args[0] != "probe" && args[0] != "flash") || os.Geteuid() == 0 {
		return false, nil
	}
	exe, err := os.Executable()
	if err != nil {
		return true, err
	}
	cmd := exec.Command("/usr/bin/pkexec", append([]string{"--disable-internal-agent", exe}, args...)...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = input, output, os.Stderr
	err = cmd.Run()
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		switch exit.ExitCode() {
		case 126:
			return true, errors.New("administrator permission was cancelled")
		case 127:
			return true, errors.New("administrator permission is unavailable. Install pkexec and run a desktop polkit authentication agent")
		default:
			// The elevated helper already reported the device error.
			return true, nil
		}
	}
	if err != nil {
		return true, fmt.Errorf("start pkexec: %w", err)
	}
	return true, nil
}
