//go:build !unix && !windows

package main

import "time"

const cpuSource = "none"

func processCPU() (time.Duration, bool) { return 0, false }
