package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"
)

// request is one corpus query sent to every server of the arm. It completes
// when each server has answered or failed once.
type request struct {
	entry int
	sent  time.Time
	done  func(*request)

	mu      sync.Mutex
	left    int
	last    time.Time
	matched bool
	err     error
}

func newRequest(entry, servers int, done func(*request)) *request {
	return &request{entry: entry, left: servers, matched: true, done: done}
}

func (q *request) arrive(at time.Time, matched bool, err error) {
	q.mu.Lock()
	q.left--
	if at.After(q.last) {
		q.last = at
	}
	q.matched = q.matched && matched && err == nil
	if err != nil && q.err == nil {
		q.err = err
	}
	finished := q.left == 0
	q.mu.Unlock()
	if finished {
		q.done(q)
	}
}

// conn is one connection to one server. Its reader goroutine matches answers
// to requests by req_id and checks each against the corpus.
type conn struct {
	nc     net.Conn
	r      *bufio.Reader
	server int
	corpus *Corpus
	onDead func(error)

	mu      sync.Mutex
	pending map[uint64]*request
	dead    error
}

// hello dials addr, sends HELLO and reads INFO.
func hello(addr string) (net.Conn, *bufio.Reader, Info, error) {
	var in Info
	nc, err := net.Dial("tcp", addr)
	if err != nil {
		return nil, nil, in, err
	}
	if tc, ok := nc.(*net.TCPConn); ok {
		_ = tc.SetNoDelay(true)
	}
	r := bufio.NewReaderSize(nc, 1<<16)
	if err := WriteFrame(nc, TypeHello, []byte(`{"protocol": 1}`)); err != nil {
		nc.Close()
		return nil, nil, in, err
	}
	typ, body, err := ReadFrame(r)
	if err == nil && typ != TypeInfo {
		err = fmt.Errorf("expected INFO, got frame type 0x%02x", typ)
	}
	if err == nil {
		err = json.Unmarshal(body, &in)
	}
	if err != nil {
		nc.Close()
		return nil, nil, in, fmt.Errorf("%s: %w", addr, err)
	}
	return nc, r, in, nil
}

// dialChecked connects to server k and checks its INFO against the corpus.
func dialChecked(addr string, k int, c *Corpus) (net.Conn, *bufio.Reader, Info, error) {
	nc, r, in, err := hello(addr)
	if err != nil {
		return nil, nil, in, err
	}
	if diff := c.Infos[k].Mismatch(in); diff != "" {
		nc.Close()
		return nil, nil, in, fmt.Errorf("%s is not the server the corpus was recorded against: %s", addr, diff)
	}
	return nc, r, in, nil
}

func newConn(nc net.Conn, r *bufio.Reader, server int, c *Corpus, onDead func(error)) *conn {
	cn := &conn{nc: nc, r: r, server: server, corpus: c, onDead: onDead, pending: map[uint64]*request{}}
	go cn.readLoop()
	return cn
}

func (c *conn) fail(err error) {
	c.mu.Lock()
	if c.dead != nil {
		c.mu.Unlock()
		return
	}
	c.dead = err
	pending := c.pending
	c.pending = map[uint64]*request{}
	c.mu.Unlock()
	now := time.Now()
	for _, q := range pending {
		q.arrive(now, false, err)
	}
	c.onDead(err)
}

func (c *conn) take(id uint64) *request {
	c.mu.Lock()
	defer c.mu.Unlock()
	q := c.pending[id]
	delete(c.pending, id)
	return q
}

func (c *conn) readLoop() {
	for {
		typ, body, err := ReadFrame(c.r)
		at := time.Now()
		if err != nil {
			c.fail(fmt.Errorf("server %d: connection lost: %w", c.server, err))
			return
		}
		switch {
		case typ == TypeAnswer && len(body) >= 24:
			q := c.take(binary.BigEndian.Uint64(body))
			if q == nil {
				c.fail(fmt.Errorf("server %d: ANSWER for a req_id not in flight", c.server))
				return
			}
			want, ans := c.corpus.Entries[q.entry][c.server], body[24:]
			q.arrive(at, uint32(len(ans)) == want.AnswerLen && sha256.Sum256(ans) == want.SHA, nil)
		case typ == TypeError && len(body) >= 8:
			err := fmt.Errorf("server %d: ERROR: %s", c.server, body[8:])
			q := c.take(binary.BigEndian.Uint64(body))
			if q == nil {
				c.fail(err)
				return
			}
			q.arrive(at, false, err)
		default:
			c.fail(fmt.Errorf("server %d: unexpected frame type 0x%02x (%d bytes)", c.server, typ, len(body)))
			return
		}
	}
}

// send registers q under id and writes its QUERY. A connection that is dead
// or fails the write answers q with the error, so q always completes.
func (c *conn) send(id uint64, q *request) {
	c.mu.Lock()
	if c.dead != nil {
		err := c.dead
		c.mu.Unlock()
		q.arrive(time.Now(), false, err)
		return
	}
	c.pending[id] = q
	c.mu.Unlock()
	if err := WriteFrame(c.nc, TypeQuery, u64(id), c.corpus.Entries[q.entry][c.server].Query); err != nil {
		c.fail(fmt.Errorf("server %d: write: %w", c.server, err))
	}
}

// close ends the connection. Requests still in flight complete with
// errStopped, and the reader's own failure is then not reported.
func (c *conn) close() {
	c.mu.Lock()
	if c.dead == nil {
		c.dead = errStopped
	}
	pending := c.pending
	c.pending = map[uint64]*request{}
	c.mu.Unlock()
	c.nc.Close()
	now := time.Now()
	for _, q := range pending {
		q.arrive(now, false, errStopped)
	}
}

// Stats is the STATS body.
type Stats struct {
	WallS        float64 `json:"wall_s"`
	CPUUserS     float64 `json:"cpu_user_s"`
	CPUSysS      float64 `json:"cpu_sys_s"`
	PeakRSSBytes int64   `json:"peak_rss_bytes"`
	Answered     uint64  `json:"answered"`
	Queued       int64   `json:"queued"`
}

// control is a connection that only asks for STATS, so its replies never
// interleave with answers.
type control struct {
	nc net.Conn
	r  *bufio.Reader
}

func (c control) stats() (Stats, error) {
	var s Stats
	if err := WriteFrame(c.nc, TypeStatsReq); err != nil {
		return s, err
	}
	typ, body, err := ReadFrame(c.r)
	if err != nil {
		return s, err
	}
	if typ != TypeStats {
		return s, fmt.Errorf("expected STATS, got frame type 0x%02x", typ)
	}
	return s, json.Unmarshal(body, &s)
}

var errStopped = errors.New("load generator stopped")
