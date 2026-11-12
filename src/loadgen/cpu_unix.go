//go:build unix

package main

import (
	"syscall"
	"time"
)

const cpuSource = "getrusage"

// processCPU is this process's user plus system CPU time.
func processCPU() (time.Duration, bool) {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return 0, false
	}
	return time.Duration(ru.Utime.Nano() + ru.Stime.Nano()), true
}
