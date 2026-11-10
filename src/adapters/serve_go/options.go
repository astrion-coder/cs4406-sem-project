package serve

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
)

// Options is the command line of --mode serve and --mode client.
type Options struct {
	Mode           string
	Arm            string
	DBBytes        int64
	RecordBytes    int64
	Seed           uint64
	Workers        int
	Listen         string
	ServerID       int // -1 when --server-id is absent
	Out            string
	Connect        []string
	SessionQueries int
	CorpusOut      string
}

// UsageError is a command-line mistake, exit code 2.
type UsageError struct{ Msg string }

func (e UsageError) Error() string { return e.Msg }

func Usagef(format string, a ...any) error { return UsageError{fmt.Sprintf(format, a...)} }

// ModeOf returns the value of --mode in args, "bench" when it is absent.
func ModeOf(args []string) string {
	for i, a := range args {
		switch {
		case a == "--mode" || a == "-mode":
			if i+1 < len(args) {
				return args[i+1]
			}
		case strings.HasPrefix(a, "--mode="):
			return strings.TrimPrefix(a, "--mode=")
		case strings.HasPrefix(a, "-mode="):
			return strings.TrimPrefix(a, "-mode=")
		}
	}
	return "bench"
}

// ParseOptions parses the serve or client command line. --fault, in any form,
// is a usage error, as is every flag that belongs to bench mode.
func ParseOptions(args []string) (Options, error) {
	o := Options{ServerID: -1}
	fs := flag.NewFlagSet("adapter", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var connect string
	fs.StringVar(&o.Mode, "mode", "", "serve or client")
	fs.StringVar(&o.Arm, "arm", "", "arm")
	fs.Int64Var(&o.DBBytes, "db-bytes", 0, "database size in bytes")
	fs.Int64Var(&o.RecordBytes, "record-bytes", 0, "record size in bytes")
	fs.Uint64Var(&o.Seed, "seed", 0, "seed")
	fs.IntVar(&o.Workers, "workers", 0, "worker threads (serve)")
	fs.StringVar(&o.Listen, "listen", "", "HOST:PORT (serve)")
	fs.IntVar(&o.ServerID, "server-id", -1, "0 or 1 (serve)")
	fs.StringVar(&o.Out, "out", "", "JSONL path")
	fs.StringVar(&connect, "connect", "", "HOST:PORT[,HOST:PORT] (client)")
	fs.IntVar(&o.SessionQueries, "session-queries", 0, "queries to run (client)")
	fs.StringVar(&o.CorpusOut, "corpus-out", "", "corpus path (client)")
	fault := fs.String("fault", "", "")
	if err := fs.Parse(args); err != nil {
		return o, Usagef("%v", err)
	}
	if fs.NArg() > 0 {
		return o, Usagef("unexpected arguments: %v", fs.Args())
	}
	set := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { set[f.Name] = true })
	if set["fault"] {
		return o, Usagef("--fault %q is not available in --mode %s, fault runs stay in bench mode", *fault, o.Mode)
	}
	need := func(names ...string) error {
		for _, n := range names {
			if !set[n] {
				return Usagef("missing --%s", n)
			}
		}
		return nil
	}
	forbid := func(names ...string) error {
		for _, n := range names {
			if set[n] {
				return Usagef("--%s is not used in --mode %s", n, o.Mode)
			}
		}
		return nil
	}
	var err error
	switch o.Mode {
	case "serve":
		if err = need("arm", "db-bytes", "record-bytes", "seed", "workers", "listen"); err == nil {
			err = forbid("connect", "session-queries", "corpus-out")
		}
		if err == nil && o.Workers < 1 {
			err = Usagef("--workers must be at least 1, got %d", o.Workers)
		}
		if err == nil && set["server-id"] && (o.ServerID < 0 || o.ServerID > 1) {
			err = Usagef("--server-id must be 0 or 1, got %d", o.ServerID)
		}
	case "client":
		if err = need("arm", "db-bytes", "record-bytes", "seed", "connect", "session-queries", "out"); err == nil {
			err = forbid("workers", "listen", "server-id")
		}
		if err == nil {
			o.Connect = strings.Split(connect, ",")
			for _, c := range o.Connect {
				if c == "" {
					err = Usagef("--connect has an empty address")
				}
			}
		}
		if err == nil && o.SessionQueries < 1 {
			err = Usagef("--session-queries must be at least 1, got %d", o.SessionQueries)
		}
	default:
		err = Usagef("--mode must be serve or client, got %q", o.Mode)
	}
	if err == nil && o.RecordBytes < 0 {
		err = Usagef("--record-bytes must not be negative")
	}
	return o, err
}

// Unsupported is a constructor's report that the library rejects these
// parameters. Main then writes no ready line, appends the records to --out
// (a client gives one bench-shaped record per query, a server none) and exits
// 0, as bench mode does.
type Unsupported struct {
	Msg     string
	Records []any
}

func (u Unsupported) Error() string { return u.Msg }

// Arm is what an adapter supplies. Either constructor may return a
// UsageError for options only this arm can judge.
type Arm interface {
	NewServer(o Options) (Server, error)
	NewClient(o Options) (Client, error)
}

// Main runs the serve or client mode named in args and returns the exit code.
func Main(args []string, a Arm) int {
	o, err := ParseOptions(args)
	if err == nil {
		switch o.Mode {
		case "serve":
			var s Server
			if s, err = a.NewServer(o); err == nil {
				err = RunServer(o, s)
			}
		case "client":
			var c Client
			if c, err = a.NewClient(o); err == nil {
				err = RunClient(o, c)
			}
		}
	}
	if err == nil {
		return 0
	}
	var un Unsupported
	if errors.As(err, &un) {
		fmt.Fprintln(os.Stderr, "unsupported parameters:", un.Msg)
		if err := writeUnsupported(o, un); err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return 1
		}
		return 0
	}
	var ue UsageError
	if errors.As(err, &ue) {
		fmt.Fprintln(os.Stderr, "usage error:", ue.Msg)
		return 2
	}
	fmt.Fprintln(os.Stderr, "error:", err)
	return 1
}
