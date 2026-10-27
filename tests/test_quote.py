"""Moomoo quotes, offline: quote rights without personal fields, snapshots and option volatility; quotes only."""
import pandas as pd
import pytest
from moomoo import RET_ERROR, RET_OK

from finclaude import account, quote
from finclaude.config import DataUnavailable, NotConfigured

SNAPSHOTS = {
    "US.NVDA": {"code": "US.NVDA", "update_time": "2026-10-16 20:01:20.696", "last_price": 214.13,
                "prev_close_price": 217.23, "volume": 121252522, "after_price": 213.7494,
                "highest52weeks_price": 243.37, "lowest52weeks_price": 163.895372, "pe_ttm_ratio": 27.07,
                "total_market_val": 5.160533e12, "option_implied_volatility": 0.0},
    "US.NVDA261030C215000": {"code": "US.NVDA261030C215000", "update_time": "2026-10-16 15:59:52", "last_price": 5.90,
                             "option_implied_volatility": 36.954},
    "US.NVDA261030P215000": {"code": "US.NVDA261030P215000", "update_time": "2026-10-16 15:59:39", "last_price": 6.46,
                             "option_implied_volatility": 37.164},
}


class FakeQuotes:
    closed = 0

    def __init__(self, host, port):
        self.host, self.port = host, port

    def get_user_info(self, info_field):
        return RET_OK, {"nick_name": "someone", "user_id": 123456, "web_key": "secret-web-key",
                        "us_qot_right": "LV3", "us_option_qot_right": "LV1", "hk_qot_right": "LV1"}

    def get_market_snapshot(self, codes):
        return RET_OK, pd.DataFrame([SNAPSHOTS[c] for c in codes])

    def get_option_chain(self, code, start=None, end=None):
        return RET_OK, pd.DataFrame({"code": ["US.NVDA261030C215000", "US.NVDA261030P215000", "US.NVDA261030C220000"],
                                     "option_type": ["CALL", "PUT", "CALL"], "strike_price": [215.0, 215.0, 220.0],
                                     "strike_time": [end] * 3})

    def close(self):
        FakeQuotes.closed += 1


@pytest.fixture(autouse=True)
def fake_opend(monkeypatch):
    FakeQuotes.closed = 0
    monkeypatch.setattr(quote, "OpenQuoteContext", FakeQuotes)
    monkeypatch.setattr(account, "_opend_reachable", lambda host, port: True)


def test_quote_rights_keep_only_the_rights():
    rights = quote.quote_rights()
    assert rights == {"us_qot_right": "LV3", "us_option_qot_right": "LV1", "hk_qot_right": "LV1"}
    assert "secret" not in str(rights) and "123456" not in str(rights) and "someone" not in str(rights)
    assert FakeQuotes.closed == 1


def test_snapshot_returns_the_last_price_with_its_eastern_time_stamp():
    snap = quote.snapshot(["US.NVDA"])
    row = snap.iloc[0]
    assert row["last_price"] == 214.13 and row["update_time"].startswith("2026-10-16 20:01")
    assert row["highest52weeks_price"] == 243.37 and row["pe_ttm_ratio"] == 27.07
    assert FakeQuotes.closed == 1


def test_option_volatility_comes_back_as_decimals_for_call_and_put():
    ivs = quote.option_ivs("US.NVDA", "2026-10-30", 215.0)
    assert ivs["call"]["iv"] == pytest.approx(0.36954) and ivs["put"]["iv"] == pytest.approx(0.37164)
    assert ivs["iv"] == pytest.approx((0.36954 + 0.37164) / 2)
    assert ivs["call"]["update_time"] == "2026-10-16 15:59:52"
    assert ivs["call"]["code"] == "US.NVDA261030C215000"


def test_missing_strike_is_data_unavailable():
    with pytest.raises(DataUnavailable):
        quote.option_ivs("US.NVDA", "2026-10-30", 300.0)


def test_closed_opend_stops_at_once(monkeypatch):
    monkeypatch.setattr(account, "_opend_reachable", lambda host, port: False)
    with pytest.raises(NotConfigured):
        quote.snapshot(["US.NVDA"])


def test_refusals_become_data_unavailable_and_the_context_is_closed(monkeypatch):
    class Refuses(FakeQuotes):
        def get_market_snapshot(self, codes):
            return RET_ERROR, "no quote right"

    monkeypatch.setattr(quote, "OpenQuoteContext", Refuses)
    with pytest.raises(DataUnavailable) as raised:
        quote.snapshot(["US.NVDA"])
    assert "no quote right" in str(raised.value)
    assert FakeQuotes.closed == 1


# --- extended hours: the price trading right now -----------------------------------------------------------

@pytest.mark.parametrize("new_york, session", [
    ("2026-10-19 01:54", "overnight"),  # Sunday night into Monday
    ("2026-10-18 20:30", "overnight"),  # Sunday 20:30
    ("2026-10-19 07:00", "pre"),
    ("2026-10-19 10:00", "regular"),
    ("2026-10-19 17:00", "after"),
    ("2026-10-23 21:00", "closed"),  # Friday night: no overnight session into Saturday
    ("2026-10-24 12:00", "closed"),  # Saturday
])
def test_us_session_follows_the_new_york_clock(new_york, session):
    now = pd.Timestamp(new_york, tz="America/New_York").tz_convert("Australia/Sydney").to_pydatetime()
    assert quote.us_session(now) == session


def test_extended_hours_takes_the_price_of_the_session_trading_now(monkeypatch):
    rows = {"US.NVDA": {"code": "US.NVDA", "update_time": "2026-10-19 01:54:15.114", "last_price": 214.13,
                        "prev_close_price": 217.23, "pre_price": 216.46, "pre_change_rate": -0.354,
                        "after_price": 213.7494, "after_change_rate": -0.177, "overnight_price": 217.85,
                        "overnight_change_rate": 1.737}}

    class Live(FakeQuotes):
        def get_market_snapshot(self, codes):
            return RET_OK, pd.DataFrame([rows[c] for c in codes])

    monkeypatch.setattr(quote, "OpenQuoteContext", Live)
    now = pd.Timestamp("2026-10-19 01:54", tz="America/New_York").to_pydatetime()
    table = quote.extended_hours(["US.NVDA"], now=now)
    row = table.iloc[0]
    assert (row["session"], row["price"]) == ("夜盘", 217.85)
    assert row["change"] == pytest.approx(0.01737) and row["update_time"].startswith("2026-10-19 01:54")
    weekend = pd.Timestamp("2026-10-24 12:00", tz="America/New_York").to_pydatetime()
    closed = quote.extended_hours(["US.NVDA"], now=weekend).iloc[0]
    assert (closed["session"], closed["price"]) == ("休市", 214.13) and "收盘" in closed["note"]
    # Monday 07:00 before any pre-market trade: the snapshot still holds Friday's pre-market price, whose change is
    # measured against Thursday's close (217.23), not the last close (214.13); it is dropped, not shown as today's
    monday = pd.Timestamp("2026-10-19 07:00", tz="America/New_York").to_pydatetime()
    stale = quote.extended_hours(["US.NVDA"], now=monday).iloc[0]
    assert stale["session"] == "盘前" and pd.isna(stale["price"]) and pd.isna(stale["change"])
    assert "还没有成交" in stale["note"]
