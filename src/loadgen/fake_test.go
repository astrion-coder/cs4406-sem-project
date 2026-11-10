package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeServer speaks docs/serve_protocol.md. Its answer to a query is a fixed
// function of the query, so a corpus can be built without an adapter.
type fakeServer struct {
	addr         string
	info         Info
	delay        time.Duration
	workers      int // 0: unlimited
	corruptEvery int // corrupt every n-th answer; 0: never
	errorEvery   int // send ERROR for every n-th query; 0: never

	start    time.Time
	quit     chan struct{}
	slots    chan struct{}
	seen     atomic.Int64
	active   atomic.Int64
	maxAct   atomic.Int64
	answered atomic.Int64
}

func answerFor(q []byte) []byte { return append(bytes.Repeat(q, 3), 0xA5) }

func testInfo(arm string, k, servers int, seed uint64) Info {
	rb := int64(32)
	return Info{Arm: arm, ServerID: k, Servers: servers, DBBytes: 1 << 20, RecordBytes: &rb, Seed: seed,
		Workers: 2, DBSHA256: "00ff"}
}

func startFake(t *testing.T, f *fakeServer) *fakeServer {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	f.addr, f.start, f.quit = ln.Addr().String(), time.Now(), make(chan struct{})
	if f.workers > 0 {
		f.slots = make(chan struct{}, f.workers)
	}
	t.Cleanup(func() {
		close(f.quit)
		ln.Close()
	})
	go func() {
		for {
			nc, err := ln.Accept()
			if err != nil {
				return
			}
			go f.handle(nc)
		}
	}()
	return f
}

func (f *fakeServer) handle(nc net.Conn) {
	defer nc.Close()
	r := bufio.NewReader(nc)
	var wmu sync.Mutex
	write := func(typ byte, parts ...[]byte) {
		wmu.Lock()
		defer wmu.Unlock()
		_ = WriteFrame(nc, typ, parts...)
	}
	for {
		typ, body, err := ReadFrame(r)
		if err != nil {
			return
		}
		switch typ {
		case TypeHello:
			b, _ := json.Marshal(f.info)
			write(TypeInfo, b)
		case TypeStatsReq:
			wall := time.Since(f.start).Seconds()
			b, _ := json.Marshal(Stats{WallS: wall, CPUUserS: 0.4 * wall, CPUSysS: 0.1 * wall, PeakRSSBytes: 4096,
				Answered: uint64(f.answered.Load())})
			write(TypeStats, b)
		case TypeQuery:
			n := f.seen.Add(1)
			id, q := body[:8], body[8:]
			go func() {
				if f.slots != nil {
					f.slots <- struct{}{}
					defer func() { <-f.slots }()
				}
				raise(&f.maxAct, f.active.Add(1))
				select {
				case <-time.After(f.delay):
				case <-f.quit:
					return
				}
				f.active.Add(-1)
				if f.errorEvery > 0 && n%int64(f.errorEvery) == 0 {
					write(TypeError, id, []byte("wrong query length"))
					return
				}
				ans := answerFor(q)
				if f.corruptEvery > 0 && n%int64(f.corruptEvery) == 0 {
					ans[0] ^= 1
				}
				f.answered.Add(1)
				write(TypeAnswer, id, u64(0), u64(uint64(f.delay)), ans)
			}()
		default:
			write(TypeError, u64(0), []byte("unknown frame type"))
			return
		}
	}
}

// testCorpus builds a corpus of n queries for the servers' INFO.
func testCorpus(t *testing.T, n int, infos ...Info) *Corpus {
	t.Helper()
	raw := make([][]byte, len(infos))
	for k, in := range infos {
		raw[k], _ = json.Marshal(in)
	}
	entries := make([][]Expect, n)
	for i := range entries {
		for k := range infos {
			q := []byte(fmt.Sprintf("query %d for server %d", i, k))
			ans := answerFor(q)
			entries[i] = append(entries[i], Expect{Query: q, AnswerLen: uint32(len(ans)), SHA: sha256.Sum256(ans)})
		}
	}
	var buf bytes.Buffer
	if err := WriteCorpus(&buf, raw, entries); err != nil {
		t.Fatal(err)
	}
	c, err := parseCorpus(&buf)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func raise(hi *atomic.Int64, v int64) {
	for {
		m := hi.Load()
		if v <= m || hi.CompareAndSwap(m, v) {
			return
		}
	}
}
