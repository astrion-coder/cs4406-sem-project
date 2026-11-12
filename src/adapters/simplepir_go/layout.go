package main

import (
	"fmt"
	"math"

	"github.com/ahenzinger/simplepir/pir"
)

const (
	secParam = uint64(1 << 10) // LWE dimension n, as pir_test.go
	logQ     = uint64(32)      // ciphertext modulus, as pir_test.go
	maxBits  = 64              // Recover returns one uint64
)

// Layout maps a record index to the database entries that hold it. A record
// is Pieces entries stacked in one column, so one query (one column) and one
// Recover call per piece retrieve it.
type Layout struct {
	RecordBytes int
	Records     uint64
	Pieces      int    // entries per record
	EntryBits   uint64 // bits per entry
	Params      pir.Params
	Ne          uint64 // Z_p elements per entry
	Packing     uint64
	RecordRows  uint64 // record rows, each Pieces entry rows tall
	Entries     uint64 // padded entry count handed to MakeDB
	OneBit      bool   // --record-bytes 0: one-bit entries, Packing to a Z_p element
}

// Entry is the database entry holding piece k of record r.
func (l Layout) Entry(r uint64, k int) uint64 {
	return (r/l.Params.M*uint64(l.Pieces)+uint64(k))*l.Params.M + r%l.Params.M
}

// element is the index of the Z_p element (column element%M, row element/M)
// that holds entry e. MakeDB and GetElem put Packing consecutive entries in one
// element. Upstream Query and Recover address entry e as element e, which is
// right only when Packing <= 1, so the adapter passes element(e) to Query and
// decodes row element(e)/M itself.
func (l Layout) element(e uint64) uint64 {
	if l.Packing > 1 {
		return e / l.Packing
	}
	return e
}

// Analytical sizes, the arithmetic of SimplePIR.GetBW (which only prints).
func (l Layout) hintBytes() int64 { return int64(l.Params.L * l.Params.N * l.Params.Logq / 8) }
func (l Layout) aBytes() int64    { return int64(l.Params.M * l.Params.N * l.Params.Logq / 8) }
func (l Layout) upBytes() int64   { return int64(l.Params.M * l.Params.Logq / 8) }
func (l Layout) downBytes() int64 { return int64(l.Params.L * l.Params.Logq / 8) }

// plan is the one place that converts --db-bytes and --record-bytes into
// SimplePIR's units. Entries are 64 bits wide (the width Recover can return)
// unless the record is shorter, then one entry holds the whole record.
// Dimensions come from the library: the plaintext modulus p comes from
// PickParams and PickParamsGivenDimensions, and the shape is the
// near-square one for which every record sits in a single column.
func plan(pi *pir.SimplePIR, dbBytes, recordBytes int64) (Layout, error) {
	if recordBytes == 0 {
		return planOneBit(pi, dbBytes), nil
	}
	n := uint64(dbBytes / recordBytes)
	if n == 0 {
		return Layout{}, fmt.Errorf("record larger than database")
	}
	pieces := int((recordBytes + 7) / 8)
	d := uint64(maxBits)
	if recordBytes < 8 {
		d = uint64(8 * recordBytes)
	}

	p := pi.PickParams(n*uint64(pieces), d, secParam, logQ)
	for iter := 0; iter < 8; iter++ {
		_, ne, packing := pir.Num_DB_entries(1, d, p.P)
		if packing > 1 {
			return Layout{}, fmt.Errorf("entry packing %d not supported", packing)
		}
		unit := uint64(pieces) * ne
		g0 := math.Sqrt(float64(n) / float64(unit))
		best, bestCost := uint64(0), uint64(math.MaxUint64)
		for _, g := range []uint64{1, uint64(g0), uint64(g0) + 1} {
			if g == 0 || g > n {
				continue
			}
			if cost := g*unit + (n+g-1)/g; cost < bestCost {
				best, bestCost = g, cost
			}
		}
		cols := (n + best - 1) / best
		q := pi.PickParamsGivenDimensions(best*unit, cols, secParam, logQ)
		if q.P != p.P {
			p = q
			continue
		}
		return Layout{
			RecordBytes: int(recordBytes),
			Records:     n,
			Pieces:      pieces,
			EntryBits:   d,
			Params:      q,
			Ne:          ne,
			Packing:     packing,
			RecordRows:  best,
			Entries:     best * cols * uint64(pieces),
		}, nil
	}
	return Layout{}, fmt.Errorf("layout did not converge on a plaintext modulus")
}

// planOneBit is the SimplePIR README setup, the RQ1 anchor (decision 21):
// 8 × db_bytes one-bit entries in the library's own near-square layout
// (PickParams), several entries to a Z_p element.
func planOneBit(pi *pir.SimplePIR, dbBytes int64) Layout {
	n := uint64(8 * dbBytes)
	p := pi.PickParams(n, 1, secParam, logQ)
	_, ne, packing := pir.Num_DB_entries(n, 1, p.P)
	return Layout{Records: n, Pieces: 1, EntryBits: 1, Params: p, Ne: ne, Packing: packing,
		Entries: n, OneBit: true}
}

// retrievedBits and nRecords read --record-bytes 0 as one-bit entries.
func retrievedBits(recordBytes int64) int64 {
	if recordBytes == 0 {
		return 1
	}
	return 8 * recordBytes
}

func recordBytesField(recordBytes int64) *int64 {
	if recordBytes == 0 {
		return nil
	}
	return &recordBytes
}

func nRecords(dbBytes, recordBytes int64) uint64 {
	if recordBytes == 0 {
		return uint64(8 * dbBytes)
	}
	return uint64(dbBytes / recordBytes)
}
