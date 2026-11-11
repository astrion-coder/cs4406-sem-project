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

	serve "github.com/imankalyan/cs4406-sem-project/src/adapters/serve_go"
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
	replicasCheat        bool
	out                  string
}

type usageError struct{ msg string }

func (e usageError) Error() string { return e.msg }

func main() {
	if serve.ModeOf(os.Args[1:]) != "bench" {
		os.Exit(serve.Main(os.Args[1:], serveArm{}))
	}
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
	fs.String("mode", "bench", "bench (serve and client are handled before this parser)")
	fs.StringVar(&cfg.armName, "arm", "", "arm: "+strings.Join(armNames(), ", "))
	fs.IntVar(&cfg.dbBytes, "db-bytes", 0, "total database size in bytes")
	fs.IntVar(&cfg.recordBytes, "record-bytes", 0, "record size in bytes; 0 for S3 and S4, which retrieve one bit")
	fs.StringVar(&cfg.phase, "phase", "", "offline, online or all")
	fs.IntVar(&cfg.reps, "reps", 0, "recorded repetitions per phase")
	fs.IntVar(&cfg.warmup, "warmup", 0, "warm-up repetitions per phase, recorded with is_warmup=true")
	fs.Uint64Var(&cfg.seed, "seed", 0, "seed for the database, query indices and faults")
	fs.StringVar(&cfg.fault, "fault", "", "none, flip_answer_bit, wrong_record, stale_record or drop_server")
	probSet := false
	fs.Func("fault-prob", "chance that each online repetition is faulted (default 1.0 when --fault is not none)", func(v string) error {
		p, err := strconv.ParseFloat(v, 64)
		if err != nil || p < 0 || p > 1 {
			return fmt.Errorf("must be a number in [0, 1]")
		}
		cfg.faultProb, probSet = p, true
		return nil
	})
	fs.StringVar(&cfg.recovery, "recovery", "abort", "abort or switch_replica (verifiable arms only)")
	fs.BoolVar(&cfg.replicasCheat, "replicas-cheat", false, "fault every replica that switch_replica turns to")
	fs.StringVar(&cfg.out, "out", "", "JSONL file the records are appended to")
	fs.Usage = func() {
		fmt.Fprintln(fs.Output(), `usage: apir-adapter --arm ARM --db-bytes N --record-bytes N --phase offline|online|all
                    --reps N --warmup N --seed N --fault KIND [--fault-prob F]
                    [--recovery abort|switch_replica] [--replicas-cheat] --out PATH.jsonl`)
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
	switch {
	case cfg.recovery == "abort":
	case cfg.recovery == "switch_replica" && a.verifiable:
	default:
		return cfg, usageError{fmt.Sprintf("--recovery %q is not supported by arm %s (retry_same is never allowed, re_preprocess needs a preprocessing proof)", cfg.recovery, cfg.armName)}
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

// baseRecord is the part of every record that the run fixes.
func baseRecord(cfg config) Record {
	r := Record{
		RunID: envOrNull("VPIR_RUN_ID"), GitSHAHarness: envOrNull("VPIR_GIT_SHA"), Platform: envOrNull("VPIR_PLATFORM"),
		UpstreamSHA: upstreamSHA, Adapter: "apir_go", Arm: cfg.armName,
		DBBytes: cfg.dbBytes, RetrievedBits: cfg.geom.retrievedBits, NRecords: cfg.geom.nRecords,
		Seed: cfg.seed, Fault: cfg.fault, FaultProb: cfg.faultProb, Recovery: cfg.recovery, Status: "ok",
	}
	if !cfg.arm.oneBit {
		r.RecordBytes = ptr(cfg.recordBytes)
	}
	return r
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
	w := &recordWriter{f: f, base: baseRecord(cfg)}
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

// attempt is one query and its answers, from query generation to the
// client's verdict. A missing answer (drop_server) counts as a rejection.
type attempt struct {
	qs, rs    span
	answerS   []span
	up, down  []int
	out       []byte
	honestOut []byte
	rejected  bool
	dropped   bool
	panicMsg  string
	refused   string // why the adapter did not call upstream's reconstruction
}

// decodeGuard is a cell whose upstream reconstruction a faulted answer can
// drive into an allocation this machine cannot hold. refuseDecode says why it
// must not run, or returns "".
type decodeGuard interface {
	refuseDecode(answers [][]byte) string
}

// doAttempt runs one query for index. inject, when non-nil, changes the
// answers before the client sees them. checkFlip also reconstructs the honest
// answer first, so the caller can compare it with the faulted output.
func doAttempt(c cell, index int, inject func(queries, answers [][]byte), checkFlip bool) (a attempt, err error) {
	queries, qs := c.query(index)
	answers := make([][]byte, len(queries))
	a.qs, a.answerS = qs, make([]span, len(queries))
	for k, q := range queries {
		answers[k], a.answerS[k] = c.answer(k, q)
	}
	a.up = lens(queries)
	if checkFlip {
		var scratch span
		if a.honestOut, err = c.reconstruct(answers, &scratch); err != nil {
			return a, fmt.Errorf("honest reconstruction rejected before fault injection: %w", err)
		}
	}
	faulted := inject != nil
	if faulted {
		inject(queries, answers)
	}
	a.down = lens(answers) // a withheld (nil) answer sends 0 bytes
	for _, ans := range answers {
		if ans == nil {
			a.dropped = true
		}
	}
	if a.dropped {
		a.rejected = true
		return a, nil
	}
	if g, ok := c.(decodeGuard); ok {
		if a.refused = g.refuseDecode(answers); a.refused != "" {
			a.rejected = true
			return a, nil
		}
	}
	a.out, a.rejected, a.panicMsg = reconstructGuarded(c, answers, &a.rs, faulted)
	return a, nil
}

// recoveryBudget is the number of further attempts after the first rejection
// (the k of the disabled retry_same, contract "Recovery").
const recoveryBudget = 3

func runOnline(cfg config, c cell, w *recordWriter, storage int) error {
	indices := rand.New(stream(cfg.seed, "index"))
	faults := rand.New(stream(cfg.seed, "fault"))
	if cfg.fault == "stale_record" {
		logf("%s: building the stale database", cfg.armName)
		c.buildStale()
	}
	checkedFlip := false
	n := cfg.warmup + cfg.reps
	for rep := 0; rep < n; rep++ {
		index := indices.IntN(cfg.geom.nRecords)
		runtime.GC()
		faulted := cfg.fault != "none" && faults.Float64() < cfg.faultProb
		var inject func(queries, answers [][]byte)
		if faulted {
			inject = func(queries, answers [][]byte) { injectFault(cfg, c, faults, index, queries, answers) }
		}
		first, err := doAttempt(c, index, inject, faulted && !checkedFlip && cfg.fault == "flip_answer_bit")
		if err != nil {
			return err
		}
		if first.honestOut != nil {
			if !first.rejected && bytes.Equal(first.out, first.honestOut) {
				return fmt.Errorf("rep %d: flip_answer_bit left the client's output unchanged; the flip rule picks a bit the output does not depend on", rep)
			}
			checkedFlip = true
			logf("%s: rep %d: checked that the flipped bit changes the honest output (rejected=%v)", cfg.armName, rep, first.rejected)
		}
		if first.panicMsg != "" {
			logf("%s: rep %d: reconstruction of a faulted answer panicked, recorded as rejected: %s", cfg.armName, rep, first.panicMsg)
		}
		if first.refused != "" {
			logf("%s: rep %d: reconstruction not called, recorded as rejected: %s", cfg.armName, rep, first.refused)
		}

		truth := c.truth(index)
		accepted := !first.rejected
		outcome, attempts := "accepted", 0
		var rec recoveryCost
		switch {
		case accepted && bytes.Equal(first.out, truth):
		case accepted:
			outcome = "silent_wrong"
		case cfg.arm.verifiable && cfg.recovery == "switch_replica":
			outcome = "aborted"
			for attempts < recoveryBudget {
				attempts++
				var inj func(queries, answers [][]byte)
				if cfg.replicasCheat && cfg.fault != "none" && faults.Float64() < cfg.faultProb {
					inj = func(queries, answers [][]byte) { injectFault(cfg, c, faults, index, queries, answers) }
				}
				a, err := doAttempt(c, index, inj, false)
				if err != nil {
					return err
				}
				rec.add(a)
				if a.panicMsg != "" {
					logf("%s: rep %d attempt %d: reconstruction of a faulted answer panicked, recorded as rejected: %s", cfg.armName, rep, attempts, a.panicMsg)
				}
				if a.refused != "" {
					logf("%s: rep %d attempt %d: reconstruction not called, recorded as rejected: %s", cfg.armName, rep, attempts, a.refused)
				}
				if a.rejected {
					continue
				}
				outcome = "recovered"
				if !bytes.Equal(a.out, truth) {
					outcome = "silent_wrong"
				}
				break
			}
		default:
			outcome = "aborted"
		}

		r := w.base
		r.Phase, r.Rep, r.IsWarmup, r.Faulted = "online", rep, rep < cfg.warmup, faulted
		r.SchemeParams = c.params()
		an := c.analytical()
		r.Analytical.UpBytes, r.Analytical.DownBytes = an.UpBytes, an.DownBytes
		m := &r.Metrics
		m.QueryGenS, m.QueryGenCPUS = ptr(first.qs.wall), ptr(first.qs.cpu)
		m.UpBytes, m.UpBytesPerServer = ptr(sum(first.up)), first.up
		m.DownBytes, m.DownBytesPerServer = ptr(sum(first.down)), first.down
		for _, s := range first.answerS {
			m.ServerAnswerS = append(m.ServerAnswerS, s.wall)
			m.ServerAnswerCPUS = append(m.ServerAnswerCPUS, s.cpu)
		}
		if first.refused == "" {
			m.ClientRecoverS, m.ClientRecoverCPUS = ptr(first.rs.wall), ptr(first.rs.cpu)
		}
		m.VerifyFused = ptr(cfg.arm.verifiable)
		r.RecoveryOutcome, r.RecoveryAttempts = ptr(outcome), ptr(attempts)
		if attempts > 0 {
			m.RecoveryS, m.RecoveryCPUS = ptr(rec.wall), ptr(rec.cpu)
			m.RecoveryUpBytes, m.RecoveryDownBytes = ptr(rec.up), ptr(rec.down)
		}
		if !first.dropped {
			r.Correct = ptr(accepted && bytes.Equal(first.out, truth))
		}
		if cfg.arm.verifiable {
			r.Detected = ptr(first.rejected)
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

// recoveryCost sums the attempts after the first rejection. Wall time counts
// the servers of an attempt as answering in parallel, its slower answer, as
// the first attempt's latency does (spec latency_e2e). CPU time sums all work.
type recoveryCost struct {
	wall, cpu float64
	up, down  int
}

func (r *recoveryCost) add(a attempt) {
	slowest := 0.0
	for _, s := range a.answerS {
		slowest = max(slowest, s.wall)
		r.cpu += s.cpu
	}
	r.wall += a.qs.wall + slowest + a.rs.wall
	r.cpu += a.qs.cpu + a.rs.cpu
	r.up += sum(a.up)
	r.down += sum(a.down)
}

// injectFault changes the serialised answers before the client parses them
// (contract fault table). flip_answer_bit and wrong_record hit one server,
// chosen at random. drop_server withholds that server's answer. stale_record
// answers every query from the stale database, as a replica set that missed
// an update would.
func injectFault(cfg config, c cell, rng *rand.Rand, index int, queries, answers [][]byte) {
	k := rng.IntN(len(answers))
	switch cfg.fault {
	case "flip_answer_bit":
		bit := pickBit(rng, c.flipBits(index))
		answers[k][bit/8] ^= 1 << (bit % 8)
	case "wrong_record":
		other := (index + 1 + rng.IntN(cfg.geom.nRecords-1)) % cfg.geom.nRecords
		answers[k] = c.shadowAnswer(other, k)
	case "drop_server":
		answers[k] = nil
	case "stale_record":
		for s, q := range queries {
			answers[s] = c.staleAnswer(s, q)
		}
	}
	// A shadow query is as large as a real one (1.9 GB for S3 at 128 MiB). Collected here, untimed, it cannot
	// slow the client's timed work for the rest of the repetition.
	runtime.GC()
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
