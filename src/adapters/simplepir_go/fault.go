package main

import (
	"math"

	"github.com/ahenzinger/simplepir/pir"
)

// A record's decoding reads a fixed span of rows of the answer and of the
// hint, and within each 32-bit element only the bits at or above log2(q/p).
// Anything else is dropped by Recover's rounding or never read, so a fault
// there demonstrates nothing. These helpers pick a bit that matters.

// decodeRows is the span of answer and hint rows that Recover reads for
// record r, which is its Pieces entry rows of Ne Z_p elements each.
func (l Layout) decodeRows(r uint64) (first, count uint64) {
	return r / l.Params.M * uint64(l.Pieces) * l.Ne, uint64(l.Pieces) * l.Ne
}

// minFlipBit is ceil(log2(q/p)), the lowest bit that survives rounding.
func (l Layout) minFlipBit() uint64 {
	return uint64(math.Ceil(math.Log2(float64(uint64(1)<<l.Params.Logq) / float64(l.Params.P))))
}

// answerFaultBit picks a uniform bit among the answer bits the output for
// record r depends on, as an index into the serialised answer.
func (s *session) answerFaultBit(r uint64) uint64 {
	first, count := s.lay.decodeRows(r)
	lo, q := s.lay.minFlipBit(), s.p.Logq
	row := first + s.faultOn.below(count)
	return row*q + lo + s.faultOn.below(q-lo)
}

// hintFaultBit picks a uniform bit among the hint bits the output for
// record r depends on, as an index into the serialised hint. Flipping bit b
// of H[j][c] shifts the client's H*secret term by 2^b * secret[c] mod q, so
// the bit matters only if that shift is at least Delta = q/p away from zero.
// The adapter knows the client secret in bench mode, so it rejects the
// others, about one draw in nine at p = 991.
func (s *session) hintFaultBit(r uint64, secret *pir.Matrix) uint64 {
	first, count := s.lay.decodeRows(r)
	lo, q, n := s.lay.minFlipBit(), s.p.Logq, s.p.N
	mask := uint64(1)<<q - 1
	sec := words(secret)
	delta := s.p.Delta()
	for {
		row := first + s.faultOn.below(count)
		col := s.faultOn.below(n)
		b := lo + s.faultOn.below(q-lo)
		shift := (uint64(sec[col]) << b) & mask
		if min(shift, mask+1-shift) >= delta {
			return (row*n+col)*q + b
		}
	}
}
