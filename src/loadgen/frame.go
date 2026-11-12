package main

import (
	"encoding/binary"
	"errors"
	"io"
	"net"
)

// Frame types, docs/serve_protocol.md "Framing". Copied from
// src/adapters/serve_go/frame.go, which is a separate module.
const (
	TypeHello    byte = 0x01
	TypeInfo     byte = 0x02
	TypeQuery    byte = 0x03
	TypeAnswer   byte = 0x04
	TypeStatsReq byte = 0x05
	TypeStats    byte = 0x06
	TypeError    byte = 0x07
	TypeExpect   byte = 0x10
)

const MaxFrame = 1 << 30

var (
	ErrFrameTooLarge = errors.New("frame longer than 1 GiB")
	ErrEmptyFrame    = errors.New("frame length 0 has no type byte")
)

// WriteFrame writes one frame whose body is the concatenation of parts.
func WriteFrame(w io.Writer, typ byte, parts ...[]byte) error {
	n := 1
	for _, p := range parts {
		n += len(p)
	}
	if n > MaxFrame {
		return ErrFrameTooLarge
	}
	var hdr [5]byte
	binary.BigEndian.PutUint32(hdr[:4], uint32(n))
	hdr[4] = typ
	bufs := net.Buffers{hdr[:]}
	for _, p := range parts {
		if len(p) > 0 {
			bufs = append(bufs, p)
		}
	}
	_, err := bufs.WriteTo(w)
	return err
}

// ReadFrame reads one frame. It rejects an oversized length before allocating.
func ReadFrame(r io.Reader) (typ byte, body []byte, err error) {
	var lb [4]byte
	if _, err = io.ReadFull(r, lb[:]); err != nil {
		return
	}
	n := binary.BigEndian.Uint32(lb[:])
	switch {
	case n == 0:
		return 0, nil, ErrEmptyFrame
	case n > MaxFrame:
		return 0, nil, ErrFrameTooLarge
	}
	buf := make([]byte, n)
	if _, err = io.ReadFull(r, buf); err != nil {
		if err == io.EOF {
			err = io.ErrUnexpectedEOF
		}
		return
	}
	return buf[0], buf[1:], nil
}

func u64(v uint64) []byte {
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], v)
	return b[:]
}
