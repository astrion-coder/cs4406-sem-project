package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math/rand/v2"

	serve "github.com/imankalyan/cs4406-sem-project/src/adapters/serve_go"
)

// serveArm plugs S3, S4, M0 and M1 into the shared serve and client loops.
type serveArm struct{}

// openCell lays out the seeded database and runs the offline phase once. The
// config it returns is the bench-mode one, so records are built the same way.
func openCell(o serve.Options) (cfg config, c cell, err error) {
	a, ok := arms[o.Arm]
	if !ok {
		return cfg, nil, serve.Usagef("unknown arm %q; this adapter serves %v", o.Arm, armNames())
	}
	g, err := geometryFor(a, int(o.DBBytes), int(o.RecordBytes))
	if err != nil {
		return cfg, nil, serve.Usagef("--arm %s: %v", o.Arm, err)
	}
	cfg = config{
		armName: o.Arm, arm: a, geom: g, dbBytes: int(o.DBBytes), recordBytes: int(o.RecordBytes),
		seed: o.Seed, fault: "none", recovery: "abort",
	}
	logf("%s: laying out %d-byte database (seed %d)", o.Arm, cfg.dbBytes, cfg.seed)
	if c, err = a.build(g, o.Seed); err != nil {
		return cfg, nil, fmt.Errorf("library rejected the parameters: %w", err)
	}
	logf("%s: offline phase", o.Arm)
	c.setup()
	return cfg, c, nil
}

func fingerprint(cfg config, c cell, serverID int) serve.Info {
	sum := sha256.Sum256(c.dbData())
	var recordBytes *int64 // null for the one-bit arms, as in bench records
	if !cfg.arm.oneBit {
		recordBytes = new(int64)
		*recordBytes = int64(cfg.recordBytes)
	}
	return serve.Info{
		Arm: cfg.armName, ServerID: serverID, Servers: cfg.arm.servers,
		DBBytes: int64(cfg.dbBytes), RecordBytes: recordBytes, NRecords: uint64(cfg.geom.nRecords),
		Seed: cfg.seed, UpstreamSHA: upstreamSHA, DBSHA256: hex.EncodeToString(sum[:]),
		// apir-code fixes the public matrix (utils.SeedMatrixA), so there is no seed to compare.
		PublicSeed: nil, SchemeParams: c.params(),
	}
}

func (serveArm) NewServer(o serve.Options) (serve.Server, error) {
	if a, ok := arms[o.Arm]; ok {
		switch {
		case a.servers == 2 && o.ServerID < 0:
			return nil, serve.Usagef("--arm %s has two servers, --server-id 0 or 1 is required", o.Arm)
		case a.servers == 1 && o.ServerID > 0:
			return nil, serve.Usagef("--arm %s has one server, --server-id must be 0 or absent", o.Arm)
		}
	}
	cfg, c, err := openCell(o)
	if err != nil {
		return nil, err
	}
	id := max(o.ServerID, 0)
	return &apirServer{cfg: cfg, c: c, id: id, qLen: c.queryLens()[id], info: fingerprint(cfg, c, id)}, nil
}

type apirServer struct {
	cfg  config
	c    cell
	id   int
	qLen int
	info serve.Info
}

func (s *apirServer) Info() serve.Info { return s.info }

func (s *apirServer) CheckQuery(q []byte) error {
	if len(q) != s.qLen {
		return fmt.Errorf("query is %d bytes, %s server %d expects %d", len(q), s.cfg.armName, s.id, s.qLen)
	}
	return nil
}

func (s *apirServer) Answer(q []byte) ([]byte, int64, error) {
	ans, sp := s.c.answer(s.id, q)
	return ans, int64(sp.wall * 1e9), nil
}

func (serveArm) NewClient(o serve.Options) (serve.Client, error) {
	if a, ok := arms[o.Arm]; ok && len(o.Connect) != a.servers {
		return nil, serve.Usagef("--arm %s has %d server(s), --connect lists %d", o.Arm, a.servers, len(o.Connect))
	}
	cfg, c, err := openCell(o)
	if err != nil {
		return nil, err
	}
	return &apirClient{
		cfg: cfg, c: c, info: fingerprint(cfg, c, 0),
		indices: rand.New(stream(cfg.seed, "index")), base: baseRecord(cfg),
	}, nil
}

type apirClient struct {
	cfg     config
	c       cell
	info    serve.Info
	indices *rand.Rand
	base    Record
	rep     int
}

func (c *apirClient) Info() serve.Info { return c.info }

func (c *apirClient) Next() serve.Round {
	rep := c.rep
	c.rep++
	index := c.indices.IntN(c.cfg.geom.nRecords)
	queries, qs := c.c.query(index)
	return serve.Round{Queries: queries, Finish: func(answers [][]byte, answerS []float64) serve.Outcome {
		var rs span
		out, rejected, panicMsg := reconstructGuarded(c.c, answers, &rs, true)
		correct := !rejected && bytes.Equal(out, c.c.truth(index))
		if panicMsg != "" {
			logf("%s: query %d: reconstruction panicked: %s", c.cfg.armName, rep, panicMsg)
		}
		r := c.base
		r.Phase, r.Rep = "online", rep
		r.SchemeParams = c.c.params()
		an := c.c.analytical()
		r.Analytical.UpBytes, r.Analytical.DownBytes = an.UpBytes, an.DownBytes
		m := &r.Metrics
		m.ProcessPeakRSSBytes = ptr(peakRSSBytes())
		m.QueryGenS, m.QueryGenCPUS = ptr(qs.wall), ptr(qs.cpu)
		up, down := lens(queries), lens(answers)
		m.UpBytes, m.UpBytesPerServer = ptr(sum(up)), up
		m.DownBytes, m.DownBytesPerServer = ptr(sum(down)), down
		m.ServerAnswerS = answerS
		m.ClientRecoverS, m.ClientRecoverCPUS = ptr(rs.wall), ptr(rs.cpu)
		m.VerifyFused = ptr(c.cfg.arm.verifiable)
		outcome := "accepted"
		switch {
		case rejected:
			outcome = "aborted"
		case !correct:
			outcome = "silent_wrong"
		}
		r.RecoveryOutcome, r.RecoveryAttempts = ptr(outcome), ptr(0)
		r.Correct = ptr(correct)
		if c.cfg.arm.verifiable {
			r.Detected = ptr(rejected)
		}
		return serve.Outcome{Record: r, Correct: correct}
	}}
}
