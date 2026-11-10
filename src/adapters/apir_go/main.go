// Command apir-adapter drives Colombo et al.'s apir-code for arms S3, S4, M0
// and M1 and writes one JSON record per repetition and phase, following the
// adapter contract in src/adapters/README.md.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math/rand/v2"
	"os"
	"runtime"
	"slices"
	"sort"
	"strconv"
	"strings"
)

var upstreamSHA = "unknown"

type config struct {
	armName              string
	arm                  arm
	geom                 geometry
	dbBytes, recordBytes int
	phase                string
	reps, warmup         int
	seed                 uint64
	fault                string
	faultProb            float64
	recovery             string
	out                  string
}

type usageError struct{ msg string }

func (e usageError) Error() string { return e.msg }

func main() {
	runtime.GOMAXPROCS(1)
	cfg, err := parseArgs(os.Args[1:])
	if errors.Is(err, flag.ErrHelp) {
		os.Exit(0)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "apir-adapter:", err)
		os.Exit(2)
	}
	if err := run(cfg); err != nil {
		fmt.Fprintln(os.Stderr, "apir-adapter:", err)
		os.Exit(1)
	}
}

func parseArgs(args []string) (config, error) {
	var cfg config
	fs := flag.NewFlagSet("apir-adapter", flag.ContinueOnError)
	fs.StringVar(&cfg.armName, "arm", "", "arm: "+strings.Join(armNames(), ", "))
	fs.IntVar(&cfg.dbBytes, "db-bytes", 0, "total database size in bytes")
	fs.IntVar(&cfg.recordBytes, "record-bytes", 0, "record size in bytes; 0 for S3 and S4, which retrieve one bit")
	fs.StringVar(&cfg.phase, "phase", "", "offline, online or all")
	fs.IntVar(&cfg.reps, "reps", 0, "recorded repetitions per phase")
	fs.IntVar(&cfg.warmup, "warmup", 0, "warm-up repetitions per phase, recorded with is_warmup=true")
	fs.Uint64Var(&cfg.seed, "seed", 0, "seed for the database, query indices and faults")
	fs.StringVar(&cfg.fault, "fault", "", "none, flip_answer_bit or wrong_record")
	probSet := false
	fs.Func("fault-prob", "chance that each online repetition is faulted (default 1.0 when --fault is not none)", func(v string) error {
		p, err := strconv.ParseFloat(v, 64)
		if err != nil || p < 0 || p > 1 {
			return fmt.Errorf("must be a number in [0, 1]")
		}
		cfg.faultProb, probSet = p, true
		return nil
	})
	fs.StringVar(&cfg.recovery, "recovery", "abort", "abort (the only policy until P7)")
	fs.StringVar(&cfg.out, "out", "", "JSONL file the records are appended to")
	fs.Usage = func() {
		fmt.Fprintln(fs.Output(), `usage: apir-adapter --arm ARM --db-bytes N --record-bytes N --phase offline|online|all
                    --reps N --warmup N --seed N --fault KIND [--fault-prob F]
                    [--recovery abort] --out PATH.jsonl`)
		fs.PrintDefaults()
	}
	if err := fs.Parse(args); err != nil {
		return cfg, err
	}
	if fs.NArg() > 0 {
		return cfg, usageError{fmt.Sprintf("unexpected arguments: %v", fs.Args())}
	}
	set := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { set[f.Name] = true })
	for _, name := range []string{"arm", "db-bytes", "record-bytes", "phase", "reps", "warmup", "seed", "fault", "out"} {
		if !set[name] {
			return cfg, usageError{"missing --" + name}
		}
	}
	a, ok := arms[cfg.armName]
	if !ok {
		return cfg, usageError{fmt.Sprintf("unknown arm %q; this adapter serves %s", cfg.armName, strings.Join(armNames(), ", "))}
	}
	cfg.arm = a
	geom, err := geometryFor(a, cfg.dbBytes, cfg.recordBytes)
	if err != nil {
		return cfg, usageError{fmt.Sprintf("--arm %s: %v", cfg.armName, err)}
	}
	cfg.geom = geom
	if !slices.Contains([]string{"offline", "online", "all"}, cfg.phase) {
		return cfg, usageError{fmt.Sprintf("unknown --phase %q", cfg.phase)}
	}
	if cfg.reps < 0 || cfg.warmup < 0 {
		return cfg, usageError{"--reps and --warmup must be non-negative"}
	}
	if !a.faults[cfg.fault] {
		return cfg, usageError{fmt.Sprintf("--fault %q is not supported by arm %s", cfg.fault, cfg.armName)}
	}
	if !probSet && cfg.fault != "none" {
		cfg.faultProb = 1
	}
	if cfg.recovery != "abort" {
		return cfg, usageError{fmt.Sprintf("--recovery %q is not supported; only abort exists until P7, and retry_same is never allowed", cfg.recovery)}
	}
	return cfg, nil
}

func armNames() []string {
	names := make([]string, 0, len(arms))
	for n := range arms {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// recordWriter appends one JSON line per record and owns the fields that are
// the same for every record of the run.
type recordWriter struct {
	f    *os.File
	base Record
}

func (w *recordWriter) write(r Record) error {
	r.Metrics.ProcessPeakRSSBytes = ptr(peakRSSBytes())
	line, err := json.Marshal(r)
	if err != nil {
		return err
	}
	_, err = w.f.Write(append(line, '\n'))
	return err
}

func envOrNull(name string) *string {
	if v, ok := os.LookupEnv(name); ok {
		return &v
	}
	return nil
}

func run(cfg config) (err error) {
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("panic: %v", p)
		}
	}()
	f, err := os.OpenFile(cfg.out, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	w := &recordWriter{f: f, base: Record{
		RunID: envOrNull("VPIR_RUN_ID"), GitSHAHarness: envOrNull("VPIR_GIT_SHA"), Platform: envOrNull("VPIR_PLATFORM"),
		UpstreamSHA: upstreamSHA, Adapter: "apir_go", Arm: cfg.armName,
		DBBytes: cfg.dbBytes, RetrievedBits: cfg.geom.retrievedBits, NRecords: cfg.geom.nRecords,
		Seed: cfg.seed, Fault: cfg.fault, FaultProb: cfg.faultProb, Recovery: cfg.recovery, Status: "ok",
	}}
	if !cfg.arm.oneBit {
		w.base.RecordBytes = ptr(cfg.recordBytes)
	}
	phases := map[string][]string{"offline": {"offline"}, "online": {"online"}, "all": {"offline", "online"}}[cfg.phase]

	logf("%s: laying out %d-byte database (seed %d)", cfg.armName, cfg.dbBytes, cfg.seed)
	c, err := cfg.arm.build(cfg.geom, cfg.seed)
	if errors.As(err, new(unsupportedError)) {
		for _, ph := range phases {
			r := w.base
			r.Phase, r.Status, r.Error = ph, "unsupported_params", ptr(err.Error())
			r.SchemeParams = map[string]any{"db_bits": cfg.geom.dbBits}
			if err := w.write(r); err != nil {
				return err
			}
		}
		logf("%s: %v", cfg.armName, err)
		return nil
	}
	if err != nil {
		return err
	}

	var cost offlineCost
	ran := false
	if slices.Contains(phases, "offline") {
		if cost, ran, err = runOffline(cfg, c, w); err != nil {
			return err
		}
	}
	if !slices.Contains(phases, "online") {
		return nil
	}
	if !ran {
		logf("%s: untimed offline phase", cfg.armName)
		cost = c.setup()
	}
	return runOnline(cfg, c, w, cost.storageBytes)
}

// runOffline returns the last repetition's cost; its setup is the state the
// online phase uses.
func runOffline(cfg config, c cell, w *recordWriter) (cost offlineCost, ran bool, err error) {
	for rep := 0; rep < cfg.warmup+cfg.reps; rep++ {
		logf("%s: offline rep %d/%d", cfg.armName, rep+1, cfg.warmup+cfg.reps)
		runtime.GC()
		cost, ran = c.setup(), true
		r := w.base
		r.Phase, r.Rep, r.IsWarmup = "offline", rep, rep < cfg.warmup
		r.SchemeParams = c.params()
		a := c.analytical()
		r.Analytical.OfflineDownBytesHintOnly = a.OfflineDownBytesHintOnly
		m := &r.Metrics
		m.ServerPreprocS, m.ServerPreprocCPUS = ptr(cost.serverPreproc.wall), ptr(cost.serverPreproc.cpu)
		m.ClientOfflineS, m.ClientOfflineCPUS = ptr(cost.clientOffline.wall), ptr(cost.clientOffline.cpu)
		m.OfflineDownBytes, m.OfflineDownBytesHintOnly = ptr(cost.downBytes), ptr(cost.downHintBytes)
		m.OfflineUpBytes, m.ClientStorageBytes = ptr(cost.upBytes), ptr(cost.storageBytes)
		if cfg.arm.verifiable {
			r.Detected = ptr(false)
		}
		if err = w.write(r); err != nil {
			return cost, ran, err
		}
	}
	return cost, ran, nil
}

func runOnline(cfg config, c cell, w *recordWriter, storage int) error {
	indices := rand.New(stream(cfg.seed, "index"))
	faults := rand.New(stream(cfg.seed, "fault"))
	checkedFlip := false
	n := cfg.warmup + cfg.reps
	for rep := 0; rep < n; rep++ {
		index := indices.IntN(cfg.geom.nRecords)
		runtime.GC()
		queries, qs := c.query(index)
		answers := make([][]byte, len(queries))
		answerS := make([]span, len(queries))
		for k, q := range queries {
			answers[k], answerS[k] = c.answer(k, q)
		}
		up, down := lens(queries), lens(answers)

		faulted := cfg.fault != "none" && faults.Float64() < cfg.faultProb
		var honestOut []byte
		if faulted && !checkedFlip && cfg.fault == "flip_answer_bit" {
			var scratch span
			out, err := c.reconstruct(answers, &scratch)
			if err != nil {
				return fmt.Errorf("honest reconstruction rejected before fault injection: %w", err)
			}
			honestOut = out
		}
		if faulted {
			injectFault(cfg, c, faults, index, answers)
		}
		var rs span
		out, rejected, panicMsg := reconstructGuarded(c, answers, &rs, faulted)
		if honestOut != nil {
			if !rejected && bytes.Equal(out, honestOut) {
				return fmt.Errorf("rep %d: flip_answer_bit left the client's output unchanged; the flip rule picks a bit the output does not depend on", rep)
			}
			checkedFlip = true
			logf("%s: rep %d: checked that the flipped bit changes the honest output (rejected=%v)", cfg.armName, rep, rejected)
		}
		if panicMsg != "" {
			logf("%s: rep %d: reconstruction of a faulted answer panicked, recorded as rejected: %s", cfg.armName, rep, panicMsg)
		}

		r := w.base
		r.Phase, r.Rep, r.IsWarmup, r.Faulted = "online", rep, rep < cfg.warmup, faulted
		r.SchemeParams = c.params()
		a := c.analytical()
		r.Analytical.UpBytes, r.Analytical.DownBytes = a.UpBytes, a.DownBytes
		m := &r.Metrics
		m.QueryGenS, m.QueryGenCPUS = ptr(qs.wall), ptr(qs.cpu)
		m.UpBytes, m.UpBytesPerServer = ptr(sum(up)), up
		m.DownBytes, m.DownBytesPerServer = ptr(sum(down)), down
		for _, s := range answerS {
			m.ServerAnswerS = append(m.ServerAnswerS, s.wall)
			m.ServerAnswerCPUS = append(m.ServerAnswerCPUS, s.cpu)
		}
		m.ClientRecoverS, m.ClientRecoverCPUS = ptr(rs.wall), ptr(rs.cpu)
		m.VerifyFused = ptr(cfg.arm.verifiable)
		r.Correct = ptr(!rejected && bytes.Equal(out, c.truth(index)))
		if cfg.arm.verifiable {
			r.Detected = ptr(rejected)
		}
		if err := w.write(r); err != nil {
			return err
		}
		if (rep+1)%10 == 0 || rep+1 == n {
			logf("%s: online rep %d/%d", cfg.armName, rep+1, n)
		}
	}
	return nil
}

// injectFault changes the serialised answers before the client parses them
// (contract fault table). Both faults hit one server, chosen at random.
func injectFault(cfg config, c cell, rng *rand.Rand, index int, answers [][]byte) {
	k := rng.IntN(len(answers))
	switch cfg.fault {
	case "flip_answer_bit":
		bit := pickBit(rng, c.flipBits(index))
		answers[k][bit/8] ^= 1 << (bit % 8)
	case "wrong_record":
		other := (index + 1 + rng.IntN(cfg.geom.nRecords-1)) % cfg.geom.nRecords
		answers[k] = c.shadowAnswer(other, k)
	}
}

// pickBit draws uniformly over all bits in ranges.
func pickBit(rng *rand.Rand, ranges []bitRange) int {
	total := 0
	for _, r := range ranges {
		total += r.n
	}
	i := rng.IntN(total)
	for _, r := range ranges {
		if i < r.n {
			return r.first + i
		}
		i -= r.n
	}
	panic("unreachable")
}

// reconstructGuarded treats a panic while reconstructing a faulted answer as
// a rejection: the client output nothing. A panic on an honest answer is an
// internal failure and propagates.
func reconstructGuarded(c cell, answers [][]byte, sp *span, faulted bool) (out []byte, rejected bool, panicMsg string) {
	defer func() {
		if !faulted {
			return
		}
		if p := recover(); p != nil {
			out, rejected, panicMsg = nil, true, fmt.Sprint(p)
		}
	}()
	out, err := c.reconstruct(answers, sp)
	return out, err != nil, ""
}

func lens(msgs [][]byte) []int {
	n := make([]int, len(msgs))
	for i, m := range msgs {
		n[i] = len(m)
	}
	return n
}

func sum(xs []int) int {
	t := 0
	for _, x := range xs {
		t += x
	}
	return t
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}

func logf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "apir-adapter: "+format+"\n", args...)
}
