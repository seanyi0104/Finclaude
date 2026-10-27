"""Single-stock analysis (decision side v1), offline: indicators, signal states, each signal's own history, option
ranges, fundamentals, the report, the Jev request and the forecast log. About the stock only: no account figures."""
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from finclaude import analysis, forecast, glossary, render

MADE_ON = dt.date(2026, 10, 17)


def bdays(n, end="2026-10-16"):
    return pd.bdate_range(end=end, periods=n, name="date")


def series(values, end="2026-10-16"):
    return pd.Series(values, index=bdays(len(values), end), dtype=float)


def walk(n=1500, seed=7, drift=0.0006, vol=0.025):
    rng = np.random.default_rng(seed)
    return series(100 * np.exp(np.cumsum(rng.normal(drift, vol, n))))


def bars_from(close, spread=0.015, seed=3):
    rng = np.random.default_rng(seed)
    volume = rng.integers(80_000_000, 200_000_000, len(close)).astype(float)
    return pd.DataFrame({"open": close.shift(1).fillna(close), "high": close * (1 + spread), "low": close * (1 - spread),
                         "close": close, "volume": volume, "raw_close": close, "source": "Treg · Tiingo"},
                        index=close.index)


# --- indicators --------------------------------------------------------------------------------------------

def test_moving_averages_are_plain_means_of_the_last_n_closes():
    close = series(np.arange(1, 251))
    ma = analysis.moving_averages(close)
    assert list(ma.columns) == [5, 10, 20, 50, 200]
    assert ma[5].iloc[-1] == pytest.approx(np.mean(np.arange(246, 251)))
    assert ma[200].iloc[-1] == pytest.approx(np.mean(np.arange(51, 251)))
    assert ma[200].iloc[:199].isna().all()


def test_macd_is_the_gap_between_12_and_26_day_exponential_averages():
    close = walk(300)
    m = analysis.macd(close)
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    assert m["macd"].iloc[-1] == pytest.approx(line.iloc[-1])
    assert m["signal"].iloc[-1] == pytest.approx(line.ewm(span=9, adjust=False).mean().iloc[-1])
    assert m["hist"].iloc[-1] == pytest.approx(m["macd"].iloc[-1] - m["signal"].iloc[-1])
    assert analysis.macd(series(np.linspace(50, 100, 200)))["macd"].iloc[-1] > 0
    assert analysis.macd(series([50.0] * 100))["hist"].abs().max() == pytest.approx(0)


def test_atr_uses_the_true_range_and_wilder_smoothing():
    idx = bdays(5)
    bars = pd.DataFrame({"open": [10, 10, 13, 12, 12], "high": [11, 11, 14, 13, 13], "low": [9, 10, 12, 11, 12],
                         "close": [10, 10.5, 13.5, 12, 12.5], "volume": 1.0}, index=idx, dtype=float)
    # true range from the second day: max(high − low, |high − previous close|, |low − previous close|)
    # day 2: max(1, 1, 0) = 1; day 3 opens with a gap: max(2, 3.5, 1.5) = 3.5; day 4: max(2, 0.5, 2.5) = 2.5
    atr = analysis.atr(bars, n=2)
    assert atr.index[0] == idx[2]
    assert atr.iloc[0] == pytest.approx((1 + 3.5) / 2)  # the first value is the plain mean of n true ranges
    assert atr.iloc[1] == pytest.approx(((1 + 3.5) / 2 * 1 + 2.5) / 2)  # then (previous × (n − 1) + today) ÷ n


def test_volume_ratio_compares_today_with_the_previous_20_days():
    volume = series([100.0] * 20 + [300.0])
    assert analysis.volume_ratio(volume).iloc[-1] == pytest.approx(3.0)


def test_price_levels_give_the_highest_high_and_lowest_low_with_their_dates():
    close = series(np.concatenate([np.linspace(100, 150, 200), np.linspace(150, 120, 60)]))
    bars = bars_from(close, spread=0.01)
    levels = analysis.price_levels(bars, {"近 20 个交易日": 20, "近半年": 126})
    recent = bars.iloc[-20:]
    assert levels["近 20 个交易日"]["high"] == pytest.approx(recent["high"].max())
    assert levels["近 20 个交易日"]["high_date"] == str(recent["high"].idxmax().date())
    half = levels["近半年"]
    assert half["high"] == pytest.approx(150 * 1.01) and half["low_date"] == str(bars.iloc[-126:]["low"].idxmin().date())
    assert half["below_high"] == pytest.approx(120 / (150 * 1.01) - 1)


@pytest.mark.parametrize("close, ma, long, short", [
    (110, {5: 108, 10: 105, 20: 100, 50: 95, 200: 80}, "上升", "向上"),
    (95, {5: 97, 10: 100, 20: 104, 50: 90, 200: 80}, "上升", "向下"),
    (70, {5: 72, 10: 75, 20: 78, 50: 85, 200: 90}, "下降", "向下"),
    (92, {5: 92, 10: 91, 20: 93, 50: 85, 200: 90}, "转换中", "震荡"),
    # NVDA on 2026-10-16: below MA10 and MA20 while MA10 is still a hair above MA20
    (214.13, {5: 221.50, 10: 228.29, 20: 228.18, 50: 222.96, 200: 202.65}, "上升", "转弱"),
    (105, {5: 104, 10: 100, 20: 102, 50: 95, 200: 90}, "上升", "转强"),
])
def test_trend_reads_the_long_and_short_averages(close, ma, long, short):
    trend = analysis.trend_state(close, ma)
    assert (trend["long"], trend["short"]) == (long, short)


def test_signal_states_on_a_steady_rise():
    close = series(np.linspace(50, 150, 400))
    benchmarks = {"SMH": series(np.linspace(50, 100, 400)), "SPY": series([100.0] * 400)}
    frame = analysis.signal_frame(bars_from(close), benchmarks)
    assert set(frame.columns) == set(analysis.STATES)
    today = frame.iloc[-1]
    assert today["close_ma200"] == "above" and today["ma50_ma200"] == "above" and today["alignment"] == "bull"
    assert today["rsi"] == "overbought" and today["macd_zero"] == "above" and today["from_high"] == "near"
    assert today["rs_sector"] == "ahead" and today["rs_market"] == "ahead"
    assert frame["close_ma200"].iloc[:199].isna().all()  # no state before the average exists


# --- each signal's own history -----------------------------------------------------------------------------

def test_forward_returns_look_ahead_the_given_number_of_trading_days():
    fwd = analysis.forward_returns(series([100, 110, 121, 133.1]), 2)
    assert fwd.iloc[0] == pytest.approx(0.21) and fwd.iloc[-2:].isna().all()


def test_independent_windows_do_not_overlap():
    assert analysis.independent_count(np.arange(26), 10) == 3  # days 0, 10, 20
    assert analysis.independent_count(np.array([0, 3, 9, 30, 35]), 10) == 2  # days 0 and 30
    assert analysis.independent_count(np.array([], dtype=int), 10) == 0


def test_a_signal_seen_too_rarely_is_insufficient_evidence():
    idx = bdays(1000)
    fwd = pd.Series(np.where(np.arange(1000) % 2 == 0, 0.01, -0.01), index=idx)
    states = pd.Series("off", index=idx)
    states.iloc[100:150] = "on"  # 50 days in a row: only 5 windows of 10 days that do not overlap
    ev = analysis.evidence(states, fwd, "on", 10)
    assert ev["days"] == 50 and ev["independent"] == 5
    assert ev["verdict"] == "证据不足"


def test_a_signal_no_different_from_any_day_is_called_useless():
    rng = np.random.default_rng(11)
    idx = bdays(3000)
    fwd = pd.Series(rng.normal(0.005, 0.05, 3000), index=idx)
    states = pd.Series(np.where(rng.random(3000) < 0.5, "on", "off"), index=idx)
    ev = analysis.evidence(states, fwd, "on", 10)
    assert ev["independent"] >= analysis.MIN_INDEPENDENT
    assert abs(ev["z"]) < 2 and ev["verdict"] == "没用"


def test_a_signal_that_changed_the_odds_says_which_way():
    rng = np.random.default_rng(12)
    idx = bdays(3000)
    states = pd.Series(np.where(rng.random(3000) < 0.3, "on", "off"), index=idx)
    fwd = pd.Series(np.where(states == "on", 0.02, rng.normal(0, 0.05, 3000)), index=idx)  # after "on" it always rose
    ev = analysis.evidence(states, fwd, "on", 10)
    assert ev["p_up"] == 1.0 and ev["base_p_up"] < 1.0
    assert ev["z"] >= 3 and ev["verdict"] == "偏涨"
    p0 = ev["base_p_up"]
    assert ev["noise"] == pytest.approx(2 * math.sqrt(p0 * (1 - p0) / ev["independent"]))


def test_key_signals_are_the_largest_differences_with_enough_evidence():
    table = {"2w": [{"signal": "a", "horizon": "2w", "z": 0.5, "verdict": "没用"},
                    {"signal": "b", "horizon": "2w", "z": -3.4, "verdict": "偏跌"},
                    {"signal": "c", "horizon": "2w", "z": 5.0, "verdict": "证据不足"}],
             "6m": [{"signal": "a", "horizon": "6m", "z": 2.2, "verdict": "弱偏涨"},
                    {"signal": "d", "horizon": "6m", "z": None, "verdict": "无可靠数据"}]}
    picked = analysis.key_signals(table)
    assert [(r["signal"], r["horizon"]) for r in picked] == [("b", "2w"), ("a", "6m")]


# --- options and fundamentals ------------------------------------------------------------------------------

def test_event_move_isolates_the_earnings_day_from_normal_days():
    base, event, days = 0.35, 0.05, 35
    iv_after = math.sqrt((base ** 2 * days / 365 + event ** 2) / (days / 365))
    assert analysis.event_move(base, iv_after, days) == pytest.approx(0.05)
    assert analysis.event_move(0.40, 0.38, days) is None  # no extra volatility priced for the event


@pytest.mark.parametrize("end, label", [
    ("2026-07-31", "FY2027 Q2（2026 年 5–7 月）"),
    ("2026-10-31", "FY2027 Q3（2026 年 8–10 月）"),
    ("2026-01-31", "FY2026 Q4（2025 年 11 月–2026 年 1 月）"),
])
def test_fiscal_labels_follow_a_january_year_end(end, label):
    assert analysis.fiscal_label(pd.Timestamp(end), 1) == label


def fake_raw():
    quarters = pd.to_datetime(["2026-07-31", "2026-04-30", "2026-01-31", "2025-10-31", "2025-07-31"])
    quarterly = pd.DataFrame([[96.221e9, 81.615e9, 68.127e9, 57.006e9, 46.743e9],
                              [63.734e9, 53.536e9, 44.299e9, 36.010e9, 28.440e9],
                              [59.688e9, 58.321e9, 42.960e9, 31.910e9, 26.422e9],
                              [2.46, 2.39, 1.76, 1.30, 1.08]],
                             index=["Total Revenue", "Operating Income", "Net Income", "Diluted EPS"], columns=quarters)
    years = pd.to_datetime(["2026-01-31", "2025-01-31", "2024-01-31", "2022-01-31"])
    annual = pd.DataFrame([[215.938e9, 130.497e9, 60.922e9, np.nan], [130.387e9, 81.453e9, 32.972e9, np.nan],
                           [120.067e9, 72.880e9, 29.760e9, np.nan], [4.90, 2.94, 1.19, np.nan]],
                          index=["Total Revenue", "Operating Income", "Net Income", "Diluted EPS"], columns=years)
    periods = ["0q", "+1q", "0y", "+1y"]
    return {"quarterly": quarterly, "annual": annual, "shares": 24.147e9, "fy_end_month": 1,
            "earnings_estimate": pd.DataFrame({"avg": [2.47325, 2.74622, 9.31242, 15.9146], "low": [2.34, 2.56, 9.01, 12.84835],
                                               "high": [2.7, 3.117, 10.1, 18.746], "numberOfAnalysts": [44, 42, 51, 52]},
                                              index=periods),
            "revenue_estimate": pd.DataFrame({"avg": [108.976837760e9, 124.48269773e9, 411.65340984e9, 692.20637265e9],
                                              "low": [104.937e9, 117.29026081e9, 403.20631955e9, 506.483e9],
                                              "high": [116.251e9, 138.49e9, 426.885e9, 778.471e9],
                                              "numberOfAnalysts": [45, 42, 54, 57]}, index=periods),
            "eps_trend": pd.DataFrame({"current": [2.47325, 2.74622, 9.31242, 15.9146],
                                       "30daysAgo": [2.47269, 2.74328, 9.30456, 15.56753],
                                       "90daysAgo": [2.33963, 2.65044, 8.94722, 12.76363]}, index=periods),
            "eps_revisions": pd.DataFrame({"upLast30days": [0, 0, 2, 7], "downLast30days": [1, 1, 0, 0]}, index=periods)}


def test_valuation_is_worked_out_from_the_last_four_quarters():
    view = analysis.fundamentals_view(fake_raw(), 214.13, as_of=MADE_ON)
    assert view["ttm"]["eps"] == pytest.approx(2.46 + 2.39 + 1.76 + 1.30)
    value = view["valuation"]
    assert value["pe_ttm"] == pytest.approx(214.13 / 7.91)
    assert value["market_cap"] == pytest.approx(214.13 * 24.147e9)
    assert value["ps_ttm"] == pytest.approx(214.13 * 24.147e9 / (96.221e9 + 81.615e9 + 68.127e9 + 57.006e9))
    assert value["pe_forward"]["0y"] == pytest.approx(214.13 / 9.31242)
    latest = view["quarters"][0]
    assert latest["label"] == "FY2027 Q2（2026 年 5–7 月）"
    assert latest["revenue_qoq"] == pytest.approx(96.221 / 81.615 - 1)
    assert latest["revenue_yoy"] == pytest.approx(96.221 / 46.743 - 1)
    assert [y["label"] for y in view["years"]] == ["FY2026", "FY2025", "FY2024"]  # the empty year is left out
    assert view["periods"] == {"0q": "FY2027 Q3（2026 年 8–10 月）", "+1q": "FY2027 Q4（2026 年 11 月–2027 年 1 月）",
                               "0y": "FY2027", "+1y": "FY2028"}
    assert view["trend"]["+1y"]["change_90d"] == pytest.approx(15.9146 / 12.76363 - 1)


def test_missing_statements_are_reported_not_invented():
    raw = fake_raw()
    raw["quarterly"] = pd.DataFrame()
    view = analysis.fundamentals_view(raw, 214.13, as_of=MADE_ON)
    assert view["quarters"] == [] and view["ttm"] is None and view["valuation"]["pe_ttm"] is None


def test_cross_checks_flag_differences_beyond_the_tolerance():
    same = analysis.compare("收盘价（美元）", ("Treg · Tiingo", 214.13), ("Moomoo", 214.13, "2026-10-16 20:01:20 美东"), 0.0005)
    assert same["ok"] and same["diff"] == 0
    off = analysis.compare("成交量（股）", ("Treg · Tiingo", 119964163.0), ("Moomoo", 121252522.0, "t"), 0.005)
    assert not off["ok"] and off["diff"] == pytest.approx(121252522 / 119964163 - 1)
    iv = analysis.compare("隐含波动率", ("yfinance", 0.3688), ("Moomoo", 0.3706, "t"), 0.01, absolute=True)
    assert iv["ok"] and iv["diff"] == pytest.approx(0.0018)
    # yfinance left the latest close empty on 2026-10-16: say so instead of calling it a mismatch
    missing = analysis.compare("收盘价（美元）", ("Treg · Tiingo", 214.13), ("yfinance", float("nan"), "t"), 0.0005)
    assert "无法核对" in missing["note"] and "ok" not in missing


def fake_second_sources(monkeypatch, bars):
    """yfinance and Moomoo agreeing with the bars' last close, as read on Monday 2026-10-19 before the open."""
    spot = float(bars["raw_close"].iloc[-1])
    monkeypatch.setattr(analysis, "_yfinance_close", lambda ticker, day: ("yfinance", spot, "2026-10-16 收盘"))
    monkeypatch.setattr(analysis.quote, "quote_rights", lambda: {"us_qot_right": "LV3", "us_option_qot_right": "LV1"})
    snap = pd.DataFrame({"code": ["US.NVDA", "US.SMH", "US.SPY"], "last_price": [spot, 539.79, 763.25],
                         "volume": [float(bars["volume"].iloc[-1])] * 3, "highest52weeks_price": [1e9] * 3,
                         "lowest52weeks_price": [0.0] * 3, "pe_ttm_ratio": [27.07] * 3, "total_market_val": [5e12] * 3,
                         "update_time": ["2026-10-19 08:41:01.142"] * 3})
    monkeypatch.setattr(analysis.quote, "snapshot", lambda codes: snap)


def test_vix_is_checked_against_cboe_the_official_source(monkeypatch):
    bars = bars_from(walk())
    fake_second_sources(monkeypatch, bars)
    day = bars.index[-1]
    yahoo = pd.Series([16.46, 19.99], index=pd.DatetimeIndex([day - pd.Timedelta(days=1), day]))
    nasdaq = yahoo * 1.36
    official = {"VIX": yahoo, "VXN": nasdaq}
    monkeypatch.setattr(analysis.data, "get_cboe_history", lambda name: official[name].copy())
    rows = analysis.cross_checks("NVDA", bars, {analysis.VIX: yahoo, analysis.VXN: nasdaq}, {}, {}, "SMH", "SPY")["rows"]
    row = next(r for r in rows if r["what"].startswith("VIX"))
    assert row["ok"] and row["theirs"][0] == "Cboe 官方"
    assert next(r for r in rows if r["what"].startswith("VXN"))["ok"]

    def unreachable(name):
        raise analysis.DataUnavailable(f"Cboe 的 {name} 历史文件读不到（HTTP 403）")

    monkeypatch.setattr(analysis.data, "get_cboe_history", unreachable)
    row = next(r for r in analysis.cross_checks("NVDA", bars, {analysis.VIX: yahoo}, {}, {}, "SMH", "SPY")["rows"]
               if r["what"].startswith("VIX"))
    assert "ok" not in row and "HTTP 403" in row["note"]


def test_option_prices_are_checked_only_against_trades_from_the_same_close(monkeypatch):
    # 2026-10-19 08:41 New York: Moomoo re-ran its own volatility with three fewer days to expiry (39.3% from Friday's
    # 5.90 call trade) and had a new put trade at 07:30; neither says anything about Friday's option prices
    bars = bars_from(walk())
    fake_second_sources(monkeypatch, bars)
    monkeypatch.setattr(analysis.quote, "option_ivs", lambda code, expiry, strike: {
        "call": {"iv": 0.393, "last_price": 5.90, "update_time": "2026-10-16 15:59:52"},
        "put": {"iv": 0.3895, "last_price": 6.4878, "update_time": "2026-10-19 07:30:00"}, "iv": 0.39125})
    options = {"2w": {"iv": 0.3688, "expiry": "2026-10-30", "strike": 215.0, "prices": {"call": 5.90, "put": 6.46}}}
    rows = analysis.cross_checks("NVDA", bars, {}, options, {}, "SMH", "SPY")["rows"]
    call = next(r for r in rows if r["what"].startswith("2 周看涨期权成交价"))
    put = next(r for r in rows if r["what"].startswith("2 周看跌期权成交价"))
    assert call["ok"] and call["diff"] == 0
    assert "ok" not in put and "2026-10-19 07:30" in put["note"] and "不比" in put["note"]
    assert not any("隐含波动率" in r["what"] for r in rows)  # Moomoo's own model is shown, not checked
    assert options["2w"]["moomoo_iv"] == pytest.approx(0.39125)


def test_card_says_how_its_cross_checks_went():
    prep = synthetic_prep()  # its Moomoo volume differs by 1.07%, beyond the 0.5% tolerance
    sources = next(line for line in card_lines(prep) if line.startswith("来源："))
    assert "交叉核对 2 项，1 项不一致：成交量" in sources
    prep["checks"]["rows"] = prep["checks"]["rows"][:1]
    sources = next(line for line in card_lines(prep) if line.startswith("来源："))
    assert "交叉核对 1 项都一致" in sources
    prep["checks"] = {"moomoo": {"note": "连不上 moomoo OpenD"}, "rows": []}
    sources = next(line for line in card_lines(prep) if line.startswith("来源："))
    assert "没有交叉核对" in sources and "Moomoo" not in sources


def test_treg_costs_count_the_days_calls_for_these_tickers(monkeypatch):
    log = pd.DataFrame({"as_of": ["2026-10-17T10:38:51+11:00", "2026-10-17T11:58:02+11:00", "2026-10-16T09:00:00+11:00",
                                  "2026-10-17T10:37:32+11:00"],
                        "params": ['{"startDate": "1900-01-01", "ticker": "nvda"}', '{"startDate": "1900-01-01", "ticker": "smh"}',
                                   '{"startDate": "1900-01-01", "ticker": "spy"}', '{"startDate": "1900-01-01", "ticker": "soxl"}'],
                        "cost_usd": [0.0, 0.0, 0.0, 0.0]})
    monkeypatch.setattr(analysis.treg, "call_log", lambda: log)
    costs = analysis.treg_costs(["NVDA", "SMH", "SPY"], MADE_ON)
    assert costs == {"treg_calls": 2, "treg_usd": 0.0, "treg_tickers": ["NVDA", "SMH"]}


# --- the report --------------------------------------------------------------------------------------------

ANALYSTS = {"ticker": "NVDA", "for": "NVDA", "source": "yfinance", "as_of": "2026-10-17", "note": "",
            "targets": {"current": 214.13, "high": 515.0, "low": 180.0, "mean": 328.71695, "median": 315.0},
            "upside_mean": 328.71695 / 214.13 - 1,
            "ratings": {"strongBuy": 10, "buy": 49, "hold": 2, "sell": 1, "strongSell": 0},
            "changes_90d": {"main": 9, "reit": 2, "init": 1},
            "recent": [{"date": "2026-10-01", "firm": "Cantor Fitzgerald", "action": "重申", "from": "Overweight",
                        "to": "Overweight", "target": 350.0, "prior_target": 350.0}],
            "revisions": {"0q": {"up_30d": 0, "down_30d": 1}}}
OFFICIAL = {"status": "未公布", "checked": "2026-10-17", "page": "https://nvidianews.nvidia.com/",
            "precedent": "去年同季（FY2026 Q3）的日期在 2025-10-29 公布，财报在 2025-11-19 发布",
            "precedent_url": "https://investor.nvidia.com/news/press-release-details/2025/"
                             "NVIDIA-Sets-Conference-Call-for-Third-Quarter-Financial-Results/"}


def option(spot, iv, expiry, days):
    return {**forecast.option_view(spot, iv, 0.0407, days), "iv": iv, "expiry": expiry, "days": days,
            "horizon_days": days, "strike": 215.0, "basis": "last", "used": ["看涨", "看跌"]}


def synthetic_prep(jev=None, board=None, six_month_options=True, ticker="NVDA", earnings=None, fundamentals=None):
    close = walk()
    spot = float(close.iloc[-1])
    benchmarks = {"SMH": walk(seed=8, drift=0.0004, vol=0.018), "SPY": walk(seed=9, drift=0.0003, vol=0.01)}
    options = {"2w": option(spot, 0.3688, "2026-10-30", 14), "1m": option(spot, 0.3702, "2026-11-20", 35),
               "3m": option(spot, 0.3755, "2027-01-15", 91),
               "6m": option(spot, 0.3807, "2027-04-16", 182) if six_month_options else
               {"p_up": None, "note": "2027-04-16 行权价 215.0 没有 2026-10-16 当天的成交，无可靠期权数据"},
               "event": {"dates": ["2026-11-17", "2026-11-18"], "before": "2026-11-13", "after": "2026-11-20",
                         "iv_before": 0.3573, "iv_after": 0.3983, "days_before": 28, "days_after": 35,
                         "move": analysis.event_move(0.3573, 0.3983, 35)}}
    earnings = earnings or {"dates": ["2026-11-17", "2026-11-18"],
                            "sources": [["2026-11-18", "yfinance calendar"], ["2026-11-17", "yfinance earnings_dates"]],
                            "official": OFFICIAL, "label": "FY2027 Q3（2026 年 8–10 月）",
                            "eps": {"avg": 2.47325, "low": 2.34, "high": 2.7, "n": 44},
                            "revenue": {"avg": 108.97683776e9, "low": 104.937e9, "high": 116.251e9, "n": 45}}
    checks = {"moomoo": {"rights": {"us_qot_right": "LV3", "us_option_qot_right": "LV1"},
                         "time": "2026-10-16 20:01:20.696"},
              "rows": [analysis.compare("收盘价（美元）", ("Treg · Tiingo", spot), ("Moomoo", spot, "2026-10-16 20:01:20 美东"),
                                        0.0005),
                       analysis.compare("成交量（股）", ("Treg · Tiingo", 119964163.0),
                                        ("Moomoo", 121252522.0, "2026-10-16 20:01:20 美东"), 0.005)]}
    if board is None:
        board = pd.DataFrame(columns=["source", "horizon", "scored", "brier", "coin_flip", "hit_rate"])
    return analysis.assemble(ticker, bars_from(close), benchmarks, made_on=MADE_ON, rate=(0.0407, "2026-10-16"),
                             options=options, analysts=ANALYSTS,
                             fundamentals=fundamentals or analysis.fundamentals_view(fake_raw(), spot, as_of=MADE_ON),
                             earnings=earnings, checks=checks, board=board, jev=jev)


def section(md, heading):
    return md.split(f"\n{heading}\n")[1].split("\n## ")[0]


def test_report_leads_with_a_short_conclusion():
    md = analysis.report_markdown(synthetic_prep())
    assert md.startswith("# NVDA 分析 v1")
    bullets = [line for line in section(md, "## 一屏结论").splitlines() if line.startswith("- ")]
    assert 1 <= len(bullets) <= len(analysis.HORIZONS) + 3  # the trend, one line per horizon, signals, earnings
    joined = "\n".join(bullets)
    for words in ("趋势", "2 周（到 2026-10-30）", "半年（到 2027-04-16）", "信号", "财报", "Jev 待你确认费用"):
        assert words in joined, words


def test_report_is_about_the_stock_only():
    md = analysis.report_markdown(synthetic_prep())
    assert md.rstrip().endswith("教育用途，非投资建议；决定权在你。")
    body = md.replace("教育用途，非投资建议；决定权在你。", "")
    for words in ("澳元", "账面", "仓位", "IPS", "建议", "买入 /", "卖出 /"):
        assert words not in body, words
    source = Path(analysis.__file__).read_text(encoding="utf-8")
    for words in ("from . import account", "account.", "load_holdings", "holdings.csv", "funds.csv", "PROFILE_DIR"):
        assert words not in source, words


def test_every_part_names_its_source_and_date():
    md = analysis.report_markdown(synthetic_prep())
    for words in ("Treg · Tiingo", "yfinance", "Moomoo", "LV3", "2026-10-16", "^IRX", "nvidianews.nvidia.com"):
        assert words in md, words


def test_dashboard_has_every_indicator_the_user_asked_for():
    dash = section(analysis.report_markdown(synthetic_prep()), "## 指标看板")
    for words in ("MA5", "MA10", "MA20", "MA50", "MA200", "RSI(14)", "MACD", "量比", "ATR(14)", "近 20 个交易日",
                  "近 52 周", "SMH", "SPY"):
        assert words in dash, words


def test_evidence_tables_list_every_signal_with_counts_and_a_verdict():
    prep = synthetic_prep()
    md = analysis.report_markdown(prep)
    assert md.count("所有日子（基准）") == len(analysis.HORIZONS)
    for h in analysis.HORIZONS:
        assert len(prep["evidence"][h]) == len(analysis.STATES)
        assert {row["verdict"] for row in prep["evidence"][h]} <= set(analysis.VERDICTS)


def test_option_range_shows_its_arithmetic():
    prep = synthetic_prep()
    md = analysis.report_markdown(prep)
    o = prep["options"]["2w"]
    assert f"{o['iv']:.2%} × √(14 ÷ 365)" in md
    assert f"{o['hi']:.2f}" in md and f"{o['lo']:.2f}" in md
    assert f"{prep['options']['event']['move']:.2%}" in md


def test_missing_option_data_is_said_not_filled_in():
    md = analysis.report_markdown(synthetic_prep(six_month_options=False))
    assert "无可靠数据" in md and "没有 2026-10-16 当天的成交" in md


def test_without_scored_sources_no_agreement_is_claimed():
    md = analysis.report_markdown(synthetic_prep())
    assert "不判断「一致」" in md and "一致：偏" not in md


def test_agreement_is_judged_only_between_sources_with_a_record():
    board = pd.DataFrame([{"source": "hist_trend", "horizon": "2w", "scored": 25, "brier": 0.231, "coin_flip": 0.25,
                           "hit_rate": 0.6},
                          {"source": "options", "horizon": "2w", "scored": 25, "brier": 0.249, "coin_flip": 0.25,
                           "hit_rate": 0.5}])
    md = analysis.report_markdown(synthetic_prep(board=board))
    odds = section(md, "## 上涨概率：三个来源")
    two_weeks, half_year = odds.split("### 1 个月")[0], odds.split("### 半年")[1]
    assert "Brier 0.231" in two_weeks and "Brier 0.249" in two_weeks and "不判断" not in two_weeks
    assert "不判断「一致」" in half_year


def test_jev_odds_appear_once_confirmed():
    md = analysis.report_markdown(synthetic_prep(jev={"2w": 0.61, "1m": 0.64, "3m": 0.68, "6m": 0.72, "cost_usd": 0.0003,
                                                     "generation_id": "g"}))
    assert "Jev 61.0%" in md and "Jev 72.0%" in md and "待你确认" not in md


def test_lessons_cover_every_indicator_with_short_and_long_term_and_pitfalls():
    lessons = section(analysis.report_markdown(synthetic_prep()), "## 讲解：每个指标怎么读")
    for name in ("均线", "RSI", "MACD", "量比", "ATR", "前高前低", "相对强弱", "隐含波动率", "市盈率"):
        assert name in lessons, name
    assert lessons.count("短线：") >= 9 and lessons.count("长线：") >= 9 and lessons.count("常见误区：") >= 9


def test_chart_draws_the_close_and_four_averages_and_survives_rendering():
    prep = synthetic_prep()
    assert prep["chart"].startswith("<svg") and prep["chart"].count("<polyline") == 5
    page = render.to_html(analysis.report_markdown(prep))
    assert page.count("<polyline") == 5 and "&lt;svg" not in page


def test_sections_are_the_report_in_order():
    prep = synthetic_prep()
    parts = analysis.sections(prep)
    assert [title for title, _ in parts][:2] == ["一屏结论", "价格和均线（近半年）"]
    assert all(md.startswith(f"## {title}") for title, md in parts)
    assert all(md in analysis.report_markdown(prep) for _, md in parts)


# --- Jev and the forecast log ------------------------------------------------------------------------------

def test_jev_request_asks_one_boolean_question_per_horizon_and_carries_no_account_data():
    prep = synthetic_prep()
    state, questions = analysis.jev_request(prep)
    assert set(questions) == {"NVDA_2w", "NVDA_1m", "NVDA_3m", "NVDA_6m"}
    assert "2026-11-15" in questions["NVDA_1m"]["instructions"] and "2027-01-15" in questions["NVDA_3m"]["instructions"]
    assert all(q["type"] == "boolean" for q in questions.values())
    assert "2026-10-30" in questions["NVDA_2w"]["instructions"] and "2027-04-16" in questions["NVDA_6m"]["instructions"]
    text = json.dumps(state, ensure_ascii=False)  # plain JSON: no numpy numbers or timestamps left
    assert "AUD" not in text and "holding" not in text
    assert json.loads(text)["close"] == pytest.approx(prep["close"])


@pytest.fixture
def journal(monkeypatch, tmp_path):
    path = tmp_path / "forecasts.csv"
    monkeypatch.setattr(forecast, "JOURNAL", path)
    return path


def test_every_horizons_odds_go_into_the_forecast_log(journal):
    prep = synthetic_prep(jev={"2w": 0.61, "1m": 0.64, "3m": 0.68, "6m": 0.72, "cost_usd": 0.0003, "generation_id": "g"})
    added = forecast.log_forecasts(analysis.journal_rows(prep), created=MADE_ON)
    log = pd.read_csv(journal)
    assert added == len(log) and set(log["horizon"]) == {"2w", "1m", "3m", "6m"}
    assert set(log.loc[log["horizon"] == "1m", "target_date"]) == {"2026-11-15"}
    assert set(log.loc[log["horizon"] == "3m", "target_date"]) == {"2027-01-15"}
    assert set(log.loc[log["horizon"] == "2w", "target_date"]) == {"2026-10-30"}
    assert set(log.loc[log["horizon"] == "6m", "target_date"]) == {"2027-04-16"}
    assert {"jev", "options", "hist_trend"} <= set(log["source"])
    assert forecast.log_forecasts(analysis.journal_rows(prep)) == 0  # the same day again adds nothing


# --- the Decision card (v2): one screen per stock --------------------------------------------------------

@pytest.fixture(autouse=True)
def no_saved_glossary(monkeypatch, tmp_path):
    """Cards read the glossary marks from profile/glossary.json; tests start with none kept (every word new)."""
    monkeypatch.setattr(glossary, "GLOSSARY_JSON", tmp_path / "glossary.json")


def card_lines(prep, **kwargs):
    return analysis.card_markdown(prep, **kwargs).splitlines()


def test_card_fits_one_screen_and_leads_with_one_sentence():
    prep = synthetic_prep(jev={"2w": 0.61, "6m": 0.72, "cost_usd": 0.0003, "generation_id": "g", "as_of": "2026-10-17T11:49:31+11:00"})
    lines = [line for line in card_lines(prep) if line.strip()]
    assert lines[0].startswith("### NVDA") and "2026-10-16 收盘" in lines[0]
    assert lines[1].startswith("一句话：")
    bullets = [line for line in lines if line.startswith("- ")]
    assert 8 <= len(bullets) <= 9 and len(lines) <= 12  # four horizons since 2026-10-20
    assert lines[-1].startswith("来源：")  # no glossary line since 2026-10-20: the tab's header points there


def test_card_lists_its_words_not_yet_known_only_when_asked():
    prep = synthetic_prep()
    assert not any(line.startswith("生词") for line in card_lines(prep))
    some = {"收盘价": "懂了", "每股盈利（EPS）": "懂了", "支撑 / 压力（前高前低）": "半懂"}
    with_terms = analysis.CARD_FIELDS + ("terms",)
    last = card_lines(prep, marks=some, fields=with_terms)[-1]
    assert last.startswith("生词") and "均线" in last and "支撑 / 压力" in last and "VIX" in last
    assert "收盘价" not in last and "EPS" not in last.replace("EPS 预期", "")
    known = {term: "懂了" for term in glossary.CARD_TERMS}
    assert card_lines(prep, marks=known, fields=with_terms)[-1].startswith("来源：")


def test_card_rounds_numbers_and_shows_no_working():
    prep = synthetic_prep(jev={"2w": 0.61, "6m": 0.72, "cost_usd": 0.0003, "generation_id": "g", "as_of": "2026-10-17T11:49:31+11:00"})
    md = analysis.card_markdown(prep)
    o = prep["options"]["2w"]
    assert f"区间 {o['lo']:.0f}–{o['hi']:.0f}" in md
    assert f"历史 {analysis._history_view(prep, '2w')['p_up']:.0%}" in md and "Jev 61%" in md and "Jev 72%" in md
    ma = prep["indicators"]["ma"]
    assert f"MA20 {ma[20]:.0f}" in md and f"MA200 {ma[200]:.0f}" in md
    for words in ("×", "√", "÷", "澳元", "IPS", "建议", "买入 /", "卖出 /"):
        assert words not in md, words


def test_card_says_what_is_missing_instead_of_guessing():
    md = analysis.card_markdown(synthetic_prep(six_month_options=False))
    six = next(line for line in md.splitlines() if line.startswith("- 半年"))
    assert "区间 无可靠数据" in six and "Jev 未调用" in six


def test_card_fields_can_be_added_or_removed_in_one_place():
    prep = synthetic_prep()
    assert analysis.CARD_FIELDS[0] == "summary"
    fewer = analysis.card_markdown(prep, fields=tuple(f for f in analysis.CARD_FIELDS if f != "levels"))
    assert "参考价位" in analysis.card_markdown(prep) and "参考价位" not in fewer


def test_card_names_the_next_event_and_whether_it_is_confirmed():
    md = analysis.card_markdown(synthetic_prep())
    event = next(line for line in md.splitlines() if line.startswith("- 下个事件"))
    assert "2026-11-17 / 2026-11-18" in event and "未官宣" in event and "±" in event


@pytest.mark.parametrize("long, short, words", [
    ("上升", "转弱", "长线还在上升，短线在回调"),
    ("上升", "向上", "长线在上升，短线也在上升"),
    ("下降", "转强", "长线还在下降，短线在反弹"),
    ("转换中", "震荡", "长线方向不明（收盘和 MA50 在 MA200 两边），短线在横盘"),
])
def test_card_trend_sentence(long, short, words):
    assert analysis.trend_sentence({"long": long, "short": short}) == words


# --- the sector fund each stock is compared with ----------------------------------------------------------

@pytest.mark.parametrize("ticker, info, fund", [
    ("NVDA", {"industry": "Semiconductors", "sector": "Technology"}, "SMH"),
    ("TSLA", {"industry": "Auto Manufacturers", "sector": "Consumer Cyclical"}, "XLY"),
    ("APP", {"industry": "Software - Application", "sector": "Technology"}, "XLK"),
    ("SOXL", {}, "SMH"),  # a leveraged ETF is compared with the sector of what it tracks
    ("ORCX", {}, "XLK"),
    ("ZZZZ", {}, "SPY"),  # nothing known: the market
])
def test_each_stock_is_compared_with_its_own_sector_fund(ticker, info, fund):
    assert analysis.sector_etf(ticker, info) == fund


def test_lessons_only_say_the_sector_fund_holds_the_stock_when_its_weight_is_known():
    prep = synthetic_prep()
    assert prep["sector_weight"] is None
    assert "本身持有" not in analysis.report_markdown(prep)
    prep["sector_weight"] = 0.1931
    assert "SMH 本身持有 NVDA（占 19.3%，yfinance）" in analysis.report_markdown(prep)


# --- VIX: the market's fear gauge, in News and in every Decision card ------------------------------------

def test_vix_view_gives_the_level_its_moves_and_its_place_in_the_past_year():
    values = [15.0] * 250 + [16.0, 16.46, 16.87, 16.5, 16.46, 19.99]
    view = analysis.vix_view(series(values), dt.date(2026, 10, 16))
    assert view["close"] == 19.99 and view["date"] == "2026-10-16"
    assert view["change_1d"] == pytest.approx(19.99 / 16.46 - 1)
    assert view["change_5d"] == pytest.approx(19.99 / 16.0 - 1)
    assert view["pct_1y"] == pytest.approx(1.0)  # higher than every close of the 252 days before
    assert analysis.vix_view(series([15.0, 16.0]), dt.date(2026, 10, 16)) is None


def test_vix_is_a_signal_with_its_own_history():
    close = walk(600)
    vix = series([15.0] * 599 + [40.0])  # today's VIX is far above its past year
    frame = analysis.signal_frame(bars_from(close), {"SMH": walk(600, seed=8), "SPY": walk(600, seed=9), "^VIX": vix})
    assert frame["vix"].iloc[-1] == "high"
    assert "vix" in analysis.STATES and frame["vix"].iloc[:251].isna().all()


def test_vxn_is_a_signal_too():
    close = walk(600)
    vxn = series([20.0] * 599 + [12.0])  # the Nasdaq-100 gauge far below its past year
    frame = analysis.signal_frame(bars_from(close), {"SMH": walk(600, seed=8), "SPY": walk(600, seed=9), "^VXN": vxn})
    assert frame["vxn"].iloc[-1] == "low" and frame["vix"].isna().all()
    assert "vxn" in analysis.STATES


def test_card_has_a_market_mood_line_from_vix():
    prep = synthetic_prep()
    prep["vix"] = {"close": 19.99, "date": "2026-10-16", "change_1d": 19.99 / 16.46 - 1, "change_5d": 0.2,
                   "pct_1y": 0.83, "median_1y": 16.9}
    prep["vxn"] = {"close": 27.13, "date": "2026-10-16", "change_1d": 27.13 / 22.89 - 1, "change_5d": 0.3,
                   "pct_1y": 0.91, "median_1y": 21.0}
    line = next(l for l in analysis.card_markdown(prep).splitlines() if l.startswith("- 市场情绪"))
    for words in ("VIX", "20.0", "+21%", "83%", "2026-10-16", "VXN（纳指恐慌指数）27.1", "+19%", "91%"):
        assert words in line, words
    prep["vxn"] = None
    assert "VXN 无可靠数据" in next(l for l in analysis.card_markdown(prep).splitlines() if l.startswith("- 市场情绪"))
    prep["vix"] = None
    line = next(l for l in analysis.card_markdown(prep).splitlines() if l.startswith("- 市场情绪"))
    assert "无可靠数据" in line


# --- verified official dates and the earnings-day move for events close by --------------------------------

def test_event_move_can_use_expiries_inside_the_coming_week():
    # Tesla reports after the close on 2026-10-21; valued on 10-16 the expiries either side are 10-19 and 10-23
    seen = []

    def odds(expiry, days=None):
        seen.append(expiry)
        return {"iv": 0.50 if expiry == "2026-10-19" else 0.60, "days": (dt.date.fromisoformat(expiry) - dt.date(2026, 10, 16)).days,
                "strike": 355.0}

    view = analysis._event_view(["2026-10-19", "2026-10-21", "2026-10-23", "2026-10-30"], ["2026-10-21", "2026-10-22"],
                                dt.date(2026, 10, 16), odds)
    assert (view["before"], view["after"]) == ("2026-10-19", "2026-10-23")
    assert view["move"] == pytest.approx(analysis.event_move(0.50, 0.60, 7))


def test_verified_official_dates_are_read_from_the_events_file(tmp_path, monkeypatch):
    path = tmp_path / "official_events.csv"
    pd.DataFrame([{"ticker": "TSLA", "event": "earnings", "date": "2026-10-21", "time": "盘后",
                   "url": "https://www.sec.gov/x.htm", "checked": "2026-10-19"},
                  {"ticker": "TSLA", "event": "earnings", "date": "2026-07-22", "time": "盘后",
                   "url": "https://www.sec.gov/old.htm", "checked": "2026-07-01"}]).to_csv(path, index=False)
    monkeypatch.setattr(analysis, "OFFICIAL_EVENTS", path)
    found = analysis.official_earnings("TSLA", dt.date(2026, 10, 16))
    assert found["date"] == "2026-10-21" and found["url"] == "https://www.sec.gov/x.htm" and found["time"] == "盘后"
    assert analysis.official_earnings("NVDA", dt.date(2026, 10, 16)) is None
    assert analysis.official_earnings("TSLA", dt.date(2026, 10, 22)) is None  # nothing official after that date


def events_file(tmp_path, monkeypatch, rows):
    path = tmp_path / "official_events.csv"
    pd.DataFrame([{"ticker": t, "event": "earnings", "date": d, "time": "盘后", "url": f"https://example.com/{t}",
                   "checked": "2026-10-19"} for t, d in rows],
                 columns=["ticker", "event", "date", "time", "url", "checked"]).to_csv(path, index=False)
    monkeypatch.setattr(analysis, "OFFICIAL_EVENTS", path)


@pytest.mark.parametrize("ticker, kind, follows", [("SOXL", "index", "NYSE 半导体指数"), ("ORCX", "stock", "ORCL"),
                                                    ("soxl", "index", "NYSE 半导体指数")])
def test_funds_are_known_by_what_they_follow(ticker, kind, follows):
    fund = analysis.fund_profile(ticker)
    assert fund["kind"] == kind and follows in (fund.get("index"), fund.get("stock"))
    assert analysis.fund_profile("NVDA") is None


def test_an_index_funds_events_are_its_holdings_official_earnings_in_the_next_two_weeks(tmp_path, monkeypatch):
    events_file(tmp_path, monkeypatch, [("KLAC", "2026-10-28"), ("LRCX", "2026-10-21"), ("INTC", "2026-10-29"),
                                        ("AAPL", "2026-10-22"),  # not a holding of the index
                                        ("AMD", "2026-11-04"),  # a holding, after the two weeks
                                        ("TSM", "2026-10-15")])  # already reported
    earnings = analysis.fund_earnings(analysis.fund_profile("SOXL"), dt.date(2026, 10, 19))
    assert [(h["ticker"], h["date"]) for h in earnings["holdings"]] == [("LRCX", "2026-10-21"), ("KLAC", "2026-10-28"),
                                                                         ("INTC", "2026-10-29")]
    assert earnings["dates"] == []  # no earnings day of its own for the options to price


def fund_card(ticker, earnings, fund):
    return card_lines(synthetic_prep(ticker=ticker, earnings=earnings, fundamentals=analysis.fund_fundamentals(fund)))


def test_an_index_fund_card_says_it_has_no_earnings_and_names_its_holdings_coming_ones(tmp_path, monkeypatch):
    events_file(tmp_path, monkeypatch, [("LRCX", "2026-10-21"), ("KLAC", "2026-10-28"), ("INTC", "2026-10-29")])
    fund = analysis.fund_profile("SOXL")
    lines = fund_card("SOXL", analysis.fund_earnings(fund, dt.date(2026, 10, 19)), fund)
    event = next(line for line in lines if line.startswith("- 下个事件"))
    assert event == ("- 下个事件：基金没有自己的财报；指数前 10 大成分里，未来 2 周已公布财报日的有 LRCX 10-21、"
                     "KLAC 10-28、INTC 10-29（公司已公布）")
    basics = next(line for line in lines if line.startswith("- 基本面"))
    assert basics == "- 基本面：基金没有自己的财报和分析师预期；跟踪 NYSE 半导体指数，每天 3 倍杠杆"
    sources = next(line for line in lines if line.startswith("来源："))
    assert "Direxion（指数成分，2026-06-30）" in sources and "yfinance（期权）" in sources and "基本面）" not in sources


def test_an_index_fund_card_with_no_coming_holdings_earnings_says_so(tmp_path, monkeypatch):
    events_file(tmp_path, monkeypatch, [])
    fund = analysis.fund_profile("SOXL")
    lines = fund_card("SOXL", analysis.fund_earnings(fund, dt.date(2026, 10, 19)), fund)
    assert "- 下个事件：基金没有自己的财报；指数前 10 大成分未来 2 周没有已公布的财报日" in lines


def test_a_single_stock_fund_card_follows_its_stocks_earnings():
    fund = analysis.fund_profile("ORCX")
    earnings = {"dates": ["2026-12-09"], "sources": [["2026-12-09", "yfinance calendar"]], "official": None,
                "label": "标的 ORCL 下一季", "fund": fund}
    lines = fund_card("ORCX", earnings, fund)
    assert "- 下个事件：标的 ORCL 下一季财报 约 2026-12-09（未官宣）" in [line.split("；期权")[0] for line in lines]
    assert "- 基本面：基金没有自己的财报和分析师预期；跟踪 ORCL，每天 2 倍杠杆" in lines


def test_once_a_later_session_opens_moomoo_is_checked_on_its_previous_close(monkeypatch):
    # 2026-10-19 10:15 New York, Monday's session: Moomoo's last price and volume are Monday's; its previous close
    # is Friday's, the day the card is valued on (four false mismatches before this rule)
    bars = bars_from(walk())
    spot, volume = float(bars["raw_close"].iloc[-1]), float(bars["volume"].iloc[-1])
    live = spot * 1.019
    monkeypatch.setattr(analysis, "_yfinance_close", lambda ticker, day: ("yfinance", spot, "2026-10-16 收盘"))
    monkeypatch.setattr(analysis.quote, "quote_rights", lambda: {"us_qot_right": "LV3", "us_option_qot_right": "LV1"})

    def snapshot_at(stamp, last):
        return pd.DataFrame({"code": ["US.NVDA", "US.SMH", "US.SPY"], "last_price": [last, 545.0, 768.0],
                             "prev_close_price": [spot, 539.79, 763.25], "volume": [3.1e7, 1e6, 1e7],
                             "highest52weeks_price": [1e9] * 3, "lowest52weeks_price": [0.0] * 3,
                             "pe_ttm_ratio": [27.07 * last / spot] * 3, "total_market_val": [5e12 * last / spot] * 3,
                             "update_time": [stamp] * 3})

    fundamentals = {"valuation": {"pe_ttm": 27.07, "market_cap": 5e12}}
    benchmarks = {"SMH": pd.Series([539.79], index=[bars.index[-1]]), "SPY": pd.Series([763.25], index=[bars.index[-1]])}
    monkeypatch.setattr(analysis.quote, "snapshot", lambda codes: snapshot_at("2026-10-19 10:15:02.000", live))
    rows = {r["what"]: r for r in analysis.cross_checks("NVDA", bars, benchmarks, {}, fundamentals, "SMH", "SPY")["rows"]}
    moomoo_close = next(r for r in rows.values() if r["what"] == "收盘价（美元）" and r["theirs"][0] == "Moomoo")
    assert moomoo_close["ok"] and "昨收" in moomoo_close["theirs"][2]
    assert "ok" not in rows["成交量（股）"] and "不比" in rows["成交量（股）"]["note"]
    assert rows["SMH 收盘价（美元）"]["ok"] and rows["SPY 收盘价（美元）"]["ok"]
    assert rows["市盈率 TTM（倍）"]["ok"] and rows["市值（亿美元）"]["ok"]
    # 08:41 the same Monday, before the open: the last price is still Friday's close and the volume Friday's
    monkeypatch.setattr(analysis.quote, "snapshot", lambda codes: snapshot_at("2026-10-19 08:41:01.142", spot))
    monkeypatch.setattr(analysis.quote, "snapshot", lambda codes: pd.DataFrame({
        "code": ["US.NVDA", "US.SMH", "US.SPY"], "last_price": [spot, 539.79, 763.25], "prev_close_price": [spot * 1.01] * 3,
        "volume": [volume] * 3, "highest52weeks_price": [1e9] * 3, "lowest52weeks_price": [0.0] * 3,
        "pe_ttm_ratio": [27.07] * 3, "total_market_val": [5e12] * 3, "update_time": ["2026-10-19 08:41:01.142"] * 3}))
    rows = {r["what"]: r for r in analysis.cross_checks("NVDA", bars, benchmarks, {}, fundamentals, "SMH", "SPY")["rows"]}
    assert rows["成交量（股）"]["ok"] and rows["SMH 收盘价（美元）"]["ok"]


def test_card_says_why_option_ranges_wait_while_us_stocks_trade():
    prep = synthetic_prep()
    prep["options"]["2w"] = {"p_up": None, "note": "2026-10-30 行权价 215.0 没有 2026-10-16 当天的成交，无可靠期权数据"}
    summary = next(line for line in card_lines(prep) if line.startswith("一句话："))
    assert "期权的最新成交不在 2026-10-16" in summary and "收盘" in summary
