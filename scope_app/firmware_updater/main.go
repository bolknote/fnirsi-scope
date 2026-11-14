package main

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
)

type event struct {
	Kind        string `json:"kind"`
	Detail      string `json:"detail,omitempty"`
	ID          string `json:"id,omitempty"`
	SHA256      string `json:"sha256,omitempty"`
	Fingerprint string `json:"fingerprint,omitempty"`
	Name        string `json:"name,omitempty"`
	Size        int    `json:"size,omitempty"`
	Done        int    `json:"done,omitempty"`
	Total       int    `json:"total,omitempty"`
}

type reporter struct {
	encoder *json.Encoder
	writing bool
}

func (r *reporter) send(message event) {
	if message.Kind == "writing" {
		r.writing = true
	}
	// Closing the app must not interrupt an IAP stream that has started.
	_ = r.encoder.Encode(message)
}

func perform(args []string, input io.Reader, r *reporter) error {
	switch {
	case len(args) == 2 && args[0] == "image":
		selected, err := loadImage(args[1])
		if err != nil {
			return err
		}
		r.send(event{Kind: "image", Name: selected.name, SHA256: selected.sum, Size: len(selected.data)})
		return nil
	case len(args) == 1 && args[0] == "detect":
		device, err := selectDevice("")
		if err != nil {
			return err
		}
		r.send(event{Kind: "detected", ID: device.id, Name: device.label})
		return nil
	case len(args) == 1 && args[0] == "probe":
		device, err := selectDevice("")
		if err != nil {
			return err
		}
		disk, err := openDevice(device, false)
		if err != nil {
			return err
		}
		defer disk.close()
		ready, err := readReady(disk)
		if err != nil {
			return err
		}
		r.send(event{Kind: "ready", ID: device.id, Name: device.label, Fingerprint: ready.fingerprint})
		return nil
	case len(args) == 4 && args[0] == "flash":
		if args[1] == "" || !sha256Text(args[2]) || !sha256Text(args[3]) {
			return errors.New("the selected file and IAP attachment must be verified first")
		}
		// The caller supplies a private snapshot of the selected file and its
		// expected hash. Read and check it entirely before opening the target.
		data, err := readImage(input)
		if err != nil {
			return err
		}
		if err := checkApplication(data); err != nil {
			return err
		}
		if checksum(data) != args[2] {
			return errors.New("the firmware file changed after selection")
		}
		device, err := selectDevice(args[1])
		if err != nil {
			return err
		}
		r.send(event{Kind: "preparing"})
		disk, err := openDevice(device, true)
		if err != nil {
			return err
		}
		defer disk.close()
		return upload(disk, data, args[3], r.send)
	default:
		return errors.New("usage: fnirsi-iap image PATH | detect | probe | flash DEVICE SHA256 READY_FINGERPRINT")
	}
}

func sha256Text(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func run(args []string, input io.Reader, output io.Writer) int {
	r := reporter{encoder: json.NewEncoder(output)}
	forwarded, err := forwardPrivileged(args, input, output)
	if !forwarded && err == nil {
		err = perform(args, input, &r)
	}
	if err != nil {
		kind := "error"
		if r.writing {
			kind = "interrupted"
		}
		r.send(event{Kind: kind, Detail: err.Error()})
	}
	if err != nil {
		return 1
	}
	return 0
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Println("fnirsi-iap 1")
		return
	}
	if code, ok := privateMode(os.Args[1:]); ok {
		os.Exit(code)
	}
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout))
}
