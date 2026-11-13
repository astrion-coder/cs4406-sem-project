import copy
import json
from pathlib import Path

import pytest

from cs4406_sem_project.harness import validate
from cs4406_sem_project.harness.validate import check_row

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "raw"


def _is_record(row: dict) -> bool:
    """Notebook summaries share results/raw/ with the records and carry neither a run_id nor mode "serve"."""
    return "run_id" in row or row.get("mode") == "serve"


def _raw_rows() -> list[tuple[str, int, dict]]:
    """Every row of every record file, so one malformed row in a record file still fails the test."""
    out = []
    for path in sorted(RAW.rglob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if rows and _is_record(rows[0]):
            out += [(str(path.relative_to(ROOT)), n, r) for n, r in enumerate(rows, 1)]
    return out


RAW_ROWS = _raw_rows()


def _base(arm: str, phase: str, mode: str | None = None) -> dict:
    def find(pred):
        for _, _, r in RAW_ROWS:
            if r.get("arm") == arm and r.get("phase") == phase and r.get("status") == "ok" and pred(r):
                return copy.deepcopy(r)
        return None

    modes = [mode] if mode else ["bench", "client"]
    for m in modes:
        row = find(lambda r: r.get("mode", "bench") == m and r["fault"] == "none")
        if row:
            return row
    row = None if mode else find(lambda r: not r["faulted"] and r.get("detected") is not True)
    if row is None:
        pytest.skip(f"no raw honest {phase} row for {arm}")
    row["fault"], row["fault_prob"] = "none", 0
    return row


def _errors(row: dict, **kw) -> list[str]:
    return [p.field for p in check_row(row, **kw) if p.severity == "error"]


def test_raw_corpus_is_large_enough_to_mean_something():
    kinds = {validate.kind(r) for _, _, r in RAW_ROWS}
    arms = {r["arm"] for _, _, r in RAW_ROWS}
    assert {"adapter", "serve"} <= kinds
    assert arms == set(validate.ADAPTER_OF)


def test_every_raw_record_validates():
    bad = [(f, n, str(p)) for f, n, r in RAW_ROWS for p in check_row(r, line=n) if p.severity == "error"]
    assert bad == [], bad[:5]


@pytest.mark.parametrize("arm", sorted(validate.ADAPTER_OF))
@pytest.mark.parametrize("phase", ["offline", "online"])
def test_base_rows_are_clean(arm, phase):
    assert _errors(_base(arm, phase)) == []


def test_unknown_top_level_field_is_rejected():
    row = _base("S1", "online")
    row["server_answr_s"] = 1.0
    assert _errors(row) == ["<record>"]


def test_missing_metric_key_is_rejected():
    row = _base("M1", "online")
    del row["metrics"]["client_verify_s"]
    assert _errors(row) == ["metrics"]


def test_bench_row_may_not_carry_rtt():
    row = _base("S1", "online", mode="bench")
    row["metrics"]["rtt_s"] = [0.001]
    assert _errors(row)


def test_client_row_needs_rtt():
    row = _base("S1", "online", mode="client")
    assert _errors(row) == []
    del row["metrics"]["rtt_s"]
    assert _errors(row)


def test_unsupported_client_row_needs_no_rtt():
    row = _base("S1", "online", mode="client")
    del row["metrics"]["rtt_s"], row["metrics"]["rtt_cpu_s"]
    row.update(status="unsupported_params", error="no suitable params", correct=None)
    assert _errors(row) == []


def test_one_bit_arm_has_null_record_bytes():
    row = _base("S3", "online")
    row["record_bytes"] = 32
    assert _errors(row)


def test_online_metric_in_offline_row():
    row = _base("S0-go", "offline")
    row["metrics"]["up_bytes"] = 10
    assert "metrics/up_bytes" in _errors(row)


def test_per_server_lists_match_server_count():
    row = _base("M0", "online")
    row["metrics"]["down_bytes_per_server"] = row["metrics"]["down_bytes_per_server"][:1]
    assert "metrics/down_bytes_per_server" in _errors(row)


def test_totals_are_sums_of_per_server():
    row = _base("M1", "online")
    row["metrics"]["up_bytes"] += 1
    assert "metrics/up_bytes" in _errors(row)


@pytest.mark.parametrize("arm", ["M1", "S3", "S4"])
def test_fused_arms_report_no_separate_verify(arm):
    row = _base(arm, "online")
    row["metrics"]["client_verify_s"] = 0.01
    assert "metrics/client_verify_s" in _errors(row)


def test_baseline_has_no_detection():
    row = _base("S0-cpp", "online")
    row["detected"] = False
    assert "detected" in _errors(row)


def test_honest_reject_is_an_error():
    row = _base("S2", "online")
    row["detected"] = True
    row["correct"] = None
    assert "detected" in _errors(row)


def test_honest_wrong_record_is_an_error():
    row = _base("S0-go", "online")
    row["correct"] = False
    assert "correct" in _errors(row)


def test_wrong_adapter_for_arm():
    row = _base("S1", "online")
    row["adapter"] = "apir_go"
    assert "adapter" in _errors(row)


def test_record_counts_are_consistent():
    row = _base("S0-go", "online")
    row["n_records"] += 1
    assert "n_records" in _errors(row)


def test_fake_preprocessing_is_refused():
    row = _base("S1", "offline")
    row["fake_preproc"] = True
    assert "fake_preproc" in _errors(row)


def _one_bit(row: dict) -> dict:
    row["record_bytes"], row["retrieved_bits"], row["n_records"] = None, 1, 8 * row["db_bytes"]
    return row


def test_one_bit_rows_only_for_s0go_s3_s4():
    """Decision 21: S0-go retrieves one bit as the SimplePIR README anchor; no other record-size arm does."""
    assert _errors(_one_bit(_base("S0-go", "online"))) == []
    assert "record_bytes" in _errors(_one_bit(_base("S1", "online")))
    row = _one_bit(_base("S0-go", "online"))
    row["retrieved_bits"] = 8
    assert "retrieved_bits" in _errors(row)


def test_platform_mismatch_is_refused():
    row = _base("S1", "online")
    row["platform"] = "x86_64-linux-Something"
    assert _errors(row, reference_platform="arm64-darwin-Apple M1 Pro") == ["platform"]
    row["platform"] = "arm64-darwin-Apple M1 Pro"
    assert _errors(row, reference_platform="arm64-darwin-Apple M1 Pro") == []


def test_comm_disagreement_is_a_warning_not_an_error():
    row = _base("S1", "online")
    before = check_row(row)
    row["analytical"]["down_bytes"] = row["metrics"]["down_bytes"] * 1.5
    added = [p for p in check_row(row) if p not in before]
    assert [(p.field, p.severity) for p in added] == [("metrics/down_bytes", "warning")]


def test_padding_over_limit_warns():
    row = _base("S1", "offline")
    row["scheme_params"]["padding_factor"] = 1.5
    assert [p.field for p in check_row(row)] == ["scheme_params/padding_factor"]


def _failure_row(**over) -> dict:
    row = {
        "run_id": "r1", "git_sha_harness": "abc1234", "platform": "arm64-darwin-Apple M1 Pro",
        "adapter": "vsp_cpp", "arm": "S2", "cell_id": "S2-1073741824-32", "db_bytes": 1 << 30,
        "record_bytes": 32, "phase": "offline", "seed": 1, "fault": "none", "fault_prob": 0.0,
        "recovery": "abort", "reps": 3, "warmup": 1, "status": "oom", "error": "memory cap",
        "harness": {"exit_code": None, "wall_s": 12.0, "peak_footprint_bytes": 15 << 30,
                    "timeout_s": 5400.0, "mem_cap_bytes": 14 << 30, "stderr_tail": ""},
    }
    row.update(over)
    return row


def test_harness_failure_rows():
    assert _errors(_failure_row()) == []
    assert _errors(_failure_row(status="ok"))
    assert validate.kind(_failure_row()) == "failure"


def _loadgen_row(**over) -> dict:
    row = {
        "mode": "loadgen", "run_id": "r1", "git_sha_harness": None, "platform": "p", "arm": "S0-go",
        "db_bytes": 1 << 20, "record_bytes": 32, "seed": 1, "workers": 4, "loop": "closed", "clients": 8,
        "offered_load_qps": None, "run": 0, "warmup_s": 10.0, "window_s": 60.0, "sent": 100,
        "completed": 99, "mismatched": 0, "errors": 0, "throughput_qps": 1.65, "lat_p50_s": 0.1,
        "lat_p95_s": 0.2, "lat_p99_s": 0.3, "e2e_lat_p50_s": 0.11, "e2e_lat_p95_s": 0.21,
        "e2e_lat_p99_s": 0.31, "server_cpu_util": 380.0, "server_peak_rss_bytes": 1 << 28,
        "loadgen_cpu_util": 20.0, "status": "ok", "error": None,
    }
    row.update(over)
    return row


def test_loadgen_rows():
    assert _errors(_loadgen_row()) == []
    assert _errors(_loadgen_row(loop="open", clients=None, offered_load_qps=50.0)) == []
    assert _errors(_loadgen_row(loop="open", clients=None))
    assert _errors(_loadgen_row(offered_load_qps=50.0))


def test_cli_exit_codes(tmp_path):
    good = tmp_path / "good.jsonl"
    good.write_text(json.dumps(_base("S0-go", "online")) + "\n")
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n")
    assert validate.main([str(good)]) == 0
    assert validate.main([str(good), str(bad)]) == 1
