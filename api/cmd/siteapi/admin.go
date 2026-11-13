package main

import (
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"time"
)

// listenLoopback listens on addr, which must be a loopback address
// (127.0.0.1, ::1 or localhost); what names the listener in errors.
func listenLoopback(what, addr string) (net.Listener, error) {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", what, err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return nil, fmt.Errorf("%s: the address must be loopback (127.0.0.1, ::1 or localhost)", what)
	}
	l, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", what, err)
	}
	return l, nil
}

// startAdmin serves h (server.AdminHandler: the full /healthz) on the
// loopback address addr until the process ends, and returns where it
// listens; none when addr is empty.
func startAdmin(addr string, h http.Handler) (net.Addr, error) {
	if addr == "" {
		return nil, nil
	}
	l, err := listenLoopback("admin", addr)
	if err != nil {
		return nil, err
	}
	hs := &http.Server{Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second}
	go func() {
		if err := hs.Serve(l); !errors.Is(err, http.ErrServerClosed) {
			slog.Error("admin listener stopped", "error", err)
		}
	}()
	slog.Info("admin listening", "addr", l.Addr().String())
	return l.Addr(), nil
}
