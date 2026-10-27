"""Treg client, offline: cost gate, token handling, raw files and the call log."""
import json
import os

import pandas as pd
import pytest

from finclaude import data, sessions, treg
from finclaude.config import CostNotApproved, DataUnavailable, NotConfigured

NEW_YORK, SYDNEY = "America/New_York", "Australia/Sydney"


def at(text, zone=NEW_YORK):
    return pd.Timestamp(text, tz=zone).to_pydatetime()


def clock(monkeypatch, when):
    monkeypatch.setattr(sessions, "now", lambda: when)


def written(folder, when):
    """Date every saved file as written at `when`."""
    stamp = pd.Timestamp(when).timestamp()
    for path in folder.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))


TOKEN = "test-token-not-real"
FREE = {"id": "tiingo.daily.prices", "provider": "tiingo", "method": "GET", "cost": {"usd": 0.0}}
PAID = {"id": "financialdatasets.prices", "provider": "financialdatasets", "method": "GET", "cost": {"usd": 0.02}}
OWN_KEY_ONLY = {"id": "polygon.aggs.range", "provider": "polygon", "method": "GET", "cost": {"usd": None}}
TIINGO_ROWS = [
    {"date": "2026-10-01T00:00:00.000Z", "close": 330.0, "adjClose": 329.5, "volume": 100, "adjVolume": 100},
    {"date": "2026-10-02T00:00:00.000Z", "close": 334.0, "adjClose": 333.69, "volume": 120, "adjVolume": 120},
]


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = headers or {}
        self.ok = status < 400

    def json(self):
        return json.loads(self.content)


class FakeTreg:
    """Stands in for treg.to: catalog entries plus one canned answer for /call/."""

    def __init__(self, answer=None):
        self.catalog = {e["id"]: e for e in (FREE, PAID, OWN_KEY_ONLY)}
        self.answer = answer or FakeResponse(200, TIINGO_ROWS, {"X-Treg-Cost-Micro": "0", "X-Treg-Call-Id": "c1"})
        self.calls = []

    def __call__(self, method, url, **kwargs):
        path = url.removeprefix(treg.BASE_URL)
        if path.startswith("/catalog/endpoints/"):
            return FakeResponse(200, {"endpoint": self.catalog[path.rsplit("/", 1)[1]]})
        self.calls.append({"method": method, "path": path, **kwargs})
        return self.answer


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No test here may reach treg.to with the real token from .env."""
    monkeypatch.setenv("TREG_TOKEN", TOKEN)
    monkeypatch.setattr(treg, "RAW_DIR", tmp_path)
    monkeypatch.setattr(treg, "_send", FakeTreg())
    clock(monkeypatch, at("2026-10-17 12:00"))  # a Saturday: Friday 10-16's close is the latest
    treg.endpoint_info.cache_clear()
    yield
    treg.endpoint_info.cache_clear()


def use(monkeypatch, fake):
    monkeypatch.setattr(treg, "_send", fake)
    return fake


def saved_text(tmp_path):
    return "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file())


def test_free_call_sends_token_header_and_a_zero_cost_ceiling(monkeypatch):
    fake = use(monkeypatch, FakeTreg())
    treg.call("tiingo.daily.prices", {"ticker": "aapl"})
    (sent,) = fake.calls
    assert sent["path"] == "/call/tiingo.daily.prices"
    assert sent["headers"]["X-Treg-Token"] == TOKEN
    assert float(sent["headers"]["X-Treg-Route-Max-Cost"]) == 0
    assert TOKEN not in json.dumps(sent["params"])


def test_raw_answer_is_saved_and_the_call_logged_without_the_token(monkeypatch, tmp_path):
    use(monkeypatch, FakeTreg())
    result = treg.call("tiingo.daily.prices", {"ticker": "aapl"})
    assert result.raw_path.read_bytes() == json.dumps(TIINGO_ROWS).encode()
    log = treg.call_log()
    assert log[["endpoint", "provider", "status", "cost_usd", "call_id"]].iloc[0].tolist() == [
        "tiingo.daily.prices", "tiingo", 200, 0.0, "c1"]
    assert TOKEN not in saved_text(tmp_path)


def test_paid_endpoint_is_refused_without_a_confirmed_ceiling(monkeypatch):
    fake = use(monkeypatch, FakeTreg())
    with pytest.raises(CostNotApproved) as refused:
        treg.call("financialdatasets.prices", {"ticker": "AAPL"})
    assert "0.02" in str(refused.value) and "次数" in str(refused.value)
    assert fake.calls == []


def test_paid_endpoint_runs_within_the_confirmed_ceiling_and_logs_the_charge(monkeypatch):
    paid = FakeResponse(200, {"prices": []}, {"X-Treg-Cost-Micro": "20000", "X-Treg-Call-Id": "c2"})
    fake = use(monkeypatch, FakeTreg(paid))
    result = treg.call("financialdatasets.prices", {"ticker": "AAPL"}, max_cost_usd=0.02)
    assert float(fake.calls[0]["headers"]["X-Treg-Route-Max-Cost"]) == 0.02
    assert result.cost_usd == 0.02
    assert treg.call_log()["cost_usd"].sum() == 0.02


def test_own_key_only_endpoint_is_refused_before_any_call(monkeypatch):
    fake = use(monkeypatch, FakeTreg())
    with pytest.raises(DataUnavailable):
        treg.call("polygon.aggs.range", {"ticker": "AAPL"}, max_cost_usd=1.0)
    assert fake.calls == []


def test_quote_lists_endpoint_count_unit_price_and_total():
    table = treg.quote({"financialdatasets.prices": 3, "tiingo.daily.prices": 12})
    assert list(table.columns) == ["端点", "数据商", "次数", "单价（美元）", "合计（美元）"]
    assert table["合计（美元）"].iloc[-1] == 0.06
    assert table["次数"].iloc[:2].tolist() == [3, 12]


def test_same_request_on_the_same_day_reads_the_saved_file(monkeypatch):
    fake = use(monkeypatch, FakeTreg())
    first = treg.call("tiingo.daily.prices", {"ticker": "aapl"})
    second = treg.call("tiingo.daily.prices", {"ticker": "aapl"})
    assert len(fake.calls) == 1
    assert second.cached and second.cost_usd == 0 and second.data == first.data
    assert len(treg.call_log()) == 1


@pytest.mark.parametrize("status, body, error, words", [
    (401, {"detail": "invalid token"}, NotConfigured, "TREG_TOKEN"),
    (402, {"error": "route_max_cost", "balance_micro": 0, "estimated_cost_micro": 22000}, DataUnavailable, "预估本次 0.022"),
    (429, {"error": "trial_allowance_reached"}, DataUnavailable, "免费额度"),
    (404, {"detail": "Error: Ticker 'zzzz' not found"}, DataUnavailable, "404"),
])
def test_refusals_become_clear_errors_without_the_token(monkeypatch, tmp_path, status, body, error, words):
    use(monkeypatch, FakeTreg(FakeResponse(status, body, {"X-Treg-Cost-Micro": "0"})))
    with pytest.raises(error) as raised:
        treg.call("tiingo.daily.prices", {"ticker": "zzzz"})
    assert words in str(raised.value)
    assert TOKEN not in str(raised.value)
    assert treg.call_log()["status"].tolist() == [status]
    assert not list(tmp_path.rglob("tiingo*.json"))


def test_no_token_stops_before_any_request(monkeypatch):
    fake = use(monkeypatch, FakeTreg())
    monkeypatch.delenv("TREG_TOKEN")
    assert not treg.is_configured()
    with pytest.raises(NotConfigured):
        treg.get_eod_history("AAPL")
    assert fake.calls == []


def test_eod_history_asks_for_full_history_and_uses_adjusted_values(monkeypatch):
    fake = use(monkeypatch, FakeTreg())
    df = treg.get_eod_history("AAPL")
    assert fake.calls[0]["params"] == {"ticker": "aapl", "startDate": treg.EOD_START}
    assert list(df.columns) == ["close", "volume"]
    assert df.index.name == "date" and str(df.index[-1].date()) == "2026-10-02"
    assert df["close"].iloc[-1] == 333.69


TIINGO_BARS = [
    {"date": "2026-10-15T00:00:00.000Z", "close": 217.23, "high": 218.4692, "low": 212.85, "open": 215.3,
     "volume": 127087854, "adjClose": 217.23, "adjHigh": 218.4692, "adjLow": 212.85, "adjOpen": 215.3,
     "adjVolume": 127087854},
    {"date": "2026-10-16T00:00:00.000Z", "close": 214.13, "high": 218.676, "low": 213.32, "open": 216.56,
     "volume": 119964163, "adjClose": 214.13, "adjHigh": 218.676, "adjLow": 213.32, "adjOpen": 216.56,
     "adjVolume": 119964163},
]


def test_eod_bars_carry_adjusted_open_high_low_and_the_traded_close(monkeypatch):
    use(monkeypatch, FakeTreg(FakeResponse(200, TIINGO_BARS, {"X-Treg-Cost-Micro": "0", "X-Treg-Call-Id": "c3"})))
    bars = treg.get_eod_bars("NVDA")
    assert list(bars.columns) == ["open", "high", "low", "close", "volume", "raw_close"]
    last = bars.loc["2026-10-16"]
    assert (last["high"], last["low"], last["close"], last["raw_close"]) == (218.676, 213.32, 214.13, 214.13)


def test_bars_and_closes_share_one_call_a_day(monkeypatch):
    fake = use(monkeypatch, FakeTreg(FakeResponse(200, TIINGO_BARS, {"X-Treg-Cost-Micro": "0", "X-Treg-Call-Id": "c4"})))
    treg.get_eod_bars("NVDA")
    assert treg.get_eod_history("NVDA")["close"].iloc[-1] == 214.13
    assert len(fake.calls) == 1


MONDAY_BAR = {"date": "2026-10-19T00:00:00.000Z", "close": 220.0, "high": 221.0, "low": 214.0, "open": 215.0,
              "volume": 1, "adjClose": 220.0, "adjHigh": 221.0, "adjLow": 214.0, "adjOpen": 215.0, "adjVolume": 1}


def tiingo(rows, call_id="c5"):
    return FakeResponse(200, rows, {"X-Treg-Cost-Micro": "0", "X-Treg-Call-Id": call_id})


def test_daily_bars_are_kept_per_us_close_not_per_sydney_date(monkeypatch, tmp_path):
    # 2026-10-20: 00:28 Sydney (Monday 09:28 New York) saved bars ending Friday; at 13:15 Sydney Monday's close is out
    fake = use(monkeypatch, FakeTreg(tiingo(TIINGO_BARS)))
    clock(monkeypatch, at("2026-10-20 00:28", SYDNEY))
    treg.get_eod_bars("NVDA")
    written(tmp_path, at("2026-10-20 00:28", SYDNEY))
    clock(monkeypatch, at("2026-10-20 01:30", SYDNEY))  # Monday still trading: Friday's close, the same file
    treg.get_eod_bars("NVDA")
    assert len(fake.calls) == 1
    fake.answer = tiingo(TIINGO_BARS + [MONDAY_BAR])
    clock(monkeypatch, at("2026-10-20 13:15", SYDNEY))
    bars = treg.get_eod_bars("NVDA")
    assert len(fake.calls) == 2 and str(bars.index[-1].date()) == "2026-10-19"
    saved = sorted(p.name.rsplit("_", 1)[-1] for p in (tmp_path / "treg").glob("*.json"))
    assert saved == ["close2026-10-16.json", "close2026-10-19.json"]


def test_a_late_tiingo_day_is_asked_again_half_an_hour_later(monkeypatch, tmp_path):
    fake = use(monkeypatch, FakeTreg(tiingo(TIINGO_BARS)))
    clock(monkeypatch, at("2026-10-19 16:40"))  # Monday's close is out, Tiingo still ends on Friday
    treg.get_eod_bars("NVDA")
    written(tmp_path, at("2026-10-19 16:40"))
    clock(monkeypatch, at("2026-10-19 17:00"))
    treg.get_eod_bars("NVDA")
    assert len(fake.calls) == 1  # asked 20 minutes ago: wait
    fake.answer = tiingo(TIINGO_BARS + [MONDAY_BAR])
    clock(monkeypatch, at("2026-10-19 17:15"))
    assert str(treg.get_eod_bars("NVDA").index[-1].date()) == "2026-10-19" and len(fake.calls) == 2


def test_a_day_still_missing_six_hours_after_the_close_is_not_asked_again(monkeypatch, tmp_path):
    fake = use(monkeypatch, FakeTreg(tiingo(TIINGO_BARS)))  # a market holiday on Monday would look like this
    clock(monkeypatch, at("2026-10-19 23:00"))
    treg.get_eod_bars("NVDA")
    written(tmp_path, at("2026-10-19 23:00"))
    clock(monkeypatch, at("2026-10-20 09:00"))
    treg.get_eod_bars("NVDA")
    assert len(fake.calls) == 1


def test_a_failed_second_try_keeps_the_bars_saved_for_that_close(monkeypatch, tmp_path):
    fake = use(monkeypatch, FakeTreg(tiingo(TIINGO_BARS)))
    clock(monkeypatch, at("2026-10-19 16:40"))
    treg.get_eod_bars("NVDA")
    written(tmp_path, at("2026-10-19 16:40"))
    fake.answer = FakeResponse(429, {"error": "trial_allowance exhausted"}, {"X-Treg-Call-Id": "c6"})
    clock(monkeypatch, at("2026-10-19 17:15"))
    assert str(treg.get_eod_bars("NVDA").index[-1].date()) == "2026-10-16" and len(fake.calls) == 2


def test_other_endpoints_keep_one_answer_a_day(monkeypatch):
    paid = FakeResponse(200, {"prices": []}, {"X-Treg-Cost-Micro": "20000", "X-Treg-Call-Id": "c7"})
    fake = use(monkeypatch, FakeTreg(paid))
    clock(monkeypatch, at("2026-10-19 09:28"))
    treg.call("financialdatasets.prices", {"ticker": "AAPL"}, max_cost_usd=0.02)
    clock(monkeypatch, at("2026-10-19 22:15"))  # a new US close never repeats a paid call
    treg.call("financialdatasets.prices", {"ticker": "AAPL"}, max_cost_usd=0.02)
    assert len(fake.calls) == 1 and treg.call_log()["cost_usd"].sum() == 0.02


def test_bars_without_high_and_low_raise_instead_of_guessing(monkeypatch):
    use(monkeypatch, FakeTreg())  # TIINGO_ROWS: closes and volumes only
    with pytest.raises(DataUnavailable):
        treg.get_eod_bars("AAPL")


def test_get_bars_routes_us_tickers_to_treg_and_others_to_yfinance(monkeypatch):
    index = pd.DatetimeIndex(["2026-10-15", "2026-10-16"], name="date")
    bars = pd.DataFrame({"open": [215.3, 216.56], "high": [218.47, 218.68], "low": [212.85, 213.32],
                         "close": [217.23, 214.13], "volume": [127087854, 119964163], "raw_close": [217.23, 214.13]},
                        index=index)
    monkeypatch.setattr(treg, "get_eod_bars", lambda ticker: bars)
    monkeypatch.setattr(data, "_yfinance_bars", lambda ticker: bars.drop(columns="raw_close"))
    us = data.get_bars("NVDA")
    assert (us["source"] == "Treg · Tiingo").all() and us["high"].iloc[-1] == 218.68
    hk = data.get_bars("0700.HK")
    assert (hk["source"] == "yfinance").all() and hk["raw_close"].isna().all()


def test_eod_history_with_no_rows_raises_instead_of_returning_empty(monkeypatch):
    use(monkeypatch, FakeTreg(FakeResponse(200, [], {"X-Treg-Cost-Micro": "0"})))
    with pytest.raises(DataUnavailable):
        treg.get_eod_history("AAPL")


def test_get_prices_routes_us_tickers_to_treg_and_names_the_provider(monkeypatch):
    history = pd.DataFrame({"close": [329.5, 333.69], "volume": [100, 120]},
                           index=pd.DatetimeIndex(["2026-10-01", "2026-10-02"], name="date"))
    monkeypatch.setattr(treg, "get_eod_history", lambda ticker: history)
    monkeypatch.setattr(data, "_yfinance_history", lambda ticker: history)
    assert (data.get_prices("AAPL")["source"] == "Treg · Tiingo").all()
    assert (data.get_prices("0700.HK")["source"] == "yfinance").all()


def test_asx_tickers_go_to_yfinance_not_treg(monkeypatch):
    history = pd.DataFrame({"close": [74.2], "volume": [1000]}, index=pd.DatetimeIndex(["2026-10-06"], name="date"))
    monkeypatch.setattr(treg, "get_eod_history", lambda ticker: pytest.fail("Treg has no ASX data"))
    monkeypatch.setattr(data, "_yfinance_history", lambda ticker: history)
    assert (data.get_prices("IVV.AX")["source"] == "yfinance").all()


@pytest.mark.parametrize("code, ticker", [("US.AAPL", "AAPL"), ("AU.IVV", "IVV.AX"), ("AU.NDQ", "NDQ.AX"),
                                          ("HK.00700", "0700.HK"), ("HK.09988", "9988.HK"), ("HK.02800", "2800.HK")])
def test_moomoo_codes_map_to_data_tickers(code, ticker):
    assert data.ticker_from_code(code) == ticker


def test_amounts_convert_to_aud_by_currency(monkeypatch):
    rates = {"AUDUSD=X": 0.7, "AUDHKD=X": 5.0}
    monkeypatch.setattr(data, "get_fx", lambda pair, start=None, end=None: pd.Series([rates[pair]]))
    assert data.to_aud(70.0, "USD") == pytest.approx(100.0)
    assert data.to_aud(50.0, "HKD") == pytest.approx(10.0)
    assert data.to_aud(816.2, "AUD") == 816.2
    assert data.currency_of("IVV.AX") == "AUD" and data.currency_of("0700.HK") == "HKD" and data.currency_of("NVDA") == "USD"


def test_one_day_spikes_are_dropped_but_real_moves_stay():
    # yfinance IVV.AX: 16.255 -> 241.509 -> 16.146 around 2015-12-28 (an unadjusted tick), 2026-10-06.
    idx = pd.bdate_range("2015-12-21", periods=8, name="date")
    close = pd.Series([16.0, 16.05, 16.255, 241.509, 16.146, 16.231, 14.2, 14.1], index=idx)
    clean, dropped = data.drop_bad_ticks(close)
    assert dropped == [idx[3]]
    assert clean.max() < 17 and len(clean) == 7
    assert 14.2 in clean.values  # a real -12.5% day is kept


def test_a_lasting_jump_is_not_a_bad_tick():
    idx = pd.bdate_range("2020-01-01", periods=5, name="date")
    close = pd.Series([10.0, 10.0, 20.0, 20.5, 21.0], index=idx)
    clean, dropped = data.drop_bad_ticks(close)
    assert dropped == [] and len(clean) == 5


def test_yfinance_prices_come_back_without_bad_ticks(monkeypatch):
    idx = pd.bdate_range("2015-12-21", periods=5, name="date")
    raw = pd.DataFrame({"close": [16.0, 16.255, 241.509, 16.146, 16.231], "volume": [1, 1, 1, 1, 1]}, index=idx)
    monkeypatch.setattr(data, "_yfinance_history", lambda ticker: raw)
    prices = data.get_prices("IVV.AX")
    assert prices["close"].max() < 17
    assert prices.attrs["dropped_ticks"] == [str(idx[2].date())]


def test_lasting_breaks_are_flagged_not_changed(monkeypatch):
    # yfinance IVV.AX: 99.602 on 2010-12-31, 6.730 on 2011-01-04 and after (about 14.8x, an unadjusted split).
    idx = pd.bdate_range("2010-12-29", periods=6, name="date")
    raw = pd.DataFrame({"close": [100.3, 99.5, 99.6, 6.73, 6.78, 6.85], "volume": [1] * 6}, index=idx)
    monkeypatch.setattr(data, "_yfinance_history", lambda ticker: raw)
    prices = data.get_prices("IVV.AX")
    assert prices.attrs["level_breaks"] == [str(idx[3].date())]
    assert len(prices) == 6  # flagged for the caller, never rescaled here


def test_yield_series_are_not_filtered(monkeypatch):
    idx = pd.bdate_range("2015-01-01", periods=4, name="date")
    raw = pd.DataFrame({"close": [0.02, 0.05, 0.02, 0.03], "volume": [0] * 4}, index=idx)
    monkeypatch.setattr(data, "_yfinance_history", lambda ticker: raw)
    prices = data.get_prices("^IRX")
    assert len(prices) == 4 and prices.attrs["dropped_ticks"] == []
