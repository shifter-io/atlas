package server_test

import (
	"bytes"
	"io"

	"github.com/andybalholm/brotli"
)

func brotliReader(b []byte) io.Reader { return brotli.NewReader(bytes.NewReader(b)) }
