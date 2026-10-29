// dpfvectors checks that dpf-go evaluates identically on two architectures.
//
//	dpfvectors gen <file>    generate random DPF instances and record their outputs
//	dpfvectors check <file>  re-evaluate the recorded keys and compare
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"os"
	"runtime"

	"github.com/dkales/dpf-go/dpf"
)

var logNs = []uint64{1, 2, 5, 6, 7, 8, 9, 12, 16, 20, 24}

const instancesPerLogN = 6
const randomPoints = 12

type point struct {
	X  uint64 `json:"x"`
	B0 byte   `json:"b0"`
	B1 byte   `json:"b1"`
}

type vector struct {
	LogN   uint64  `json:"log_n"`
	Alpha  uint64  `json:"alpha"`
	K0     []byte  `json:"k0"`
	K1     []byte  `json:"k1"`
	Full0  []byte  `json:"full0_sha256"`
	Full1  []byte  `json:"full1_sha256"`
	Points []point `json:"points"`
}

type vectorFile struct {
	GOARCH  string   `json:"goarch"`
	Vectors []vector `json:"vectors"`
}

func randUint64() uint64 {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return binary.LittleEndian.Uint64(b[:])
}

func bit(buf []byte, x uint64) byte { return (buf[x/8] >> (x % 8)) & 1 }

// evaluate computes every output of one instance on this architecture and checks
// DPF semantics: the two shares XOR to the point function at alpha.
func evaluate(logN, alpha uint64, k0, k1 []byte, xs []uint64) (vector, error) {
	v := vector{LogN: logN, Alpha: alpha, K0: k0, K1: k1}
	f0, f1 := dpf.EvalFull(k0, logN), dpf.EvalFull(k1, logN)
	h0, h1 := sha256.Sum256(f0), sha256.Sum256(f1)
	v.Full0, v.Full1 = h0[:], h1[:]

	set := 0
	for i := range f0 {
		d := f0[i] ^ f1[i]
		for d != 0 {
			set++
			d &= d - 1
		}
	}
	if set != 1 || bit(f0, alpha)^bit(f1, alpha) != 1 {
		return v, fmt.Errorf("logN=%d alpha=%d: EvalFull(k0) XOR EvalFull(k1) has %d bits set, want only bit alpha", logN, alpha, set)
	}

	for _, x := range xs {
		p := point{X: x, B0: dpf.Eval(k0, x, logN), B1: dpf.Eval(k1, x, logN)}
		if p.B0 != bit(f0, x) || p.B1 != bit(f1, x) {
			return v, fmt.Errorf("logN=%d alpha=%d x=%d: Eval disagrees with EvalFull", logN, alpha, x)
		}
		if (x == alpha) != (p.B0^p.B1 == 1) {
			return v, fmt.Errorf("logN=%d alpha=%d x=%d: Eval shares XOR to %d", logN, alpha, x, p.B0^p.B1)
		}
		v.Points = append(v.Points, p)
	}
	return v, nil
}

func samplePoints(logN, alpha uint64) []uint64 {
	n := uint64(1) << logN
	xs := []uint64{alpha, 0, n - 1, alpha ^ 1}
	for i := 0; i < randomPoints; i++ {
		xs = append(xs, randUint64()&(n-1))
	}
	return xs
}

func gen(path string) error {
	out := vectorFile{GOARCH: runtime.GOARCH}
	for _, logN := range logNs {
		n := uint64(1) << logN
		for i := 0; i < instancesPerLogN; i++ {
			alpha := randUint64() & (n - 1)
			switch i {
			case 0:
				alpha = 0
			case 1:
				alpha = n - 1
			}
			k0, k1 := dpf.Gen(alpha, logN)
			v, err := evaluate(logN, alpha, k0, k1, samplePoints(logN, alpha))
			if err != nil {
				return err
			}
			out.Vectors = append(out.Vectors, v)
		}
	}
	data, err := json.Marshal(out)
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return err
	}
	fmt.Printf("gen: GOARCH=%s wrote %d vectors to %s\n", runtime.GOARCH, len(out.Vectors), path)
	return nil
}

func sameOutputs(a, b vector) bool {
	if !bytes.Equal(a.Full0, b.Full0) || !bytes.Equal(a.Full1, b.Full1) || len(a.Points) != len(b.Points) {
		return false
	}
	for i := range a.Points {
		if a.Points[i] != b.Points[i] {
			return false
		}
	}
	return true
}

func check(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var in vectorFile
	if err := json.Unmarshal(data, &in); err != nil {
		return err
	}
	if len(in.Vectors) == 0 {
		return fmt.Errorf("%s holds no vectors", path)
	}
	mismatches := 0
	for _, want := range in.Vectors {
		xs := make([]uint64, len(want.Points))
		for i, p := range want.Points {
			xs[i] = p.X
		}
		got, err := evaluate(want.LogN, want.Alpha, want.K0, want.K1, xs)
		if err != nil {
			return err
		}
		if !sameOutputs(got, want) {
			mismatches++
			fmt.Fprintf(os.Stderr, "mismatch: logN=%d alpha=%d\n", want.LogN, want.Alpha)
		}
	}
	if mismatches > 0 {
		return fmt.Errorf("%d of %d vectors from GOARCH=%s differ on GOARCH=%s", mismatches, len(in.Vectors), in.GOARCH, runtime.GOARCH)
	}
	fmt.Printf("check: GOARCH=%s matched all %d vectors from GOARCH=%s\n", runtime.GOARCH, len(in.Vectors), in.GOARCH)
	return nil
}

func main() {
	if len(os.Args) != 3 || (os.Args[1] != "gen" && os.Args[1] != "check") {
		fmt.Fprintln(os.Stderr, "usage: dpfvectors gen|check <file>")
		os.Exit(2)
	}
	run := gen
	if os.Args[1] == "check" {
		run = check
	}
	if err := run(os.Args[2]); err != nil {
		fmt.Fprintln(os.Stderr, "dpfvectors:", err)
		os.Exit(1)
	}
}
