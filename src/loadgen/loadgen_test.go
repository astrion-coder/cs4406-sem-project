package main

import (
	"bytes"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

func closedCfg(addrs []string, clients int, window time.Duration) Config {
	return Config{Connect: addrs, Loop: "closed", Clients: clients, Warmup: 100 * time.Millisecond,
		Window: window, Drain: 5 * time.Second}
}

func openCfg(addrs []string, rate float64, window time.Duration) Config {
	return Config{Connect: addrs, Loop: "open", Rate: rate, Conns: 2, Warmup: 100 * time.Millisecond,
		Window: window, Drain: 5 * time.Second, Seed: 1}
}

var lab = Labels{RunID: "run1", Platform: "test-platform"}

func runRow(t *testing.T, cfg Config, c *Corpus, l Labels) (Row, *Result) {
	t.Helper()
	res := Run(cfg, c)
	return BuildRow(cfg, res, c, nil, l), res
}

func TestCorpusRoundTrip(t *testing.T) {
	c := testCorpus(t, 5, testInfo("M1", 0, 2, 7), testInfo("M1", 1, 2, 7))
	if len(c.Infos) != 2 || len(c.Entries) != 5 || len(c.Entries[4]) != 2 {
		t.Fatalf("parsed %d infos and %d entries", len(c.Infos), len(c.Entries))
	}
	if got := string(c.Entries[3][1].Query); got != "query 3 for server 1" {
		t.Fatalf("entry 3, server 1 query is %q", got)
	}
}

func TestCorpusRejectsAnEntryForTheWrongServer(t *testing.T) {
	in, _ := json.Marshal(testInfo("S0-go", 0, 1, 7))
	var buf bytes.Buffer
	_ = WriteCorpus(&buf, [][]byte{in}, nil)
	_ = WriteFrame(&buf, TypeQuery, u64(1), []byte("q"))
	_ = WriteFrame(&buf, TypeExpect, u64(1), append([]byte{3, 0, 0, 0, 1}, make([]byte, 32)...))
	if _, err := parseCorpus(&buf); err == nil || !strings.Contains(err.Error(), "server 3") {
		t.Fatalf("want a server-id error, got %v", err)
	}
}

func TestClosedLoopKeepsOneQueryPerClientAndMatchesEveryAnswer(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S0-go", 0, 1, 7), delay: 2 * time.Millisecond})
	c := testCorpus(t, 8, f.info)
	row, _ := runRow(t, closedCfg([]string{f.addr}, 4, time.Second), c, lab)
	if row.Status != "ok" || row.Mismatched != 0 || row.Errors != 0 {
		t.Fatalf("status %s, mismatched %d, errors %d, error %v", row.Status, row.Mismatched, row.Errors, row.Error)
	}
	if got := f.maxAct.Load(); got > 4 {
		t.Fatalf("%d queries in service at once with 4 closed-loop clients", got)
	}
	// 4 clients and a 2 ms service time cannot complete more than 2000 in 1 s.
	if row.Completed < 100 || row.Completed > 2000 {
		t.Fatalf("completed %d in the window", row.Completed)
	}
	if row.ThroughputQPS != float64(row.Completed) || *row.Clients != 4 || row.OfferedLoadQPS != nil {
		t.Fatalf("throughput %g, clients %v, offered %v", row.ThroughputQPS, row.Clients, row.OfferedLoadQPS)
	}
	if *row.LatP50S < 0.002 || *row.LatP50S > *row.LatP95S || *row.LatP95S > *row.LatP99S {
		t.Fatalf("latencies p50 %g p95 %g p99 %g", *row.LatP50S, *row.LatP95S, *row.LatP99S)
	}
	if math.Abs(*row.ServerCPUUtil-50) > 1 || *row.ServerPeakRSS != 4096 {
		t.Fatalf("server cpu %g %%, peak rss %d; the fake burns 50 %% of its wall time", *row.ServerCPUUtil, *row.ServerPeakRSS)
	}
	if row.LoadgenCPUUtil == nil || *row.LoadgenCPUUtil < 0 {
		t.Fatalf("loadgen cpu %v", row.LoadgenCPUUtil)
	}
	if row.E2ELatP50S != nil {
		t.Fatal("end-to-end latency without client records")
	}
}

func TestTwoServerQueryCompletesWhenBothAnswered(t *testing.T) {
	fast := startFake(t, &fakeServer{info: testInfo("M1", 0, 2, 7), delay: time.Millisecond})
	slow := startFake(t, &fakeServer{info: testInfo("M1", 1, 2, 7), delay: 30 * time.Millisecond})
	c := testCorpus(t, 4, fast.info, slow.info)
	row, _ := runRow(t, closedCfg([]string{fast.addr, slow.addr}, 2, time.Second), c, lab)
	if row.Status != "ok" || row.Completed == 0 {
		t.Fatalf("status %s, completed %d, error %v", row.Status, row.Completed, row.Error)
	}
	if *row.LatP50S < 0.030 {
		t.Fatalf("p50 %g s is shorter than the slow server's 30 ms", *row.LatP50S)
	}
	if fast.answered.Load() != slow.answered.Load() {
		t.Fatalf("servers answered %d and %d", fast.answered.Load(), slow.answered.Load())
	}
}

func TestCorruptAnswerIsAMismatch(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S1", 0, 1, 7), delay: time.Millisecond, corruptEvery: 5})
	c := testCorpus(t, 8, f.info)
	row, _ := runRow(t, closedCfg([]string{f.addr}, 2, 500*time.Millisecond), c, lab)
	if row.Mismatched == 0 || row.Status != "error" || !strings.Contains(*row.Error, "did not match their corpus hash") {
		t.Fatalf("mismatched %d, status %s, error %v", row.Mismatched, row.Status, row.Error)
	}
	if row.Completed == 0 {
		t.Fatal("the honest answers were not counted")
	}
}

func TestServerErrorIsCounted(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S1", 0, 1, 7), delay: time.Millisecond, errorEvery: 3})
	c := testCorpus(t, 8, f.info)
	row, _ := runRow(t, closedCfg([]string{f.addr}, 2, 500*time.Millisecond), c, lab)
	if row.Errors == 0 || row.Status != "error" || !strings.Contains(*row.Error, "wrong query length") {
		t.Fatalf("errors %d, status %s, error %v", row.Errors, row.Status, row.Error)
	}
}

func TestServerWithOtherSeedIsRefused(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S0-go", 0, 1, 8)})
	c := testCorpus(t, 2, testInfo("S0-go", 0, 1, 7))
	row, res := runRow(t, closedCfg([]string{f.addr}, 1, time.Second), c, lab)
	if res.Fatal == nil || row.Status != "error" || !strings.Contains(*row.Error, "seed: corpus 7, server 8") {
		t.Fatalf("status %s, error %v", row.Status, row.Error)
	}
	if f.seen.Load() != 0 {
		t.Fatal("queries went to a server with another database")
	}
}

func TestOpenLoopHonoursTheRate(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S0-go", 0, 1, 7), delay: time.Millisecond})
	c := testCorpus(t, 8, f.info)
	row, _ := runRow(t, openCfg([]string{f.addr}, 400, 2*time.Second), c, lab)
	if row.Status != "ok" || row.Clients != nil || *row.OfferedLoadQPS != 400 {
		t.Fatalf("status %s, clients %v, offered %v, error %v", row.Status, row.Clients, row.OfferedLoadQPS, row.Error)
	}
	// 800 expected arrivals; Poisson's standard deviation is about 28.
	if row.Sent < 650 || row.Sent > 950 {
		t.Fatalf("sent %d in a 2 s window at 400/s", row.Sent)
	}
	if math.Abs(row.ThroughputQPS-400) > 80 {
		t.Fatalf("throughput %g at 400/s offered", row.ThroughputQPS)
	}
}

func TestOpenLoopAboveCapacityIsSaturated(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S0-go", 0, 1, 7), delay: 10 * time.Millisecond, workers: 1})
	c := testCorpus(t, 8, f.info)
	cfg := openCfg([]string{f.addr}, 300, time.Second)
	cfg.Drain = 30 * time.Second
	row, _ := runRow(t, cfg, c, lab)
	if row.Status != "saturated" {
		t.Fatalf("status %s at 3x the server's 100/s capacity, error %v", row.Status, row.Error)
	}
	if f.maxAct.Load() != 1 {
		t.Fatalf("the one-worker fake ran %d queries at once", f.maxAct.Load())
	}
}

func TestClosedLoopOverTheP95LimitIsSaturated(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S0-go", 0, 1, 7), delay: 5 * time.Millisecond})
	c := testCorpus(t, 8, f.info)
	l := lab
	l.P95LimitS = 0.001
	row, _ := runRow(t, closedCfg([]string{f.addr}, 1, 500*time.Millisecond), c, l)
	if row.Status != "saturated" {
		t.Fatalf("status %s with p95 %g over a 1 ms limit", row.Status, *row.LatP95S)
	}
}

func TestUnansweredQueriesEndTheRunAfterTheDrain(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S0-go", 0, 1, 7), delay: time.Hour})
	c := testCorpus(t, 2, f.info)
	cfg := closedCfg([]string{f.addr}, 3, 200*time.Millisecond)
	cfg.Drain = 200 * time.Millisecond
	t0 := time.Now()
	row, res := runRow(t, cfg, c, lab)
	if el := time.Since(t0); el > 3*time.Second {
		t.Fatalf("run took %s", el)
	}
	if res.Unanswered != 3 || row.Status != "error" || !strings.Contains(*row.Error, "3 requests unanswered") {
		t.Fatalf("unanswered %d, status %s, error %v", res.Unanswered, row.Status, row.Error)
	}
}

func TestPercentileInterpolatesLikeNumpy(t *testing.T) {
	xs := []float64{5, 1, 4, 2, 3}
	sort.Float64s(xs)
	for p, want := range map[float64]float64{0.5: 3, 0.95: 4.8, 0.99: 4.96, 0: 1, 1: 5} {
		if got := *Percentile(xs, p); math.Abs(got-want) > 1e-12 {
			t.Errorf("p%g = %g, want %g", p*100, got, want)
		}
	}
	if *Percentile([]float64{7}, 0.99) != 7 || Percentile(nil, 0.5) != nil {
		t.Error("one sample or none")
	}
}

func TestEndToEndAddsTheClientSideTimes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "client.jsonl")
	lines := strings.Join([]string{
		`{"mode": "client", "status": "ok", "rep": 1, "metrics": {"query_gen_s": 0.5, "client_recover_s": 0.25, "client_verify_s": null}}`,
		`{"mode": "serve", "arm": "S1"}`,
		`{"mode": "client", "status": "ok", "rep": 0, "metrics": {"query_gen_s": 1, "client_recover_s": 2, "client_verify_s": 3}}`,
	}, "\n")
	if err := os.WriteFile(path, []byte(lines), 0o644); err != nil {
		t.Fatal(err)
	}
	extra, err := clientExtra(path, 2)
	if err != nil || extra[0] != 6 || extra[1] != 0.75 {
		t.Fatalf("extra %v, err %v", extra, err)
	}
	if _, err := clientExtra(path, 3); err == nil {
		t.Fatal("a corpus entry without a client record was accepted")
	}

	f := startFake(t, &fakeServer{info: testInfo("S1", 0, 1, 7), delay: time.Millisecond})
	c := testCorpus(t, 2, f.info)
	cfg := closedCfg([]string{f.addr}, 1, 300*time.Millisecond)
	row := BuildRow(cfg, Run(cfg, c), c, extra, lab)
	if *row.E2ELatP50S < *row.LatP50S+0.75 || *row.E2ELatP99S > *row.LatP99S+6 {
		t.Fatalf("e2e p50 %g, p99 %g against observed %g, %g", *row.E2ELatP50S, *row.E2ELatP99S, *row.LatP50S, *row.LatP99S)
	}
}

func TestRowHasExactlyTheSchemaFields(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "cs4406_sem_project", "harness", "schema.json"))
	if err != nil {
		t.Fatal(err)
	}
	var schema struct {
		Defs map[string]struct {
			Required   []string                   `json:"required"`
			Properties map[string]json.RawMessage `json:"properties"`
		} `json:"$defs"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(Row{})
	var row map[string]any
	_ = json.Unmarshal(b, &row)
	def := schema.Defs["loadgen_record"]
	if len(row) != len(def.Required) || len(row) != len(def.Properties) {
		t.Fatalf("row has %d fields, schema requires %d", len(row), len(def.Required))
	}
	for _, k := range def.Required {
		if _, ok := row[k]; !ok {
			t.Errorf("row lacks %s", k)
		}
	}
}

func TestCommandLineAppendsOneRowPerRun(t *testing.T) {
	f := startFake(t, &fakeServer{info: testInfo("S0-go", 0, 1, 7), delay: time.Millisecond})
	c := testCorpus(t, 3, f.info)
	dir := t.TempDir()
	corpus, out := filepath.Join(dir, "corpus.bin"), filepath.Join(dir, "rows.jsonl")
	var buf bytes.Buffer
	raw, _ := json.Marshal(f.info)
	if err := WriteCorpus(&buf, [][]byte{raw}, c.Entries); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(corpus, buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
	args := []string{"--corpus", corpus, "--connect", f.addr, "--loop", "closed", "--clients", "2",
		"--warmup-s", "0.1", "--window-s", "0.3", "--run", "2", "--run-id", "r", "--platform", "p", "--out", out}
	var stderr bytes.Buffer
	for i := 0; i < 2; i++ {
		if code := run(args, &stderr); code != exitOK {
			t.Fatalf("exit %d: %s", code, stderr.String())
		}
	}
	b, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	var row Row
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &row); err != nil || len(lines) != 2 {
		t.Fatalf("%d lines, %v", len(lines), err)
	}
	if row.Run != 2 || row.RunID != "r" || row.GitSHAHarness != nil || row.Arm != "S0-go" || row.Workers != 2 {
		t.Fatalf("row %+v", row)
	}
	if code := run([]string{"--loop", "sideways"}, &stderr); code != exitUsage {
		t.Fatalf("usage error exit %d", code)
	}
}
