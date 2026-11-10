package main

import (
	"fmt"

	"github.com/si-co/vpir-code/lib/client"
	"github.com/si-co/vpir-code/lib/database"
	"github.com/si-co/vpir-code/lib/matrix"
	"github.com/si-co/vpir-code/lib/server"
	"github.com/si-co/vpir-code/lib/utils"
)

// tECCByBits is the integrity-amplification parameter per database size in
// bits. Computed with the authors' scripts/integrity_amplification.py at the
// paper's 2^-64 integrity target (p.3845), with db_sizes extended to 2^27 and
// 2^30 (plan decision 9). simulations/simul.go defines only 2^13, 2^23, 2^33.
var tECCByBits = map[int]int{1 << 23: 4, 1 << 27: 5, 1 << 30: 6, 1 << 33: 7}

// lwePlusCell is S3: client.NewAmplify runs 2*tECC+1 independent LWE
// instances over one bit matrix and majority-decodes them.
type lwePlusCell struct {
	g          geometry
	seed       uint64
	tECC       int
	bits       []byte // the seeded database, bit i at bits[i/8]>>(i%8)
	db         *database.LWE
	p          *utils.ParamsLWE
	srv        *server.Amplify
	cli, other *client.Amplify
}

func newLWEPlusCell(g geometry, seed uint64) (cell, error) {
	t, ok := tECCByBits[g.dbBits]
	if !ok {
		return nil, unsupportedError{fmt.Sprintf("LWE+ has a tECC value only for 2^23, 2^27, 2^30 and 2^33 bits (1 MiB, 16 MiB, 128 MiB, 1 GiB); got %d bits", g.dbBits)}
	}
	rows, cols := database.CalculateNumRowsAndColumns(g.dbBits, true)
	c := &lwePlusCell{g: g, seed: seed, tECC: t, bits: streamBytes(seed, "db", rows*cols/8+1)}
	// Same layout as database.CreateRandomBinaryLWE, built here so the digest,
	// which that constructor computes inline, can be timed on its own in setup.
	m := matrix.NewBytes(rows, cols)
	for i := 0; i < m.Len(); i++ {
		m.SetData(i, (c.bits[i/8]>>(i%8))&1)
	}
	c.db = &database.LWE{Matrix: m, Info: database.Info{NumRows: rows, NumColumns: cols, BlockSize: 1}}
	c.p = utils.ParamsWithDatabaseSize(rows, cols)
	return c, nil
}

func (c *lwePlusCell) params() map[string]any {
	return map[string]any{
		"scheme": "LWE+ (client.NewAmplify)", "tECC": c.tECC, "lwe_instances": 2*c.tECC + 1,
		"n": c.p.N, "logq": 8 * c.p.BytesMod, "p": c.p.P, "sigma": c.p.Sigma, "bound_B": c.p.B,
		"db_rows": c.p.L, "db_cols": c.p.M, "integrity_target_log2": -64,
	}
}

// analytical is upstream's accounting in simulations/simul.go (pirLWE):
// Matrix.BytesSize() = rows*cols*4 per matrix, without the encoding headers.
func (c *lwePlusCell) analytical() Analytical {
	inst := 2*c.tECC + 1
	return Analytical{
		UpBytes:                  ptr(inst * 4 * c.p.L),
		DownBytes:                ptr(inst * 4 * c.p.M),
		OfflineDownBytesHintOnly: ptr(4 * c.p.N * c.p.M),
	}
}

func (c *lwePlusCell) setup() offlineCost {
	var digest *matrix.Matrix
	pre := timed(func() { digest = database.Digest(c.db, c.p.L) })
	c.db.Auth = &database.Auth{DigestLWE: digest}
	c.srv = server.NewAmplify(c.db)
	cl := timed(func() { c.cli = client.NewAmplify(stream(c.seed, "client"), &c.db.Info, c.p, c.tECC) })
	c.other = client.NewAmplify(stream(c.seed, "shadow-client"), &c.db.Info, c.p, c.tECC)
	hint := len(matrix.MatrixToBytes(digest))
	// The client also needs the 16-byte seed of the public matrix A.
	full := hint + len(utils.SeedMatrixA)
	return offlineCost{serverPreproc: pre, clientOffline: cl, downBytes: full, downHintBytes: hint, storageBytes: full}
}

func (c *lwePlusCell) query(index int) ([][]byte, span) {
	i, j := utils.VectorToMatrixIndices(index, c.p.M)
	var qs []*matrix.Matrix
	sp := timed(func() { qs = c.cli.Query(i, j) })
	return [][]byte{matrix.MatricesToBytes(qs)}, sp
}

func (c *lwePlusCell) answer(_ int, q []byte) ([]byte, span) {
	qs := matrix.BytesToMatrices(q)
	var as []*matrix.Matrix
	sp := timed(func() { as = c.srv.Answer(qs) })
	return matrix.MatricesToBytes(as), sp
}

func (c *lwePlusCell) reconstruct(answers [][]byte, sp *span) ([]byte, error) {
	as := matrix.BytesToMatrices(answers[0])
	var bit uint32
	var err error
	timeInto(sp, func() { bit, err = c.cli.Reconstruct(as) })
	return []byte{byte(bit)}, err
}

func (c *lwePlusCell) shadowAnswer(index, _ int) []byte {
	i, j := utils.VectorToMatrixIndices(index, c.p.M)
	return matrix.MatricesToBytes(c.srv.Answer(c.other.Query(i, j)))
}

// flipBits is the bit at log2(q/p) = 31 of the queried column's element in
// each sub-answer; lower bits vanish below the decoding bound B. The
// MatricesToBytes layout is a 4-byte length, then per matrix an 8-byte
// rows/cols header and 4 big-endian bytes per element, so bit 31 is the top
// bit of the element's first byte.
func (c *lwePlusCell) flipBits(index int) []bitRange {
	j := index % c.p.M
	per := 8 + 4*c.p.M
	bits := make([]bitRange, 2*c.tECC+1)
	for s := range bits {
		bits[s] = bitRange{first: 8*(4+s*per+8+4*j) + 7, n: 1}
	}
	return bits
}

func (c *lwePlusCell) truth(index int) []byte {
	return []byte{(c.bits[index/8] >> (index % 8)) & 1}
}
