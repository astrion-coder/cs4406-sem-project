package main

import (
	"fmt"
	"math"
	"sort"
)

// Row is one line of the output, schema.json $defs/loadgen_record.
type Row struct {
	Mode           string   `json:"mode"`
	RunID          string   `json:"run_id"`
	GitSHAHarness  *string  `json:"git_sha_harness"`
	Platform       string   `json:"platform"`
	Arm            string   `json:"arm"`
	DBBytes        int64    `json:"db_bytes"`
	RecordBytes    *int64   `json:"record_bytes"`
	Seed           uint64   `json:"seed"`
	Workers        int      `json:"workers"`
	Loop           string   `json:"loop"`
	Clients        *int     `json:"clients"`
	OfferedLoadQPS *float64 `json:"offered_load_qps"`
	Run            int      `json:"run"`
	WarmupS        float64  `json:"warmup_s"`
	WindowS        float64  `json:"window_s"`
	Sent           int64    `json:"sent"`
	Completed      int64    `json:"completed"`
	Mismatched     int64    `json:"mismatched"`
	Errors         int64    `json:"errors"`
	ThroughputQPS  float64  `json:"throughput_qps"`
	LatP50S        *float64 `json:"lat_p50_s"`
	LatP95S        *float64 `json:"lat_p95_s"`
	LatP99S        *float64 `json:"lat_p99_s"`
	E2ELatP50S     *float64 `json:"e2e_lat_p50_s"`
	E2ELatP95S     *float64 `json:"e2e_lat_p95_s"`
	E2ELatP99S     *float64 `json:"e2e_lat_p99_s"`
	ServerCPUUtil  *float64 `json:"server_cpu_util"`
	ServerPeakRSS  *int64   `json:"server_peak_rss_bytes"`
	LoadgenCPUUtil *float64 `json:"loadgen_cpu_util"`
	Status         string   `json:"status"`
	Error          *string  `json:"error"`
}

// Labels are the row fields the run itself does not measure.
type Labels struct {
	RunID     string
	GitSHA    *string
	Platform  string
	Run       int
	P95LimitS float64 // closed loop: a p95 above this marks the row saturated; 0 for none
}

// OpenSaturation is the share of the window's arrivals that must complete
// inside the window for an open-loop run to count as keeping up.
const OpenSaturation = 0.9

// Percentile is the linearly interpolated p-quantile of sorted xs, numpy's
// default method, so the analysis can recompute it from the same samples.
func Percentile(sorted []float64, p float64) *float64 {
	if len(sorted) == 0 {
		return nil
	}
	h := float64(len(sorted)-1) * p
	lo := int(math.Floor(h))
	v := sorted[lo]
	if lo+1 < len(sorted) {
		v += (h - float64(lo)) * (sorted[lo+1] - sorted[lo])
	}
	return &v
}

func ptr[T any](v T) *T { return &v }

// BuildRow turns a run's result into its row. extra holds each corpus
// entry's client-side seconds for end-to-end latency, or is nil.
func BuildRow(cfg Config, res *Result, corpus *Corpus, extra []float64, lab Labels) Row {
	in := corpus.Infos[0]
	if len(res.Infos) > 0 {
		in = res.Infos[0]
	}
	r := Row{
		Mode: "loadgen", RunID: lab.RunID, GitSHAHarness: lab.GitSHA, Platform: lab.Platform,
		Arm: in.Arm, DBBytes: in.DBBytes, RecordBytes: in.RecordBytes, Seed: in.Seed, Workers: in.Workers,
		Loop: cfg.Loop, Run: lab.Run, WarmupS: cfg.Warmup.Seconds(), WindowS: cfg.Window.Seconds(),
		Sent: res.SentInWin, Status: "ok",
	}
	if cfg.Loop == "closed" {
		r.Clients = ptr(cfg.Clients)
	} else {
		r.OfferedLoadQPS = ptr(cfg.Rate)
	}

	var lat, e2e []float64
	var runMismatched, runFailed int64
	for _, s := range res.Samples {
		switch s.out {
		case mismatched:
			runMismatched++
		case failed:
			runFailed++
		}
		if s.done.Before(res.WindowFrom) || !s.done.Before(res.WindowTo) {
			continue
		}
		switch s.out {
		case matched:
			r.Completed++
			l := s.done.Sub(s.sent).Seconds()
			lat = append(lat, l)
			if extra != nil {
				e2e = append(e2e, l+extra[s.entry])
			}
		case mismatched:
			r.Mismatched++
		case failed:
			r.Errors++
		}
	}
	r.ThroughputQPS = float64(r.Completed) / cfg.Window.Seconds()
	sort.Float64s(lat)
	sort.Float64s(e2e)
	r.LatP50S, r.LatP95S, r.LatP99S = Percentile(lat, 0.50), Percentile(lat, 0.95), Percentile(lat, 0.99)
	if extra != nil {
		r.E2ELatP50S, r.E2ELatP95S, r.E2ELatP99S = Percentile(e2e, 0.50), Percentile(e2e, 0.95), Percentile(e2e, 0.99)
	}

	a, b := res.Start, res.End
	if n := len(corpus.Infos); len(a.Stats) == n && len(b.Stats) == n {
		util, rss := 0.0, int64(0)
		for k := range b.Stats {
			cpu := b.Stats[k].CPUUserS + b.Stats[k].CPUSysS - a.Stats[k].CPUUserS - a.Stats[k].CPUSysS
			if wall := b.Stats[k].WallS - a.Stats[k].WallS; wall > 0 {
				util += 100 * cpu / wall
			}
			rss += b.Stats[k].PeakRSSBytes
		}
		r.ServerCPUUtil, r.ServerPeakRSS = ptr(math.Max(util, 0)), ptr(rss)
	}
	if a.HasCPU && b.HasCPU && b.At.After(a.At) {
		r.LoadgenCPUUtil = ptr(100 * (b.CPU - a.CPU).Seconds() / b.At.Sub(a.At).Seconds())
	}

	var problems []string
	if res.Fatal != nil {
		problems = append(problems, res.Fatal.Error())
	}
	if runMismatched > 0 {
		problems = append(problems, fmt.Sprintf("%d answers did not match their corpus hash", runMismatched))
	}
	if runFailed > 0 {
		problems = append(problems, fmt.Sprintf("%d requests failed, the first: %v", runFailed, res.FirstError))
	}
	if res.Unanswered > 0 {
		problems = append(problems, fmt.Sprintf("%d requests unanswered %g s after the window", res.Unanswered, cfg.Drain.Seconds()))
	}
	if len(problems) == 0 && r.Completed == 0 {
		problems = append(problems, "no query completed inside the window")
	}
	switch {
	case len(problems) > 0:
		r.Status = "error"
		msg := problems[0]
		for _, p := range problems[1:] {
			msg += "; " + p
		}
		r.Error = &msg
	case cfg.Loop == "closed" && lab.P95LimitS > 0 && *r.LatP95S > lab.P95LimitS:
		r.Status = "saturated"
	case cfg.Loop == "open" && float64(r.Completed+r.Mismatched+r.Errors) < OpenSaturation*float64(r.Sent):
		r.Status = "saturated"
	}
	return r
}
