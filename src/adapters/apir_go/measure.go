package main

import (
	"crypto/sha256"
	"encoding/binary"
	"math"
	"math/rand/v2"
	"runtime"
	"syscall"
	"time"
)

// span is one timed scheme call: wall-clock and process CPU seconds.
type span struct{ wall, cpu float64 }

func timed(f func()) span {
	var sp span
	timeInto(&sp, f)
	return sp
}

// timeInto fills sp even when f panics, so a rejection by panic keeps its time.
func timeInto(sp *span, f func()) {
	c0 := cpuSeconds()
	t0 := time.Now()
	defer func() {
		// getrusage has microsecond resolution; rounding drops float noise.
		cpu := math.Round((cpuSeconds()-c0)*1e6) / 1e6
		*sp = span{wall: time.Since(t0).Seconds(), cpu: cpu}
	}()
	f()
}

func cpuSeconds() float64 {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		panic(err)
	}
	tv := func(t syscall.Timeval) float64 { return float64(t.Sec) + float64(t.Usec)*1e-6 }
	return tv(ru.Utime) + tv(ru.Stime)
}

// peakRSSBytes converts ru_maxrss, which is bytes on macOS and KiB on Linux.
func peakRSSBytes() int64 {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		panic(err)
	}
	if runtime.GOOS == "linux" {
		return int64(ru.Maxrss) * 1024
	}
	return int64(ru.Maxrss)
}

// stream returns a ChaCha8 generator keyed by SHA-256(label, seed), so each
// consumer (database, indices, faults, clients) has its own reproducible stream
// and drawing from one never shifts another.
func stream(seed uint64, label string) *rand.ChaCha8 {
	var s [8]byte
	binary.BigEndian.PutUint64(s[:], seed)
	return rand.NewChaCha8(sha256.Sum256(append([]byte(label+"\x00"), s[:]...)))
}

func streamBytes(seed uint64, label string, n int) []byte {
	b := make([]byte, n)
	if _, err := stream(seed, label).Read(b); err != nil {
		panic(err)
	}
	return b
}
