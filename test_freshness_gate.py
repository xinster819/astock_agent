"""TDD tests for the equity freshness gate."""
import datetime as dt

import freshness_gate as fg


def _checks(result):
    return {flag["check"] for flag in result["red_flags"]}


def _row(day, **extra):
    row = {"时间": day, "总资产": "1000000"}
    row.update(extra)
    return row


def test_missing_equity_is_not_fresh():
    result = fg.check({}, [])

    assert result["fresh"] is False
    assert "missing_equity" in _checks(result)


def test_recent_equity_is_fresh_and_current_week_is_present():
    result = fg.check(
        {},
        [_row("2026-07-09 15:00:00")],
        now=dt.datetime(2026, 7, 10, 12),
        review_start=dt.datetime(2026, 7, 6),
        review_end=dt.datetime(2026, 7, 10, 23, 59),
    )

    assert result == {"fresh": True, "red_flags": []}


def test_weekend_does_not_make_fridays_equity_stale():
    result = fg.check(
        {},
        [_row("2026-07-10 15:00:00")],
        now=dt.datetime(2026, 7, 12, 12),
        review_start=dt.datetime(2026, 7, 6),
        review_end=dt.datetime(2026, 7, 12, 23, 59),
    )

    assert result["fresh"] is True
    assert "stale_equity" not in _checks(result)


def test_equity_older_than_trading_day_tolerance_is_stale():
    result = fg.check(
        {},
        [_row("2026-07-06 15:00:00")],
        now=dt.datetime(2026, 7, 10, 12),
        review_start=dt.datetime(2026, 7, 6),
        review_end=dt.datetime(2026, 7, 10, 23, 59),
    )

    assert result["fresh"] is False
    assert "stale_equity" in _checks(result)


def test_no_equity_point_in_review_week_is_flagged():
    result = fg.check(
        {},
        [_row("2026-07-03 15:00:00")],
        now=dt.datetime(2026, 7, 10, 12),
        review_start=dt.datetime(2026, 7, 6),
        review_end=dt.datetime(2026, 7, 10, 23, 59),
    )

    assert result["fresh"] is False
    assert "no_current_week_equity" in _checks(result)


def test_previous_round_must_advance_when_supplied():
    result = fg.check(
        {"round": 7, "previous_round": 7},
        [_row("2026-07-10 15:00:00")],
        now=dt.datetime(2026, 7, 10, 16),
        review_start=dt.datetime(2026, 7, 6),
        review_end=dt.datetime(2026, 7, 10, 23, 59),
    )

    assert result["fresh"] is False
    assert "non_advancing_round" in _checks(result)


def test_previous_round_zero_is_supported_and_round_one_is_fresh():
    result = fg.check(
        {"round": 1, "previous_round": 0},
        [_row("2026-07-10 15:00:00")],
        now=dt.datetime(2026, 7, 10, 16),
        review_start=dt.datetime(2026, 7, 6),
        review_end=dt.datetime(2026, 7, 10, 23, 59),
    )

    assert result["fresh"] is True
