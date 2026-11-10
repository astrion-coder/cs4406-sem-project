package main

import (
	"fmt"
	"math"
)

// arm is one registry row: what the command line accepts for the arm and how
// to build its scheme. Everything arm-specific lives behind build.
type arm struct {
	servers int
	// oneBit arms (S3, S4) retrieve one bit per query and require --record-bytes 0.
	oneBit bool
	// verifiable arms report detected as a boolean. In this library every one
	// of them verifies inside reconstruction, so verify_fused equals verifiable.
	verifiable bool
	faults     map[string]bool
	// build lays out the seeded database and constructs the scheme. An
	// unsupportedError becomes status="unsupported_params".
	build func(g geometry, seed uint64) (cell, error)
}

var onlineFaults = map[string]bool{"none": true, "flip_answer_bit": true, "wrong_record": true}

var arms = map[string]arm{
	"S3": {servers: 1, oneBit: true, verifiable: true, faults: onlineFaults, build: newLWEPlusCell},
	"S4": {servers: 1, oneBit: true, verifiable: true, faults: onlineFaults, build: newDDHCell},
	"M0": {servers: 2, verifiable: false, faults: onlineFaults, build: func(g geometry, seed uint64) (cell, error) { return newXORCell(g, seed, false), nil }},
	"M1": {servers: 2, verifiable: true, faults: onlineFaults, build: func(g geometry, seed uint64) (cell, error) { return newXORCell(g, seed, true), nil }},
}

// unsupportedError is a parameter combination the scheme rejects.
type unsupportedError struct{ msg string }

func (e unsupportedError) Error() string { return e.msg }

// cell is one (arm, database) instance. Methods that wrap a scheme call time
// only that call; serialisation and deserialisation stay outside the span.
type cell interface {
	params() map[string]any
	analytical() Analytical
	// setup runs the offline phase from the same database: server
	// preprocessing, then client construction.
	setup() offlineCost
	// query returns one message per server.
	query(index int) ([][]byte, span)
	answer(server int, q []byte) ([]byte, span)
	// reconstruct fills sp even when the scheme call panics.
	reconstruct(answers [][]byte, sp *span) ([]byte, error)
	// shadowAnswer is the given server's honest answer to a query for index
	// made by a second client, so the main client's state is untouched.
	shadowAnswer(index, server int) []byte
	// flipBits are the bits of one server's answer that the honest client's
	// output for the record at index depends on (contract fault rule).
	flipBits(index int) []bitRange
	truth(index int) []byte
}

// bitRange is n consecutive bits starting at bit first of a message; bit b
// is mask 1<<(b%8) of byte b/8.
type bitRange struct{ first, n int }

func bytesRange(off, n int) bitRange { return bitRange{first: 8 * off, n: 8 * n} }

type offlineCost struct {
	serverPreproc, clientOffline                    span
	downBytes, downHintBytes, upBytes, storageBytes int
}

// geometry converts the contract's byte sizes to the library's units. It is
// the only place that conversion happens.
type geometry struct {
	dbBits        int // apir-code's database constructors take bits
	recordBytes   int // 0 for one-bit arms
	nRecords      int // bits for one-bit arms, blocks otherwise
	retrievedBits int
	// rows of the two-server block matrix. The upstream tests use a square
	// layout (pir_test.go, merkle_test.go); this takes the largest divisor of
	// the block count not above its square root, so no block is dropped.
	rows int
}

func geometryFor(a arm, dbBytes, recordBytes int) (geometry, error) {
	if dbBytes <= 0 {
		return geometry{}, fmt.Errorf("--db-bytes must be positive")
	}
	g := geometry{dbBits: 8 * dbBytes}
	if a.oneBit {
		if recordBytes != 0 {
			return geometry{}, fmt.Errorf("this arm retrieves one bit per query and requires --record-bytes 0 (plan decision 10)")
		}
		g.nRecords, g.retrievedBits = g.dbBits, 1
		return g, nil
	}
	if recordBytes <= 0 || dbBytes%recordBytes != 0 {
		return geometry{}, fmt.Errorf("--record-bytes must be positive and divide --db-bytes")
	}
	g.recordBytes, g.nRecords, g.retrievedBits = recordBytes, dbBytes/recordBytes, 8*recordBytes
	if g.nRecords < 2 {
		return geometry{}, fmt.Errorf("the database must hold at least two records")
	}
	g.rows = int(math.Sqrt(float64(g.nRecords)))
	for g.nRecords%g.rows != 0 {
		g.rows--
	}
	return g, nil
}
