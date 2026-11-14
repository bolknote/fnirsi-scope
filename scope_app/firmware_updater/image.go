package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

const (
	appStart       = 0x08007000
	appLimit       = 0x080ff800
	stockImageSize = 751232
	maxImageSize   = appLimit - appStart
	stockSHA       = "a17c5c35c97bb898f15672a1747bc1041d8ed507c16999ddba0d1e4e2ec0c760"
)

var publishedImages = map[string]string{
	stockSHA: "FNIRSI 1.2.0",
	"8cd9a9bf391924aac9a9942dcec865ef0c694889fd366ecf5dcaff79ee37c7bf": "RU REMOTE r6j",
	"5bd3f01bc0c294158f68b155f60a70bb0131f3f0b5301719e1cff1dcd96e9ce9": "RU REMOTE r6j",
}

type image struct {
	data []byte
	sum  string
	name string
}

func checksum(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func readImage(input io.Reader) ([]byte, error) {
	return io.ReadAll(io.LimitReader(input, maxImageSize+1))
}

func checkApplication(data []byte) error {
	if len(data) < stockImageSize || len(data) > maxImageSize {
		return errors.New("the image size does not fit the 2C53T application region")
	}
	// All supported images retain the 1.2.0 vector table and the unchanged
	// end of the stock application. The rest may include locally built code.
	if checksum(data[:0x180]) != "7c8c2632bc09d84cd7904e65ac874baa635ba4a826f425f4ff64a9949e8dd806" ||
		checksum(data[0xb4000:stockImageSize]) != "7d0758782c6ccf3a3700413b5faf8727e18f44171cb038db6f6c15b6d38582f1" {
		return errors.New("the image does not match the FNIRSI 2C53T 1.2.0 application layout")
	}
	sp := binary.LittleEndian.Uint32(data)
	reset := binary.LittleEndian.Uint32(data[4:])
	if sp <= 0x20000000 || sp > 0x20038000 || sp%8 != 0 ||
		reset&1 != 1 || reset&^1 < appStart || reset&^1 >= uint32(appStart+len(data)) {
		return errors.New("invalid reset vector")
	}
	return nil
}

func loadImage(path string) (image, error) {
	f, err := os.Open(path)
	if err != nil {
		return image{}, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return image{}, err
	}
	if !info.Mode().IsRegular() {
		return image{}, errors.New("select a firmware file on disk")
	}
	data, err := readImage(f)
	if err != nil {
		return image{}, err
	}
	if err := checkApplication(data); err != nil {
		return image{}, err
	}
	sum := checksum(data)
	if name := publishedImages[sum]; name != "" {
		return image{data, sum, name}, nil
	}
	// A different Clang, font rasterizer or startup image changes the hash.
	// For a local build, check the checksum recorded by the modifier itself.
	manifest, err := os.Open(filepath.Join(filepath.Dir(path), "manifest.json"))
	if err != nil {
		return image{}, errors.New("unrecognized image. For a local r6j build select the BIN beside its original manifest.json")
	}
	defer manifest.Close()
	var build struct {
		Revision      string
		StockSHA256   string
		PatchedSHA256 string
		PatchedBytes  int
	}
	if err := json.NewDecoder(io.LimitReader(manifest, 1<<20)).Decode(&build); err != nil {
		return image{}, fmt.Errorf("read build manifest: %w", err)
	}
	if build.Revision != "1.2.0-RU-REMOTE-r6j" || build.StockSHA256 != stockSHA ||
		sum != build.PatchedSHA256 || len(data) != build.PatchedBytes || len(data) <= stockImageSize {
		return image{}, errors.New("the image does not match its r6j build manifest")
	}
	return image{data, sum, "RU REMOTE r6j"}, nil
}
