package main

import (
	"fmt"
	"runtime"
	"runtime/debug"
	"syscall"
	"time"

	"github.com/ahenzinger/simplepir/pir"
)

type span struct{ wall, cpu float64 }

func cpuSeconds() float64 {
	var ru syscall.Rusage
	syscall.Getrusage(syscall.RUSAGE_SELF, &ru)
	tv := func(t syscall.Timeval) float64 { return float64(t.Sec) + float64(t.Usec)/1e6 }
	return tv(ru.Utime) + tv(ru.Stime)
}

func timed(f func()) span {
	c0, t0 := cpuSeconds(), time.Now()
	f()
	return span{time.Since(t0).Seconds(), cpuSeconds() - c0}
}

// peakRSS converts ru_maxrss (bytes on macOS, KiB on Linux) in one place.
func peakRSS() int64 {
	var ru syscall.Rusage
	syscall.Getrusage(syscall.RUSAGE_SELF, &ru)
	if runtime.GOOS == "darwin" {
		return int64(ru.Maxrss)
	}
	return int64(ru.Maxrss) * 1024
}

type config struct {
	arm                     string
	dbBytes                 int64
	recordBytes             int64
	seed                    int64
	fault                   string
	faultProb               float64
	recovery                string
	runID, gitSHA, platform *string
}

// session holds the server and client state of one database. Setup mutates
// the database in place (the library squishes it), so squished tracks when
// Reset must run before the next Setup.
type session struct {
	cfg config
	pi  *pir.SimplePIR
	lay Layout
	p   pir.Params
	db  *pir.Database

	records  *stream
	index    *stream
	faultOn  *stream
	faultOff *stream
	check    *stream

	squished      bool
	faultChecked  bool
	decodeChecked bool
	serverShared  pir.State
	serverState   pir.State
	hintBytes     []byte
	aBytes        []byte
	clientA       pir.State
	clientH       pir.Msg
}

func (s *session) record(r uint64, buf []byte) {
	s.records.fill(buf, r*uint64(s.lay.RecordBytes))
}

func newSession(cfg config, lay Layout, pi *pir.SimplePIR) (*session, error) {
	s := &session{
		cfg: cfg, pi: pi, lay: lay, p: lay.Params,
		records:  newStream("db", cfg.seed),
		index:    newStream("index", cfg.seed),
		faultOn:  newStream("fault-online", cfg.seed),
		faultOff: newStream("fault-offline", cfg.seed),
		check:    newStream("check", cfg.seed),
	}
	vals := make([]uint64, lay.Entries)
	buf := make([]byte, lay.RecordBytes)
	for r := uint64(0); r < lay.Records; r++ {
		s.record(r, buf)
		for k, v := range pieces(buf, lay.Pieces) {
			vals[lay.Entry(r, k)] = v
		}
	}
	s.db = pir.MakeDB(lay.Entries, lay.EntryBits, &s.p, vals)
	vals = nil
	runtime.GC()
	if err := s.spotCheck(4); err != nil {
		return nil, err
	}
	return s, nil
}

// spotCheck reads random records back out of the unsquished database and
// compares them with the generator, so a layout or Reset bug cannot hide.
func (s *session) spotCheck(count int) error {
	buf := make([]byte, s.lay.RecordBytes)
	for i := 0; i < count; i++ {
		r := s.check.below(s.lay.Records)
		s.record(r, buf)
		for k, want := range pieces(buf, s.lay.Pieces) {
			if got := s.db.GetElem(s.lay.Entry(r, k)); got != want {
				return fmt.Errorf("database entry check failed: record %d piece %d got %x want %x", r, k, got, want)
			}
		}
	}
	return nil
}

func (s *session) base(phase string, rep, warmup int) Record {
	c := s.cfg
	return Record{
		RunID: c.runID, GitSHAHarness: c.gitSHA, Platform: c.platform,
		UpstreamSHA: upstreamSHA, Adapter: "simplepir_go", Arm: c.arm,
		DBBytes: c.dbBytes, RecordBytes: c.recordBytes, RetrievedBits: 8 * c.recordBytes,
		NRecords: s.lay.Records, Phase: phase, Rep: rep, IsWarmup: rep < warmup,
		Seed: c.seed, Fault: c.fault, FaultProb: c.faultProb, Recovery: c.recovery,
		Status: "ok", SchemeParams: s.schemeParams(),
	}
}

func (s *session) schemeParams() SchemeParams {
	l, p := s.lay, s.p
	return SchemeParams{
		N: p.N, LogQ: p.Logq, P: p.P, Sigma: p.Sigma, Ell: p.L, M: p.M,
		EntryBits: l.EntryBits, Ne: l.Ne, Packing: l.Packing, PiecesPerRecord: l.Pieces,
		RecordRows: l.RecordRows, Entries: l.Entries,
		Basis: s.db.Info.Basis, Squishing: s.db.Info.Squishing,
		CompressedA: false, Parallelism: "single-thread",
	}
}

// offlineTimes is what one offline phase measured.
type offlineTimes struct {
	init, setup   span
	hint, aMatrix int64
}

// offline runs Init (public matrix A) and Setup on a pristine database, each
// timed alone. Init runs with the collector on, because the library draws
// every element of A through math/big and leaves garbage behind.
func (s *session) offline() (offlineTimes, error) {
	if s.squished {
		s.pi.Reset(s.db, s.p)
		s.squished = false
		s.serverShared, s.serverState = pir.State{}, pir.State{}
		runtime.GC()
		if err := s.spotCheck(4); err != nil {
			return offlineTimes{}, err
		}
	}
	var t offlineTimes
	debug.SetGCPercent(100)
	t.init = timed(func() { s.serverShared = s.pi.Init(s.db.Info, s.p) })
	debug.SetGCPercent(-1)
	runtime.GC()

	var h pir.Msg
	t.setup = timed(func() { s.serverState, h = s.pi.Setup(s.db, s.serverShared, s.p) })
	s.squished = true

	s.hintBytes = encode(h.Data[0])
	s.aBytes = encode(s.serverShared.Data[0])
	s.clientA, s.clientH = pir.State{}, pir.Msg{}
	t.hint, t.aMatrix = int64(len(s.hintBytes)), int64(len(s.aBytes))
	return t, nil
}

func (s *session) offlineRecord(rep, warmup int) (Record, error) {
	t, err := s.offline()
	if err != nil {
		return Record{}, err
	}
	rec := s.base("offline", rep, warmup)
	if s.cfg.fault == "flip_hint_bit" && s.faultOff.float() < s.cfg.faultProb {
		flipped := append([]byte(nil), s.hintBytes...)
		flipBytesBit(flipped, s.faultOff.below(8*uint64(len(flipped))))
		rec.Faulted = true
	}
	rec.Metrics = Metrics{
		PublicParamsS: f64(t.init.wall), PublicParamsCPUS: f64(t.init.cpu),
		ServerPreprocS: f64(t.setup.wall), ServerPreprocCPUS: f64(t.setup.cpu),
		ClientOfflineS: f64(0), ClientOfflineCPUS: f64(0),
		OfflineDownBytes: i64(t.hint + t.aMatrix), OfflineDownBytesHintOnly: i64(t.hint),
		OfflineUpBytes: i64(0), ClientStorageBytes: i64(t.hint + t.aMatrix),
		ProcessPeakRSSBytes: peakRSS(),
	}
	rec.Analytical = s.analytical()
	return rec, nil
}

// analytical is upstream's size formulas, the same in every record.
func (s *session) analytical() Analytical {
	return Analytical{
		UpBytes: i64(s.lay.upBytes()), DownBytes: i64(s.lay.downBytes()),
		OfflineDownBytesHintOnly: i64(s.lay.hintBytes()),
	}
}

// clientStates builds the client's copy of A and H from the downloaded bytes.
func (s *session) clientStates() {
	if s.clientA.Data == nil {
		s.clientA = pir.MakeState(decode(s.aBytes, s.p.M, s.p.N))
		s.clientH = pir.MakeMsg(decode(s.hintBytes, s.p.L, s.p.N))
	}
}

// recoverRecord decodes record idx once. Everything that does not depend on
// the piece (the query offset and H * secret over the whole column) is
// computed once, then each piece is read out of the answer rows it occupies
// and rebuilt with ReconstructElem. It is Recover's body with the
// piece-independent part hoisted out of the per-piece loop, written with
// exported functions only, and it leaves the answer untouched.
func (s *session) recoverRecord(idx uint64, query, ans pir.Msg, cstate pir.State) []uint64 {
	p, info := s.p, s.db.Info
	a := ans.Data[0]

	offset := uint64(0)
	for j := uint64(0); j < p.M; j++ {
		offset += (p.P / 2) * query.Data[0].Get(j, 0)
	}
	offset %= 1 << p.Logq
	offset = 1<<p.Logq - offset

	interm := pir.MatrixMul(s.clientH.Data[0], cstate.Data[0])

	got := make([]uint64, s.lay.Pieces)
	vals := make([]uint64, info.Ne)
	for k := range got {
		entry := s.lay.Entry(idx, k)
		row := entry / p.M
		for e := range vals {
			j := row*info.Ne + uint64(e)
			vals[e] = p.Round(uint64(a.Data[j]-interm.Data[j]) + offset)
		}
		got[k] = pir.ReconstructElem(vals, entry, info)
	}
	return got
}

// checkAgainstUpstream compares the decode with the library's own Recover on
// the record's first and last piece. Recover is called outside every timer.
func (s *session) checkAgainstUpstream(idx uint64, got []uint64, query, ans pir.Msg, cstate pir.State) error {
	for _, k := range []int{0, len(got) - 1} {
		want := s.pi.Recover(s.lay.Entry(idx, k), 0, s.clientH, query, ans, s.clientA, cstate, s.p, s.db.Info)
		if got[k] != want {
			return fmt.Errorf("decode check: record %d piece %d got %x, upstream Recover %x", idx, k, got[k], want)
		}
	}
	return nil
}

func sameRecord(a, b []uint64) bool {
	for k := range a {
		if a[k] != b[k] {
			return false
		}
	}
	return true
}

func (s *session) onlineRecord(rep, warmup int) (Record, error) {
	s.clientStates()
	p, db := s.p, s.db
	runtime.GC()

	idx := s.index.below(s.lay.Records)
	faulted := s.cfg.fault != "none" && s.faultOn.float() < s.cfg.faultProb
	m := Metrics{VerifyFused: bptr(false)}

	var cstate pir.State
	var query pir.Msg
	qg := timed(func() { cstate, query = s.pi.Query(s.lay.Entry(idx, 0), s.clientA, p, db.Info) })
	qBytes := encode(query.Data[0])
	qRows := query.Data[0].Rows
	serverQuery := pir.MakeMsgSlice(pir.MakeMsg(decode(qBytes, qRows, 1)))

	var ans pir.Msg
	sa := timed(func() { ans = s.pi.Answer(db, serverQuery, s.serverState, s.serverShared, p) })
	aBytes := encode(ans.Data[0])
	down := int64(len(aBytes))

	want := make([]byte, s.lay.RecordBytes)
	s.record(idx, want)
	wantPieces := pieces(want, s.lay.Pieces)

	// The first targeted fault is checked against an untimed honest decode:
	// the honest client must return the record, and the fault must change it.
	targeted := faulted && (s.cfg.fault == "flip_answer_bit" || s.cfg.fault == "flip_hint_bit")
	var honest []uint64
	if targeted && !s.faultChecked {
		honest = s.recoverRecord(idx, query, pir.MakeMsg(decode(aBytes, ans.Data[0].Rows, 1)), cstate)
		if !sameRecord(honest, wantPieces) {
			return Record{}, fmt.Errorf("fault check: honest decode of record %d is wrong", idx)
		}
	}

	var restoreHint func()
	if faulted {
		switch s.cfg.fault {
		case "flip_answer_bit":
			flipBytesBit(aBytes, s.answerFaultBit(idx))
		case "wrong_record":
			other := idx
			if s.lay.Records > 1 {
				other = (idx + 1 + s.faultOn.below(s.lay.Records-1)) % s.lay.Records
			}
			_, oq := s.pi.Query(s.lay.Entry(other, 0), s.clientA, p, db.Info)
			oa := s.pi.Answer(db, pir.MakeMsgSlice(oq), s.serverState, s.serverShared, p)
			aBytes = encode(oa.Data[0])
		case "flip_hint_bit":
			bit := s.hintFaultBit(idx, cstate.Data[0])
			flipMatrixBit(s.clientH.Data[0], bit)
			restoreHint = func() { flipMatrixBit(s.clientH.Data[0], bit) }
		}
	}
	clientAns := pir.MakeMsg(decode(aBytes, ans.Data[0].Rows, 1))

	var got []uint64
	rc := timed(func() { got = s.recoverRecord(idx, query, clientAns, cstate) })
	if !s.decodeChecked {
		if err := s.checkAgainstUpstream(idx, got, query, clientAns, cstate); err != nil {
			return Record{}, err
		}
		s.decodeChecked = true
	}
	if restoreHint != nil {
		restoreHint()
	}
	correct := sameRecord(got, wantPieces)
	if honest != nil {
		if correct {
			return Record{}, fmt.Errorf("fault check: %s did not change the decoded record %d", s.cfg.fault, idx)
		}
		s.faultChecked = true
	}

	rec := s.base("online", rep, warmup)
	rec.Faulted = faulted
	m.QueryGenS, m.QueryGenCPUS = f64(qg.wall), f64(qg.cpu)
	m.UpBytes, m.UpBytesPerServer = i64(int64(len(qBytes))), []int64{int64(len(qBytes))}
	m.ServerAnswerS, m.ServerAnswerCPUS = []float64{sa.wall}, []float64{sa.cpu}
	m.DownBytes, m.DownBytesPerServer = i64(down), []int64{down}
	m.ClientRecoverS, m.ClientRecoverCPUS = f64(rc.wall), f64(rc.cpu)
	m.ProcessPeakRSSBytes = peakRSS()
	rec.Metrics = m
	rec.Analytical = s.analytical()
	rec.Correct = bptr(correct)
	return rec, nil
}
