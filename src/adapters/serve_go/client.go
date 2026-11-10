package serve

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync"
	"time"
)

// Client is one arm's client side: one object per client process, never
// shared between goroutines.
type Client interface {
	// Info is the client's own fingerprint, rebuilt from the seed.
	Info() Info
	// Next generates the next query, one message per server.
	Next() Round
}

// Round is one generated query and what to do with its answers.
type Round struct {
	Queries [][]byte
	// Finish decodes and verifies the answers. serverAnswerS is each
	// server's answer_ns in seconds. The record is bench-shaped, and the loop
	// adds mode and rtt_s.
	Finish func(answers [][]byte, serverAnswerS []float64) Outcome
}

type Outcome struct {
	Record  any
	Correct bool
}

type reply struct {
	queueNS, answerNS uint64
	ans               []byte
	at                time.Time
	err               error
}

// clientConn is one connection to one server. Several queries may be in
// flight, and the reader goroutine matches answers to them by request id.
type clientConn struct {
	nc      net.Conn
	r       *bufio.Reader
	wmu     sync.Mutex
	pmu     sync.Mutex
	pending map[uint64]chan reply
	dead    error
	info    Info
	rawInfo []byte
}

func dial(addr string) (*clientConn, error) {
	nc, err := net.Dial("tcp", addr)
	if err != nil {
		return nil, err
	}
	c := &clientConn{nc: nc, r: bufio.NewReaderSize(nc, 1<<16), pending: map[uint64]chan reply{}}
	if err := WriteFrame(nc, TypeHello, []byte(`{"protocol": 1}`)); err != nil {
		nc.Close()
		return nil, err
	}
	typ, body, err := ReadFrame(c.r)
	if err != nil {
		nc.Close()
		return nil, fmt.Errorf("%s: reading INFO: %w", addr, err)
	}
	if typ != TypeInfo {
		nc.Close()
		return nil, fmt.Errorf("%s: expected INFO, got frame type 0x%02x %q", addr, typ, body)
	}
	if err := json.Unmarshal(body, &c.info); err != nil {
		nc.Close()
		return nil, fmt.Errorf("%s: bad INFO: %w", addr, err)
	}
	c.rawInfo = body
	go c.readLoop()
	return c, nil
}

func (c *clientConn) fail(err error) {
	c.pmu.Lock()
	defer c.pmu.Unlock()
	if c.dead == nil {
		c.dead = err
	}
	for id, ch := range c.pending {
		ch <- reply{err: err}
		delete(c.pending, id)
	}
}

func (c *clientConn) readLoop() {
	for {
		typ, body, err := ReadFrame(c.r)
		if err != nil {
			c.fail(fmt.Errorf("connection lost: %w", err))
			return
		}
		at := time.Now()
		switch typ {
		case TypeAnswer:
			if len(body) < 24 {
				c.fail(fmt.Errorf("ANSWER shorter than its header"))
				return
			}
			id := binary.BigEndian.Uint64(body)
			c.deliver(id, reply{
				queueNS: binary.BigEndian.Uint64(body[8:]), answerNS: binary.BigEndian.Uint64(body[16:]),
				ans: body[24:], at: at,
			})
		case TypeError:
			if len(body) < 8 {
				c.fail(fmt.Errorf("ERROR shorter than its request id"))
				return
			}
			err := fmt.Errorf("server error: %s", body[8:])
			if id := binary.BigEndian.Uint64(body); !c.deliver(id, reply{err: err}) {
				c.fail(err)
				return
			}
		case TypeStats:
		default:
			c.fail(fmt.Errorf("unexpected frame type 0x%02x from the server", typ))
			return
		}
	}
}

func (c *clientConn) deliver(id uint64, r reply) bool {
	c.pmu.Lock()
	defer c.pmu.Unlock()
	ch, ok := c.pending[id]
	if ok {
		ch <- r
		delete(c.pending, id)
	}
	return ok
}

// send writes one QUERY and returns the channel its reply arrives on and the
// time just before the write.
func (c *clientConn) send(id uint64, q []byte) (<-chan reply, time.Time, error) {
	ch := make(chan reply, 1)
	c.pmu.Lock()
	if c.dead != nil {
		c.pmu.Unlock()
		return nil, time.Time{}, c.dead
	}
	c.pending[id] = ch
	c.pmu.Unlock()
	c.wmu.Lock()
	t0 := time.Now()
	err := WriteFrame(c.nc, TypeQuery, u64(id), q)
	c.wmu.Unlock()
	return ch, t0, err
}

// RunClient runs o.SessionQueries queries, one at a time, against the
// servers in o.Connect, writes one record per query to o.Out and, with
// o.CorpusOut, the corpus. It returns an error, so the process exits 1, on a
// fingerprint difference or a wrong decode.
func RunClient(o Options, cl Client) error {
	own := cl.Info()
	conns := make([]*clientConn, len(o.Connect))
	for k, addr := range o.Connect {
		c, err := dial(addr)
		if err != nil {
			return err
		}
		defer c.nc.Close()
		conns[k] = c
		if diff := own.Mismatch(c.info); diff != "" {
			return fmt.Errorf("%s: fingerprint differs from this client's, %s", addr, diff)
		}
		if c.info.ServerID != k || c.info.Servers != len(o.Connect) {
			return fmt.Errorf("%s: is server %d of %d, expected server %d of %d", addr, c.info.ServerID, c.info.Servers, k, len(o.Connect))
		}
	}
	out, err := os.OpenFile(o.Out, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer out.Close()
	var corpus *bufio.Writer
	if o.CorpusOut != "" {
		f, err := os.Create(o.CorpusOut)
		if err != nil {
			return err
		}
		defer f.Close()
		corpus = bufio.NewWriterSize(f, 1<<20)
		defer corpus.Flush()
		for _, c := range conns {
			if err := WriteFrame(corpus, TypeInfo, c.rawInfo); err != nil {
				return err
			}
		}
	}

	for i := 0; i < o.SessionQueries; i++ {
		round := cl.Next()
		if len(round.Queries) != len(conns) {
			return fmt.Errorf("the arm made %d queries for %d servers", len(round.Queries), len(conns))
		}
		id := uint64(i) + 1 // 0 is reserved for errors about the whole connection
		chans := make([]<-chan reply, len(conns))
		sent := make([]time.Time, len(conns))
		for k, c := range conns {
			if chans[k], sent[k], err = c.send(id, round.Queries[k]); err != nil {
				return fmt.Errorf("server %d: %w", k, err)
			}
		}
		answers := make([][]byte, len(conns))
		answerS := make([]float64, len(conns))
		rtt := make([]float64, len(conns))
		for k := range conns {
			r := <-chans[k]
			if r.err != nil {
				return fmt.Errorf("server %d, query %d: %w", k, i, r.err)
			}
			answers[k], answerS[k], rtt[k] = r.ans, float64(r.answerNS)*1e-9, r.at.Sub(sent[k]).Seconds()
		}
		res := round.Finish(answers, answerS)
		line, err := clientRecordLine(res.Record, rtt)
		if err != nil {
			return err
		}
		if _, err := out.Write(line); err != nil {
			return err
		}
		if !res.Correct {
			return fmt.Errorf("query %d decoded to the wrong record or failed verification", i)
		}
		if corpus != nil {
			for k := range conns {
				sum := sha256.Sum256(answers[k])
				var exp bytes.Buffer
				exp.Write(u64(id))
				exp.WriteByte(byte(k))
				var n [4]byte
				binary.BigEndian.PutUint32(n[:], uint32(len(answers[k])))
				exp.Write(n[:])
				exp.Write(sum[:])
				if err := WriteFrame(corpus, TypeQuery, u64(id), round.Queries[k]); err != nil {
					return err
				}
				if err := WriteFrame(corpus, TypeExpect, exp.Bytes()); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// clientRecordLine adds mode and metrics.rtt_s to a bench-shaped record
// without the adapters' record types having to carry client-only fields.
func clientRecordLine(rec any, rtt []float64) ([]byte, error) {
	raw, err := json.Marshal(rec)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var m map[string]any
	if err := dec.Decode(&m); err != nil {
		return nil, err
	}
	m["mode"] = "client"
	metrics, ok := m["metrics"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("record has no metrics object")
	}
	metrics["rtt_s"] = rtt
	line, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	return append(line, '\n'), nil
}
