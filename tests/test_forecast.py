"""Forecast cards, offline: history odds, option-implied odds, analyst summary, Jev request, journal and scoring."""
import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from finclaude import forecast, jev
from finclaude.config import DataUnavailable

VALUED_ON = dt.date(2026, 10, 2)


def series(values, end="2026-10-02"):
    index = pd.bdate_range(end=end, periods=len(values), name="date")
    return pd.Series(values, index=index, dtype=float)


def chain(spot=100.0, vol=0.3, rate=0.04, expiry="2026-11-06", traded=VALUED_ON, strikes=(95.0, 100.0, 105.0)):
    """A yfinance-shaped option chain priced with Black-Scholes; last trades at 15:59 New York time."""
    years = (dt.date.fromisoformat(expiry) - VALUED_ON).days / 365
    when = pd.Timestamp(dt.datetime.combine(traded, dt.time(19, 59)), tz="UTC")
    rows = []
    for kind in ("call", "put"):
        for k in strikes:
            price = forecast.bs_price(spot, k, years, rate, vol, kind == "call")
            rows.append({"strike": k, "lastPrice": round(price, 4), "bid": 0.0, "ask": 0.0, "lastTradeDate": when,
                         "impliedVolatility": 0.00001, "type": kind, "expiry": expiry})
    return pd.DataFrame(rows)


# --- history -----------------------------------------------------------------------------------------------

def test_history_odds_counts_windows_that_ended_higher():
    rising = series(np.linspace(10, 50, 400))
    odds = forecast.history_odds(rising, 21)
    assert odds["all"]["p_up"] == 1.0
    assert odds["all"]["windows"] == 400 - 21
    falling = series(np.linspace(50, 10, 400))
    assert forecast.history_odds(falling, 21)["all"]["p_up"] == 0.0


def test_history_odds_reports_quantiles_and_flags_thin_samples():
    noisy = series(100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.02, 600))))
    odds = forecast.history_odds(noisy, 63)
    assert odds["all"]["p10"] < odds["all"]["p50"] < odds["all"]["p90"]
    short = series(np.linspace(10, 11, 70))
    thin = forecast.history_odds(short, 21)["all"]
    assert thin["p_up"] is None and "49" in thin["note"]


def test_trend_odds_use_only_days_in_the_same_state_as_today():
    # 300 days rising (above the 200-day average), then 200 days falling (below it); today is below.
    values = np.concatenate([np.linspace(50, 100, 300), np.linspace(99, 40, 200)])
    odds = forecast.history_odds(series(values), 21)
    assert odds["trend"]["state"] == "below"
    assert odds["trend"]["windows"] < odds["all"]["windows"]
    assert odds["trend"]["p_up"] < odds["all"]["p_up"]


def test_last_ten_years_window_is_a_subset():
    long = series(100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0.0003, 0.02, 4000))))
    odds = forecast.history_odds(long, 21)
    assert odds["last10y"]["windows"] < odds["all"]["windows"]
    assert odds["last10y"]["start"] >= "2016-10-02"


# --- options -----------------------------------------------------------------------------------------------

def test_implied_vol_recovers_the_volatility_used_to_price():
    price = forecast.bs_price(233.95, 235.0, 35 / 365, 0.0399, 0.2976, True)
    assert forecast.implied_vol(price, 233.95, 235.0, 35 / 365, 0.0399, True) == pytest.approx(0.2976, abs=1e-6)


def test_pick_expiry_takes_the_listed_date_closest_to_the_horizon():
    expiries = ["2026-10-09", "2026-10-30", "2026-11-06", "2026-12-18", "2027-01-08"]
    assert forecast.pick_expiry(expiries, VALUED_ON, 30) == "2026-10-30"
    assert forecast.pick_expiry(expiries, VALUED_ON, 91) == "2027-01-08"
    with pytest.raises(DataUnavailable):
        forecast.pick_expiry(["2026-10-05"], VALUED_ON, 30)


def test_option_odds_back_out_volatility_from_same_day_last_trades():
    odds = forecast.option_odds(chain(), 100.0, VALUED_ON, "2026-11-06", 0.04)
    years = 35 / 365
    assert odds["strike"] == 100.0 and odds["basis"] == "last"
    assert odds["iv"] == pytest.approx(0.3, abs=1e-3)
    assert odds["p_up"] == pytest.approx(norm.cdf((0.04 - 0.3 ** 2 / 2) * math.sqrt(years) / 0.3), abs=1e-3)
    assert odds["lo"] == pytest.approx(100 * math.exp(-0.3 * math.sqrt(years)), rel=1e-3)
    assert odds["hi"] == pytest.approx(100 * math.exp(0.3 * math.sqrt(years)), rel=1e-3)
    # the last trades used are kept, so a second source can be checked against the same prices
    assert set(odds["prices"]) == {"call", "put"} and all(p > 0 for p in odds["prices"].values())


def test_option_odds_are_taken_to_the_target_date_not_the_expiry():
    # the nearest listed expiry is 35 days out; the card's 1-month horizon is 30 days
    odds = forecast.option_odds(chain(), 100.0, VALUED_ON, "2026-11-06", 0.04, horizon_days=30)
    years = 30 / 365
    assert odds["iv"] == pytest.approx(0.3, abs=1e-3)  # the volatility still comes from the 35-day option
    assert odds["days"] == 35 and odds["horizon_days"] == 30
    assert odds["p_up"] == pytest.approx(norm.cdf((0.04 - 0.3 ** 2 / 2) * math.sqrt(years) / 0.3), abs=1e-3)
    assert odds["hi"] == pytest.approx(100 * math.exp(0.3 * math.sqrt(years)), rel=1e-3)
    assert odds["width"] == pytest.approx(odds["iv"] * math.sqrt(years))


def test_option_view_is_the_same_math_from_a_known_volatility():
    view = forecast.option_view(19.77, 1.116, 0.0402, 30)
    width = 1.116 * math.sqrt(30 / 365)
    assert view["width"] == pytest.approx(width)
    assert view["lo"] == pytest.approx(19.77 * math.exp(-width)) and view["hi"] == pytest.approx(19.77 * math.exp(width))
    assert view["p_up"] == pytest.approx(norm.cdf((0.0402 - 1.116 ** 2 / 2) * math.sqrt(30 / 365) / 1.116))


def test_stale_option_trades_are_not_used():
    stale = chain(traded=dt.date(2026, 9, 25))
    with pytest.raises(DataUnavailable):
        forecast.option_odds(stale, 100.0, VALUED_ON, "2026-11-06", 0.04)


# --- analysts ----------------------------------------------------------------------------------------------

def fake_analyst(ticker):
    changes = pd.DataFrame(
        {"Firm": ["Cantor Fitzgerald", "Mizuho", "Old Firm"], "ToGrade": ["Overweight", "Outperform", "Sell"],
         "FromGrade": ["Overweight", "Neutral", "Buy"], "Action": ["reit", "up", "down"],
         "priceTargetAction": ["Maintains", "Raises", "Lowers"], "currentPriceTarget": [350.0, 315.0, 90.0],
         "priorPriceTarget": [350.0, 300.0, 120.0]},
        index=pd.DatetimeIndex(["2026-10-01 10:34", "2026-08-27 18:33", "2026-03-01 09:00"], name="GradeDate"))
    return {"ticker": ticker,
            "targets": {"current": 200.0, "high": 515.0, "low": 180.0, "mean": 328.7, "median": 315.0},
            "summary": pd.DataFrame({"period": ["0m", "-1m"], "strongBuy": [10, 9], "buy": [48, 48], "hold": [2, 2],
                                     "sell": [1, 1], "strongSell": [0, 0]}),
            "changes": changes,
            "revisions": pd.DataFrame({"upLast30days": [35, 27], "downLast30days": [2, 10]}, index=["0q", "+1q"])}


def test_analyst_view_quotes_targets_ratings_and_recent_changes(monkeypatch):
    monkeypatch.setattr(forecast, "_fetch_analyst", fake_analyst)
    view = forecast.analyst_view("NVDA", today=dt.date(2026, 10, 5))
    assert view["ticker"] == "NVDA" and view["source"] == "yfinance"
    assert view["targets"]["mean"] == 328.7
    assert view["upside_mean"] == pytest.approx(328.7 / 200.0 - 1)
    assert view["ratings"] == {"strongBuy": 10, "buy": 48, "hold": 2, "sell": 1, "strongSell": 0}
    assert view["changes_90d"] == {"up": 1, "reit": 1}  # the March change is older than 90 days
    assert [c["firm"] for c in view["recent"]] == ["Cantor Fitzgerald", "Mizuho"]
    assert view["revisions"]["0q"] == {"up_30d": 35, "down_30d": 2}


def test_leveraged_etf_uses_its_underlying_analysts(monkeypatch):
    seen = []
    monkeypatch.setattr(forecast, "_fetch_analyst", lambda t: seen.append(t) or fake_analyst(t))
    view = forecast.analyst_view("ORCX", today=dt.date(2026, 10, 5))
    assert seen == ["ORCL"] and view["ticker"] == "ORCL" and view["for"] == "ORCX"
    assert "ORCL" in view["note"]


# --- cards, Jev, journal -----------------------------------------------------------------------------------

def prepared(ticker="NVDA", spot=100.0):
    close = series(100 * np.exp(np.cumsum(np.random.default_rng(3).normal(0.0005, 0.02, 1500))))
    close.iloc[-1] = spot
    opt = forecast.option_odds(chain(spot=spot), spot, VALUED_ON, "2026-11-06", 0.04, horizon_days=30)
    return {"ticker": ticker, "close": spot, "valued_on": VALUED_ON, "price_source": "Treg · Tiingo",
            "history": {h: forecast.history_odds(close, d) for h, (d, _) in forecast.HORIZONS.items()},
            "options": {"1m": opt, "3m": {"p_up": None, "note": "没有可靠期权数据"}},
            "analysts": {"available": False, "note": "test"}}


def test_card_rows_carry_every_source_and_the_aud_range():
    rows = forecast.card_rows(prepared(), holding_aud=735.35, jev_probs={"NVDA_1m": 0.62})
    one = next(r for r in rows if r["horizon"] == "1m")
    assert one["target_date"] == dt.date(2026, 11, 1)
    assert one["p_options"] is not None and one["p_jev"] == 0.62
    assert one["aud_lo"] == pytest.approx(735.35 * (one["range_lo"] / 100.0 - 1))
    three = next(r for r in rows if r["horizon"] == "3m")
    assert three["p_options"] is None and three["p_jev"] is None


@pytest.mark.parametrize("probs, verdict", [
    ([0.6, 0.7, 0.58], "一致：偏涨"),
    ([0.3, 0.4, 0.2], "一致：偏跌"),
    ([0.6, 0.5, 0.3], "不一致"),
    ([0.5, None, None], "来源不足"),
])
def test_agreement_uses_a_neutral_band(probs, verdict):
    assert forecast.agreement(probs) == verdict


def test_jev_request_asks_one_boolean_per_holding_and_horizon():
    rows = forecast.card_rows(prepared("NVDA"), 735.35) + forecast.card_rows(prepared("APP"), 897.08)
    state, questions = forecast.jev_request([prepared("NVDA"), prepared("APP")], rows)
    assert set(questions) == {f"{t}_{h}" for t in ("NVDA", "APP") for h in forecast.HORIZONS}
    assert all(q["type"] == "boolean" for q in questions.values())
    assert "2026-11-01" in questions["NVDA_1m"]["instructions"]
    assert {h["ticker"] for h in state["holdings"]} == {"NVDA", "APP"}


def test_jev_odds_return_each_probability(monkeypatch):
    class Answer:
        answers = {"NVDA_1m": {"type": "boolean", "probability": 0.61}, "NVDA_3m": {"type": "boolean", "probability": 0.4}}

    monkeypatch.setattr(jev, "evaluate", lambda state, questions, max_cost_usd, save_as: Answer())
    probs, _ = forecast.jev_odds({}, {"NVDA_1m": {}, "NVDA_3m": {}}, max_cost_usd=0.01)
    assert probs == {"NVDA_1m": 0.61, "NVDA_3m": 0.4}


@pytest.fixture
def journal(monkeypatch, tmp_path):
    path = tmp_path / "forecasts.csv"
    monkeypatch.setattr(forecast, "JOURNAL", path)
    return path


def test_forecasts_are_logged_once_per_source(journal):
    rows = forecast.card_rows(prepared(), 735.35, jev_probs={"NVDA_1m": 0.62, "NVDA_3m": 0.55})
    forecast.log_forecasts(rows)
    forecast.log_forecasts(rows)  # same day again: no duplicates
    log = pd.read_csv(journal)
    assert set(log["source"]) <= set(forecast.SCORED_SOURCES)
    assert not log.duplicated(["as_of", "ticker", "horizon", "source"]).any()
    assert log.loc[log["source"] == "jev", "p_up"].tolist() == [0.62, 0.55]
    assert "options" in set(log.loc[log["horizon"] == "1m", "source"])
    assert "options" not in set(log.loc[log["horizon"] == "3m", "source"])


def test_score_resolves_only_forecasts_whose_date_has_passed(journal):
    pd.DataFrame([
        {"created": "2026-08-01", "as_of": "2026-08-01", "ticker": "NVDA", "horizon": "1m", "target_date": "2026-08-31",
         "source": "jev", "p_up": 0.8, "base_price": 100.0},
        {"created": "2026-08-01", "as_of": "2026-08-01", "ticker": "NVDA", "horizon": "1m", "target_date": "2026-08-31",
         "source": "options", "p_up": 0.3, "base_price": 100.0},
        {"created": "2026-10-01", "as_of": "2026-10-01", "ticker": "NVDA", "horizon": "1m", "target_date": "2026-10-31",
         "source": "jev", "p_up": 0.6, "base_price": 100.0},
    ]).to_csv(journal, index=False)
    prices = pd.DataFrame({"close": [100.0, 110.0, 111.0]},
                          index=pd.DatetimeIndex(["2026-08-01", "2026-09-01", "2026-10-02"], name="date"))
    resolved = forecast.score(load=lambda ticker: prices)
    assert resolved == 2
    log = pd.read_csv(journal)
    done = log.dropna(subset=["outcome"])
    assert done["resolved_date"].tolist() == ["2026-09-01", "2026-09-01"]  # first close on or after the target date
    assert done["outcome"].tolist() == [1, 1]
    assert done["brier"].round(4).tolist() == [0.04, 0.49]
    assert log["outcome"].isna().sum() == 1


def test_scoreboard_compares_each_source_with_a_coin_flip(journal):
    pd.DataFrame([
        {"source": "jev", "horizon": "1m", "p_up": 0.8, "outcome": 1, "brier": 0.04},
        {"source": "jev", "horizon": "1m", "p_up": 0.7, "outcome": 0, "brier": 0.49},
        {"source": "options", "horizon": "1m", "p_up": 0.5, "outcome": 1, "brier": 0.25},
        {"source": "hist_all", "horizon": "3m", "p_up": 0.6, "outcome": None, "brier": None},
    ]).to_csv(journal, index=False)
    board = forecast.scoreboard()
    jev_row = board[(board["source"] == "jev") & (board["horizon"] == "1m")].iloc[0]
    assert jev_row["scored"] == 2
    assert jev_row["brier"] == pytest.approx(0.265)
    assert jev_row["hit_rate"] == 0.5
    assert set(board["source"]) == {"jev", "options"}  # nothing scored yet for hist_all


def test_card_leads_with_the_verdict_and_reads_line_by_line(monkeypatch, journal):
    monkeypatch.setattr(forecast, "_fetch_analyst", fake_analyst)
    prep = prepared()
    prep["analysts"] = forecast.analyst_view("NVDA", today=dt.date(2026, 10, 5))
    rows = forecast.card_rows(prep, 735.35, jev_probs={"NVDA_1m": 0.62})
    lines = forecast.card_markdown(prep, rows, forecast.scoreboard()).splitlines()
    md = "\n".join(lines)
    assert lines[0].startswith("### NVDA")
    assert lines[2].startswith("**结论**")
    assert not any(line.startswith("|") for line in lines)  # no wide tables: each line reads on its own
    one = next(line for line in lines if line.startswith("- 1 个月（到 2026-11-01）："))
    assert "历史" in one and "期权" in one and "Jev 62.0%" in one and "→" in one
    three = next(line for line in lines if line.startswith("- 3 个月（到 2027-01-01）："))
    assert "期权 无可靠数据" in three and "Jev 无数据" in three
    for words in ("68%", "各 16%", "80%", "Cantor Fitzgerald", "原样引用", "328.7"):
        assert words in md, words
    # about the stock only (CLAUDE.md 经验 2026-10-16): no holding amount and no AUD for a US stock
    assert "735.35" not in md and "澳元" not in md and "你的持仓" not in md
    hist = prep["history"]["1m"]["all"]
    assert f"{100.0 * (1 + hist['p10']):.2f}–{100.0 * (1 + hist['p90']):.2f} 美元" in md
    # which options: the holding's own, both call and put at the strike nearest the close, scaled to the target date
    option = next(line for line in lines if "隐含波动率" in line)
    for words in ("NVDA 自己的期权", "看涨", "看跌", "行权价 100.00", "2026-11-06 到期", "30 天"):
        assert words in option, words
    assert "未到期" not in md  # nothing scored yet: no column full of 未到期
    assert "买入 /" not in md and "建议" not in md  # Claude's own voice never says buy or sell


def test_primer_teaches_options_with_this_holdings_numbers():
    prep = prepared()
    row = next(r for r in forecast.card_rows(prep, 735.35) if r["horizon"] == "1m")
    md = forecast.primer_markdown(prep, row)
    width = row["iv"] * math.sqrt(30 / 365)
    for words in ("看涨期权（call）", "看跌期权（put）", "隐含波动率（implied volatility", "风险中性概率",
                  "行权价 100.00 美元", "2026-11-06", f"{row['iv']:.1%}", f"{width:.1%}", f"{row['range_hi']:.2f}",
                  "不用买卖期权"):
        assert words in md, words
    assert "买入 /" not in md and "建议" not in md
    assert "澳元" not in md and "你的持仓：" not in md


def test_card_says_which_history_number_it_uses(journal):
    prep = prepared()
    md = forecast.card_markdown(prep, forecast.card_rows(prep, 735.35), forecast.scoreboard())
    assert "200 日均线" in md and "全部历史" in md and "近 10 年" in md


def test_card_shows_the_score_once_a_source_has_one(journal):
    pd.DataFrame([{"source": "jev", "horizon": "1m", "p_up": 0.8, "outcome": 1, "brier": 0.04}]).to_csv(journal, index=False)
    prep = prepared()
    md = forecast.card_markdown(prep, forecast.card_rows(prep, 735.35), forecast.scoreboard())
    assert "过去准不准" in md and "Jev 综合 1 个月：1 次，Brier 0.040" in md


def test_two_week_and_half_year_scores_are_named_in_words(journal):
    # finclaude.analysis logs 2-week and half-year odds into the same journal
    pd.DataFrame([{"source": "jev", "horizon": "2w", "p_up": 0.6, "outcome": 1, "brier": 0.16},
                  {"source": "options", "horizon": "6m", "p_up": 0.5, "outcome": 0, "brier": 0.25}]).to_csv(journal, index=False)
    prep = prepared()
    md = forecast.card_markdown(prep, forecast.card_rows(prep, 735.35), forecast.scoreboard())
    assert "Jev 综合 2 周：1 次，Brier 0.160" in md and "期权市场 半年：1 次，Brier 0.250" in md


def test_last_ten_years_is_skipped_when_history_is_shorter():
    short = series(100 * np.exp(np.cumsum(np.random.default_rng(4).normal(0, 0.02, 800))))
    odds = forecast.history_odds(short, 21)
    assert odds["last10y"]["p_up"] is None and "10 年" in odds["last10y"]["note"]
    assert odds["all"]["p_up"] is not None


def test_cards_name_the_trading_currency(monkeypatch):
    prep = prepared("IVV.AX", spot=74.2)
    prep["currency"] = "AUD"
    md = forecast.card_markdown(prep, forecast.card_rows(prep, 816.2), forecast.scoreboard())
    assert "收盘 74.20 澳元" in md and "美元" not in md


def test_etf_without_analyst_coverage_says_so(monkeypatch):
    empty = {"ticker": "IVV.AX", "targets": {}, "summary": pd.DataFrame(), "changes": pd.DataFrame(),
             "revisions": pd.DataFrame()}
    monkeypatch.setattr(forecast, "_fetch_analyst", lambda t: empty)
    view = forecast.analyst_view("IVV.AX", today=dt.date(2026, 10, 6))
    assert view["available"] is False and "分析师" in view["note"]


def test_windows_across_a_level_break_are_left_out():
    # IVV.AX: 99.60 on 2010-12-31, 6.73 on 2011-01-04 (about 14.8x). Real moves on each side, a fake -93% across.
    rng = np.random.default_rng(5)
    before = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 600)))
    after = before[-1] / 14.8 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 600)))
    close = series(np.concatenate([before, after]))
    brk = close.index[600]
    with_break = forecast.history_odds(close, 21, breaks=[brk])
    naive = forecast.history_odds(close, 21)
    assert with_break["all"]["windows"] == naive["all"]["windows"] - 21
    assert with_break["all"]["p10"] > naive["all"]["p10"] - 1e-9
    assert with_break["all"]["p_up"] >= naive["all"]["p_up"]
    left, right = close.iloc[:600], close.iloc[600:]
    fwd = lambda s: (s.shift(-21) / s - 1).dropna()
    expected = (pd.concat([fwd(left), fwd(right)]) > 0).mean()
    assert with_break["all"]["p_up"] == pytest.approx(expected)


def test_prepare_passes_flagged_breaks_to_history(monkeypatch):
    seen = {}
    idx = pd.bdate_range(end="2026-10-02", periods=300, name="date")
    prices = pd.DataFrame({"close": np.linspace(10, 20, 300), "volume": 1, "source": "yfinance"}, index=idx)
    prices.attrs["level_breaks"] = [str(idx[100].date())]
    monkeypatch.setattr(forecast.data, "get_prices", lambda t: prices)
    monkeypatch.setattr(forecast.data, "option_expiries", lambda t: [])
    monkeypatch.setattr(forecast, "analyst_view", lambda t: {"available": False, "note": "x"})
    real = forecast.history_odds
    monkeypatch.setattr(forecast, "history_odds", lambda c, d, breaks=(): seen.setdefault(d, list(breaks)) or real(c, d, breaks))
    forecast.prepare("IVV.AX", 0.04)
    assert seen[21] == [pd.Timestamp(idx[100])]
