import pytest

from cs4406_sem_project.harness import hygiene


@pytest.fixture
def on_ac_power(monkeypatch):
    """Stub the machine readings hygiene.Watch takes, so a test passes on battery or with the lid shut.

    The Watch itself still runs, sleep check included. A test that wants a hygiene failure injects its own
    watch, as test_run_grid's BadWatch does.
    """
    monkeypatch.setattr(hygiene, "power_source", lambda: "AC Power")
    monkeypatch.setattr(hygiene, "lid_closed", lambda: False)
