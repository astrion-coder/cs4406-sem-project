"""Validate result records: the JSON schema, then the cross-field rules a schema cannot state.

Errors make a row unusable for analysis. Warnings flag a row for a human (spec 6: measured and
analytical bytes that disagree by more than 1 %; decision 19: padding above 1.1).

    uv run python -m cs4406_sem_project.harness.validate results/raw/<run_id>/*.jsonl \
        [--reference-platform env/reference_platform.txt]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

import jsonschema

SCHEMA_PATH = Path(__file__).with_name("schema.json")

ONE_BIT_ARMS = frozenset({"S3", "S4"})
TWO_SERVER_ARMS = frozenset({"M0", "M1"})
BASELINE_ARMS = frozenset({"S0-go", "S0-cpp", "M0"})
FUSED_ARMS = frozenset({"M1", "S3", "S4"})
ADAPTER_OF = {"S0-go": "simplepir_go", "S0-cpp": "vsp_cpp", "S1": "vsp_cpp", "S2": "vsp_cpp",
              "S3": "apir_go", "S4": "apir_go", "M0": "apir_go", "M1": "apir_go"}
OFFLINE_METRICS = ("public_params_s", "server_preproc_s", "server_preproc_proof_s", "client_offline_s",
                   "offline_down_bytes", "offline_down_bytes_hint_only", "offline_up_bytes",
                   "client_storage_bytes")
ONLINE_METRICS = ("query_gen_s", "up_bytes", "up_bytes_per_server", "server_answer_s", "down_bytes",
                  "down_bytes_per_server", "client_recover_s", "client_verify_s", "verify_fused")
COMM_TOLERANCE = 0.01  # spec 6 and 11 (test_comm_analytical)
PADDING_LIMIT = 1.1  # decision 19

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Problem:
    line: int  # 1-based line in the file, 0 when not from a file
    severity: Severity
    field: str
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.severity}: {self.field}: {self.message}"


KIND_DEF = {"adapter": "adapter_record", "failure": "harness_failure", "serve": "serve_summary",
            "loadgen": "loadgen_record"}


@cache
def _validator(record_kind: str) -> jsonschema.Draft202012Validator:
    """The schema narrowed to one record kind, so an error names the field, not the oneOf."""
    schema = json.loads(SCHEMA_PATH.read_text())
    schema["oneOf"] = [{"$ref": f"#/$defs/{KIND_DEF[record_kind]}"}]
    return jsonschema.Draft202012Validator(schema)


def kind(row: dict) -> str:
    """adapter, failure, serve or loadgen, from the keys that tell the kinds apart."""
    mode = row.get("mode")
    if mode in ("serve", "loadgen"):
        return mode
    return "failure" if "harness" in row else "adapter"


def _path(err: jsonschema.ValidationError) -> str:
    return "/".join(str(p) for p in err.absolute_path) or "<record>"


def _schema_problems(row: dict) -> list[tuple[str, str]]:
    errors = list(_validator(kind(row)).iter_errors(row))
    if not errors:
        return []
    best = jsonschema.exceptions.best_match(errors)
    return [(_path(best), best.message)]


Rule = Callable[[dict], Iterator[tuple[Severity, str, str]]]


def _rule_identity(r: dict):
    if r["adapter"] != ADAPTER_OF[r["arm"]]:
        yield "error", "adapter", f"arm {r['arm']} is served by {ADAPTER_OF[r['arm']]}, not {r['adapter']}"
    if r["arm"] in ONE_BIT_ARMS or r["record_bytes"] is None:
        if r["n_records"] != 8 * r["db_bytes"]:
            yield "error", "n_records", "one-bit rows count bits: n_records must be 8 x db_bytes"
    else:
        if r["retrieved_bits"] != 8 * r["record_bytes"]:
            yield "error", "retrieved_bits", "must be 8 x record_bytes"
        if r["n_records"] != r["db_bytes"] // r["record_bytes"]:
            yield "error", "n_records", "must be db_bytes // record_bytes"
    if r.get("fake_preproc"):
        yield "error", "fake_preproc", "fake preprocessing is never reported (spec 10 rule 2)"


def _rule_phase_nulls(r: dict):
    if r["status"] != "ok":
        return
    m = r["metrics"]
    other = ONLINE_METRICS if r["phase"] == "offline" else OFFLINE_METRICS
    for key in other:
        if m[key] is not None:
            yield "error", f"metrics/{key}", f"must be null in an {r['phase']} record"
    if r["phase"] == "offline":
        for key in ("recovery_outcome", "recovery_attempts", "correct"):
            if r[key] is not None:
                yield "error", key, "must be null in an offline record"


def _rule_online(r: dict):
    if r["status"] != "ok" or r["phase"] != "online":
        return
    m, arm = r["metrics"], r["arm"]
    servers = 2 if arm in TWO_SERVER_ARMS else 1
    for key in ("up_bytes_per_server", "down_bytes_per_server", "server_answer_s"):
        if m[key] is None or len(m[key]) != servers:
            yield "error", f"metrics/{key}", f"needs one entry per server ({servers})"
    for total, per in (("up_bytes", "up_bytes_per_server"), ("down_bytes", "down_bytes_per_server")):
        if m[total] is not None and m[per] is not None and sum(m[per]) != m[total]:
            yield "error", f"metrics/{total}", f"is not the sum of {per}"
    fused = arm in FUSED_ARMS
    if m["verify_fused"] is not fused:
        yield "error", "metrics/verify_fused", f"must be {str(fused).lower()} for {arm} (decision 14)"
    if fused and m["client_verify_s"] is not None:
        yield "error", "metrics/client_verify_s", "must be null when verification is fused"
    if arm in BASELINE_ARMS and r["detected"] is not None:
        yield "error", "detected", "baselines have no check, so detected must be null"
    if arm not in BASELINE_ARMS and r["detected"] is None:
        yield "error", "detected", "verifiable arms report whether the client rejected"
    if r["correct"] is None and r["fault"] != "drop_server" and not r["detected"]:
        yield "error", "correct", "only a withheld (drop_server) or rejected answer leaves correct null"
    if r["recovery_outcome"] is None or r["recovery_attempts"] is None:
        yield "error", "recovery_outcome", "online records report the recovery outcome"
    elif (r["recovery_attempts"] == 0) != (m["recovery_s"] is None):
        yield "error", "metrics/recovery_s", "is null exactly when recovery_attempts is 0"
    if r["fault"] == "none" and arm not in BASELINE_ARMS and r["detected"]:
        yield "error", "detected", "an honest answer was rejected (false reject)"
    if r["fault"] == "none" and r["correct"] is False:
        yield "error", "correct", "an honest run decoded a wrong record"


def _rule_analytical(r: dict):
    a = r["analytical"]
    if r["status"] != "ok" or a is None or r.get("faulted"):
        return
    m = r["metrics"]
    pairs = (("up_bytes", "up_bytes"), ("down_bytes", "down_bytes"),
             ("offline_down_bytes_hint_only", "offline_down_bytes_hint_only"))
    for measured_key, formula_key in pairs:
        measured, formula = m.get(measured_key), a.get(formula_key)
        if measured is None or formula in (None, 0):
            continue
        dev = abs(measured - formula) / formula
        if dev > COMM_TOLERANCE:
            yield ("warning", f"metrics/{measured_key}",
                   f"measured {measured} vs analytical {formula:g} differ by {dev:.1%} (> 1 %)")


def _rule_padding(r: dict):
    pad = r["scheme_params"].get("padding_factor")
    if isinstance(pad, (int, float)) and pad > PADDING_LIMIT:
        yield "warning", "scheme_params/padding_factor", f"{pad:.3f} > {PADDING_LIMIT} (decision 19)"


ADAPTER_RULES: tuple[Rule, ...] = (_rule_identity, _rule_phase_nulls, _rule_online, _rule_analytical,
                                   _rule_padding)


def check_row(row: dict, *, line: int = 0, reference_platform: str | None = None) -> list[Problem]:
    found = [Problem(line, "error", f, msg) for f, msg in _schema_problems(row)]
    if found:
        return found
    if reference_platform is not None and kind(row) != "serve" and row.get("platform") != reference_platform:
        found.append(Problem(line, "error", "platform",
                             f"{row.get('platform')!r} is not the reference platform {reference_platform!r}"))
    if kind(row) == "adapter":
        for rule in ADAPTER_RULES:
            found.extend(Problem(line, sev, f, msg) for sev, f, msg in rule(row))
    return found


def check_lines(lines: Iterable[str], *, reference_platform: str | None = None) -> list[Problem]:
    found: list[Problem] = []
    for n, text in enumerate(lines, 1):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError as e:
            found.append(Problem(n, "error", "<line>", f"not JSON: {e}"))
            continue
        if not isinstance(row, dict):
            found.append(Problem(n, "error", "<line>", "not a JSON object"))
            continue
        found.extend(check_row(row, line=n, reference_platform=reference_platform))
    return found


def check_file(path: Path, *, reference_platform: str | None = None) -> list[Problem]:
    with path.open() as f:
        return check_lines(f, reference_platform=reference_platform)


def read_reference_platform(path: Path) -> str:
    return path.read_text().strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--reference-platform", type=Path, help="refuse rows whose platform differs from this file")
    p.add_argument("--quiet-warnings", action="store_true")
    args = p.parse_args(argv)
    ref = read_reference_platform(args.reference_platform) if args.reference_platform else None
    errors = warnings = 0
    for path in args.files:
        for prob in check_file(path, reference_platform=ref):
            if prob.severity == "error":
                errors += 1
            else:
                warnings += 1
                if args.quiet_warnings:
                    continue
            print(f"{path}: {prob}")
    print(f"{len(args.files)} files, {errors} errors, {warnings} warnings", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
