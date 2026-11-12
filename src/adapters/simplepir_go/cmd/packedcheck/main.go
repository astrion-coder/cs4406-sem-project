// Command packedcheck runs upstream SimplePIR's own call sequence (real Init,
// Setup, Query, Answer, Recover, as RunPIR in pir.go) on a one-bit database,
// whose entries the library packs several to a Z_p element, and counts how many
// queried entries come back right, come back wrong, or panic, against GetElem.
// The same run on 8-bit entries, which do not pack, is the control. The
// database and the indices are seeded, so the counts repeat.
//
//	cd src/adapters/simplepir_go && go run ./cmd/packedcheck
package main

import (
	"fmt"
	"math/rand"

	"github.com/ahenzinger/simplepir/pir"
)

const (
	entries = 1 << 16
	queries = 200
)

func try(f func() uint64) (v uint64, panicked bool) {
	defer func() {
		if recover() != nil {
			panicked = true
		}
	}()
	return f(), false
}

func main() {
	for _, d := range []uint64{1, 8} {
		pi := pir.SimplePIR{}
		p := pi.PickParams(entries, d, 1024, 32)
		rng := rand.New(rand.NewSource(1))
		vals := make([]uint64, entries)
		for i := range vals {
			vals[i] = rng.Uint64() % (1 << d)
		}
		db := pir.MakeDB(entries, d, &p, vals)
		idx := []uint64{0, 1, 2}
		for len(idx) < queries {
			idx = append(idx, rng.Uint64()%entries)
		}
		shared := pi.Init(db.Info, p)
		server, hint := pi.Setup(db, shared, p)
		right, wrong, panics := 0, 0, 0
		for _, i := range idx {
			got, pan := try(func() uint64 {
				cs, q := pi.Query(i, shared, p, db.Info)
				ans := pi.Answer(db, pir.MakeMsgSlice(q), server, shared, p)
				return pi.Recover(i, 0, hint, q, ans, shared, cs, p, db.Info)
			})
			switch {
			case pan:
				panics++
			case got == vals[i]:
				right++
			default:
				wrong++
			}
		}
		fmt.Printf("entry_bits=%d p=%d packing=%d of %d queries: right=%d wrong=%d panic=%d\n",
			d, p.P, db.Info.Packing, len(idx), right, wrong, panics)
	}
}
