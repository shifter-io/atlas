//go:build race

package server_test

// The race detector slows everything ~10x; latency targets do not apply.
const raceEnabled = true
