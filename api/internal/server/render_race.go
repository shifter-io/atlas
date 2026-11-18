//go:build race

package server

// raceDetector is true in a build with the race detector (the nightly go
// test -race, RACE-1): every body then compresses at LevelFast. Brotli's
// qualities 10 and 11 were most of the server tests' time under it, past
// Go's 10-minute limit (NIGHTLY-API-1); the tests without it still render
// at LevelBest, and a served build never has the race detector.
const raceDetector = true
