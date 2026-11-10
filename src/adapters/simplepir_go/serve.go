package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"math"
	"os"
	"runtime/debug"
	"time"

	"github.com/ahenzinger/simplepir/pir"
	serve "github.com/imankalyan/cs4406-sem-project/src/adapters/serve_go"
)

// serveArm plugs S0-go into the shared serve and client loops.
type serveArm struct{}

// publicKey is the seed of the public matrix A, derived from --seed so a
// server and a client started with the same seed agree on it.
func publicKey(seed int64) *pir.PRGKey {
	var s [8]byte
	binary.LittleEndian.PutUint64(s[:], uint64(seed))
	h := sha256.Sum256(append([]byte("vpir-serve/public-seed/"), s[:]...))
	var k pir.PRGKey
	copy(k[:], h[:len(k)])
	return &k
}

// openSession builds the database from the seed and runs the offline phase
// once, with upstream's InitCompressedSeeded, which also seeds the library's
// global PRG, so nothing else may call it later.
func openSession(o serve.Options) (*session, error) {
	switch {
	case o.Arm != arm:
		return nil, serve.Usagef("unknown arm %q, this adapter serves %s", o.Arm, arm)
	case o.RecordBytes < 1:
		return nil, serve.Usagef("--record-bytes must be at least 1, got %d", o.RecordBytes)
	case o.DBBytes < o.RecordBytes:
		return nil, serve.Usagef("--db-bytes %d is smaller than --record-bytes %d", o.DBBytes, o.RecordBytes)
	case o.Seed > math.MaxInt64:
		return nil, serve.Usagef("--seed must fit in 63 bits for %s", arm)
	}
	// The library prints parameters and progress to stdout.
	os.Stdout = os.Stderr
	cfg := config{
		arm: arm, dbBytes: o.DBBytes, recordBytes: o.RecordBytes, seed: int64(o.Seed),
		fault: "none", recovery: "abort",
		runID: serve.Env("VPIR_RUN_ID"), gitSHA: serve.Env("VPIR_GIT_SHA"), platform: serve.Env("VPIR_PLATFORM"),
	}
	pi := &pir.SimplePIR{}
	var lay Layout
	var s *session
	if err := guard(func() {
		var perr error
		if lay, perr = plan(pi, cfg.dbBytes, cfg.recordBytes); perr != nil {
			panic(perr)
		}
		if s, perr = newSession(cfg, lay, pi); perr != nil {
			panic(perr)
		}
	}); err != nil {
		return nil, fmt.Errorf("library rejected the parameters: %w", err)
	}
	s.publicKey = publicKey(cfg.seed)
	if _, err := s.offline(); err != nil {
		return nil, err
	}
	debug.SetGCPercent(100)
	return s, nil
}

func (s *session) serveInfo(serverParallelism string) serve.Info {
	sum := sha256.New()
	buf := make([]byte, s.lay.RecordBytes)
	for r := uint64(0); r < s.lay.Records; r++ {
		s.record(r, buf)
		sum.Write(buf)
	}
	key := hex.EncodeToString(s.publicKey[:])
	params := s.schemeParams()
	if serverParallelism != "" {
		params.Parallelism = serverParallelism
	}
	return serve.Info{
		Arm: arm, ServerID: 0, Servers: 1, DBBytes: s.cfg.dbBytes, RecordBytes: &s.cfg.recordBytes,
		NRecords: s.lay.Records, Seed: uint64(s.cfg.seed), UpstreamSHA: upstreamSHA,
		DBSHA256: hex.EncodeToString(sum.Sum(nil)), PublicSeed: &key, SchemeParams: params,
	}
}

func (serveArm) NewServer(o serve.Options) (serve.Server, error) {
	if o.ServerID > 0 {
		return nil, serve.Usagef("%s has one server, --server-id must be 0 or absent", arm)
	}
	s, err := openSession(o)
	if err != nil {
		return nil, err
	}
	// A real query shows the length the parameters imply.
	_, q := s.pi.Query(0, s.serverShared, s.p, s.db.Info)
	return &spServer{
		s: s, qRows: q.Data[0].Rows, workers: o.Workers,
		info: s.serveInfo(fmt.Sprintf("%d workers", o.Workers)),
	}, nil
}

type spServer struct {
	s       *session
	qRows   uint64
	workers int
	info    serve.Info
}

func (v *spServer) Info() serve.Info { return v.info }

func (v *spServer) CheckQuery(q []byte) error {
	if uint64(len(q)) != 4*v.qRows {
		return fmt.Errorf("query is %d bytes, %s expects %d", len(q), arm, 4*v.qRows)
	}
	return nil
}

func (v *spServer) Answer(q []byte) ([]byte, int64, error) {
	s := v.s
	query := pir.MakeMsgSlice(pir.MakeMsg(decode(q, v.qRows, 1)))
	t0 := time.Now()
	ans := s.pi.Answer(s.db, query, s.serverState, s.serverShared, s.p)
	ns := time.Since(t0).Nanoseconds()
	return encode(ans.Data[0]), ns, nil
}

func (serveArm) NewClient(o serve.Options) (serve.Client, error) {
	if len(o.Connect) != 1 {
		return nil, serve.Usagef("%s has one server, --connect takes one address", arm)
	}
	s, err := openSession(o)
	if err != nil {
		return nil, err
	}
	s.clientStates()
	return &spClient{s: s, info: s.serveInfo("")}, nil
}

type spClient struct {
	s       *session
	info    serve.Info
	rep     int
	checked bool
}

func (c *spClient) Info() serve.Info { return c.info }

func (c *spClient) Next() serve.Round {
	s := c.s
	rep := c.rep
	c.rep++
	idx := s.index.below(s.lay.Records)
	var cstate pir.State
	var query pir.Msg
	qg := timed(func() { cstate, query = s.pi.Query(s.lay.Entry(idx, 0), s.clientA, s.p, s.db.Info) })
	qBytes := encode(query.Data[0])
	return serve.Round{Queries: [][]byte{qBytes}, Finish: func(answers [][]byte, answerS []float64) serve.Outcome {
		aBytes := answers[0]
		want := make([]byte, s.lay.RecordBytes)
		s.record(idx, want)
		correct := false
		var rc span
		if uint64(len(aBytes)) == 4*s.p.L {
			clientAns := pir.MakeMsg(decode(aBytes, s.p.L, 1))
			var got []uint64
			rc = timed(func() { got = s.recoverRecord(idx, query, clientAns, cstate) })
			if !c.checked {
				if err := s.checkAgainstUpstream(idx, got, query, clientAns, cstate); err != nil {
					fmt.Fprintln(os.Stderr, "decode check:", err)
					got = nil
				}
				c.checked = true
			}
			correct = got != nil && sameRecord(got, pieces(want, s.lay.Pieces))
		}
		rec := s.base("online", rep, 0)
		m := Metrics{VerifyFused: bptr(false)}
		m.QueryGenS, m.QueryGenCPUS = f64(qg.wall), f64(qg.cpu)
		m.UpBytes, m.UpBytesPerServer = i64(int64(len(qBytes))), []int64{int64(len(qBytes))}
		m.ServerAnswerS = answerS
		m.DownBytes, m.DownBytesPerServer = i64(int64(len(aBytes))), []int64{int64(len(aBytes))}
		m.ClientRecoverS, m.ClientRecoverCPUS = f64(rc.wall), f64(rc.cpu)
		m.ProcessPeakRSSBytes = peakRSS()
		rec.Metrics = m
		rec.Analytical = s.analytical()
		rec.Correct = bptr(correct)
		outcome := "accepted"
		if !correct {
			outcome = "silent_wrong"
		}
		rec.RecoveryOutcome, rec.RecoveryAttempts = sptr(outcome), iptr(0)
		return serve.Outcome{Record: rec, Correct: correct}
	}}
}
