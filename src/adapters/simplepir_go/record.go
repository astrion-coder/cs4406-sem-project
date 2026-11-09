package main

// Record is one JSON line, exactly the fields of src/adapters/README.md.
type Record struct {
	RunID         *string    `json:"run_id"`
	GitSHAHarness *string    `json:"git_sha_harness"`
	Platform      *string    `json:"platform"`
	UpstreamSHA   string     `json:"upstream_sha"`
	Adapter       string     `json:"adapter"`
	Arm           string     `json:"arm"`
	SchemeParams  any        `json:"scheme_params"`
	DBBytes       int64      `json:"db_bytes"`
	RecordBytes   int64      `json:"record_bytes"`
	RetrievedBits int64      `json:"retrieved_bits"`
	NRecords      uint64     `json:"n_records"`
	Phase         string     `json:"phase"`
	Rep           int        `json:"rep"`
	IsWarmup      bool       `json:"is_warmup"`
	Seed          int64      `json:"seed"`
	Fault         string     `json:"fault"`
	FaultProb     float64    `json:"fault_prob"`
	Faulted       bool       `json:"faulted"`
	Recovery      string     `json:"recovery"`
	Status        string     `json:"status"`
	Metrics       Metrics    `json:"metrics"`
	Analytical    Analytical `json:"analytical"`
	Correct       *bool      `json:"correct"`
	Detected      *bool      `json:"detected"`
	Error         *string    `json:"error"`
}

// Metrics carries every metric key of the contract. A key that does not
// apply (the other phase, or no verification in this arm) stays nil and
// marshals as null.
type Metrics struct {
	PublicParamsS            *float64 `json:"public_params_s"`
	PublicParamsCPUS         *float64 `json:"public_params_cpu_s"`
	ServerPreprocS           *float64 `json:"server_preproc_s"`
	ServerPreprocCPUS        *float64 `json:"server_preproc_cpu_s"`
	ServerPreprocProofS      *float64 `json:"server_preproc_proof_s"`
	ServerPreprocProofCPUS   *float64 `json:"server_preproc_proof_cpu_s"`
	ClientOfflineS           *float64 `json:"client_offline_s"`
	ClientOfflineCPUS        *float64 `json:"client_offline_cpu_s"`
	OfflineDownBytes         *int64   `json:"offline_down_bytes"`
	OfflineDownBytesHintOnly *int64   `json:"offline_down_bytes_hint_only"`
	OfflineUpBytes           *int64   `json:"offline_up_bytes"`
	ClientStorageBytes       *int64   `json:"client_storage_bytes"`

	QueryGenS          *float64  `json:"query_gen_s"`
	QueryGenCPUS       *float64  `json:"query_gen_cpu_s"`
	UpBytes            *int64    `json:"up_bytes"`
	UpBytesPerServer   []int64   `json:"up_bytes_per_server"`
	ServerAnswerS      []float64 `json:"server_answer_s"`
	ServerAnswerCPUS   []float64 `json:"server_answer_cpu_s"`
	DownBytes          *int64    `json:"down_bytes"`
	DownBytesPerServer []int64   `json:"down_bytes_per_server"`
	ClientRecoverS     *float64  `json:"client_recover_s"`
	ClientRecoverCPUS  *float64  `json:"client_recover_cpu_s"`
	ClientVerifyS      *float64  `json:"client_verify_s"`
	ClientVerifyCPUS   *float64  `json:"client_verify_cpu_s"`
	VerifyFused        *bool     `json:"verify_fused"`

	ProcessPeakRSSBytes int64 `json:"process_peak_rss_bytes"`
}

// Analytical holds upstream's size formulas (pir/simple_pir.go GetBW), all
// three in every record.
type Analytical struct {
	UpBytes                  *int64 `json:"up_bytes"`
	DownBytes                *int64 `json:"down_bytes"`
	OfflineDownBytesHintOnly *int64 `json:"offline_down_bytes_hint_only"`
}

// SchemeParams is what the library actually chose, plus our layout.
type SchemeParams struct {
	N               uint64  `json:"n"`
	LogQ            uint64  `json:"logq"`
	P               uint64  `json:"p"`
	Sigma           float64 `json:"sigma"`
	Ell             uint64  `json:"ell"`
	M               uint64  `json:"m"`
	EntryBits       uint64  `json:"entry_bits"`
	Ne              uint64  `json:"ne"`
	Packing         uint64  `json:"packing"`
	PiecesPerRecord int     `json:"pieces_per_record"`
	RecordRows      uint64  `json:"record_rows"`
	Entries         uint64  `json:"entries"`
	Basis           uint64  `json:"basis"`
	Squishing       uint64  `json:"squishing"`
	CompressedA     bool    `json:"compressed_a"`
	Parallelism     string  `json:"parallelism"`
}

func f64(x float64) *float64 { return &x }
func i64(x int64) *int64     { return &x }
func bptr(x bool) *bool      { return &x }
func sptr(x string) *string  { return &x }
