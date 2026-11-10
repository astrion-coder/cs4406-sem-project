package serve

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/signal"
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// QueueSize is the bound of the one FIFO queue the readers and workers share.
const QueueSize = 4096

// Server is one arm's server side. Everything on it is called from many
// goroutines at once except Info.
type Server interface {
	// Info is the INFO body, with Protocol and Workers left for the loop.
	Info() Info
	// CheckQuery rejects a query whose length does not match the arm's
	// parameters. It must not call the library.
	CheckQuery(q []byte) error
	// Answer calls the library's answer function and returns its encoded
	// answer and the nanoseconds the library call alone took. It must be safe
	// to call concurrently. A panic becomes an ERROR frame.
	Answer(q []byte) (ans []byte, answerNS int64, err error)
}

type job struct {
	c    *serverConn
	id   uint64
	q    []byte
	read time.Time
}

type serverConn struct {
	nc      net.Conn
	mu      sync.Mutex
	pending sync.WaitGroup
}

func (c *serverConn) send(typ byte, parts ...[]byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return WriteFrame(c.nc, typ, parts...)
}

type loop struct {
	srv      Server
	info     []byte
	queue    chan job
	answered atomic.Uint64
	queued   atomic.Int64 // read, not yet started by a worker

	mu      sync.Mutex
	conns   map[*serverConn]struct{}
	closing bool
	readers sync.WaitGroup
}

// RunServer serves until SIGTERM or SIGINT, then finishes the queries it has
// read, appends the summary record and returns nil.
func RunServer(o Options, s Server) error {
	runtime.GOMAXPROCS(o.Workers + 1)
	info := s.Info()
	info.Protocol, info.Workers = 1, o.Workers
	body, err := json.Marshal(info)
	if err != nil {
		return err
	}
	ln, err := net.Listen("tcp", o.Listen)
	if err != nil {
		return err
	}
	l := &loop{srv: s, info: body, queue: make(chan job, QueueSize), conns: map[*serverConn]struct{}{}}

	var workers sync.WaitGroup
	for i := 0; i < o.Workers; i++ {
		workers.Add(1)
		go func() { defer workers.Done(); l.work() }()
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	acceptErr := make(chan error, 1)
	go func() { acceptErr <- l.accept(ln) }()

	fmt.Fprintf(os.Stderr, "vpir-serve ready %s\n", ln.Addr())

	select {
	case <-sig:
	case err = <-acceptErr:
	}
	ln.Close()
	l.mu.Lock()
	l.closing = true
	for c := range l.conns {
		c.nc.SetReadDeadline(time.Now())
	}
	l.mu.Unlock()
	l.readers.Wait()
	close(l.queue)
	workers.Wait()
	if err != nil {
		return err
	}
	if o.Out == "" {
		return nil
	}
	line, err := json.Marshal(summaryRecord{
		Mode: "serve", Protocol: info.Protocol, Arm: info.Arm, ServerID: info.ServerID, Servers: info.Servers,
		DBBytes: info.DBBytes, RecordBytes: info.RecordBytes, NRecords: info.NRecords, Seed: info.Seed,
		Workers: info.Workers, UpstreamSHA: info.UpstreamSHA, DBSHA256: info.DBSHA256, PublicSeed: info.PublicSeed,
		Stats: readStats(l.answered.Load(), l.queued.Load()),
	})
	if err != nil {
		return err
	}
	f, err := os.OpenFile(o.Out, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.Write(append(line, '\n'))
	return err
}

func (l *loop) accept(ln net.Listener) error {
	for {
		nc, err := ln.Accept()
		if err != nil {
			l.mu.Lock()
			closing := l.closing
			l.mu.Unlock()
			if closing {
				return nil
			}
			return err
		}
		c := &serverConn{nc: nc}
		l.mu.Lock()
		if l.closing {
			l.mu.Unlock()
			nc.Close()
			continue
		}
		l.conns[c] = struct{}{}
		l.readers.Add(1)
		l.mu.Unlock()
		go l.read(c)
	}
}

// read is one connection's reader goroutine: the handshake, then frames until
// the peer closes, misbehaves or the server shuts down. The connection closes
// once every query it queued has been answered.
func (l *loop) read(c *serverConn) {
	defer func() {
		l.readers.Done()
		go func() {
			c.pending.Wait()
			c.nc.Close()
			l.mu.Lock()
			delete(l.conns, c)
			l.mu.Unlock()
		}()
	}()
	r := bufio.NewReaderSize(c.nc, 1<<16)
	fail := func(format string, a ...any) { _ = c.send(TypeError, errFrameBody(0, format, a...)...) }

	typ, body, err := ReadFrame(r)
	if err != nil {
		l.readFailed(fail, err)
		return
	}
	var hello struct {
		Protocol int `json:"protocol"`
	}
	if typ != TypeHello || json.Unmarshal(body, &hello) != nil || hello.Protocol != 1 {
		fail("expected HELLO with protocol 1")
		return
	}
	if c.send(TypeInfo, l.info) != nil {
		return
	}
	for {
		typ, body, err := ReadFrame(r)
		if err != nil {
			l.readFailed(fail, err)
			return
		}
		switch typ {
		case TypeQuery:
			if len(body) < 8 {
				fail("QUERY shorter than its request id")
				return
			}
			id, q := binary.BigEndian.Uint64(body), body[8:]
			if err := l.srv.CheckQuery(q); err != nil {
				_ = c.send(TypeError, errFrameBody(id, "%v", err)...)
				continue
			}
			c.pending.Add(1)
			l.queued.Add(1)
			l.queue <- job{c: c, id: id, q: q, read: time.Now()}
		case TypeStatsReq:
			b, _ := json.Marshal(readStats(l.answered.Load(), l.queued.Load()))
			_ = c.send(TypeStats, b)
		default:
			if knownType(typ) {
				fail("unexpected frame type 0x%02x from a client", typ)
			} else {
				fail("unknown frame type 0x%02x", typ)
			}
			return
		}
	}
}

// readFailed answers an oversized or empty frame with ERROR. A closed
// connection, a shutdown deadline and a reset need no answer.
func (l *loop) readFailed(fail func(string, ...any), err error) {
	if err == ErrFrameTooLarge || err == ErrEmptyFrame {
		fail("%v", err)
	}
}

func (l *loop) work() {
	for j := range l.queue {
		start := time.Now()
		l.queued.Add(-1)
		queueNS := start.Sub(j.read).Nanoseconds()
		ans, answerNS, err := l.answer(j.q)
		if err != nil {
			_ = j.c.send(TypeError, errFrameBody(j.id, "%v", err)...)
		} else {
			if j.c.send(TypeAnswer, u64(j.id), u64(uint64(queueNS)), u64(uint64(answerNS)), ans) == nil {
				l.answered.Add(1)
			}
		}
		j.c.pending.Done()
	}
}

func (l *loop) answer(q []byte) (ans []byte, answerNS int64, err error) {
	defer func() {
		if p := recover(); p != nil {
			ans, err = nil, fmt.Errorf("answer failed: %v", p)
			fmt.Fprintln(os.Stderr, "worker recovered:", err)
		}
	}()
	return l.srv.Answer(q)
}
