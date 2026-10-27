"""When a market's close is out, offline: saved downloads follow each market's close, not the Sydney date."""
import datetime as dt
import os

import pandas as pd
import pytest

from finclaude import sessions

NEW_YORK, SYDNEY, HONG_KONG = "America/New_York", "Australia/Sydney", "Asia/Hong_Kong"
FRIDAY, MONDAY = dt.date(2026, 10, 16), dt.date(2026, 10, 19)


def at(text, zone=NEW_YORK):
    return pd.Timestamp(text, tz=zone).to_pydatetime()


def saved(folder, written, name="saved.csv"):
    path = folder / name
    path.write_text("x", encoding="utf-8")
    stamp = pd.Timestamp(written).timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("now, market, close", [
    # 2026-10-20 00:28 Sydney = Monday 10-19 09:28 New York: Monday has not closed, Friday's is the latest close
    (at("2026-10-20 00:28", SYDNEY), "US", "2026-10-16"),
    # 2026-10-20 13:15 Sydney = Monday 22:15 New York: Monday's close is out
    (at("2026-10-20 13:15", SYDNEY), "US", "2026-10-19"),
    (at("2026-10-19 16:29"), "US", "2026-10-16"),  # closed at 16:00; providers get half an hour
    (at("2026-10-19 16:30"), "US", "2026-10-19"),
    (at("2026-10-24 12:00"), "US", "2026-10-23"),  # Saturday: Friday's
    (at("2026-10-25 21:00"), "US", "2026-10-23"),  # Sunday night, overnight trading: still Friday's
    # New York leaves daylight saving on 11-01 and Sydney does not: Monday 11-02's close is out 08:30 Sydney Tuesday
    (at("2026-11-03 08:29", SYDNEY), "US", "2026-10-30"),
    (at("2026-11-03 08:30", SYDNEY), "US", "2026-11-02"),
    (at("2026-10-20 10:00", SYDNEY), "ASX", "2026-10-19"),  # ASX trading on Tuesday: Monday's close
    (at("2026-10-20 16:30", SYDNEY), "ASX", "2026-10-20"),
    (at("2026-10-20 16:29", HONG_KONG), "HK", "2026-10-19"),
    (at("2026-10-20 16:30", HONG_KONG), "HK", "2026-10-20"),
])
def test_the_latest_close_follows_each_markets_own_clock(now, market, close):
    assert sessions.last_close(market, now) == dt.date.fromisoformat(close)


def test_the_clock_defaults_to_now(monkeypatch):
    monkeypatch.setattr(sessions, "now", lambda: at("2026-10-20 13:15", SYDNEY))
    assert sessions.last_close("US") == MONDAY


@pytest.mark.parametrize("ticker, market", [("NVDA", "US"), ("^VIX", "US"), ("AUDUSD=X", "US"), ("IVV.AX", "ASX"),
                                            ("ndq.ax", "ASX"), ("0700.HK", "HK")])
def test_each_ticker_follows_its_markets_close(ticker, market):
    assert sessions.market_of(ticker) == market


def test_a_missing_file_is_fetched(tmp_path):
    assert sessions.fetch_again(tmp_path / "none.csv", "US", MONDAY, None, at("2026-10-19 22:15"))


def test_a_file_holding_its_close_is_kept_until_the_next_close(tmp_path):
    path = saved(tmp_path, at("2026-10-19 22:00"))
    assert not sessions.fetch_again(path, "US", MONDAY, MONDAY, at("2026-10-20 09:00"))


def test_a_file_written_before_its_close_was_out_is_fetched_again(tmp_path):
    path = saved(tmp_path, at("2026-10-19 09:28"))
    assert sessions.fetch_again(path, "US", MONDAY, FRIDAY, at("2026-10-19 22:15"))


def test_a_late_provider_is_asked_again_half_an_hour_later(tmp_path):
    path = saved(tmp_path, at("2026-10-19 16:40"))  # after Monday's close was out, still ending on Friday
    assert not sessions.fetch_again(path, "US", MONDAY, FRIDAY, at("2026-10-19 17:05"))
    assert sessions.fetch_again(path, "US", MONDAY, FRIDAY, at("2026-10-19 17:10"))


def test_a_day_still_missing_six_hours_after_its_close_is_taken_as_a_holiday(tmp_path):
    path = saved(tmp_path, at("2026-10-19 22:31"))  # Monday 16:30 + 6 hours has passed
    assert not sessions.fetch_again(path, "US", MONDAY, FRIDAY, at("2026-10-20 09:00"))
    earlier = saved(tmp_path, at("2026-10-19 22:00"), "earlier.csv")
    assert sessions.fetch_again(earlier, "US", MONDAY, FRIDAY, at("2026-10-20 09:00"))
