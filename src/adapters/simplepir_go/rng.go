package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha256"
	"encoding/binary"
)

// stream is an AES-128 counter-mode keystream keyed by (domain, seed). It is
// random access, so a record's bytes can be regenerated from its index.
type stream struct {
	block cipher.Block
	next  uint64
}

func newStream(domain string, seed int64) *stream {
	var s [8]byte
	binary.LittleEndian.PutUint64(s[:], uint64(seed))
	h := sha256.Sum256(append([]byte("vpir-bench/"+domain+"/"), s[:]...))
	b, err := aes.NewCipher(h[:16])
	if err != nil {
		panic(err)
	}
	return &stream{block: b}
}

// fill writes keystream bytes [off, off+len(dst)).
func (s *stream) fill(dst []byte, off uint64) {
	var ctr, out [16]byte
	for len(dst) > 0 {
		binary.LittleEndian.PutUint64(ctr[:8], off/16)
		s.block.Encrypt(out[:], ctr[:])
		n := copy(dst, out[off%16:])
		dst, off = dst[n:], off+uint64(n)
	}
}

func (s *stream) uint64() uint64 {
	var b [8]byte
	s.fill(b[:], s.next*8)
	s.next++
	return binary.LittleEndian.Uint64(b[:])
}

func (s *stream) below(n uint64) uint64 { return s.uint64() % n }

func (s *stream) float() float64 { return float64(s.uint64()>>11) / (1 << 53) }

// pieces splits a record into 64-bit little-endian entry values, zero padded.
func pieces(rec []byte, count int) []uint64 {
	out := make([]uint64, count)
	var b [8]byte
	for k := range out {
		b = [8]byte{}
		copy(b[:], rec[min(len(rec), 8*k):min(len(rec), 8*k+8)])
		out[k] = binary.LittleEndian.Uint64(b[:])
	}
	return out
}
