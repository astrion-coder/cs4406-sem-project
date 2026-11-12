package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"os"
)

// Info is the part of an INFO body the load generator reads.
type Info struct {
	Arm         string  `json:"arm"`
	ServerID    int     `json:"server_id"`
	Servers     int     `json:"servers"`
	DBBytes     int64   `json:"db_bytes"`
	RecordBytes *int64  `json:"record_bytes"`
	Seed        uint64  `json:"seed"`
	Workers     int     `json:"workers"`
	DBSHA256    string  `json:"db_sha256"`
	PublicSeed  *string `json:"public_seed"`
}

// Mismatch names the first field on which a live server's INFO differs from
// the INFO the corpus recorded, or returns "". Workers may differ: RQ7
// replays one corpus against servers started with several W.
func (rec Info) Mismatch(live Info) string {
	str := func(p *string) string {
		if p == nil {
			return "null"
		}
		return *p
	}
	num := func(p *int64) string {
		if p == nil {
			return "null"
		}
		return fmt.Sprint(*p)
	}
	pairs := [][3]string{
		{"arm", rec.Arm, live.Arm},
		{"server_id", fmt.Sprint(rec.ServerID), fmt.Sprint(live.ServerID)},
		{"servers", fmt.Sprint(rec.Servers), fmt.Sprint(live.Servers)},
		{"db_bytes", fmt.Sprint(rec.DBBytes), fmt.Sprint(live.DBBytes)},
		{"record_bytes", num(rec.RecordBytes), num(live.RecordBytes)},
		{"seed", fmt.Sprint(rec.Seed), fmt.Sprint(live.Seed)},
		{"db_sha256", rec.DBSHA256, live.DBSHA256},
		{"public_seed", str(rec.PublicSeed), str(live.PublicSeed)},
	}
	for _, p := range pairs {
		if p[1] != p[2] {
			return fmt.Sprintf("%s: corpus %s, server %s", p[0], p[1], p[2])
		}
	}
	return ""
}

// Expect is one server's share of a corpus query: the query bytes after the
// req_id, and the length and SHA-256 of the honest answer.
type Expect struct {
	Query     []byte
	AnswerLen uint32
	SHA       [32]byte
}

// Corpus is a parsed corpus file: one INFO per server, then per query one
// Expect per server, in req_id order.
type Corpus struct {
	Infos   []Info
	Entries [][]Expect
}

// ReadCorpus parses the file the adapters' --corpus-out writes
// (docs/serve_protocol.md, "Corpus").
func ReadCorpus(path string) (*Corpus, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseCorpus(bytes.NewReader(data))
}

func parseCorpus(r io.Reader) (*Corpus, error) {
	type frame struct {
		typ  byte
		body []byte
	}
	var frames []frame
	for {
		typ, body, err := ReadFrame(r)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("corpus frame %d: %w", len(frames), err)
		}
		frames = append(frames, frame{typ, body})
	}
	c := &Corpus{}
	for len(frames) > 0 && frames[0].typ == TypeInfo {
		var in Info
		if err := json.Unmarshal(frames[0].body, &in); err != nil {
			return nil, fmt.Errorf("corpus INFO %d: %w", len(c.Infos), err)
		}
		c.Infos = append(c.Infos, in)
		frames = frames[1:]
	}
	n := len(c.Infos)
	if n == 0 {
		return nil, fmt.Errorf("corpus starts with no INFO frame")
	}
	for k, in := range c.Infos {
		if in.ServerID != k || in.Servers != n {
			return nil, fmt.Errorf("corpus INFO %d is server %d of %d", k, in.ServerID, in.Servers)
		}
	}
	if len(frames) == 0 || len(frames)%(2*n) != 0 {
		return nil, fmt.Errorf("corpus has %d frames after INFO, not a positive multiple of %d", len(frames), 2*n)
	}
	for i := 0; i < len(frames); i += 2 * n {
		id := uint64(len(c.Entries)) + 1
		row := make([]Expect, n)
		for k := 0; k < n; k++ {
			q, e := frames[i+2*k], frames[i+2*k+1]
			if q.typ != TypeQuery || e.typ != TypeExpect || len(q.body) < 8 || len(e.body) != 45 {
				return nil, fmt.Errorf("corpus query %d, server %d: expected QUERY then EXPECT", id, k)
			}
			qid, eid, sid := binary.BigEndian.Uint64(q.body), binary.BigEndian.Uint64(e.body), int(e.body[8])
			if qid != id || eid != id || sid != k {
				return nil, fmt.Errorf("corpus query %d, server %d: frames carry req_id %d/%d and server %d", id, k, qid, eid, sid)
			}
			row[k] = Expect{Query: q.body[8:], AnswerLen: binary.BigEndian.Uint32(e.body[9:13])}
			copy(row[k].SHA[:], e.body[13:45])
		}
		c.Entries = append(c.Entries, row)
	}
	return c, nil
}

// WriteCorpus writes a corpus in the same layout. Tests use it to stand in
// for an adapter's --corpus-out.
func WriteCorpus(w io.Writer, infos [][]byte, entries [][]Expect) error {
	for _, in := range infos {
		if err := WriteFrame(w, TypeInfo, in); err != nil {
			return err
		}
	}
	for i, row := range entries {
		id := uint64(i) + 1
		for k, e := range row {
			var tail [37]byte
			tail[0] = byte(k)
			binary.BigEndian.PutUint32(tail[1:5], e.AnswerLen)
			copy(tail[5:], e.SHA[:])
			if err := WriteFrame(w, TypeQuery, u64(id), e.Query); err != nil {
				return err
			}
			if err := WriteFrame(w, TypeExpect, u64(id), tail[:]); err != nil {
				return err
			}
		}
	}
	return nil
}
