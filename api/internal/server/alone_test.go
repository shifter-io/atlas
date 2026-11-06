package server

import (
	"bytes"
	"os"
	"os/exec"
	"testing"
)

// aloneEnv names, in a child process of the test binary, the test it runs.
const aloneEnv = "SERVER_TEST_ALONE"

// alone runs the calling test by itself in a child process of the test
// binary, so a test that sets GOGC or measures the whole heap shares that
// process with no other test while it runs in parallel with them here. It
// reports whether the caller is that child, which runs the test's body; the
// parent, on false, returns at once with the child's output logged and its
// failure its own.
func alone(t *testing.T) bool {
	t.Helper()
	if os.Getenv(aloneEnv) == t.Name() {
		return true
	}
	t.Parallel()
	args := []string{"-test.run=^" + t.Name() + "$", "-test.count=1", "-test.v"}
	if testing.Short() {
		args = append(args, "-test.short")
	}
	cmd := exec.Command(os.Args[0], args...)
	cmd.Env = append(os.Environ(), aloneEnv+"="+t.Name())
	out, err := cmd.CombinedOutput()
	t.Logf("%s alone:\n%s", t.Name(), out)
	if err != nil {
		t.Fatalf("%s alone: %v", t.Name(), err)
	}
	// Never an empty pass: the child ran the test and it passed.
	if !bytes.Contains(out, []byte("--- PASS: "+t.Name()+" ")) {
		t.Fatalf("%s alone did not pass", t.Name())
	}
	return false
}
