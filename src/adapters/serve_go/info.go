package serve

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"runtime"
	"syscall"
	"time"
)

// Info is the INFO body. An arm fills every field but Protocol and Workers.
type Info struct {
	Protocol     int     `json:"protocol"`
	Arm          string  `json:"arm"`
	ServerID     int     `json:"server_id"`
	Servers      int     `json:"servers"`
	DBBytes      int64   `json:"db_bytes"`
	RecordBytes  *int64  `json:"record_bytes"`
	NRecords     uint64  `json:"n_records"`
	Seed         uint64  `json:"seed"`
	Workers      int     `json:"workers"`
	UpstreamSHA  string  `json:"upstream_sha"`
	DBSHA256     string  `json:"db_sha256"`
	PublicSeed   *string `json:"public_seed"`
	SchemeParams any     `json:"scheme_params"`
}

// Mismatch names the first fingerprint field on which a server's INFO differs
// from the client's own, or returns "".
func (own Info) Mismatch(server Info) string {
	ps := func(p *string) string {
		if p == nil {
			return "null"
		}
		return *p
	}
	pi := func(p *int64) string {
		if p == nil {
			return "null"
		}
		return fmt.Sprint(*p)
	}
	switch {
	case own.Arm != server.Arm:
		return fmt.Sprintf("arm: client %q, server %q", own.Arm, server.Arm)
	case own.DBBytes != server.DBBytes:
		return fmt.Sprintf("db_bytes: client %d, server %d", own.DBBytes, server.DBBytes)
	case pi(own.RecordBytes) != pi(server.RecordBytes):
		return fmt.Sprintf("record_bytes: client %s, server %s", pi(own.RecordBytes), pi(server.RecordBytes))
	case own.Seed != server.Seed:
		return fmt.Sprintf("seed: client %d, server %d", own.Seed, server.Seed)
	case own.DBSHA256 != server.DBSHA256:
		return fmt.Sprintf("db_sha256: client %s, server %s", own.DBSHA256, server.DBSHA256)
	case ps(own.PublicSeed) != ps(server.PublicSeed):
		return fmt.Sprintf("public_seed: client %s, server %s", ps(own.PublicSeed), ps(server.PublicSeed))
	}
	return ""
}

// Stats is the STATS body and the server's final counters.
type Stats struct {
	WallS        float64 `json:"wall_s"`
	CPUUserS     float64 `json:"cpu_user_s"`
	CPUSysS      float64 `json:"cpu_sys_s"`
	PeakRSSBytes int64   `json:"peak_rss_bytes"`
	Answered     uint64  `json:"answered"`
	Queued       int64   `json:"queued"`
}

// summaryRecord is the line appended to --out at shutdown: mode, the INFO
// fields but scheme_params, and the final STATS fields.
type summaryRecord struct {
	Mode        string  `json:"mode"`
	Protocol    int     `json:"protocol"`
	Arm         string  `json:"arm"`
	ServerID    int     `json:"server_id"`
	Servers     int     `json:"servers"`
	DBBytes     int64   `json:"db_bytes"`
	RecordBytes *int64  `json:"record_bytes"`
	NRecords    uint64  `json:"n_records"`
	Seed        uint64  `json:"seed"`
	Workers     int     `json:"workers"`
	UpstreamSHA string  `json:"upstream_sha"`
	DBSHA256    string  `json:"db_sha256"`
	PublicSeed  *string `json:"public_seed"`
	Stats
}

var processStart = time.Now()

func readStats(answered uint64, queued int64) Stats {
	var ru syscall.Rusage
	_ = syscall.Getrusage(syscall.RUSAGE_SELF, &ru)
	tv := func(t syscall.Timeval) float64 { return float64(t.Sec) + float64(t.Usec)*1e-6 }
	rss := int64(ru.Maxrss)
	if runtime.GOOS != "darwin" {
		rss *= 1024
	}
	return Stats{
		WallS: time.Since(processStart).Seconds(), CPUUserS: tv(ru.Utime), CPUSysS: tv(ru.Stime),
		PeakRSSBytes: rss, Answered: answered, Queued: queued,
	}
}

// PeakRSSBytes is the process's peak resident set size.
func PeakRSSBytes() int64 { return readStats(0, 0).PeakRSSBytes }

// HashHex returns the hex SHA-256 of everything the callback writes.
func HashHex(write func(w io.Writer)) string {
	h := sha256.New()
	write(h)
	return hex.EncodeToString(h.Sum(nil))
}

// Env returns the value of an environment variable, nil when unset, as the
// bench records' run_id, git_sha_harness and platform fields want it.
func Env(name string) *string {
	if v, ok := os.LookupEnv(name); ok {
		return &v
	}
	return nil
}
