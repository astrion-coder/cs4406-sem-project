import math

import pandas as pd
import pytest

from cs4406_sem_project.analysis import amortise, frame, synthetic
from cs4406_sem_project.harness import grid

PROFILES = amortise.load_profiles()


def costs(offline_bytes=0, online_bytes=0, **extra):
    base = {"offline_down_bytes": offline_bytes, "offline_up_bytes": None, "up_bytes": online_bytes / 2,
            "down_bytes": online_bytes / 2, "client_offline_s": 0.0, "client_online_s": 0.0,
            "server_offline_s": 0.0, "server_online_s": 0.0}
    return base | extra


def test_latency_e2e_by_hand_on_broadband():
    c = {"query_gen_s": 0.01, "up_bytes": 125_000, "server_online_s": 0.3, "down_bytes": 1_250_000,
         "client_recover_s": 0.002, "client_verify_s": 0.003}
    # 0.01 + 1e6 bit / 20 Mbit/s + 20 ms x 1 round + 0.3 + 1e7 bit / 100 Mbit/s + 0.002 + 0.003
    assert amortise.latency_e2e(c, PROFILES["broadband"]) == pytest.approx(0.01 + 0.05 + 0.02 + 0.3 + 0.1 + 0.005)


def test_fused_verify_adds_nothing_to_latency():
    c = {"query_gen_s": 0.0, "up_bytes": 0, "server_online_s": 0.0, "down_bytes": 0, "client_recover_s": 0.0,
         "client_verify_s": None}
    assert amortise.latency_e2e(c, PROFILES["mobile"]) == pytest.approx(0.060)


def test_amortised_bytes_spreads_the_offline_download():
    c = costs(offline_bytes=1000, online_bytes=10, offline_up_bytes=100)
    assert [amortise.amortised_bytes(c, q) for q in (1, 10, 100)] == [1110, 120, 21]


def test_break_even_by_hand():
    a = costs(offline_bytes=1000, online_bytes=10)  # 1000/Q + 10
    b = costs(offline_bytes=100, online_bytes=20)  # 100/Q + 20; equal at Q = 900/10 = 90
    assert amortise.break_even_Q(a, b) == 90
    assert amortise.break_even_Q(b, a) == 1
    assert amortise.break_even_Q(a, costs(offline_bytes=100, online_bytes=5)) is None


def test_break_even_on_client_time():
    a = costs(client_offline_s=3.0, client_online_s=0.1)
    b = costs(client_offline_s=0.0, client_online_s=0.4)  # 3/Q + 0.1 <= 0.4 from Q = 10
    assert amortise.break_even_Q(a, b, "client_s") == 10


def test_latency_table_has_every_cell_and_profile():
    cells = [c for c in grid.expand(grid.load(), "smoke") if c.phase == "online"]
    df = frame.rows_to_frame(synthetic.rows_for_cells(cells))
    t = amortise.latency_table(df, PROFILES)
    assert len(t) == len(cells) * len(PROFILES)
    assert (t.p95 >= t.p50).all() and (t.n == 3).all()
    by_profile = t.pivot_table(index="arm", columns="profile", values="p50")
    assert (by_profile["mobile"] > by_profile["lan"]).all()


def test_amortised_table_covers_every_q():
    t = amortise.amortised_table(pd.DataFrame([costs(offline_bytes=1e6, online_bytes=10) | {"arm": "S1"}]))
    assert list(t.Q) == list(amortise.QS)
    assert t.bytes.is_monotonic_decreasing and math.isclose(t.bytes.iloc[-1], 110)


def test_total_time_is_client_plus_server_and_has_a_break_even():
    a = costs(client_offline_s=4.0, client_online_s=1.0, server_offline_s=6.0, server_online_s=1.0)
    b = costs(client_online_s=2.0, server_online_s=2.0)
    assert amortise.amortised_total_s(a, 10) == pytest.approx(1.0 + 2.0)
    assert amortise.break_even_Q(a, b, "total_s") == 5
