// Command simplepir-adapter drives the original Go SimplePIR (arm S0-go)
// through the adapter contract in src/adapters/README.md.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"runtime"
	"runtime/debug"

	"github.com/ahenzinger/simplepir/pir"
)

var upstreamSHA = "unknown"

const arm = "S0-go"

type usageError string

func (e usageError) Error() string { return string(e) }

func parse(args []string) (cfg config, phase string, reps, warmup int, out string, err error) {
	fs := flag.NewFlagSet("simplepir-adapter", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	armF := fs.String("arm", "", "arm to run (S0-go)")
	fs.Int64Var(&cfg.dbBytes, "db-bytes", 0, "total database size in bytes")
	fs.Int64Var(&cfg.recordBytes, "record-bytes", 0, "record size in bytes")
	fs.StringVar(&phase, "phase", "", "offline, online or all")
	fs.IntVar(&reps, "reps", 0, "measured repetitions")
	fs.IntVar(&warmup, "warmup", 0, "warm-up repetitions, recorded first")
	fs.Int64Var(&cfg.seed, "seed", 0, "seed for the database, query indices and faults")
	fs.StringVar(&cfg.fault, "fault", "none", "none, flip_answer_bit, wrong_record or flip_hint_bit")
	prob := fs.Float64("fault-prob", -1, "chance that a repetition is faulted (1.0 when --fault is not none, else 0)")
	fs.StringVar(&cfg.recovery, "recovery", "abort", "abort")
	fs.StringVar(&out, "out", "", "JSONL file to append records to")
	if err = fs.Parse(args); err != nil {
		return
	}
	bad := func(format string, a ...any) { err = usageError(fmt.Sprintf(format, a...)) }
	switch {
	case *armF != arm:
		bad("unknown arm %q, this adapter serves %s", *armF, arm)
	case cfg.recordBytes < 1:
		bad("--record-bytes must be at least 1, got %d", cfg.recordBytes)
	case cfg.dbBytes < cfg.recordBytes:
		bad("--db-bytes %d is smaller than --record-bytes %d", cfg.dbBytes, cfg.recordBytes)
	case phase != "offline" && phase != "online" && phase != "all":
		bad("--phase must be offline, online or all, got %q", phase)
	case reps < 0 || warmup < 0:
		bad("--reps and --warmup must not be negative")
	case out == "":
		bad("--out is required")
	case cfg.recovery != "abort":
		bad("--recovery %q is not available yet, only abort", cfg.recovery)
	}
	switch cfg.fault {
	case "none", "flip_answer_bit", "wrong_record", "flip_hint_bit":
	default:
		bad("unsupported --fault %q for %s", cfg.fault, arm)
	}
	if err != nil {
		return
	}
	switch {
	case *prob < 0 && cfg.fault == "none":
		cfg.faultProb = 0
	case *prob < 0:
		cfg.faultProb = 1
	case *prob > 1:
		bad("--fault-prob must be in [0, 1], got %v", *prob)
	default:
		cfg.faultProb = *prob
	}
	cfg.arm = arm
	env := func(k string) *string {
		if v, ok := os.LookupEnv(k); ok {
			return &v
		}
		return nil
	}
	cfg.runID, cfg.gitSHA, cfg.platform = env("VPIR_RUN_ID"), env("VPIR_GIT_SHA"), env("VPIR_PLATFORM")
	return
}

type writer struct{ f *os.File }

func (w writer) write(r Record) error {
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	_, err = w.f.Write(append(b, '\n'))
	return err
}

// guard turns a library panic into an error.
func guard(f func()) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("%v", r)
		}
	}()
	f()
	return
}

func run(cfg config, phase string, reps, warmup int, w writer) error {
	pi := &pir.SimplePIR{}
	var lay Layout
	var s *session
	err := guard(func() {
		var perr error
		if lay, perr = plan(pi, cfg.dbBytes, cfg.recordBytes); perr != nil {
			panic(perr)
		}
		if s, perr = newSession(cfg, lay, pi); perr != nil {
			panic(perr)
		}
	})
	if err != nil {
		return writeUnsupported(cfg, phase, lay, fmt.Errorf("library rejected the parameters: %w", err), w)
	}
	fmt.Fprintf(os.Stderr, "layout: %d records, %d pieces each, entry %d bits, p=%d, ell=%d, m=%d, record rows %d\n",
		lay.Records, lay.Pieces, lay.EntryBits, lay.Params.P, lay.Params.L, lay.Params.M, lay.RecordRows)

	total := warmup + reps
	if phase == "online" {
		if _, err := s.offline(); err != nil {
			return err
		}
	} else {
		for rep := 0; rep < total; rep++ {
			rec, err := s.offlineRecord(rep, warmup)
			if err != nil {
				return err
			}
			if err := w.write(rec); err != nil {
				return err
			}
		}
		if phase == "offline" {
			return nil
		}
		if total == 0 {
			if _, err := s.offline(); err != nil {
				return err
			}
		}
	}
	for rep := 0; rep < total; rep++ {
		rec, err := s.onlineRecord(rep, warmup)
		if err != nil {
			return err
		}
		if err := w.write(rec); err != nil {
			return err
		}
	}
	return nil
}

// writeUnsupported records a parameter rejection as one result per phase.
func writeUnsupported(cfg config, phase string, lay Layout, cause error, w writer) error {
	phases := []string{phase}
	if phase == "all" {
		phases = []string{"offline", "online"}
	}
	for _, ph := range phases {
		rec := Record{
			RunID: cfg.runID, GitSHAHarness: cfg.gitSHA, Platform: cfg.platform,
			UpstreamSHA: upstreamSHA, Adapter: "simplepir_go", Arm: cfg.arm,
			SchemeParams: map[string]any{},
			DBBytes:      cfg.dbBytes, RecordBytes: cfg.recordBytes, RetrievedBits: 8 * cfg.recordBytes,
			NRecords: uint64(cfg.dbBytes / cfg.recordBytes), Phase: ph, Seed: cfg.seed,
			Fault: cfg.fault, FaultProb: cfg.faultProb, Recovery: cfg.recovery,
			Status: "unsupported_params", Error: sptr(cause.Error()),
			Metrics: Metrics{ProcessPeakRSSBytes: peakRSS()},
		}
		if err := w.write(rec); err != nil {
			return err
		}
	}
	return nil
}

func main() {
	cfg, phase, reps, warmup, out, err := parse(os.Args[1:])
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			os.Exit(0)
		}
		var ue usageError
		if errors.As(err, &ue) {
			fmt.Fprintln(os.Stderr, "usage error:", err)
		}
		os.Exit(2)
	}
	runtime.GOMAXPROCS(1)
	debug.SetGCPercent(-1)
	// The library prints parameters and progress to stdout. Records go only to --out.
	os.Stdout = os.Stderr

	f, err := os.OpenFile(out, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "open --out:", err)
		os.Exit(1)
	}
	defer f.Close()
	if err := run(cfg, phase, reps, warmup, writer{f}); err != nil {
		fmt.Fprintln(os.Stderr, "internal failure:", err)
		os.Exit(1)
	}
}
