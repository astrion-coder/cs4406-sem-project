"""Gate G1 conformance test, written from src/adapters/README.md and spec 9 and 11 only.

Each adapter is run as a subprocess and judged by the JSON lines it writes.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from cs4406_sem_project.harness import memcap, validate

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = Path(os.environ.get("VPIR_ADAPTER_DIR", ROOT / "build" / "adapters"))
REPS = int(os.environ.get("VPIR_CONFORMANCE_REPS", "100"))
# Sized for the slowest run, S4 wrong_record at 1 MiB: the DDH digest, then two server answers
# per query (the honest one and the substitute), with other partitions running alongside.
TIMEOUT_S = float(os.environ.get("VPIR_CONFORMANCE_TIMEOUT", "7200"))
# Each adapter run is killed if its memory footprint (resident plus compressed) passes this cap,
# so a runaway allocation fails one test instead of exhausting the machine. 2 GiB is about 7x the
# largest honest peak at these sizes (S3, 0.28 GiB), and five parallel partitions fit in 16 GiB.
MEM_CAP = memcap.cap_from_env(2)
DB_BYTES = 1 << 20
SEED = 1234
WARMUP = 1
HARNESS_ENV = {"VPIR_RUN_ID": "run-x", "VPIR_GIT_SHA": "abc123", "VPIR_PLATFORM": "plat-x"}

ONLINE_FAULTS = ("flip_answer_bit", "wrong_record")


@dataclass(frozen=True)
class Arm:
    name: str
    binary: str
    verifiable: bool
    record_sizes: tuple[int, ...]
    hint_fault: bool
    fused: bool = False


ARMS = (
    Arm("S0-go", "simplepir-adapter", False, (32, 1024), True),
    Arm("S0-cpp", "vsp-adapter", False, (32, 1024), True),
    Arm("S1", "vsp-adapter", True, (32, 1024), True),
    Arm("S2", "vsp-adapter", True, (32, 1024), True),
    Arm("S3", "apir-adapter", True, (0,), False, fused=True),
    Arm("S4", "apir-adapter", True, (0,), False, fused=True),
    Arm("M0", "apir-adapter", False, (32, 1024), False),
    Arm("M1", "apir-adapter", True, (32, 1024), False, fused=True),
)
BY_NAME = {a.name: a for a in ARMS}
ARM_IDS = [a.name for a in ARMS]

CORRECTNESS_CASES = [
    pytest.param(a, rb, id=f"{a.name}-{rb}") for a in ARMS for rb in a.record_sizes
]
DETECTION_CASES = [
    pytest.param(a, f, "online", id=f"{a.name}-{f}")
    for a in ARMS
    if a.verifiable
    for f in ONLINE_FAULTS
] + [
    pytest.param(a, "flip_hint_bit", "offline", id=f"{a.name}-flip_hint_bit")
    for a in ARMS
    if a.verifiable and a.hint_fault
]
BASELINE_CASES = [
    pytest.param(a, f, id=f"{a.name}-{f}") for a in ARMS if not a.verifiable for f in ONLINE_FAULTS
]


@dataclass(frozen=True)
class Field:
    name: str
    types: tuple[str, ...]
    nullable: bool = False


FIELDS = (
    Field("run_id", ("str",), True),
    Field("git_sha_harness", ("str",), True),
    Field("platform", ("str",), True),
    Field("upstream_sha", ("str",)),
    Field("adapter", ("str",)),
    Field("arm", ("str",)),
    Field("scheme_params", ("obj",)),
    Field("db_bytes", ("int",)),
    Field("record_bytes", ("int",), True),
    Field("retrieved_bits", ("int",)),
    Field("n_records", ("int",)),
    Field("phase", ("str",)),
    Field("rep", ("int",)),
    Field("is_warmup", ("bool",)),
    Field("seed", ("int",)),
    Field("fault", ("str",)),
    Field("fault_prob", ("num",)),
    Field("faulted", ("bool",)),
    Field("recovery", ("str",)),
    Field("status", ("str",)),
    Field("metrics", ("obj",)),
    Field("analytical", ("obj",), True),
    Field("correct", ("bool",), True),
    Field("detected", ("bool",), True),
    Field("error", ("str",), True),
)


@dataclass(frozen=True)
class Metric:
    name: str
    phase: str
    kind: str


METRICS = (
    Metric("public_params_s", "offline", "num"),
    Metric("server_preproc_s", "offline", "num"),
    Metric("server_preproc_proof_s", "offline", "num"),
    Metric("client_offline_s", "offline", "num"),
    Metric("offline_down_bytes", "offline", "int"),
    Metric("offline_down_bytes_hint_only", "offline", "int"),
    Metric("offline_up_bytes", "offline", "int"),
    Metric("client_storage_bytes", "offline", "int"),
    Metric("query_gen_s", "online", "num"),
    Metric("up_bytes", "online", "int"),
    Metric("up_bytes_per_server", "online", "int_list"),
    Metric("server_answer_s", "online", "num_list"),
    Metric("down_bytes", "online", "int"),
    Metric("down_bytes_per_server", "online", "int_list"),
    Metric("client_recover_s", "online", "num"),
    Metric("client_verify_s", "online", "num"),
    Metric("verify_fused", "online", "bool"),
    Metric("process_peak_rss_bytes", "both", "int"),
)

SCALARS = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "obj": lambda v: isinstance(v, dict),
}


def _metric_ok(kind: str, v) -> bool:
    if kind.endswith("_list"):
        return isinstance(v, list) and all(SCALARS[kind[:-5]](x) for x in v)
    return SCALARS[kind](v)


def _binary(arm: Arm) -> Path:
    path = ADAPTER_DIR / arm.binary
    if not path.is_file():
        pytest.skip(f"{arm.binary} not built; run make build")
    return path


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

    def _run(arm: Arm, *, record_bytes: int, phase: str = "all", reps: int = REPS,
             warmup: int = WARMUP, fault: str = "none", env: dict | None = None,
             arm_name: str | None = None, db_bytes: int = DB_BYTES, flags: tuple[str, ...] = ()) -> Result:
        key = (arm.name, arm_name, db_bytes, record_bytes, phase, reps, warmup, fault, flags,
               tuple(sorted((env or {}).items())))
        if key in cache:
            return cache[key]
        binary = _binary(arm)
        out = tmp_path_factory.mktemp("adapter") / "out.jsonl"
        argv = (
            str(binary), "--arm", arm_name or arm.name, "--db-bytes", str(db_bytes),
            "--record-bytes", str(record_bytes), "--phase", phase, "--reps", str(reps),
            "--warmup", str(warmup), "--seed", str(SEED), "--fault", fault, "--out", str(out), *flags,
        )
        child_env = {k: v for k, v in os.environ.items() if k not in HARNESS_ENV}
        child_env.update(env or {})
        try:
            proc = memcap.run(argv, timeout_s=TIMEOUT_S, cap_bytes=MEM_CAP, env=child_env)
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
        cache[key] = Result(argv, proc.returncode, proc.stderr, records)
        return cache[key]

    return _run


def _check_record(arm: Arm, record_bytes: int, rec: dict) -> None:
    where = f"{arm.name} {rec.get('phase')} rep {rec.get('rep')}"
    for f in FIELDS:
        assert f.name in rec, f"{where}: missing field {f.name!r}"
        v = rec[f.name]
        if v is None:
            assert f.nullable, f"{where}: field {f.name!r} is null but not nullable"
        else:
            assert any(SCALARS[t](v) for t in f.types), (
                f"{where}: field {f.name!r} has {type(v).__name__} {v!r}, allowed {f.types}"
            )
    assert rec["phase"] in ("offline", "online"), f"{where}: phase {rec['phase']!r}"
    metrics = rec["metrics"]
    for m in METRICS:
        assert m.name in metrics, f"{where}: metrics missing {m.name!r} (null, never omitted)"
        if m.phase not in (rec["phase"], "both"):
            assert metrics[m.name] is None, f"{where}: {m.phase} metric {m.name!r} must be null in a {rec['phase']} record"
            continue
        v = metrics[m.name]
        if v is None:
            assert m.name != "verify_fused", f"{where}: verify_fused must be a bool"
        else:
            assert _metric_ok(m.kind, v), (
                f"{where}: metric {m.name!r} has {v!r}, expected {m.kind}"
            )
    for k, v in metrics.items():
        if k.endswith("_s") and not k.endswith("_cpu_s"):
            sib = k[:-2] + "_cpu_s"
            assert sib in metrics, f"{where}: {k!r} has no sibling {sib!r}"
            assert (v is None) == (metrics[sib] is None), (
                f"{where}: {k!r}={v!r} but {sib!r}={metrics[sib]!r}"
            )
            if isinstance(v, list):
                assert len(v) == len(metrics[sib]), f"{where}: {k!r} and {sib!r} differ in length"
    if rec["phase"] == "online":
        assert metrics["verify_fused"] is arm.fused, (
            f"{where}: verify_fused={metrics['verify_fused']!r}, expected {arm.fused}"
        )
        if metrics["verify_fused"]:
            assert metrics["client_verify_s"] is None, f"{where}: fused but client_verify_s set"


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_record_fields(arm, run):
    rb = arm.record_sizes[0]
    reps, warmup = 1, 1
    recs = run(arm, record_bytes=rb, reps=reps, warmup=warmup).ok()
    for rec in recs:
        _check_record(arm, rb, rec)
        assert rec["arm"] == arm.name
        assert rec["db_bytes"] == DB_BYTES and rec["seed"] == SEED
        assert rec["fault"] == "none" and rec["faulted"] is False
        assert rec["record_bytes"] == (None if rb == 0 else rb), f"record_bytes {rec['record_bytes']!r}"
        assert rec["retrieved_bits"] == (1 if rb == 0 else 8 * rb)
        assert rec["n_records"] > 0
        assert rec["is_warmup"] is (rec["rep"] < warmup)
        for name in HARNESS_ENV:
            field = {"VPIR_RUN_ID": "run_id", "VPIR_GIT_SHA": "git_sha_harness",
                     "VPIR_PLATFORM": "platform"}[name]
            assert rec[field] is None, f"{field} must be null when {name} is unset"
    for phase in ("offline", "online"):
        reps_seen = [r["rep"] for r in recs if r["phase"] == phase]
        assert reps_seen == list(range(warmup + reps)), f"{phase} reps {reps_seen}"

    env_recs = run(arm, record_bytes=rb, phase="online", reps=1, warmup=0, env=HARNESS_ENV).ok()
    for rec in env_recs:
        assert (rec["run_id"], rec["git_sha_harness"], rec["platform"]) == (
            "run-x", "abc123", "plat-x"
        ), "harness environment variables not echoed into the record"


@pytest.mark.parametrize("arm,record_bytes", CORRECTNESS_CASES)
def test_correctness(arm, record_bytes, run):
    # Online only: setup runs once untimed, so a slow setup (S4's DDH digest) is not repeated per query.
    recs = run(arm, record_bytes=record_bytes, phase="online").ok()
    online = [r for r in recs if r["phase"] == "online"]
    assert len(online) == REPS + WARMUP, f"{len(online)} online records, expected {REPS + WARMUP}"
    for r in online:
        where = f"{arm.name} rep {r['rep']}"
        assert r["status"] == "ok", f"{where}: status {r['status']!r}, error {r['error']!r}"
        assert r["correct"] is True, f"{where}: correct={r['correct']!r}"
        want = False if arm.verifiable else None
        assert r["detected"] is want, f"{where}: detected={r['detected']!r}, expected {want!r}"


@pytest.mark.parametrize("arm,fault,phase", DETECTION_CASES)
def test_fault_detection(arm, fault, phase, run):
    recs = run(arm, record_bytes=arm.record_sizes[0], phase=phase, fault=fault).ok()
    timed = [r for r in recs if r["phase"] == phase and not r["is_warmup"]]
    faulted = [r for r in timed if r["faulted"]]
    assert faulted, f"{arm.name} {fault}: no faulted non-warm-up record, test would be vacuous"
    missed = [r["rep"] for r in faulted if r["detected"] is not True]
    assert not missed, (
        f"{arm.name} {fault}: {len(missed)}/{len(faulted)} faulted reps not detected, reps {missed[:10]}"
    )


@pytest.mark.parametrize("arm,fault", BASELINE_CASES)
def test_fault_baseline(arm, fault, run):
    recs = run(arm, record_bytes=arm.record_sizes[0], phase="online", fault=fault).ok()
    bad = [r["rep"] for r in recs if r["detected"] is not None]
    assert not bad, f"{arm.name} {fault}: baseline reported detected on reps {bad[:10]}"
    faulted = [r for r in recs if r["faulted"]]
    assert faulted, f"{arm.name} {fault}: no faulted record"
    assert any(r["correct"] is False for r in faulted), (
        f"{arm.name} {fault}: none of {len(faulted)} faulted reps has correct=false"
    )


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_comm_analytical(arm, run):
    rb = arm.record_sizes[0]
    recs = run(arm, record_bytes=rb, phase="online").ok()
    for r in recs:
        if r["phase"] != "online" or not r["analytical"]:
            continue
        for key in ("up_bytes", "down_bytes"):
            want = r["analytical"].get(key)
            if want is None:
                continue
            got = r["metrics"][key]
            assert got is not None, f"{arm.name} rep {r['rep']}: analytical {key} set, measured null"
            assert abs(got - want) <= 0.01 * want, (
                f"{arm.name} rep {r['rep']}: measured {key} {got} vs analytical {want}"
            )


@pytest.mark.parametrize("arm", ARMS, ids=ARM_IDS)
def test_usage_errors(arm, run):
    rb = arm.record_sizes[0]
    r = run(arm, record_bytes=rb, reps=1, warmup=0, fault="no_such_fault")
    assert r.returncode == 2, f"unknown --fault exited {r.returncode}, expected 2"
    r = run(arm, record_bytes=rb, reps=1, warmup=0, arm_name="NO-SUCH-ARM")
    assert r.returncode == 2, f"unknown --arm exited {r.returncode}, expected 2"
    if arm.record_sizes == (0,):
        r = run(arm, record_bytes=32, reps=1, warmup=0)
        assert r.returncode == 2, f"--record-bytes 32 exited {r.returncode}, expected 2"


@pytest.mark.parametrize("arm, record_bytes", [("S3", 0), ("M1", 32)])
def test_offline_repetitions_release_the_previous_state(arm, record_bytes, run):
    """Each offline repetition builds the arm's whole state again. If the previous repetition's state is
    still live while it does, the peak climbs with every repetition, and at 128 MiB and 1 GiB the cell
    runs out of memory (decision 26). At 16 MiB that growth is tens of MiB or more, well above noise."""
    r = run(BY_NAME[arm], record_bytes=record_bytes, phase="offline", reps=3, warmup=0, db_bytes=16 << 20)
    peaks = [x["metrics"]["process_peak_rss_bytes"] for x in r.ok()]
    assert peaks[-1] - peaks[0] < 8 << 20, [p >> 20 for p in peaks]


@pytest.mark.parametrize("arm", ["S0-cpp", "S1", "S2"])
def test_vsp_offline_in_process(arm, run):
    """--offline-in-process runs honest offline repetitions in the adapter's own process, and every record
    says which way it ran (decision 27). A faulted run refuses it: an upstream assert may end only a child."""
    here = run(BY_NAME[arm], record_bytes=32, phase="offline", reps=2, warmup=0, flags=("--offline-in-process",))
    assert [x["scheme_params"]["offline_in_process"] for x in here.ok()] == [True, True]
    assert [str(p) for x in here.records for p in validate.check_row(x) if p.severity == "error"] == []
    forked = run(BY_NAME[arm], record_bytes=32, phase="offline", reps=2, warmup=0)
    assert [x["scheme_params"]["offline_in_process"] for x in forked.ok()] == [False, False]
    faulted = run(BY_NAME[arm], record_bytes=32, phase="offline", reps=1, warmup=0, fault="flip_hint_bit",
                  flags=("--offline-in-process",))
    assert faulted.returncode == 2 and "--offline-in-process takes --fault none" in faulted.stderr, faulted.stderr[-500:]


def test_s0go_one_bit_anchor(run):
    """--record-bytes 0 is SimplePIR's README setup (decision 21): one-bit entries packed several to a
    Z_p element. Upstream Query and Recover mis-address those past the first element, so the adapter
    addresses them itself, and every bit must still come back right."""
    r = run(BY_NAME["S0-go"], record_bytes=0)
    assert r.returncode == 0, r.stderr[-1500:]
    online = [x for x in r.records if x["phase"] == "online"]
    assert online and all(x["correct"] for x in online)
    assert all(x["record_bytes"] is None and x["retrieved_bits"] == 1 and x["n_records"] == 8 * DB_BYTES
               for x in r.records)
    assert r.records[0]["scheme_params"]["packing"] > 1
    assert [str(p) for x in r.records for p in validate.check_row(x) if p.severity == "error"] == []


def test_s0go_one_bit_refuses_faults(run):
    r = run(BY_NAME["S0-go"], record_bytes=0, reps=1, warmup=0, fault="flip_answer_bit")
    assert r.returncode == 2, f"one-bit entries with a fault exited {r.returncode}, expected 2"
