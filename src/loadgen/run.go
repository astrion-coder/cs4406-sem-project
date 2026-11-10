package main

import (
	"fmt"
	"math/rand"
	"sync"
	"sync/atomic"
	"time"
)

// Config is one load-generator run.
type Config struct {
	Connect []string // one address per server, in server order
	Loop    string   // "closed" or "open"
	Clients int      // closed loop: clients, each with one query in flight
	Rate    float64  // open loop: Poisson arrivals per second
	Conns   int      // open loop: connections per server the arrivals rotate over
	Warmup  time.Duration
	Window  time.Duration
	Drain   time.Duration // how long to wait for answers in flight after the window
	Seed    int64         // open loop: seed of the arrival times
}

type outcome uint8

const (
	matched outcome = iota
	mismatched
	failed
	stopped // still in flight when the load generator closed the connection
)

// sample is one completed request.
type sample struct {
	sent, done time.Time
	entry      int32
	out        outcome
}

// Snapshot is what the run read at one end of the window.
type Snapshot struct {
	At     time.Time
	Stats  []Stats // one per server
	CPU    time.Duration
	HasCPU bool
}

// Result is everything a run measured, before it becomes a row.
type Result struct {
	Infos      []Info // the live servers' INFO
	Start, End Snapshot
	WindowFrom time.Time
	WindowTo   time.Time
	Samples    []sample
	SentInWin  int64
	Unanswered int64 // requests still in flight when the drain ended
	Fatal      error // a connection failed or the run could not start
	FirstError error // the first per-request error, if any
}

type runState struct {
	corpus *Corpus
	cfg    Config
	from   time.Time
	to     time.Time
	stop   chan struct{}

	nextID    atomic.Uint64
	nextEntry atomic.Uint64
	sentInWin atomic.Int64
	open      atomic.Int64 // requests in flight

	mu         sync.Mutex
	samples    []sample
	fatal      error
	fatalCh    chan struct{}
	firstError error
}

func (s *runState) setFatal(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fatal == nil && err != errStopped {
		s.fatal = err
		close(s.fatalCh)
	}
}

func (s *runState) record(q *request) {
	out := matched
	switch {
	case q.err == errStopped:
		out = stopped
	case q.err != nil:
		out = failed
	case !q.matched:
		out = mismatched
	}
	s.open.Add(-1)
	s.mu.Lock()
	s.samples = append(s.samples, sample{sent: q.sent, done: q.last, entry: int32(q.entry), out: out})
	if q.err != nil && q.err != errStopped && s.firstError == nil {
		s.firstError = q.err
	}
	s.mu.Unlock()
}

func (s *runState) stopped() bool {
	select {
	case <-s.stop:
		return true
	default:
		return false
	}
}

// issue sends the next corpus entry, round-robin, to every server over one
// connection each. It sends nothing once the run has stopped, and counts the
// request as open before that check so the drain cannot miss it.
func (s *runState) issue(group []*conn, done func(*request)) bool {
	s.open.Add(1)
	if s.stopped() {
		s.open.Add(-1)
		return false
	}
	entry := int((s.nextEntry.Add(1) - 1) % uint64(len(s.corpus.Entries)))
	id := s.nextID.Add(1)
	q := newRequest(entry, len(group), done)
	q.sent = time.Now()
	if !q.sent.Before(s.from) && q.sent.Before(s.to) {
		s.sentInWin.Add(1)
	}
	for _, c := range group {
		c.send(id, q)
	}
	return true
}

func (s *runState) closedClient(group []*conn) {
	finished := make(chan struct{}, 1)
	done := func(q *request) {
		s.record(q)
		finished <- struct{}{}
	}
	for s.issue(group, done) {
		<-finished
	}
}

func (s *runState) openArrivals(groups [][]*conn) {
	rng := rand.New(rand.NewSource(s.cfg.Seed))
	next := time.Now()
	for i := 0; ; i++ {
		next = next.Add(time.Duration(rng.ExpFloat64() / s.cfg.Rate * float64(time.Second)))
		if !next.Before(s.to) {
			return
		}
		if d := time.Until(next); d > 0 {
			select {
			case <-time.After(d):
			case <-s.stop:
				return
			}
		}
		if !s.issue(groups[i%len(groups)], s.record) {
			return
		}
	}
}

func snapshot(ctl []control) (Snapshot, error) {
	snap := Snapshot{At: time.Now()}
	snap.CPU, snap.HasCPU = processCPU()
	for k, c := range ctl {
		st, err := c.stats()
		if err != nil {
			return snap, fmt.Errorf("server %d: STATS: %w", k, err)
		}
		snap.Stats = append(snap.Stats, st)
	}
	return snap, nil
}

func sleepUntil(t time.Time, abort <-chan struct{}) bool {
	d := time.Until(t)
	if d <= 0 {
		return true
	}
	select {
	case <-time.After(d):
		return true
	case <-abort:
		return false
	}
}

// Run drives the servers in cfg.Connect with the corpus and returns what it
// measured. A non-nil Result.Fatal means the run is an error row.
func Run(cfg Config, corpus *Corpus) *Result {
	res := &Result{}
	n := len(corpus.Infos)
	if len(cfg.Connect) != n {
		res.Fatal = fmt.Errorf("the corpus has %d servers, --connect names %d", n, len(cfg.Connect))
		return res
	}
	s := &runState{corpus: corpus, cfg: cfg, stop: make(chan struct{}), fatalCh: make(chan struct{})}

	var ctl []control
	var all []*conn
	defer func() {
		for _, c := range ctl {
			c.nc.Close()
		}
		for _, c := range all {
			c.close()
		}
	}()
	for k, addr := range cfg.Connect {
		nc, r, in, err := dialChecked(addr, k, corpus)
		if err != nil {
			res.Fatal = err
			return res
		}
		ctl = append(ctl, control{nc, r})
		res.Infos = append(res.Infos, in)
	}
	groupCount := cfg.Clients
	if cfg.Loop == "open" {
		groupCount = cfg.Conns
	}
	groups := make([][]*conn, groupCount)
	for g := range groups {
		for k, addr := range cfg.Connect {
			nc, r, _, err := dialChecked(addr, k, corpus)
			if err != nil {
				res.Fatal = err
				return res
			}
			c := newConn(nc, r, k, corpus, s.setFatal)
			all = append(all, c)
			groups[g] = append(groups[g], c)
		}
	}

	start := time.Now()
	s.from, s.to = start.Add(cfg.Warmup), start.Add(cfg.Warmup+cfg.Window)
	res.WindowFrom, res.WindowTo = s.from, s.to
	var load sync.WaitGroup
	if cfg.Loop == "closed" {
		for _, g := range groups {
			load.Add(1)
			go func(g []*conn) {
				defer load.Done()
				s.closedClient(g)
			}(g)
		}
	} else {
		load.Add(1)
		go func() {
			defer load.Done()
			s.openArrivals(groups)
		}()
	}

	var err error
	if sleepUntil(s.from, s.fatalCh) {
		res.Start, err = snapshot(ctl)
		if err == nil && sleepUntil(s.to, s.fatalCh) {
			res.End, err = snapshot(ctl)
		}
	}
	close(s.stop)

	deadline := time.Now().Add(cfg.Drain)
	for s.open.Load() > 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	res.Unanswered = s.open.Load()
	for _, c := range all {
		c.close()
	}
	load.Wait()

	s.mu.Lock()
	defer s.mu.Unlock()
	res.Fatal = s.fatal
	if res.Fatal == nil && err != nil {
		res.Fatal = err
	}
	res.FirstError = s.firstError
	res.Samples = append([]sample(nil), s.samples...)
	res.SentInWin = s.sentInWin.Load()
	return res
}
