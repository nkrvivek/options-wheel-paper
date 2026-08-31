"""nav_history.jsonl holds one row per session date, not one row per run.

r2_state's module docstring has claimed "one row per session date" since the
migration, and nothing enforced it. Two runs on 2026-08-29 left two rows for
that date in R2. Anything computing a per-row return then counts a day twice,
and the attendance rail's day set hides the duplicate entirely, so the wrong
series is the quiet failure of the two.

The day's newest reading wins. Equity at the second run is a later truth about
the same day than equity at the first.
"""
from __future__ import annotations

import json

from core.r2_state import WheelState

KEY = "state/nav_history.jsonl"


def _store(tmp_path) -> WheelState:
    return WheelState(local_root=tmp_path)


def _rows(store: WheelState) -> list[dict]:
    return store.read_jsonl(KEY)


def test_separate_days_each_keep_their_row(tmp_path):
    store = _store(tmp_path)

    store.append_jsonl(KEY, {"date": "2026-08-28", "equity": 100_000.0}, unique_key="date")
    store.append_jsonl(KEY, {"date": "2026-08-31", "equity": 100_100.0}, unique_key="date")

    assert [r["date"] for r in _rows(store)] == ["2026-08-28", "2026-08-31"]


def test_a_second_run_on_the_same_day_replaces_rather_than_duplicates(tmp_path):
    store = _store(tmp_path)

    store.append_jsonl(KEY, {"date": "2026-08-29", "equity": 100_000.0}, unique_key="date")
    store.append_jsonl(KEY, {"date": "2026-08-29", "equity": 99_500.0}, unique_key="date")

    rows = _rows(store)
    assert len(rows) == 1, "one row per session date"
    assert rows[0]["equity"] == 99_500.0, "the day's newest reading wins"


def test_the_replaced_row_keeps_its_place_in_the_series(tmp_path):
    """Order is the series. A same-day rewrite must not move the day to the end."""
    store = _store(tmp_path)
    for day in ("2026-08-27", "2026-08-28", "2026-08-31"):
        store.append_jsonl(KEY, {"date": day, "equity": 100_000.0}, unique_key="date")

    store.append_jsonl(KEY, {"date": "2026-08-28", "equity": 101_000.0}, unique_key="date")

    rows = _rows(store)
    assert [r["date"] for r in rows] == ["2026-08-27", "2026-08-28", "2026-08-31"]
    assert rows[1]["equity"] == 101_000.0


def test_an_unreadable_existing_line_is_kept_not_dropped(tmp_path):
    """A line that will not parse is somebody's data. Rewriting the file must
    not be how it disappears."""
    store = _store(tmp_path)
    path = tmp_path / KEY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"date": "2026-08-27", "equity": 100000.0}\nnot json at all\n')

    store.append_jsonl(KEY, {"date": "2026-08-31", "equity": 100_000.0}, unique_key="date")

    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert "not json at all" in lines, "an unparseable row survives the rewrite"
    assert len(lines) == 3


def test_without_a_unique_key_the_append_is_unchanged(tmp_path):
    """Other ledgers append genuinely repeated events. Only the caller knows."""
    store = _store(tmp_path)

    store.append_jsonl("state/events.jsonl", {"event": "tick"})
    store.append_jsonl("state/events.jsonl", {"event": "tick"})

    assert len(store.read_jsonl("state/events.jsonl")) == 2


def test_a_record_missing_the_unique_field_is_appended_not_swallowed(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl(KEY, {"date": "2026-08-31", "equity": 100_000.0}, unique_key="date")

    store.append_jsonl(KEY, {"equity": 99_000.0}, unique_key="date")

    rows = _rows(store)
    assert len(rows) == 2, "a row with no date cannot replace one, and is never dropped"


def test_the_daily_run_writes_one_row_per_date():
    """The call site passes the unique key. A helper nobody uses fixes nothing."""
    source = (__import__("pathlib").Path(__file__).parent.parent
              / "scripts" / "run_daily.py").read_text()
    idx = source.rindex("NAV_HISTORY_KEY,")  # the call site, not the import
    assert 'unique_key="date"' in source[idx:idx + 400], (
        "run_daily must append nav history with unique_key=\"date\""
    )


def test_json_round_trip_survives(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl(KEY, {"date": "2026-08-31", "equity": 100_000.0, "cash": 1.5},
                       unique_key="date")
    raw = (tmp_path / KEY).read_text().strip()
    assert json.loads(raw)["cash"] == 1.5
