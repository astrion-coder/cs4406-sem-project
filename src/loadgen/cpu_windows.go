//go:build windows

package main

import (
	"syscall"
	"time"
)

const cpuSource = "GetProcessTimes"

// processCPU is this process's user plus kernel CPU time. Windows has no
// getrusage; GetProcessTimes reports the same two times in 100 ns units.
func processCPU() (time.Duration, bool) {
	var creation, exit, kernel, user syscall.Filetime
	h, err := syscall.GetCurrentProcess()
	if err != nil {
		return 0, false
	}
	if err := syscall.GetProcessTimes(h, &creation, &exit, &kernel, &user); err != nil {
		return 0, false
	}
	ticks := func(f syscall.Filetime) int64 { return int64(f.HighDateTime)<<32 | int64(f.LowDateTime) }
	return time.Duration((ticks(kernel) + ticks(user)) * 100), true
}
