import json
import subprocess
import sys

from cs4406_sem_project.harness.sysinfo import platform_id, snapshot


def test_platform_id_format():
    snap = {"machine": {"arch": "arm64", "cpu_model": "Apple M1 Pro"}}
    assert platform_id(snap) == "arm64-darwin-Apple M1 Pro"


def test_snapshot_machine_fields():
    snap = snapshot()
    m = snap["machine"]
    assert m["arch"] == "arm64"
    assert isinstance(m["cpu_model"], str) and m["cpu_model"]
    for key in ("physical_cpus", "performance_cores", "efficiency_cores", "mem_total_bytes"):
        assert isinstance(m[key], int) and m[key] > 0
    assert m["performance_cores"] + m["efficiency_cores"] == m["physical_cpus"]
    json.dumps(snap)


def test_cli_writes_both_files(tmp_path):
    js = tmp_path / "sub" / "sysinfo.json"
    pf = tmp_path / "other" / "reference_platform.txt"
    subprocess.run(
        [sys.executable, "-m", "cs4406_sem_project.harness.sysinfo", "--json", str(js), "--platform-file", str(pf)],
        check=True,
    )
    assert json.loads(js.read_text())["machine"]["arch"] == "arm64"
    assert pf.read_text() == platform_id() + "\n"
