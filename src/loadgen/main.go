// Command loadgen replays a verified query corpus against an adapter's
// serve-mode servers in a closed or open loop and appends one row per run
// (docs/serve_protocol.md, "What the load generator measures"). It holds no
// PIR cryptography: each answer is checked against its recorded SHA-256.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

const (
	exitOK    = 0
	exitError = 1
	exitUsage = 2
)

type options struct {
	cfg           Config
	lab           Labels
	corpus        string
	clientRecords string
	out           string
}

func envOr(name, def string) string {
	if v, ok := os.LookupEnv(name); ok {
		return v
	}
	return def
}

func parseArgs(args []string) (options, error) {
	var o options
	fs := flag.NewFlagSet("loadgen", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var connect, gitSHA string
	var warmup, window, drain, p95Limit float64
	fs.StringVar(&o.corpus, "corpus", "", "corpus file written by an adapter's --corpus-out")
	fs.StringVar(&connect, "connect", "", "HOST:PORT[,HOST:PORT], one per server in server order")
	fs.StringVar(&o.cfg.Loop, "loop", "", "closed or open")
	fs.IntVar(&o.cfg.Clients, "clients", 0, "closed loop: concurrent clients")
	fs.Float64Var(&o.cfg.Rate, "rate", 0, "open loop: Poisson arrival rate, queries/s")
	fs.IntVar(&o.cfg.Conns, "conns", 4, "open loop: connections per server")
	fs.Int64Var(&o.cfg.Seed, "seed", 1, "open loop: seed of the arrival times")
	fs.Float64Var(&warmup, "warmup-s", 10, "seconds before the window")
	fs.Float64Var(&window, "window-s", 60, "steady-state window, seconds")
	fs.Float64Var(&drain, "drain-s", 300, "seconds to wait for answers in flight after the window")
	fs.Float64Var(&p95Limit, "p95-limit-s", 0, "closed loop: mark the row saturated when p95 exceeds this")
	fs.IntVar(&o.lab.Run, "run", 0, "run number within its point")
	fs.StringVar(&o.lab.RunID, "run-id", envOr("VPIR_RUN_ID", ""), "default $VPIR_RUN_ID")
	fs.StringVar(&o.lab.Platform, "platform", envOr("VPIR_PLATFORM", ""), "default $VPIR_PLATFORM")
	fs.StringVar(&gitSHA, "git-sha", envOr("VPIR_GIT_SHA", ""), "default $VPIR_GIT_SHA; empty for null")
	fs.StringVar(&o.clientRecords, "client-records", "", "the corpus run's client records, for end-to-end latency")
	fs.StringVar(&o.out, "out", "", "JSONL file the row is appended to")
	if err := fs.Parse(args); err != nil {
		return o, err
	}
	if fs.NArg() > 0 {
		return o, fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if connect != "" {
		o.cfg.Connect = strings.Split(connect, ",")
	}
	o.cfg.Warmup = time.Duration(warmup * float64(time.Second))
	o.cfg.Window = time.Duration(window * float64(time.Second))
	o.cfg.Drain = time.Duration(drain * float64(time.Second))
	o.lab.P95LimitS = p95Limit
	if gitSHA != "" {
		o.lab.GitSHA = &gitSHA
	}
	switch {
	case o.corpus == "" || o.out == "" || len(o.cfg.Connect) == 0:
		return o, fmt.Errorf("--corpus, --connect and --out are required")
	case o.lab.RunID == "" || o.lab.Platform == "":
		return o, fmt.Errorf("--run-id and --platform (or $VPIR_RUN_ID and $VPIR_PLATFORM) are required")
	case o.cfg.Loop == "closed" && o.cfg.Clients < 1:
		return o, fmt.Errorf("--loop closed needs --clients >= 1")
	case o.cfg.Loop == "open" && (o.cfg.Rate <= 0 || o.cfg.Conns < 1):
		return o, fmt.Errorf("--loop open needs --rate > 0 and --conns >= 1")
	case o.cfg.Loop != "closed" && o.cfg.Loop != "open":
		return o, fmt.Errorf("--loop must be closed or open")
	case warmup < 0 || window <= 0 || drain < 0 || o.lab.Run < 0:
		return o, fmt.Errorf("--warmup-s and --drain-s must be >= 0, --window-s > 0 and --run >= 0")
	}
	return o, nil
}

// clientExtra reads the corpus run's client records and returns, per corpus
// entry, query_gen_s + client_recover_s + client_verify_s. Entry i is the
// record with rep i, the query sent with req_id i + 1. client_verify_s is
// null where verification is fused into recovery, and counts as 0 there.
func clientExtra(path string, entries int) ([]float64, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	extra := make([]float64, entries)
	seen := make([]bool, entries)
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for line := 1; sc.Scan(); line++ {
		if strings.TrimSpace(sc.Text()) == "" {
			continue
		}
		var rec struct {
			Mode    string `json:"mode"`
			Status  string `json:"status"`
			Rep     int    `json:"rep"`
			Metrics struct {
				QueryGenS      *float64 `json:"query_gen_s"`
				ClientRecoverS *float64 `json:"client_recover_s"`
				ClientVerifyS  *float64 `json:"client_verify_s"`
			} `json:"metrics"`
		}
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			return nil, fmt.Errorf("%s:%d: %w", path, line, err)
		}
		if rec.Mode != "client" {
			continue
		}
		m := rec.Metrics
		switch {
		case rec.Status != "ok":
			return nil, fmt.Errorf("%s:%d: status %q", path, line, rec.Status)
		case rec.Rep < 0 || rec.Rep >= entries || seen[rec.Rep]:
			return nil, fmt.Errorf("%s:%d: rep %d is not a new entry of a %d-query corpus", path, line, rec.Rep, entries)
		case m.QueryGenS == nil || m.ClientRecoverS == nil:
			return nil, fmt.Errorf("%s:%d: query_gen_s or client_recover_s is null", path, line)
		}
		seen[rec.Rep] = true
		extra[rec.Rep] = *m.QueryGenS + *m.ClientRecoverS
		if m.ClientVerifyS != nil {
			extra[rec.Rep] += *m.ClientVerifyS
		}
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	for i, ok := range seen {
		if !ok {
			return nil, fmt.Errorf("%s has no client record for corpus entry %d", path, i)
		}
	}
	return extra, nil
}

func appendLine(path string, v any) error {
	line, err := json.Marshal(v)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(line, '\n')); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// load reads everything a run needs before it touches a server. Its errors
// are usage errors: nothing was measured.
func load(args []string) (options, *Corpus, []float64, error) {
	o, err := parseArgs(args)
	if err != nil {
		return o, nil, nil, err
	}
	corpus, err := ReadCorpus(o.corpus)
	if err != nil {
		return o, nil, nil, fmt.Errorf("--corpus: %w", err)
	}
	if o.clientRecords == "" {
		return o, corpus, nil, nil
	}
	extra, err := clientExtra(o.clientRecords, len(corpus.Entries))
	if err != nil {
		return o, nil, nil, fmt.Errorf("--client-records: %w", err)
	}
	return o, corpus, extra, nil
}

func run(args []string, stderr io.Writer) int {
	o, corpus, extra, err := load(args)
	if err != nil {
		fmt.Fprintln(stderr, "loadgen:", err)
		return exitUsage
	}
	res := Run(o.cfg, corpus)
	row := BuildRow(o.cfg, res, corpus, extra, o.lab)
	if err := appendLine(o.out, row); err != nil {
		fmt.Fprintln(stderr, "loadgen:", err)
		return exitError
	}
	fmt.Fprintf(stderr, "loadgen: %s %s run %d: %s, %d completed, %.1f q/s, %d unanswered, own CPU from %s
",
		row.Arm, o.cfg.Loop, o.lab.Run, row.Status, row.Completed, row.ThroughputQPS, res.Unanswered, cpuSource)
	if row.Status == "error" {
		fmt.Fprintln(stderr, "loadgen:", *row.Error)
		return exitError
	}
	return exitOK
}

func main() {
	os.Exit(run(os.Args[1:], os.Stderr))
}
