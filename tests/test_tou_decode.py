"""Decoding for scripts/read_tou_schedule.py.

A time decoder that renders a wrong convention plausibly is worse than one that
fails: "06:00" is believable whichever way you got there. These tests pin the
convention (WIT VPP stores plain minutes since midnight, NOT the hex-packed
hours*256+minutes some other Growatt families use) and prove that a value which
cannot be minutes is refused rather than rendered.

Real values from the reference inverter, 2026-09-03, are used throughout.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "read_tou_schedule.py"
_spec = importlib.util.spec_from_file_location("read_tou_schedule", _PATH)
tou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tou)


def test_the_register_block_matches_the_documented_layout():
    assert tou.TOU_BASE == 30412
    assert tou.TOU_LAST == 30471                 # 20 periods x 3 registers
    assert tou.TOU_MAX_PERIODS == 20


@pytest.mark.parametrize("raw,expected", [
    (0, "00:00"),
    (1, "00:01"),
    (60, "01:00"),
    (541, "09:01"),
    (715, "11:55"),
    (1261, "21:01"),
    (1439, "23:59"),
    (1440, "24:00"),        # the legal end-of-day value for a period end
])
def test_minutes_since_midnight_render_as_hhmm(raw, expected):
    assert tou.as_hhmm(raw) == expected


@pytest.mark.parametrize("raw", [1441, 1536, 5632, 65535, -1])
def test_a_value_that_cannot_be_minutes_is_refused_not_rendered(raw):
    """1536 is 0x0600 — "06:00" under the OTHER convention. Never guess."""
    assert tou.as_hhmm(raw) is None


def test_the_alternative_convention_is_offered_only_as_a_diagnosis():
    assert tou.as_packed_hhmm(1536) == "06:00 if hex-packed"
    assert tou.as_packed_hhmm(5632) == "22:00 if hex-packed"
    assert "not a packed time" in tou.as_packed_hhmm(65535)


@pytest.mark.parametrize("raw,expected", [
    (50, 50), (100, 100), (1, 1), (0, 0),
    (65531, -5), (65521, -15), (65436, -100),
])
def test_period_power_is_two_s_complement_signed(raw, expected):
    assert tou.decode_signed(raw) == expected


def registers(pairs, declared):
    values = {30411: declared}
    for index, (start, end, power) in enumerate(pairs):
        base = tou.TOU_BASE + index * 3
        values[base], values[base + 1], values[base + 2] = start, end, power
    return values


def test_periods_decode_a_real_schedule():
    """Periods 10 and 11 as actually read from the inverter."""
    decoded = tou.periods(registers(
        [(1, 60, 50), (541, 600, 65531), (601, 660, 65436)], declared=3))

    assert decoded[0]["start"] == "00:01"
    assert decoded[0]["end"] == "01:00"
    assert decoded[0]["power_pct"] == 50

    assert decoded[1]["power_pct"] == -5
    assert decoded[1]["power_raw"] == 65531        # raw is preserved too

    assert decoded[2]["start"] == "10:01"
    assert decoded[2]["power_pct"] == -100


def test_all_twenty_slots_are_decoded_regardless_of_the_declared_count():
    """30411 says how many are active; it does not limit what can be read."""
    decoded = tou.periods(registers([(1, 60, 50)] * 20, declared=16))

    assert len(decoded) == 20
    assert [p["period"] for p in decoded] == list(range(1, 21))
    assert sum(1 for p in decoded if p["active"]) == 16
    assert decoded[15]["active"] is True
    assert decoded[16]["active"] is False


def test_an_unreadable_block_decodes_to_nulls_not_to_zeros():
    decoded = tou.periods({30411: 2})

    assert decoded[0]["start"] is None
    assert decoded[0]["power_pct"] is None


def test_power_is_described_with_its_direction():
    assert "charge" in tou.describe_power(50)
    assert "discharge" in tou.describe_power(-100)
    # 0 is documented as "suspend forced cycle", which is why HOLD is +1%.
    assert "suspend" in tou.describe_power(0)


def snapshot(values, taken_at="2026-09-03T21:08:07+03:00"):
    return {"taken_at": taken_at, "device_id": "dev",
            "registers": {str(k): v for k, v in values.items()}}


def test_diff_reports_no_change_for_identical_snapshots(capsys):
    snap = snapshot(registers([(1, 60, 50)], declared=1))

    assert tou.diff(snap, snap) == 0

    out = capsys.readouterr().out
    assert "no register changed" in out
    assert "no period changed" in out


def test_diff_names_the_period_and_field_behind_a_changed_register(capsys):
    """A bare "30442 changed" is useless when watching 60 registers."""
    before = snapshot(registers([(1, 60, 50), (601, 660, 65436)], declared=2))
    after = snapshot(registers([(1, 60, 50), (601, 660, 50)], declared=2))

    assert tou.diff(before, after) == 1

    out = capsys.readouterr().out
    assert "period 2 power" in out
    assert "65436 -> 50" in out
    assert "-100% discharge" in out       # the before summary
    assert "+50% charge" in out           # the after summary


def test_diff_sees_a_period_count_change(capsys):
    """15 -> 10 -> 16 on the reference unit: the thing this tool exists for."""
    before = snapshot(registers([(1, 60, 50)] * 3, declared=2))
    after = snapshot(registers([(1, 60, 50)] * 3, declared=3))

    assert tou.diff(before, after) == 1

    out = capsys.readouterr().out
    assert "tou_num_periods" in out
    assert "2 -> 3" in out
    assert "period  3" in out             # flipped inactive -> active
