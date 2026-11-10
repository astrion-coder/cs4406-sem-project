"""P7 recovery test (spec 11 `test_recovery`), written from src/adapters/README.md ("Command line",
"Faults", "Recovery") and spec 5.1, 6.1 and 11 only. Each adapter runs as a subprocess and is judged
by its exit code and the JSON lines it writes.

Sizes. Every arm runs at 1 MiB except S4, which runs at 64 KiB: S4 costs about 17 s per query and a
~6 min setup at 1 MiB, and `stale_record` builds a second database on top of that. S3 and S4 retrieve
one bit per query and require `--record-bytes 0`; S3 supports only 1 MiB among the small sizes.

Counts come from env vars: VPIR_RECOVERY_REPS (faulted trials, default 100),
VPIR_RECOVERY_HONEST_REPS (honest queries, default 1000), VPIR_RECOVERY_BYTES_REPS (default 20) and
VPIR_RECOVERY_TIMEOUT (subprocess timeout in seconds, default 7200). VPIR_ADAPTER_MEM_CAP_GIB caps
each adapter's memory footprint (default 2, see MEM_CAP).
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cs4406_sem_project.harness import memcap

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = Path(os.environ.get("VPIR_ADAPTER_DIR", ROOT / "build" / "adapters"))
REPS = int(os.environ.get("VPIR_RECOVERY_REPS", "100"))
HONEST_REPS = int(os.environ.get("VPIR_RECOVERY_HONEST_REPS", "1000"))
BYTES_REPS = int(os.environ.get("VPIR_RECOVERY_BYTES_REPS", "20"))
TIMEOUT_S = float(os.environ.get("VPIR_RECOVERY_TIMEOUT", "7200"))
# Each adapter run is killed if its memory footprint (resident plus compressed) passes this cap,
# so a runaway allocation fails one test instead of exhausting the machine. 2 GiB is about 7x the
# largest honest peak at these sizes (S3, 0.28 GiB), and five parallel partitions fit in 16 GiB.
MEM_CAP = memcap.cap_from_env(2)
SEED = 1234
BUDGET = 3
OUTCOMES = ("accepted", "recovered", "aborted", "silent_wrong")
RECOVERY_FAULTS = ("flip_answer_bit", "wrong_record", "stale_record", "drop_server")
SILENT_FAULTS = ("flip_answer_bit", "wrong_record", "stale_record")
RECOVERY_METRICS = ("recovery_s", "recovery_cpu_s", "recovery_up_bytes", "recovery_down_bytes")


@dataclass(frozen=True)
class Arm:
    name: str
    binary: str
    verifiable: bool
    db_bytes: int = 1 << 20
    record_bytes: int = 32

    @property
    def re_preprocess(self) -> bool:
        return self.name in ("S1", "S2")

    @property
    def policies(self) -> tuple[str, ...]:
        if not self.verifiable:
            return ("abort",)
        return ("abort", "switch_replica") + (("re_preprocess",) if self.re_preprocess else ())


ARMS = (
    Arm("S0-go", "simplepir-adapter", False),
    Arm("S0-cpp", "vsp-adapter", False),
    Arm("S1", "vsp-adapter", True),
    Arm("S2", "vsp-adapter", True),
    Arm("S3", "apir-adapter", True, record_bytes=0),
    Arm("S4", "apir-adapter", True, db_bytes=1 << 16, record_bytes=0),
    Arm("M0", "apir-adapter", False),
    Arm("M1", "apir-adapter", True),
)
VERIFIABLE = tuple(a for a in ARMS if a.verifiable)
BASELINES = tuple(a for a in ARMS if not a.verifiable)
PROOF_ARMS = tuple(a for a in ARMS if a.re_preprocess)


def _ids(arms):
    return [a.name for a in arms]


MATRIX = [
    pytest.param(a, f, p, id=f"{a.name}-{f}-{p}")
    for a in VERIFIABLE for f in RECOVERY_FAULTS for p in a.policies
]
CHEAT_MATRIX = [
    pytest.param(a, f, p, id=f"{a.name}-{f}-{p}")
    for a in VERIFIABLE for f in RECOVERY_FAULTS for p in a.policies if p != "abort"
]
USAGE_CASES = (
    [pytest.param(a, "retry_same", "flip_answer_bit", "online", id=f"{a.name}-retry_same")
     for a in ARMS]
    + [pytest.param(a, "switch_replica", "flip_answer_bit", "online",
                    id=f"{a.name}-switch_replica") for a in BASELINES]
    + [pytest.param(a, "re_preprocess", "flip_answer_bit", "online",
                    id=f"{a.name}-re_preprocess") for a in ARMS if not a.re_preprocess]
    + [pytest.param(a, None, "bad_proof", "offline", id=f"{a.name}-bad_proof")
       for a in ARMS if not a.re_preprocess]
)
BASELINE_CASES = [
    pytest.param(a, f, id=f"{a.name}-{f}") for a in BASELINES for f in RECOVERY_FAULTS
]
SILENT_CASES = [pytest.param(a, f, id=f"{a.name}-{f}") for a in VERIFIABLE for f in SILENT_FAULTS]
METRIC_CASES = [
    pytest.param(a, p, id=f"{a.name}-{p}") for a in VERIFIABLE for p in a.policies if p != "abort"
]


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stderr: str
    records: list[dict]

    def ok(self) -> list[dict]:
        assert self.returncode == 0, (
            f"exit {self.returncode} from {' '.join(self.argv)}\nstderr tail:\n{self.stderr[-1500:]}"
        )
        assert self.records, f"no records written by {' '.join(self.argv)}"
        return self.records


@pytest.fixture(scope="session")
def run(tmp_path_factory):
    cache: dict = {}

    def _run(arm: Arm, *, phase: str = "online", fault: str | None = "none",
             recovery: str | None = None, reps: int = REPS, fault_prob: float | None = None,
             replicas_cheat: bool = False) -> Result:
        key = (arm.name, phase, fault, recovery, reps, fault_prob, replicas_cheat)
        if key in cache:
            return cache[key]
        binary = ADAPTER_DIR / arm.binary
        if not binary.is_file():
            pytest.skip(f"{arm.binary} not built; run make build")
        out = tmp_path_factory.mktemp("adapter") / "out.jsonl"
        argv = [
            str(binary), "--arm", arm.name, "--db-bytes", str(arm.db_bytes),
            "--record-bytes", str(arm.record_bytes), "--phase", phase, "--reps", str(reps),
            "--warmup", "0", "--seed", str(SEED), "--out", str(out),
        ]
        if fault is not None:
            argv += ["--fault", fault]
        if recovery is not None:
            argv += ["--recovery", recovery]
        if fault_prob is not None:
            argv += ["--fault-prob", str(fault_prob)]
        if replicas_cheat:
            argv.append("--replicas-cheat")
        try:
            proc = memcap.run(argv, timeout_s=TIMEOUT_S, cap_bytes=MEM_CAP)
        except subprocess.TimeoutExpired:
            pytest.fail(f"timed out after {TIMEOUT_S}s: {' '.join(argv)}")
        if proc.over_cap:
            pytest.fail(
                f"memory footprint passed the {MEM_CAP / memcap.GIB:g} GiB cap (VPIR_ADAPTER_MEM_CAP_GIB),"
                f" killed: {' '.join(argv)}\nstderr tail:\n{proc.stderr[-1500:]}"
            )
        records = []
        if out.is_file():
            for n, line in enumerate(out.read_text().splitlines(), 1):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    pytest.fail(f"{out} line {n} is not JSON ({e}): {line[:200]!r}")
        cache[key] = Result(tuple(argv), proc.returncode, proc.stderr, records)
        return cache[key]

    return _run


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_recovery_record(arm: Arm, rec: dict) -> None:
    where = f"{arm.name} {rec['fault']} {rec['recovery']} rep {rec['rep']}"
    outcome, attempts = rec.get("recovery_outcome"), rec.get("recovery_attempts")
    assert outcome in OUTCOMES, f"{where}: recovery_outcome {outcome!r}"
    assert _int(attempts) and 0 <= attempts <= BUDGET, f"{where}: recovery_attempts {attempts!r}"

    metrics = rec["metrics"]
    for name in RECOVERY_METRICS:
        assert name in metrics, f"{where}: metrics missing {name!r} (null, never omitted)"
        v = metrics[name]
        if attempts == 0:
            assert v is None, f"{where}: {name}={v!r} but recovery_attempts is 0"
        elif name == "recovery_down_bytes" and rec["fault"] == "drop_server":
            # A withheld answer sends nothing: a single server whose every attempt drops downloads 0.
            assert _int(v) and v >= 0, f"{where}: {name}={v!r} after {attempts} attempts"
        elif name.endswith("bytes"):
            assert _int(v) and v > 0, f"{where}: {name}={v!r} after {attempts} attempts"
        else:
            assert _num(v) and v >= 0, f"{where}: {name}={v!r} after {attempts} attempts"

    if not rec["faulted"]:
        assert outcome == "accepted" and attempts == 0, (
            f"{where}: unfaulted rep has outcome {outcome!r}, attempts {attempts}"
        )
    if outcome == "accepted":
        assert attempts == 0, f"{where}: accepted after {attempts} attempts"
        assert rec["correct"] is True, f"{where}: accepted but correct={rec['correct']!r}"
        if arm.verifiable:
            assert rec["detected"] is False, f"{where}: accepted but detected={rec['detected']!r}"
    if arm.verifiable:
        assert outcome != "silent_wrong", f"{where}: verifiable arm accepted a wrong record"
        if rec["faulted"]:
            assert rec["detected"] is True, f"{where}: faulted rep not detected"
        if outcome == "recovered":
            assert attempts >= 1, f"{where}: recovered with 0 attempts"
            assert rec["detected"] is True, f"{where}: recovered without a rejection"
        if outcome == "aborted":
            assert rec["detected"] is True, f"{where}: aborted without a rejection"
    else:
        assert rec["detected"] is None, f"{where}: baseline detected={rec['detected']!r}"
        assert outcome in ("accepted", "silent_wrong", "aborted"), f"{where}: baseline {outcome!r}"
        assert attempts == 0, f"{where}: baseline made {attempts} further attempts"
        if outcome == "silent_wrong":
            assert rec["correct"] is False, f"{where}: silent_wrong but correct={rec['correct']!r}"
        if outcome == "aborted":
            assert rec["fault"] == "drop_server", f"{where}: baseline aborted under {rec['fault']}"


def online(res: Result, arm: Arm) -> list[dict]:
    recs = [r for r in res.ok() if r["phase"] == "online" and not r["is_warmup"]]
    assert recs, f"no online records from {' '.join(res.argv)}"
    for r in recs:
        _check_recovery_record(arm, r)
    return recs


def faulted_of(recs: list[dict], what: str) -> list[dict]:
    faulted = [r for r in recs if r["faulted"]]
    assert faulted, f"{what}: no faulted repetition, test would be vacuous"
    return faulted


@pytest.mark.parametrize("arm,policy,fault,phase", USAGE_CASES)
def test_usage_errors(arm, policy, fault, phase, run):
    if policy is None:
        res = run(arm, phase=phase, fault="bad_proof", reps=1)
    else:
        res = run(arm, phase=phase, fault=fault, recovery=policy, reps=1)
    assert res.returncode == 2, (
        f"exit {res.returncode}, expected usage error 2: {' '.join(res.argv)}"
    )


def test_usage_error_proof_arms_accept_their_policies(run):
    arm = PROOF_ARMS[0]
    for policy in ("abort", "switch_replica", "re_preprocess"):
        res = run(arm, fault="flip_answer_bit", recovery=policy, reps=1)
        assert res.returncode == 0, f"{arm.name} --recovery {policy} exited {res.returncode}"


@pytest.mark.parametrize("arm", VERIFIABLE, ids=_ids(VERIFIABLE))
def test_stale_record_detected(arm, run):
    recs = online(run(arm, fault="stale_record", recovery="abort"), arm)
    faulted = faulted_of(recs, f"{arm.name} stale_record")
    missed = [r["rep"] for r in faulted if r["detected"] is not True]
    assert not missed, f"{arm.name} stale_record: {len(missed)}/{len(faulted)} not detected {missed[:10]}"


@pytest.mark.parametrize("arm", BASELINES, ids=_ids(BASELINES))
def test_stale_record_baseline(arm, run):
    recs = online(run(arm, fault="stale_record", recovery="abort"), arm)
    faulted = faulted_of(recs, f"{arm.name} stale_record")
    for r in faulted:
        assert r["detected"] is None, f"{arm.name} rep {r['rep']}: detected={r['detected']!r}"
        assert r["correct"] is False, f"{arm.name} rep {r['rep']}: stale answer was correct"


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_drop_server_aborts(arm, run):
    recs = online(run(arm, fault="drop_server", recovery="abort"), arm)
    faulted = faulted_of(recs, f"{arm.name} drop_server")
    for r in faulted:
        assert r["recovery_outcome"] == "aborted", (
            f"{arm.name} rep {r['rep']}: outcome {r['recovery_outcome']!r}"
        )
        assert r["recovery_attempts"] == 0
        assert r["correct"] is None, f"{arm.name} rep {r['rep']}: nothing reconstructed, correct={r['correct']!r}"
        per_server, answer_s = r["metrics"]["down_bytes_per_server"], r["metrics"]["server_answer_s"]
        assert isinstance(per_server, list) and 0 in per_server, (
            f"{arm.name} rep {r['rep']}: withheld answer counted, down_bytes_per_server={per_server!r}"
        )
        assert r["metrics"]["down_bytes"] == sum(per_server)
        assert isinstance(answer_s, list) and len(answer_s) == len(per_server) and all(map(_num, answer_s)), (
            f"{arm.name} rep {r['rep']}: server_answer_s={answer_s!r}"
        )
    assert not [r for r in recs if r["recovery_outcome"] == "recovered"]


@pytest.mark.parametrize("arm", PROOF_ARMS, ids=_ids(PROOF_ARMS))
def test_bad_proof_detected(arm, run):
    recs = run(arm, phase="offline", fault="bad_proof", recovery="abort").ok()
    timed = [r for r in recs if r["phase"] == "offline" and not r["is_warmup"]]
    faulted = faulted_of(timed, f"{arm.name} bad_proof")
    missed = [r["rep"] for r in faulted if r["detected"] is not True]
    assert not missed, f"{arm.name} bad_proof: {len(missed)}/{len(faulted)} not rejected {missed[:10]}"


@pytest.mark.parametrize("arm,fault,policy", MATRIX)
def test_recovery(arm, fault, policy, run):
    if policy == "re_preprocess":
        # The same server serves every attempt, so a probability below 1 lets some attempts be honest.
        recs = online(run(arm, fault=fault, recovery=policy, fault_prob=0.5), arm)
    else:
        recs = online(run(arm, fault=fault, recovery=policy), arm)
    faulted = faulted_of(recs, f"{arm.name} {fault} {policy}")
    outcomes = [(r["rep"], r["recovery_outcome"], r["recovery_attempts"]) for r in faulted]
    if policy == "abort":
        bad = [o for o in outcomes if o[1:] != ("aborted", 0)]
        assert not bad, f"abort must stop at once, got {bad[:5]}"
    elif policy == "switch_replica":
        bad = [o for o in outcomes if o[1:] != ("recovered", 1)]
        assert not bad, f"honest replica must recover with one further attempt, got {bad[:5]}"
    else:
        bad = [o for o in outcomes
               if o[1] not in ("recovered", "aborted")
               or (o[1] == "recovered" and not 1 <= o[2] <= BUDGET)
               or (o[1] == "aborted" and o[2] != BUDGET)]
        assert not bad, f"re_preprocess outcomes outside the budget of {BUDGET}: {bad[:5]}"
        assert any(o[1] == "recovered" for o in outcomes), (
            f"no recovery in {len(outcomes)} faulted reps with fault-prob 0.5"
        )


@pytest.mark.parametrize("arm,fault,policy", CHEAT_MATRIX)
def test_recovery_cheating_servers_abort(arm, fault, policy, run):
    if policy == "switch_replica":
        res = run(arm, fault=fault, recovery=policy, replicas_cheat=True)
    else:
        res = run(arm, fault=fault, recovery=policy, fault_prob=1.0)
    recs = online(res, arm)
    faulted = faulted_of(recs, f"{arm.name} {fault} {policy} cheating")
    bad = [(r["rep"], r["recovery_outcome"], r["recovery_attempts"]) for r in faulted
           if (r["recovery_outcome"], r["recovery_attempts"]) != ("aborted", BUDGET)]
    assert not bad, f"every attempt is faulted, expected aborted after {BUDGET} attempts: {bad[:5]}"


@pytest.mark.parametrize("arm", VERIFIABLE, ids=_ids(VERIFIABLE))
def test_no_false_rejects(arm, run):
    recs = online(run(arm, fault="none", recovery="abort", reps=HONEST_REPS), arm)
    assert len(recs) == HONEST_REPS, f"{len(recs)} records, expected {HONEST_REPS}"
    bad = [r["rep"] for r in recs
           if r["recovery_outcome"] != "accepted" or r["recovery_attempts"] != 0]
    assert not bad, f"{arm.name}: {len(bad)}/{HONEST_REPS} honest queries rejected, reps {bad[:10]}"


@pytest.mark.parametrize("arm,fault", SILENT_CASES)
def test_verifiable_never_silent_wrong(arm, fault, run):
    for policy in arm.policies:
        recs = online(run(arm, fault=fault, recovery=policy,
                          fault_prob=0.5 if policy == "re_preprocess" else None), arm)
        wrong = [r["rep"] for r in recs if r["recovery_outcome"] == "silent_wrong"]
        assert not wrong, f"{arm.name} {fault} {policy}: silent_wrong on reps {wrong[:10]}"


@pytest.mark.parametrize("arm,fault", BASELINE_CASES)
def test_baseline_outcomes(arm, fault, run):
    recs = online(run(arm, fault=fault, recovery="abort"), arm)
    faulted = faulted_of(recs, f"{arm.name} {fault}")
    if fault == "drop_server":
        assert all(r["recovery_outcome"] == "aborted" for r in faulted)
    else:
        assert any(r["recovery_outcome"] == "silent_wrong" for r in faulted), (
            f"{arm.name} {fault}: no faulted rep ended silent_wrong"
        )


@pytest.mark.parametrize("arm,policy", METRIC_CASES)
def test_recovery_metrics_populated(arm, policy, run):
    fault_prob = 0.5 if policy == "re_preprocess" else None
    recs = online(run(arm, fault="flip_answer_bit", recovery=policy, fault_prob=fault_prob), arm)
    retried = [r for r in recs if r["recovery_attempts"] > 0]
    assert retried, f"{arm.name} {policy}: no repetition made a further attempt"
    for r in retried:
        assert all(r["metrics"][m] is not None for m in RECOVERY_METRICS)
    clean = [r for r in recs if r["recovery_attempts"] == 0]
    for r in clean:
        assert all(r["metrics"][m] is None for m in RECOVERY_METRICS)


@pytest.mark.parametrize("arm", PROOF_ARMS, ids=_ids(PROOF_ARMS))
def test_re_preprocess_pays_offline_bytes(arm, run):
    res = run(arm, phase="all", fault="flip_answer_bit", recovery="re_preprocess",
              fault_prob=0.5, reps=BYTES_REPS)
    recs = res.ok()
    offline = [r["metrics"]["offline_down_bytes"] for r in recs if r["phase"] == "offline"]
    assert offline and all(_int(b) and b > 0 for b in offline), f"offline_down_bytes {offline[:3]}"
    once = [r for r in online(res, arm) if r["recovery_attempts"] == 1
            and r["recovery_outcome"] == "recovered"]
    assert once, f"{arm.name}: no recovery after one further attempt in {BYTES_REPS} reps"
    for r in once:
        got = r["metrics"]["recovery_down_bytes"]
        assert got >= min(offline), (
            f"{arm.name} rep {r['rep']}: recovery_down_bytes {got} omits the offline phase "
            f"({min(offline)} bytes)"
        )
