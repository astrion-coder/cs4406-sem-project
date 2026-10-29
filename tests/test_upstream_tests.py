from pathlib import Path

from cs4406_sem_project.harness import upstream_tests as ut
from cs4406_sem_project.harness.upstream_tests import Status, Suite, run


def _suites():
    return [
        Suite("ok", ".", ("true",)),
        Suite("bad", ".", ("false",)),
        Suite("gone", "no/such/dir", ("true",)),
        Suite("slow", ".", ("sleep", "30"), timeout_s=1),
    ]


def test_run_reports_each_status_and_writes_logs(tmp_path: Path):
    results = run(_suites(), tmp_path, tmp_path / "logs")
    by_name = {r.suite.name: r for r in results}

    assert by_name["ok"].status is Status.passed and by_name["ok"].exit_code == 0
    assert by_name["bad"].status is Status.failed and by_name["bad"].exit_code == 1
    assert by_name["gone"].status is Status.not_built and by_name["gone"].exit_code is None
    assert by_name["slow"].status is Status.timed_out

    for r in results:
        assert r.log_path.is_file()
        assert "argv:" in r.log_path.read_text()
    summary = (results[0].log_path.parent / "SUMMARY.md").read_text()
    for name in by_name:
        assert name in summary


def test_log_captures_combined_output_and_env(tmp_path: Path):
    suite = Suite("echo", ".", ("sh", "-c", "echo out; echo err >&2; echo $FOO"), {"FOO": "bar"})
    (result,) = run([suite], tmp_path, tmp_path / "logs")
    text = result.log_path.read_text()
    assert result.status is Status.passed
    assert "out" in text and "err" in text and "FOO=bar" in text and "bar\n" in text


def test_missing_script_is_not_built(tmp_path: Path):
    suite = Suite("s", ".", ("bash", "tools/check.sh"))
    (result,) = run([suite], tmp_path, tmp_path / "logs")
    assert result.status is Status.not_built


def test_cli_exit_code(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ut, "SUITES", (Suite("ok", ".", ("true",)),))
    assert ut.main(["--logs-root", str(tmp_path / "a")]) == 0
    monkeypatch.setattr(ut, "SUITES", (Suite("ok", ".", ("true",)), Suite("bad", ".", ("false",))))
    assert ut.main(["--logs-root", str(tmp_path / "b")]) == 1
    assert ut.main(["--only", "ok", "--logs-root", str(tmp_path / "c")]) == 0
