package main

import (
	"encoding/binary"
	"unsafe"

	"github.com/ahenzinger/simplepir/pir"
)

// The wire encoding of a matrix is its elements as little-endian uint32,
// row-major, with no header (the receiver knows the dimensions). Its length
// is Rows*Cols*Logq/8 for Logq = 32.

func words(m *pir.Matrix) []uint32 {
	return unsafe.Slice((*uint32)(unsafe.Pointer(&m.Data[0])), len(m.Data))
}

func encode(m *pir.Matrix) []byte {
	w := words(m)
	b := make([]byte, 4*len(w))
	for i, x := range w {
		binary.LittleEndian.PutUint32(b[4*i:], x)
	}
	return b
}

func decode(b []byte, rows, cols uint64) *pir.Matrix {
	if uint64(len(b)) != 4*rows*cols {
		panic("wire: length does not match dimensions")
	}
	m := pir.MatrixNew(rows, cols)
	w := words(m)
	for i := range w {
		w[i] = binary.LittleEndian.Uint32(b[4*i:])
	}
	return m
}

func flipBytesBit(b []byte, bit uint64) { b[bit/8] ^= 1 << (bit % 8) }

// flipMatrixBit flips the bit that flipBytesBit would flip in encode(m).
func flipMatrixBit(m *pir.Matrix, bit uint64) { words(m)[bit/32] ^= 1 << (bit % 32) }
