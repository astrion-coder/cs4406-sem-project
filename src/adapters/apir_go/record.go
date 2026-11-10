package main

// Record is one line of --out. Its fields are exactly the adapter contract's
// (src/adapters/README.md). Pointer and slice fields marshal to null when unset.
type Record struct {
	RunID            *string        `json:"run_id"`
	GitSHAHarness    *string        `json:"git_sha_harness"`
	Platform         *string        `json:"platform"`
	UpstreamSHA      string         `json:"upstream_sha"`
	Adapter          string         `json:"adapter"`
	Arm              string         `json:"arm"`
	SchemeParams     map[string]any `json:"scheme_params"`
	DBBytes          int            `json:"db_bytes"`
	RecordBytes      *int           `json:"record_bytes"`
	RetrievedBits    int            `json:"retrieved_bits"`
	NRecords         int            `json:"n_records"`
	Phase            string         `json:"phase"`
	Rep              int            `json:"rep"`
	IsWarmup         bool           `json:"is_warmup"`
	Seed             uint64         `json:"seed"`
	Fault            string         `json:"fault"`
	FaultProb        float64        `json:"fault_prob"`
	Faulted          bool           `json:"faulted"`
	Recovery         string         `json:"recovery"`
	RecoveryOutcome  *string        `json:"recovery_outcome"`
	RecoveryAttempts *int           `json:"recovery_attempts"`
	Status           string         `json:"status"`
	Metrics          Metrics        `json:"metrics"`
	Analytical       Analytical     `json:"analytical"`
	Correct          *bool          `json:"correct"`
	Detected         *bool          `json:"detected"`
	Error            *string        `json:"error"`
}

// Metrics carries every metric key in every record. Keys of the other phase,
// and keys that do not apply to the arm, are null.
type Metrics struct {
	PublicParamsS            *float64  `json:"public_params_s"`
	PublicParamsCPUS         *float64  `json:"public_params_cpu_s"`
	ServerPreprocS           *float64  `json:"server_preproc_s"`
	ServerPreprocCPUS        *float64  `json:"server_preproc_cpu_s"`
	ServerPreprocProofS      *float64  `json:"server_preproc_proof_s"`
	ServerPreprocProofCPUS   *float64  `json:"server_preproc_proof_cpu_s"`
	ClientOfflineS           *float64  `json:"client_offline_s"`
	ClientOfflineCPUS        *float64  `json:"client_offline_cpu_s"`
	OfflineDownBytes         *int      `json:"offline_down_bytes"`
	OfflineDownBytesHintOnly *int      `json:"offline_down_bytes_hint_only"`
	OfflineUpBytes           *int      `json:"offline_up_bytes"`
	ClientStorageBytes       *int      `json:"client_storage_bytes"`
	QueryGenS                *float64  `json:"query_gen_s"`
	QueryGenCPUS             *float64  `json:"query_gen_cpu_s"`
	UpBytes                  *int      `json:"up_bytes"`
	UpBytesPerServer         []int     `json:"up_bytes_per_server"`
	ServerAnswerS            []float64 `json:"server_answer_s"`
	ServerAnswerCPUS         []float64 `json:"server_answer_cpu_s"`
	DownBytes                *int      `json:"down_bytes"`
	DownBytesPerServer       []int     `json:"down_bytes_per_server"`
	ClientRecoverS           *float64  `json:"client_recover_s"`
	ClientRecoverCPUS        *float64  `json:"client_recover_cpu_s"`
	ClientVerifyS            *float64  `json:"client_verify_s"`
	ClientVerifyCPUS         *float64  `json:"client_verify_cpu_s"`
	VerifyFused              *bool     `json:"verify_fused"`
	RecoveryS                *float64  `json:"recovery_s"`
	RecoveryCPUS             *float64  `json:"recovery_cpu_s"`
	RecoveryUpBytes          *int      `json:"recovery_up_bytes"`
	RecoveryDownBytes        *int      `json:"recovery_down_bytes"`
	ProcessPeakRSSBytes      *int64    `json:"process_peak_rss_bytes"`
}

// Analytical holds upstream's own size accounting for the same messages,
// null where upstream has none.
type Analytical struct {
	UpBytes                  *int `json:"up_bytes"`
	DownBytes                *int `json:"down_bytes"`
	OfflineDownBytesHintOnly *int `json:"offline_down_bytes_hint_only"`
}

func ptr[T any](v T) *T { return &v }
