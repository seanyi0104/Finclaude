"""Single-stock analysis, the decision side (v1): where one stock stands now, and what the same signals were followed
by in its own history.

For 2 weeks (10 trading days) and half a year (126 trading days):
- indicators: the close against MA5/10/20/50/200, RSI(14), MACD(12, 26, 9), volume against the previous 20 days,
  ATR(14), the highs and lows of the last 20 days, half year and 52 weeks, strength against a sector ETF and the market
- every signal with the stock's own history: the share of days with the same signal that ended higher 10 / 126 trading
  days later, the median move, and the sample counted twice (days, and windows that do not overlap). Fewer than
  MIN_INDEPENDENT windows is 证据不足; a difference inside twice the random noise is 没用
- odds from history and options (finclaude.forecast) and Jev, compared only between sources with a scored record
- option ranges to each horizon and the move priced for the next earnings day; fundamentals for the half year
About the stock only: no holdings, account figures or IPS (CLAUDE.md 经验 2026-10-16), no buy or sell wording, and key
prices come only from moving averages and past highs and lows. Every probability goes into journal/forecasts.csv.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import pandas as pd
import yfinance as yf

from . import data, forecast, glossary, jev, metrics, quote, treg
from .config import RAW_DIR, ROOT, DataUnavailable, NotConfigured

# Official dates read on the company's own page or an SEC filing (News checks them); cards use these first.
OFFICIAL_EVENTS = ROOT / "journal" / "official_events.csv"

# horizon -> (trading days for history windows, calendar days for options and target dates)
HORIZONS = {"2w": (10, 14), "1m": (21, 30), "3m": (63, 91), "6m": (126, 182)}
HORIZON_NAMES = {"2w": "2 周", "1m": "1 个月", "3m": "3 个月", "6m": "半年"}
# What each horizon is for, in the user's words (2026-10-20): short-term trades, medium to long, long
HORIZON_ROLES = {"2w": "短线", "1m": "中长线", "3m": "中长线", "6m": "长线"}
MA_WINDOWS = (5, 10, 20, 50, 200)
LEVEL_WINDOWS = {"近 20 个交易日": 20, "近半年（126 个交易日）": 126, "近 52 周（252 个交易日）": 252}
RELATIVE_WINDOWS = (10, 20, 126)
SECTOR, MARKET = "SMH", "SPY"
FUND_NAMES = {"SMH": "半导体 ETF", "SOXX": "半导体 ETF", "SPY": "标普 500 ETF", "QQQ": "纳斯达克 100 ETF",
              "XLK": "科技板块 ETF", "XLY": "可选消费 ETF", "XLC": "通信服务 ETF", "XLF": "金融 ETF", "XLV": "医疗 ETF",
              "XLI": "工业 ETF", "XLE": "能源 ETF", "XLP": "必需消费 ETF", "XLU": "公用事业 ETF", "XLRE": "房地产 ETF",
              "XLB": "原材料 ETF"}
# yfinance sector -> the SPDR sector fund; semiconductors get SMH instead of the whole tech sector
SECTOR_FUNDS = {"Technology": "XLK", "Consumer Cyclical": "XLY", "Communication Services": "XLC",
                "Financial Services": "XLF", "Healthcare": "XLV", "Industrials": "XLI", "Energy": "XLE",
                "Consumer Defensive": "XLP", "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB"}
LEVERAGED_FUNDS = {"SOXL": "SMH", "ORCX": "XLK"}  # leveraged ETFs: the sector of what they track
# Funds report no earnings of their own. An index fund's card names the coming official earnings of its index's
# largest holdings (the issuer's list, as of its date); a single-stock leveraged fund follows its stock
# (forecast.UNDERLYING).
INDEX_FUNDS = {"SOXL": {"index": "NYSE 半导体指数", "leverage": 3, "source": "Direxion", "as_of": "2026-06-30",
                        "holdings": ("MU", "AMD", "NVDA", "INTC", "AVGO", "AMAT", "KLAC", "MRVL", "LRCX", "TSM")}}
FUND_EVENT_DAYS = 14  # the card's first horizon
RSI_HIGH, RSI_LOW = 70, 30
VOLUME_HEAVY, VOLUME_LIGHT = 1.5, 0.7
ATR_LOW, ATR_HIGH = 0.2, 0.8  # quantiles of ATR% over the past year
NEAR_HIGH, FAR_FROM_HIGH = -0.05, -0.20  # close against the highest close of the past 252 days
RS_DAYS = 20
MACD_WARMUP = 34  # 26 + 9 − 1 days before the signal line means anything
MIN_INDEPENDENT = 20
NOISE_Z, STRONG_Z = 2.0, 3.0
MIN_SCORED = 20  # forecasts scored before a source counts as having a record
DISCLAIMER = "教育用途，非投资建议；决定权在你。"

# signal -> today's state -> how the report says it ({sector} and {market} are filled in)
STATES = {
    "close_ma5": {"above": "收盘在 MA5 上方", "below": "收盘在 MA5 下方"},
    "close_ma10": {"above": "收盘在 MA10 上方", "below": "收盘在 MA10 下方"},
    "close_ma20": {"above": "收盘在 MA20 上方", "below": "收盘在 MA20 下方"},
    "close_ma50": {"above": "收盘在 MA50 上方", "below": "收盘在 MA50 下方"},
    "close_ma200": {"above": "收盘在 MA200 上方", "below": "收盘在 MA200 下方"},
    "ma50_ma200": {"above": "MA50 在 MA200 上方（金叉状态）", "below": "MA50 在 MA200 下方（死叉状态）"},
    "alignment": {"bull": "均线多头排列（MA5 > MA10 > MA20 > MA50 > MA200）",
                  "bear": "均线空头排列（MA5 < MA10 < MA20 < MA50 < MA200）",
                  "mixed": "均线交织（既不是多头排列也不是空头排列）"},
    "rsi": {"overbought": "RSI(14) ≥ 70（超买区）", "oversold": "RSI(14) ≤ 30（超卖区）", "neutral": "RSI(14) 在 30–70 之间"},
    "macd_signal": {"above": "MACD 在信号线上方（柱为正）", "below": "MACD 在信号线下方（柱为负）"},
    "macd_zero": {"above": "MACD 在零轴上方", "below": "MACD 在零轴下方"},
    "volume": {"heavy_up": "放量上涨（量比 ≥ 1.5 且收涨）", "heavy_down": "放量下跌（量比 ≥ 1.5 且收平或收跌）",
               "light": "缩量（量比 ≤ 0.7）", "normal": "量能正常（量比在 0.7–1.5 之间）"},
    "atr": {"high": "波动偏高（ATR% 在过去一年最高的 20% 里）", "low": "波动偏低（ATR% 在过去一年最低的 20% 里）",
            "middle": "波动居中（ATR% 在过去一年的中间 60%）"},
    "from_high": {"near": "离 52 周最高收盘不到 5%", "mid": "比 52 周最高收盘低 5%–20%", "far": "比 52 周最高收盘低 20% 以上"},
    "rs_sector": {"ahead": "近 20 个交易日跑赢 {sector}", "behind": "近 20 个交易日跑输 {sector}"},
    "rs_market": {"ahead": "近 20 个交易日跑赢 {market}", "behind": "近 20 个交易日跑输 {market}"},
    "vix": {"high": "VIX 在过去一年最高的 20% 里（市场紧张）", "low": "VIX 在过去一年最低的 20% 里（市场平静）",
            "middle": "VIX 在过去一年的中间 60%"},
    "vxn": {"high": "VXN 在过去一年最高的 20% 里（科技股紧张）", "low": "VXN 在过去一年最低的 20% 里（科技股平静）",
            "middle": "VXN 在过去一年的中间 60%"},
}
VIX = "^VIX"  # Cboe Volatility Index (yfinance): the fear gauge News and every Decision card refer to
VXN = "^VXN"  # Cboe Nasdaq-100 Volatility Index: the same gauge for the tech-heavy Nasdaq-100 (the user's choice, 2026-10-20)
GAUGES = {"vix": VIX, "vxn": VXN}  # signal / prep key -> data ticker
RSI_WORDS = {"overbought": "在 70 以上（超买区）", "oversold": "在 30 以下（超卖区）",
             "neutral": "在 30–70 之间，不在超买区也不在超卖区"}
VERDICTS = ("证据不足", "没用", "弱偏涨", "弱偏跌", "偏涨", "偏跌", "无可靠数据")
SOURCE_WORDS = {"hist_trend": "历史基准率", "options": "期权", "jev": "Jev"}


# --- indicators --------------------------------------------------------------------------------------------

def moving_averages(close: pd.Series, windows=MA_WINDOWS) -> pd.DataFrame:
    """Simple moving averages: the mean of the last n closes, one column per n."""
    return pd.DataFrame({n: close.rolling(n).mean() for n in windows})


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line = EMA(fast) − EMA(slow); signal = EMA(signal) of the line; hist = line − signal."""
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    trigger = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": trigger, "hist": line - trigger})


def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average true range with Wilder smoothing. True range = max(high − low, |high − previous close|,
    |low − previous close|), from the second day; the first ATR is their plain mean over n days, then
    ATR = (previous ATR × (n − 1) + true range) ÷ n."""
    prev = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - prev).abs(), (bars["low"] - prev).abs()],
                   axis=1).max(axis=1).iloc[1:]
    if len(tr) < n:
        raise ValueError(f"ATR{n} 至少需要 {n + 1} 天")
    values = tr.to_numpy(dtype=float)
    out = [values[:n].mean()]
    for x in values[n:]:
        out.append((out[-1] * (n - 1) + x) / n)
    return pd.Series(out, index=tr.index[n - 1:], name=f"atr{n}")


def volume_ratio(volume: pd.Series, n: int = 20) -> pd.Series:
    """Today's volume ÷ the mean volume of the previous n days (today left out)."""
    return volume / volume.shift(1).rolling(n).mean()


def price_levels(bars: pd.DataFrame, windows=LEVEL_WINDOWS) -> dict:
    """Highest high and lowest low of each recent window (today included), their dates and the close against them."""
    close = float(bars["close"].iloc[-1])
    out = {}
    for name, days in windows.items():
        recent = bars.iloc[-days:]
        hi, lo = recent["high"].idxmax(), recent["low"].idxmin()
        high, low = float(recent.loc[hi, "high"]), float(recent.loc[lo, "low"])
        out[name] = {"days": days, "high": high, "high_date": str(hi.date()), "low": low, "low_date": str(lo.date()),
                     "below_high": close / high - 1, "above_low": close / low - 1}
    return out


def _period_return(close: pd.Series, n: int) -> float | None:
    if len(close) <= n or pd.isna(close.iloc[-1]) or pd.isna(close.iloc[-1 - n]):
        return None
    return float(close.iloc[-1] / close.iloc[-1 - n] - 1)


def indicators(bars: pd.DataFrame, benchmarks: dict, sector: str = SECTOR, market: str = MARKET) -> dict:
    """Today's dashboard values."""
    close = bars["close"]
    now = float(close.iloc[-1])
    ma = moving_averages(close).iloc[-1]
    m = macd(close)
    a = atr(bars)
    a_pct = a / close.reindex(a.index)
    relative = {}
    for n in RELATIVE_WINDOWS:
        relative[n] = {"stock": _period_return(close, n)}
        for name in (sector, market):
            series = benchmarks.get(name)
            relative[n][name] = None if series is None else _period_return(series.reindex(close.index), n)
    return {"close": now, "date": str(close.index[-1].date()), "change_1d": now / float(close.iloc[-2]) - 1,
            "ma": {n: float(ma[n]) for n in MA_WINDOWS}, "vs_ma": {n: now / float(ma[n]) - 1 for n in MA_WINDOWS},
            "rsi": float(metrics.rsi_wilder(close).iloc[-1]),
            "macd": {"macd": float(m["macd"].iloc[-1]), "signal": float(m["signal"].iloc[-1]),
                     "hist": float(m["hist"].iloc[-1]), "hist_prev": float(m["hist"].iloc[-2])},
            "volume": float(bars["volume"].iloc[-1]), "volume_avg20": float(bars["volume"].iloc[-21:-1].mean()),
            "volume_ratio": float(volume_ratio(bars["volume"]).iloc[-1]),
            "atr": float(a.iloc[-1]), "atr_pct": float(a_pct.iloc[-1]), "atr_pct_median_1y": float(a_pct.iloc[-252:].median()),
            "levels": price_levels(bars), "relative": relative}


def trend_state(close: float, ma: dict) -> dict:
    """Long term from the close and MA50 against MA200. Short term from the close against MA10 and MA20: above both is
    向上 once MA10 is above MA20 (转强 before), below both is 向下 once MA10 is below MA20 (转弱 before), between is 震荡."""
    if close > ma[200] and ma[50] > ma[200]:
        long = "上升"
    elif close < ma[200] and ma[50] < ma[200]:
        long = "下降"
    else:
        long = "转换中"
    if close > ma[10] and close > ma[20]:
        short = "向上" if ma[10] > ma[20] else "转强"
    elif close < ma[10] and close < ma[20]:
        short = "向下" if ma[10] < ma[20] else "转弱"
    else:
        short = "震荡"
    return {"long": long, "short": short}


# --- signals -----------------------------------------------------------------------------------------------

def _labels(conditions, labels, default, index, defined) -> pd.Series:
    values = np.select([np.asarray(c, dtype=bool) for c in conditions], labels, default)
    return pd.Series(values, index=index, dtype=object).where(np.asarray(defined, dtype=bool))


def _side(a: pd.Series, b: pd.Series) -> pd.Series:
    return _labels([a > b], ["above"], "below", a.index, a.notna() & b.notna())


def signal_frame(bars: pd.DataFrame, benchmarks: dict, sector: str = SECTOR, market: str = MARKET) -> pd.DataFrame:
    """Each signal's state on every day (NaN until it can be computed); the columns are the keys of STATES."""
    close = bars["close"]
    index = close.index
    ma = moving_averages(close)
    frame = pd.DataFrame(index=index)
    for n in MA_WINDOWS:
        frame[f"close_ma{n}"] = _side(close, ma[n])
    frame["ma50_ma200"] = _side(ma[50], ma[200])
    ordered = [ma[n] for n in MA_WINDOWS]
    bull = np.logical_and.reduce([(a > b).to_numpy() for a, b in zip(ordered, ordered[1:])])
    bear = np.logical_and.reduce([(a < b).to_numpy() for a, b in zip(ordered, ordered[1:])])
    frame["alignment"] = _labels([bull, bear], ["bull", "bear"], "mixed", index, ma[200].notna())
    rsi = metrics.rsi_wilder(close).reindex(index)
    frame["rsi"] = _labels([rsi >= RSI_HIGH, rsi <= RSI_LOW], ["overbought", "oversold"], "neutral", index, rsi.notna())
    m = macd(close)
    warm = np.arange(len(index)) >= MACD_WARMUP
    frame["macd_signal"] = _side(m["macd"], m["signal"]).where(warm)
    frame["macd_zero"] = _side(m["macd"], pd.Series(0.0, index=index)).where(warm)
    ratio = volume_ratio(bars["volume"])
    rose = close > close.shift(1)
    frame["volume"] = _labels([(ratio >= VOLUME_HEAVY) & rose, ratio >= VOLUME_HEAVY, ratio <= VOLUME_LIGHT],
                              ["heavy_up", "heavy_down", "light"], "normal", index, ratio.notna())
    a_pct = atr(bars).reindex(index) / close
    lo, hi = a_pct.rolling(252).quantile(ATR_LOW), a_pct.rolling(252).quantile(ATR_HIGH)
    frame["atr"] = _labels([a_pct >= hi, a_pct <= lo], ["high", "low"], "middle", index, hi.notna() & a_pct.notna())
    off = close / close.rolling(252).max() - 1
    frame["from_high"] = _labels([off >= NEAR_HIGH, off >= FAR_FROM_HIGH], ["near", "mid"], "far", index, off.notna())
    for key, name in (("rs_sector", sector), ("rs_market", market)):
        series = benchmarks.get(name)
        if series is None:
            frame[key] = pd.Series(np.nan, index=index, dtype=object)
            continue
        bench = series.reindex(index)
        gap = (close / close.shift(RS_DAYS) - 1) - (bench / bench.shift(RS_DAYS) - 1)
        frame[key] = _labels([gap > 0], ["ahead"], "behind", index, gap.notna())
    for key, name in GAUGES.items():
        fear = benchmarks.get(name)
        if fear is None:
            frame[key] = pd.Series(np.nan, index=index, dtype=object)
            continue
        fear = fear.reindex(index)
        top, bottom = fear.rolling(252).quantile(0.8), fear.rolling(252).quantile(0.2)
        frame[key] = _labels([fear >= top, fear <= bottom], ["high", "low"], "middle", index, top.notna() & fear.notna())
    return frame


def vix_view(vix: pd.Series, day: dt.date) -> dict | None:
    """A volatility index (VIX, VXN) at the close of `day`: level, one-day and five-day change, the share of the past
    252 closes below it."""
    vix = vix.dropna().loc[:pd.Timestamp(day)]
    if len(vix) < 6:
        return None
    now, past_year = float(vix.iloc[-1]), vix.iloc[-253:-1]
    return {"close": now, "date": str(vix.index[-1].date()), "change_1d": now / float(vix.iloc[-2]) - 1,
            "change_5d": now / float(vix.iloc[-6]) - 1, "pct_1y": float((past_year < now).mean()),
            "median_1y": float(past_year.median())}


def sector_etf(ticker: str, info: dict | None) -> str:
    """The sector fund a stock is compared with: SMH for semiconductors, otherwise the SPDR fund of its yfinance
    sector; a leveraged ETF uses the sector of what it tracks; SPY (the market) when nothing is known."""
    if ticker.upper() in LEVERAGED_FUNDS:
        return LEVERAGED_FUNDS[ticker.upper()]
    info = info or {}
    if "semiconductor" in str(info.get("industry", "")).lower():
        return "SMH"
    return SECTOR_FUNDS.get(info.get("sector"), MARKET)


def describe(signal: str, state: str, sector: str = SECTOR, market: str = MARKET) -> str:
    return STATES[signal][state].format(sector=sector, market=market)


# --- each signal's own history -----------------------------------------------------------------------------

def forward_returns(close: pd.Series, days: int) -> pd.Series:
    """close[t + days] ÷ close[t] − 1; empty for the last `days` days."""
    return close.shift(-days) / close - 1


def independent_count(positions, gap: int) -> int:
    """How many windows of `gap` days starting at these day positions fit without overlapping (taken from the start)."""
    count, free_from = 0, None
    for p in positions:
        if free_from is None or p >= free_from:
            count += 1
            free_from = p + gap
    return count


def _verdict(z: float | None, independent: int) -> str:
    if z is None or independent < MIN_INDEPENDENT:
        return "证据不足"
    if abs(z) < NOISE_Z:
        return "没用"
    way = "偏涨" if z > 0 else "偏跌"
    return way if abs(z) >= STRONG_Z else f"弱{way}"


def evidence(states: pd.Series, fwd: pd.Series, today, days: int) -> dict:
    """What followed the days in today's state, against all days on which the signal could be computed.

    noise = √(p0 × (1 − p0) ÷ independent windows) with p0 the share of all days that ended higher; z = difference ÷ noise.
    """
    known = (states.notna() & fwd.notna()).to_numpy()
    same = known & (states == today).to_numpy()
    base, hits = fwd[known], fwd[same]
    out = {"state": today, "days": int(same.sum()), "independent": independent_count(np.flatnonzero(same), days),
           "base_days": int(known.sum()), "base_independent": independent_count(np.flatnonzero(known), days),
           "base_p_up": float((base > 0).mean()) if len(base) else None,
           "base_median": float(base.median()) if len(base) else None,
           "start": str(base.index[0].date()) if len(base) else None, "end": str(base.index[-1].date()) if len(base) else None,
           "p_up": None, "median": None, "diff": None, "noise": None, "z": None}
    p0 = out["base_p_up"]
    if hits.empty or p0 is None:
        out["verdict"] = "证据不足"
        return out
    p1 = float((hits > 0).mean())
    out.update(p_up=p1, median=float(hits.median()), diff=p1 - p0)
    if out["independent"] and 0 < p0 < 1:
        se = math.sqrt(p0 * (1 - p0) / out["independent"])
        out.update(noise=2 * se, z=(p1 - p0) / se)
    out["verdict"] = _verdict(out["z"], out["independent"])
    return out


def baseline(close: pd.Series, days: int) -> dict:
    """All days of the history, the row every signal is compared with."""
    return evidence(pd.Series("all", index=close.index), forward_returns(close, days), "all", days)


def evidence_table(frame: pd.DataFrame, close: pd.Series, horizons=HORIZONS) -> dict:
    """horizon -> one row per signal in today's state."""
    today = frame.iloc[-1]
    table = {}
    for h, (days, _) in horizons.items():
        fwd = forward_returns(close, days)
        rows = []
        for key in frame.columns:
            state = today[key]
            if pd.isna(state):
                rows.append({"signal": key, "horizon": h, "state": None, "z": None, "verdict": "无可靠数据"})
                continue
            rows.append({"signal": key, "horizon": h, **evidence(frame[key], fwd, state, days)})
        table[h] = rows
    return table


def key_signals(table: dict, k: int = 2) -> list[dict]:
    """The signals whose history differs most from all days (largest |z|), among those with enough windows."""
    best = {}
    for rows in table.values():
        for r in rows:
            if r.get("z") is None or r["verdict"] in ("证据不足", "无可靠数据"):
                continue
            if r["signal"] not in best or abs(r["z"]) > abs(best[r["signal"]]["z"]):
                best[r["signal"]] = r
    return sorted(best.values(), key=lambda r: -abs(r["z"]))[:k]


# --- options -----------------------------------------------------------------------------------------------

def event_move(iv_before: float, iv_after: float, days_after: int) -> float | None:
    """One standard deviation of the earnings-day move. The expiry after the event carries the normal variance plus
    the event's: iv_after² × T = iv_before² × T + event², so event = √((iv_after² − iv_before²) × T), T = days ÷ 365.
    None when the later expiry prices no extra volatility."""
    extra = (iv_after ** 2 - iv_before ** 2) * days_after / 365
    return math.sqrt(extra) if extra > 0 else None


def option_views(ticker: str, spot: float, valued_on: dt.date, rate: float, earnings_dates: list[str]) -> dict:
    """At-the-money odds and ranges per horizon, and the earnings-day move from the expiries either side of it."""
    try:
        expiries = data.option_expiries(ticker)
    except Exception as e:  # noqa: BLE001 - the report says why options are missing
        expiries, reason = [], f"读不到期权到期日（{type(e).__name__}）"
    else:
        reason = "没有挂牌期权"
    chains = {}

    def odds(expiry, days=None):
        if expiry not in chains:
            chains[expiry] = data.get_option_chain(ticker, expiry)
        return forecast.option_odds(chains[expiry], spot, valued_on, expiry, rate, horizon_days=days)

    out = {}
    for h, (_, calendar_days) in HORIZONS.items():
        try:
            if not expiries:
                raise DataUnavailable(reason)
            out[h] = odds(forecast.pick_expiry(expiries, valued_on, calendar_days), calendar_days)
        except DataUnavailable as e:
            out[h] = {"p_up": None, "note": str(e)}
    out["event"] = _event_view(expiries, earnings_dates, valued_on, odds)
    return out


def _event_view(expiries: list[str], dates: list[str], valued_on: dt.date, odds) -> dict:
    if not dates:
        return {"note": "没有下一次财报的日期"}
    first, last = dt.date.fromisoformat(min(dates)), dt.date.fromisoformat(max(dates))
    # expiries inside the coming week count here: an event a few days out needs the expiry just before it
    before = [e for e in expiries if valued_on < dt.date.fromisoformat(e) < first]
    after = [e for e in expiries if dt.date.fromisoformat(e) > last]
    if not before or not after:
        return {"note": "财报前后没有合适的期权到期日"}
    try:
        b, a = odds(before[-1]), odds(after[0])
    except DataUnavailable as e:
        return {"note": str(e)}
    view = {"dates": dates, "before": before[-1], "after": after[0], "iv_before": b["iv"], "iv_after": a["iv"],
            "days_before": b["days"], "days_after": a["days"], "strike_before": b["strike"], "strike_after": a["strike"],
            "move": event_move(b["iv"], a["iv"], a["days"])}
    if view["move"] is None:
        view["note"] = f"财报后到期的隐含波动率 {a['iv']:.2%} 不比财报前的 {b['iv']:.2%} 高，期权没有为财报额外定价"
    return view


# --- fundamentals and the next earnings date ---------------------------------------------------------------

STATEMENT_ROWS = {"revenue": "Total Revenue", "operating_income": "Operating Income", "net_income": "Net Income",
                  "eps": "Diluted EPS"}


def _fiscal_year(end: pd.Timestamp, fy_end_month: int) -> int:
    return end.year if end.month <= fy_end_month else end.year + 1


def fiscal_label(end, fy_end_month: int) -> str:
    """The quarter ending in `end`'s month, as fiscal year and quarter plus the calendar months it covers."""
    end = pd.Timestamp(end)
    quarter = ((end.month - fy_end_month - 1) % 12) // 3 + 1
    start = end - pd.DateOffset(months=2)
    months = (f"{end.year} 年 {start.month}–{end.month} 月" if start.year == end.year
              else f"{start.year} 年 {start.month} 月–{end.year} 年 {end.month} 月")
    return f"FY{_fiscal_year(end, fy_end_month)} Q{quarter}（{months}）"


def _statement(frame) -> list[dict]:
    """Newest period first; periods without revenue are left out."""
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Total Revenue" not in frame.index:
        return []
    out = []
    for end in sorted(frame.columns, reverse=True):
        column = frame[end]
        if pd.isna(column.get("Total Revenue")):
            continue
        out.append({"end": pd.Timestamp(end), **{k: float(column[row]) if row in column.index and pd.notna(column[row])
                                                 else None for k, row in STATEMENT_ROWS.items()}})
    return out


def _growth(new, old) -> float | None:
    return None if new is None or not old else new / old - 1


def _table(frame, fields: dict) -> dict:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {}
    out = {}
    for period, row in frame.iterrows():
        out[str(period)] = {k: (float(row[c]) if c in row.index and pd.notna(row[c]) else None) for k, c in fields.items()}
    return out


def fundamentals_view(raw: dict, price: float, as_of) -> dict:
    """Revenue and profit by quarter and year, valuation worked out from them, estimates and their revisions."""
    fy_end = int(raw.get("fy_end_month") or 12)
    quarters = _statement(raw.get("quarterly"))
    for i, q in enumerate(quarters):
        q["label"] = fiscal_label(q["end"], fy_end)
        q["revenue_qoq"] = _growth(q["revenue"], quarters[i + 1]["revenue"]) if i + 1 < len(quarters) else None
        q["revenue_yoy"] = _growth(q["revenue"], quarters[i + 4]["revenue"]) if i + 4 < len(quarters) else None
    years = _statement(raw.get("annual"))
    for i, y in enumerate(years):
        y["label"] = f"FY{_fiscal_year(y['end'], fy_end)}"
        older = years[i + 1] if i + 1 < len(years) else {}
        y["revenue_yoy"] = _growth(y["revenue"], older.get("revenue"))
        y["net_income_yoy"] = _growth(y["net_income"], older.get("net_income"))
    ttm = None
    last4 = quarters[:4]
    if len(last4) == 4 and all(q["eps"] is not None and q["revenue"] is not None for q in last4):
        ttm = {"eps": sum(q["eps"] for q in last4), "revenue": sum(q["revenue"] for q in last4),
               "eps_parts": [q["eps"] for q in last4], "labels": [q["label"] for q in last4]}
    shares = raw.get("shares")
    cap = price * shares if shares else None
    estimates = _table(raw.get("earnings_estimate"), {"avg": "avg", "low": "low", "high": "high", "n": "numberOfAnalysts"})
    revenue_estimates = _table(raw.get("revenue_estimate"),
                               {"avg": "avg", "low": "low", "high": "high", "n": "numberOfAnalysts"})
    trend = _table(raw.get("eps_trend"), {"current": "current", "30d": "30daysAgo", "90d": "90daysAgo"})
    for t in trend.values():
        t["change_30d"], t["change_90d"] = _growth(t["current"], t["30d"]), _growth(t["current"], t["90d"])
    revisions = _table(raw.get("eps_revisions"), {"up_30d": "upLast30days", "down_30d": "downLast30days"})
    periods = {}
    if quarters:
        coming = quarters[0]["end"] + pd.offsets.MonthEnd(3)
        year = _fiscal_year(coming, fy_end)
        periods = {"0q": fiscal_label(coming, fy_end), "+1q": fiscal_label(coming + pd.offsets.MonthEnd(3), fy_end),
                   "0y": f"FY{year}", "+1y": f"FY{year + 1}"}
    valuation = {"price": price, "shares": shares, "market_cap": cap,
                 "pe_ttm": price / ttm["eps"] if ttm and ttm["eps"] > 0 else None,
                 "ps_ttm": cap / ttm["revenue"] if ttm and cap else None,
                 "pe_forward": {p: price / e["avg"] for p, e in estimates.items() if p in ("0y", "+1y") and e.get("avg")}}
    return {"available": True, "source": "yfinance", "as_of": str(as_of), "fy_end_month": fy_end, "quarters": quarters,
            "years": years, "ttm": ttm, "valuation": valuation, "estimates": estimates,
            "revenue_estimates": revenue_estimates, "periods": periods, "trend": trend, "revisions": revisions}


def _save_raw(name: str, record: dict) -> None:
    path = RAW_DIR / "yfinance" / f"{name}_{dt.date.today().isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = {k: v.reset_index().to_dict(orient="records") if isinstance(v, pd.DataFrame) else v for k, v in record.items()}
    path.write_text(json.dumps(frames, ensure_ascii=False, default=str, indent=1), encoding="utf-8")


def _fetch_fundamentals(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info or {}
    last_fy = info.get("lastFiscalYearEnd")
    raw = {"quarterly": t.quarterly_income_stmt, "annual": t.income_stmt, "shares": info.get("sharesOutstanding"),
           "fy_end_month": dt.datetime.fromtimestamp(last_fy, dt.timezone.utc).month if last_fy else None,
           "earnings_estimate": t.earnings_estimate, "revenue_estimate": t.revenue_estimate,
           "eps_trend": t.eps_trend, "eps_revisions": t.eps_revisions}
    _save_raw(f"{ticker}_fundamentals", {k: (v.T if k in ("quarterly", "annual") and isinstance(v, pd.DataFrame) else v)
                                         for k, v in raw.items()})
    return raw


def official_earnings(ticker: str, after: dt.date) -> dict | None:
    """The next earnings date after `after` that was verified on an official page (journal/official_events.csv:
    ticker, event, date, time, url, checked), or None."""
    if not OFFICIAL_EVENTS.exists():
        return None
    events = pd.read_csv(OFFICIAL_EVENTS, dtype=str).fillna("")
    mine = events[(events["ticker"] == ticker.upper()) & (events["event"] == "earnings") & (events["date"] > str(after))]
    if mine.empty:
        return None
    row = mine.sort_values("date").iloc[0]
    return {"date": row["date"], "time": row.get("time", ""), "url": row["url"], "checked": row["checked"]}


def fund_profile(ticker: str) -> dict | None:
    """What a fund follows: an index (INDEX_FUNDS) or one stock at a daily leverage (forecast.UNDERLYING); None for
    a company."""
    ticker = ticker.upper()
    if ticker in INDEX_FUNDS:
        return {"kind": "index", **INDEX_FUNDS[ticker]}
    if ticker in forecast.UNDERLYING:
        stock, leverage = forecast.UNDERLYING[ticker]
        return {"kind": "stock", "stock": stock, "leverage": leverage}
    return None


def fund_earnings(fund: dict, valued_on: dt.date) -> dict:
    """An index fund's coming events: the official earnings of its index holdings in the next FUND_EVENT_DAYS days
    (no earnings day of its own for the options to price)."""
    end = str(valued_on + dt.timedelta(days=FUND_EVENT_DAYS))
    coming = [{"ticker": t, **found} for t in fund["holdings"]
              if (found := official_earnings(t, valued_on)) and found["date"] <= end]
    return {"dates": [], "sources": [], "official": None, "fund": fund,
            "holdings": sorted(coming, key=lambda h: h["date"])}


def fund_fundamentals(fund: dict) -> dict:
    return {"available": False, "fund": fund, "note": "基金没有自己的财报"}


def next_earnings(ticker: str, valued_on: dt.date, official: dict | None = None) -> dict:
    """Coming earnings dates from yfinance (calendar and earnings_dates), with the company's own notice when given.
    `official` is what was read on the company's site: {"date", "url"} once announced, else a note of the check."""
    t = yf.Ticker(ticker)
    sources = []
    try:
        for day in (t.calendar or {}).get("Earnings Date") or []:
            if day > valued_on:
                sources.append([str(day), "yfinance calendar"])
    except Exception:  # noqa: BLE001 - a missing source is listed as missing
        pass
    try:
        listed = t.get_earnings_dates(limit=6)
        for when, row in listed.iterrows():
            day = when.tz_convert("America/New_York").date() if when.tzinfo else when.date()
            if day > valued_on and pd.isna(row.get("Reported EPS")):
                sources.append([str(day), "yfinance earnings_dates"])
    except Exception:  # noqa: BLE001
        pass
    _save_raw(f"{ticker}_earnings_dates", {"sources": sources, "official": official})
    dates = [official["date"]] if official and official.get("date") else sorted({d for d, _ in sources})
    return {"dates": dates, "sources": sources, "official": official}


# --- cross-checks ------------------------------------------------------------------------------------------

def compare(what: str, ours: tuple, theirs: tuple, tolerance: float, absolute: bool = False, fmt: str = ",.2f") -> dict:
    """One number from two sources. ours = (source, value); theirs = (source, value, time stamp). The difference is
    theirs ÷ ours − 1, or theirs − ours when `absolute`; within `tolerance` they agree."""
    a, b = ours[1], theirs[1]
    for source, value in ((ours[0], a), (theirs[0], b)):
        if value is None or math.isnan(value):
            return {"what": what, "note": f"{source} 没有给出数值，无法核对"}
    diff = (b - a) if absolute else (b / a - 1 if a else None)
    return {"what": what, "ours": ours, "theirs": theirs, "diff": diff, "absolute": absolute, "fmt": fmt,
            "ok": diff is not None and abs(diff) <= tolerance}


def _yfinance_close(ticker: str, day: pd.Timestamp) -> tuple[str, float, str]:
    """yfinance's own close for `day` (not adjusted). Its daily history sometimes leaves the latest day empty; then the
    quote's regularMarketPrice is used and named as such."""
    t = yf.Ticker(ticker)
    closes = t.history(period="5d", auto_adjust=False)["Close"]
    closes.index = closes.index.tz_localize(None).normalize()
    value = closes.get(day)
    if value is not None and not pd.isna(value):
        return "yfinance", float(value), f"{day.date()} 收盘"
    info = t.info or {}
    price, stamp = info.get("regularMarketPrice"), info.get("regularMarketTime")
    if price is None:
        raise DataUnavailable(f"yfinance 的 {day.date()} 收盘是空的")
    when = (pd.Timestamp(stamp, unit="s", tz="UTC").tz_convert("America/New_York").strftime("%Y-%m-%d %H:%M:%S") + " 美东"
            if stamp else "时间未给出")
    return f"yfinance regularMarketPrice（日线里 {day.date()} 的收盘是空的）", float(price), when


def _day_close(row: pd.Series, day: pd.Timestamp) -> tuple[float, str, bool]:
    """Moomoo's price for `day`'s close, its label, and whether a later session has opened: the last price while the
    snapshot is still on that day or before the next regular session opens (09:30 New York); after that the last
    price and the volume belong to the new session, and the previous close is `day`'s close (2026-10-19 10:00 New
    York: four false mismatches before this rule)."""
    stamp = pd.Timestamp(str(row["update_time"])[:19])
    if stamp.date() > day.date() and (stamp.hour, stamp.minute) >= (9, 30):
        return float(row["prev_close_price"]), f"{stamp:%Y-%m-%d %H:%M:%S} 美东的昨收", True
    return float(row["last_price"]), f"{stamp:%Y-%m-%d %H:%M:%S} 美东", False


def cross_checks(ticker: str, bars: pd.DataFrame, benchmarks: dict, options: dict, fundamentals: dict,
                 sector: str = SECTOR, market: str = MARKET) -> dict:
    """The day's close against yfinance and Moomoo; volume, 52-week range, valuation and the option trades behind the
    volatility against Moomoo's trades from the same close. Moomoo's own implied volatility is worked out when it is
    read (2026-10-19 pre-market: three days fewer to expiry, so higher from the same Friday price); it is added to
    `options` for the report and not checked."""
    out = {"moomoo": None, "rows": []}
    last, day = bars.iloc[-1], bars.index[-1]
    try:
        out["rows"].append(compare("收盘价（美元）", ("Treg · Tiingo", float(last["raw_close"])),
                                   _yfinance_close(ticker, day), 0.0005))
    except Exception as e:  # noqa: BLE001
        out["rows"].append({"what": "收盘价（美元）", "note": f"读不到 yfinance 的 {day.date()} 收盘价（{e}）"})
    for name in GAUGES.values():
        gauge, label = benchmarks.get(name), name.lstrip("^")
        if gauge is None or day not in gauge.index:
            continue
        try:
            official = data.get_cboe_history(label)
        except DataUnavailable as e:
            out["rows"].append({"what": f"{label} 收盘", "note": f"Cboe 官方数读不到（{e}）"})
            continue
        out["rows"].append(compare(f"{label} 收盘", ("yfinance", float(gauge[day])),
                                   ("Cboe 官方", float(official[day]), f"{day.date()} 收盘"), 0.0005)
                           if day in official.index else
                           {"what": f"{label} 收盘", "note": f"Cboe 官方文件里还没有 {day.date()} 的收盘"})
    try:
        rights = quote.quote_rights()
        snap = quote.snapshot([f"US.{t}" for t in (ticker, sector, market)]).set_index("code")
    except (NotConfigured, DataUnavailable) as e:
        out["moomoo"] = {"note": str(e)}
        return out
    row = snap.loc[f"US.{ticker}"]
    when = f"{str(row['update_time'])[:19]} 美东"
    out["moomoo"] = {"rights": rights, "time": str(row["update_time"])}
    close, close_when, moved_on = _day_close(row, day)
    year = price_levels(bars, {"52 周": 252})["52 周"]
    out["rows"].append(compare("收盘价（美元）", ("Treg · Tiingo", float(last["raw_close"])), ("Moomoo", close, close_when),
                               0.0005))
    if moved_on:
        out["rows"].append({"what": "成交量（股）", "note": f"Moomoo 已在 {when} 的交易时段，成交量是新一天的，不比"})
    else:
        out["rows"].append(compare("成交量（股）", ("Treg · Tiingo", float(last["volume"])), ("Moomoo", float(row["volume"]), when),
                                   0.005, fmt=",.0f"))
    out["rows"] += [
        compare("52 周最高（美元）", ("Treg · Tiingo", year["high"]), ("Moomoo", float(row["highest52weeks_price"]), when), 0.005),
        compare("52 周最低（美元）", ("Treg · Tiingo", year["low"]), ("Moomoo", float(row["lowest52weeks_price"]), when), 0.005)]
    for name in (sector, market):
        series, other = benchmarks.get(name), snap.loc[f"US.{name}"]
        if series is not None:
            other_close, other_when, _ = _day_close(other, day)
            out["rows"].append(compare(f"{name} 收盘价（美元）", ("Treg · Tiingo", float(series.iloc[-1])),
                                       ("Moomoo", other_close, other_when), 0.0005))
    # Moomoo's valuation moves with the price of the moment: taken back to the same close before comparing
    scale = close / float(row["last_price"]) if moved_on else 1.0
    worded = f"{when}，按昨收换算" if moved_on else when
    value = fundamentals.get("valuation") or {}
    if value.get("pe_ttm"):
        out["rows"].append(compare("市盈率 TTM（倍）", ("yfinance 财报算出", value["pe_ttm"]),
                                   ("Moomoo", float(row["pe_ttm_ratio"]) * scale, worded), 0.01))
    if value.get("market_cap"):
        out["rows"].append(compare("市值（亿美元）", ("股价 × yfinance 股数", value["market_cap"] / 1e8),
                                   ("Moomoo", float(row["total_market_val"]) * scale / 1e8, worded), 0.01, fmt=",.0f"))
    for h in HORIZONS:
        o = options.get(h) or {}
        if o.get("iv") is None:
            continue
        contract = f"（{o['expiry']} 到期，行权价 {o['strike']:.2f}）"
        try:
            theirs = quote.option_ivs(f"US.{ticker}", o["expiry"], o["strike"])
        except (NotConfigured, DataUnavailable) as e:
            out["rows"].append({"what": f"{HORIZON_NAMES[h]}期权成交价{contract}", "note": f"Moomoo 没有给出（{e}）"})
            continue
        o["moomoo_iv"] = theirs["iv"]
        for leg, word in (("call", "看涨"), ("put", "看跌")):
            ours, mine = (o.get("prices") or {}).get(leg), theirs.get(leg)
            if ours is None or mine is None:
                continue
            what, stamp = f"{HORIZON_NAMES[h]}{word}期权成交价{contract}", str(mine["update_time"])
            if stamp[:10] != str(day.date()):
                out["rows"].append({"what": what, "note": f"Moomoo 的最新成交在 {stamp[:16]} 美东，不是 {day.date()} "
                                                          "收盘前的，不比"})
                continue
            out["rows"].append(compare(what, ("yfinance", ours), ("Moomoo", float(mine["last_price"]), f"{stamp} 美东"), 0.01))
    return out


# --- putting it together -----------------------------------------------------------------------------------

def assemble(ticker: str, bars: pd.DataFrame, benchmarks: dict, *, made_on: dt.date, rate: tuple, options: dict,
             analysts: dict, fundamentals: dict, earnings: dict, checks: dict, board: pd.DataFrame,
             jev: dict | None = None, costs: dict | None = None, sector: str = SECTOR, market: str = MARKET,
             sector_weight: float | None = None) -> dict:
    """Everything the report needs, computed from data already fetched (no network)."""
    close = bars["close"]
    frame = signal_frame(bars, benchmarks, sector, market)
    ind = indicators(bars, benchmarks, sector, market)
    prep = {"ticker": ticker, "made_on": made_on, "valued_on": close.index[-1].date(), "close": float(close.iloc[-1]),
            "price_source": str(bars["source"].iloc[-1]) if "source" in bars.columns else "",
            "start": str(close.index[0].date()), "sector": sector, "market": market, "sector_weight": sector_weight,
            "benchmark_starts": {n: str(s.dropna().index[0].date()) for n, s in benchmarks.items()},
            "indicators": ind, "states": {k: (None if pd.isna(v) else v) for k, v in frame.iloc[-1].items()},
            "trend": trend_state(float(close.iloc[-1]), ind["ma"]),
            "evidence": evidence_table(frame, close),
            "baseline": {h: baseline(close, days) for h, (days, _) in HORIZONS.items()},
            "history": {h: forecast.history_odds(close, days) for h, (days, _) in HORIZONS.items()},
            "rate": rate, "options": options, "analysts": analysts, "fundamentals": fundamentals,
            "earnings": earnings, "checks": checks, "board": board, "jev": jev, "costs": costs or {},
            "chart": price_chart_svg(bars, ticker=ticker),
            **{key: vix_view(benchmarks[name], close.index[-1].date()) if benchmarks.get(name) is not None else None
               for key, name in GAUGES.items()}}
    prep["key_signals"] = key_signals(prep["evidence"])
    return prep


def _sector_weight(sector: str, ticker: str) -> float | None:
    try:
        holdings = yf.Ticker(sector).funds_data.top_holdings
        return float(holdings.loc[ticker, "Holding Percent"]) if ticker in holdings.index else None
    except Exception:  # noqa: BLE001 - only used for one sentence in the lessons
        return None


def _info(ticker: str) -> dict:
    try:
        return yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001 - without it the stock is compared with the market
        return {}


def prepare(ticker: str = "NVDA", sector: str | None = None, market: str = MARKET, official: dict | None = None,
            made_on: dt.date | None = None) -> dict:
    """Fetch everything (Treg · Tiingo daily bars, yfinance options and fundamentals, Moomoo quotes) and assemble.
    The sector fund defaults to the stock's own (sector_etf); the earnings date to a verified official one when
    journal/official_events.csv has it, else `official` (what a check of the company's page found)."""
    made_on = made_on or dt.date.today()
    fund = fund_profile(ticker)
    sector = sector or sector_etf(ticker, None if fund else _info(ticker))
    bars = data.get_bars(ticker)
    benchmarks = {name: data.get_prices(name)["close"] for name in (sector, market, VIX, VXN)}
    close = bars["close"]
    spot, valued_on = float(close.iloc[-1]), close.index[-1].date()
    rate = forecast.risk_free()
    if fund:
        fundamentals = fund_fundamentals(fund)
    else:
        try:
            fundamentals = fundamentals_view(_fetch_fundamentals(ticker), spot, as_of=made_on)
        except Exception as e:  # noqa: BLE001
            fundamentals = {"available": False, "note": f"读不到基本面（{type(e).__name__}）"}
    if fund and fund["kind"] == "index":
        earnings = fund_earnings(fund, valued_on)
    else:
        stock = fund["stock"] if fund else ticker
        earnings = next_earnings(stock, valued_on, official_earnings(stock, valued_on) or official)
        earnings["label"] = f"标的 {stock} 下一季" if fund else (fundamentals.get("periods") or {}).get("0q", "下一季")
        if fund:
            earnings["fund"] = fund
    options = option_views(ticker, spot, valued_on, rate[0], earnings["dates"])
    try:
        analysts = forecast.analyst_view(ticker, today=made_on)
    except Exception as e:  # noqa: BLE001
        analysts = {"available": False, "note": f"读不到分析师数据（{type(e).__name__}）"}
    checks = cross_checks(ticker, bars, benchmarks, options, fundamentals, sector, market)
    return assemble(ticker, bars, benchmarks, made_on=made_on, rate=rate, options=options, analysts=analysts,
                    fundamentals=fundamentals, earnings=earnings, checks=checks, board=forecast.scoreboard(),
                    costs=treg_costs([ticker, sector, market], made_on), sector=sector, market=market,
                    sector_weight=_sector_weight(sector, ticker))


def treg_costs(tickers: list[str], day: dt.date) -> dict:
    """Treg calls made on `day` for these tickers (by any run: a later run before the next US close reads the saved
    file instead)."""
    log = treg.call_log()
    wanted = {t.lower() for t in tickers}
    rows = []
    for _, r in log.iterrows():
        try:
            asked = json.loads(r["params"]).get("ticker")
        except (TypeError, ValueError):
            continue
        if str(r["as_of"]).startswith(day.isoformat()) and asked in wanted:
            rows.append((asked.upper(), float(r["cost_usd"] or 0)))
    return {"treg_calls": len(rows), "treg_usd": sum(c for _, c in rows), "treg_tickers": sorted({t for t, _ in rows})}


# --- chart -------------------------------------------------------------------------------------------------

CHART_LINES = (("close", "收盘", "#0969da", 2.2, ""), (10, "MA10", "#bf8700", 1.3, ""), (20, "MA20", "#cf222e", 1.3, ""),
               (50, "MA50", "#1a7f37", 1.4, "5 3"), (200, "MA200", "#8250df", 1.6, "5 3"))


def _ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(int((hi - first) // step) + 1)]


def price_chart_svg(bars: pd.DataFrame, days: int = 126, ticker: str = "", width: int = 860, height: int = 340) -> str:
    """The close and MA10/20/50/200 over the last `days` days with that window's high and low, as one line of SVG."""
    close = bars["close"]
    ma = moving_averages(close)
    frame = pd.DataFrame({"close": close, **{n: ma[n] for n in (10, 20, 50, 200)}}).iloc[-days:]
    level = price_levels(bars, {"window": days})["window"]
    lo = min(float(frame.min().min()), level["low"])
    hi = max(float(frame.max().max()), level["high"])
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 48, 104, 12, 28
    halo = 'paint-order="stroke" stroke="var(--bg, #fff)" stroke-width="3"'  # labels stay readable over the lines
    plot_w, plot_h = width - left - right, height - top - bottom

    def x(i):
        return left + i * plot_w / (len(frame) - 1)

    def y(v):
        return top + (hi - v) * plot_h / (hi - lo)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="{ticker} 近 {days} 个交易日收盘价和均线" style="max-width:{width}px;font:12px sans-serif">']
    for v in _ticks(lo, hi):
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="currentColor" '
                     f'stroke-opacity="0.12"/><text x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end" '
                     f'fill="currentColor" fill-opacity="0.7">{v:g}</text>')
    seen = set()
    for i, day in enumerate(frame.index):
        if (day.year, day.month) not in seen:
            seen.add((day.year, day.month))
            if i > 0:
                parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" text-anchor="middle" fill="currentColor" '
                             f'fill-opacity="0.7">{day.month} 月</text>')
    # the high's label sits above its line at the left, the low's below its line at the right: open space on both
    for value, label, x_text, dy, anchor in ((level["high"], f"最高 {level['high']:.2f}（{level['high_date']}）", left + 4, -5, "start"),
                                             (level["low"], f"最低 {level['low']:.2f}（{level['low_date']}）", width - right - 4, 15, "end")):
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y(value):.1f}" y2="{y(value):.1f}" stroke="currentColor" '
                     f'stroke-opacity="0.45" stroke-dasharray="2 4"/><text x="{x_text}" y="{y(value) + dy:.1f}" '
                     f'text-anchor="{anchor}" fill="currentColor" {halo}>{label}</text>')
    ends = []
    for key, label, color, stroke, dash in CHART_LINES:
        s = frame[key].dropna()
        points = " ".join(f"{x(frame.index.get_loc(d)):.1f},{y(v):.1f}" for d, v in s.items())
        dashes = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="{stroke}"{dashes} points="{points}"/>')
        ends.append([y(float(s.iloc[-1])), f"{label} {float(s.iloc[-1]):.2f}", color])
    ends.sort()
    for i in range(1, len(ends)):  # keep the end labels from overlapping
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 13)
    for pos, label, color in ends:
        parts.append(f'<text x="{width - right + 4}" y="{pos + 4:.1f}" fill="{color}" {halo}>{label}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --- the report --------------------------------------------------------------------------------------------

def _pct(x, digits: int = 1, signed: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "无可靠数据"
    return f"{x:+.{digits}%}" if signed else f"{x:.{digits}%}"


def _pp(x: float) -> str:
    return f"{x * 100:+.1f} 个百分点"


def _yi(x: float) -> str:
    return f"{x / 1e8:,.2f}"


def _stamp(iso: str | None) -> str:
    """2026-10-17T11:49:31+11:00 -> 2026-10-17 11:49 (local time of the run)."""
    return (iso or "")[:16].replace("T", " ")


def _target(prep: dict, h: str) -> dt.date:
    return prep["valued_on"] + dt.timedelta(days=HORIZONS[h][1])


def _history_view(prep: dict, h: str) -> dict:
    """The history number the report leads with: same side of the 200-day average when there is one."""
    odds = prep["history"][h]
    return odds["trend"] if odds["trend"].get("p_up") is not None else odds["all"]


def _jev_word(prep: dict, h: str) -> str:
    odds = prep.get("jev")
    return f"Jev {_pct(odds[h])}" if odds and odds.get(h) is not None else "Jev 待你确认费用"


def _higher(a: float, b: float) -> str:
    return "高于" if a > b else "低于"


def _trend_words(trend: dict) -> str:
    if trend["long"] == "上升" and trend["short"] in ("向下", "转弱"):
        return f"长线上升、短线{trend['short']}（上升趋势里的回调）"
    if trend["long"] == "下降" and trend["short"] in ("向上", "转强"):
        return f"长线下降、短线{trend['short']}（下降趋势里的反弹）"
    return f"长线{trend['long']}、短线{trend['short']}"


def _signal_words(prep: dict, r: dict) -> str:
    return describe(r["signal"], r["state"], prep["sector"], prep["market"])


def _key_words(prep: dict) -> str:
    keys = prep["key_signals"]
    if not keys:
        return "没有一个信号有足够的独立样本，看不出哪个有用。"
    parts = [f"「{_signal_words(prep, r)}」（{HORIZON_NAMES[r['horizon']]}：之后上涨 {_pct(r['p_up'])}，平常 "
             f"{_pct(r['base_p_up'])}，{r['verdict']}）" for r in keys]
    if all(r["verdict"] == "没用" for r in keys):
        return (f"有足够样本的信号在 {prep['ticker']} 历史上都没有明显作用；和平常差别最大的是" + "和".join(parts)
                + "，也都在随机波动以内。")
    return "；".join(parts) + "。"


def _earnings_when(prep: dict) -> str:
    e = prep["earnings"]
    official = e.get("official") or {}
    if official.get("date"):
        return f"{official['date']}（NVIDIA 官方公告）" if prep["ticker"] == "NVDA" else f"{official['date']}（公司公告）"
    dates = " 或 ".join(e.get("dates") or []) or "无"
    return f"公司还没公布日期（yfinance 估 {dates}）"


def _conclusion(prep: dict) -> str:
    ind, close = prep["indicators"], prep["close"]
    ma = ind["ma"]
    if (close > ma[10]) == (close > ma[20]):
        short = f"收盘{_higher(close, ma[10])} MA10 {ma[10]:.2f} 和 MA20 {ma[20]:.2f}"
    else:
        short = f"收盘{_higher(close, ma[10])} MA10 {ma[10]:.2f}、{_higher(close, ma[20])} MA20 {ma[20]:.2f}"
    crossed = "（两条线还没交叉）" if prep["trend"]["short"] in ("转弱", "转强") else ""
    lines = ["## 一屏结论", "",
             f"- **趋势**：{_trend_words(prep['trend'])}。长线：收盘 {close:.2f} 美元{_higher(close, ma[200])} MA200 "
             f"{ma[200]:.2f}，MA50 {ma[50]:.2f} {_higher(ma[50], ma[200])} MA200。短线：{short}，MA10 "
             f"{_higher(ma[10], ma[20])} MA20{crossed}。"]
    for h in HORIZONS:
        o = prep["options"].get(h) or {}
        span = (f"期权给的正常波动区间 {o['lo']:.2f}–{o['hi']:.2f} 美元（68% 的可能落在里面）" if o.get("lo") is not None
                else "期权区间无可靠数据")
        lines.append(f"- **{HORIZON_NAMES[h]}（到 {_target(prep, h)}）**：上涨概率 历史 {_pct(_history_view(prep, h)['p_up'])}、"
                     f"期权 {_pct(o.get('p_up'))}、{_jev_word(prep, h)}；{span}。")
    lines.append(f"- **最关键的两个信号**：{_key_words(prep)}")
    event = prep["options"].get("event") or {}
    move = (f"；期权市场为财报那一天定的波动约 ±{event['move']:.2%}（1 个标准差）" if event.get("move") else "")
    lines.append(f"- **下一个官方事件**：{prep['earnings'].get('label', '下一季')}财报，{_earnings_when(prep)}{move}。")
    return "\n".join(lines)


def _chart_section(prep: dict) -> str:
    days = 126
    start = prep["indicators"]["levels"]["近半年（126 个交易日）"]
    return "\n".join(["## 价格和均线（近半年）", "",
                      f"{prep['price_source']} 复权日线，最近 {days} 个交易日（到 {prep['valued_on']}）。实线是收盘、MA10、MA20，"
                      f"虚线是 MA50、MA200；两条点线是这段时间的最高价 {start['high']:.2f}（{start['high_date']}）和最低价 "
                      f"{start['low']:.2f}（{start['low_date']}）。", "",
                      '<div class="chart">', prep["chart"], "</div>"])


def _fund_name(name: str) -> str:
    return f"{name}（{FUND_NAMES[name]}）" if name in FUND_NAMES else name


def _dashboard(prep: dict) -> str:
    ind, close, t = prep["indicators"], prep["close"], prep["ticker"]
    states, sector, market = prep["states"], prep["sector"], prep["market"]
    moomoo = (prep["checks"].get("moomoo") or {})
    live = (f"Moomoo 实时行情（美股 {moomoo['rights'].get('us_qot_right')}）的收盘价见文末核对，时间戳 "
            f"{moomoo['time'][:19]} 美东。" if moomoo.get("rights") else
            f"Moomoo 实时行情没有核对（{moomoo.get('note', 'OpenD 没开')}）。")
    m = ind["macd"]
    lines = ["## 指标看板", "",
             f"{t} {prep['valued_on']} 收盘 {close:.2f} 美元，当天 {_pct(ind['change_1d'], 2, True)}。数据：{prep['price_source']} "
             f"复权日线（复权 = 按拆股和分红调整过，历史可以直接比），{prep['start']} 起。{live}", "",
             "### 均线（moving average，MA）", "", "| 均线 | 数值（美元） | 收盘比它 |", "| --- | --- | --- |"]
    for n in MA_WINDOWS:
        gap = ind["vs_ma"][n]
        lines.append(f"| MA{n} | {ind['ma'][n]:.2f} | {_pct(gap, 2, True)}（{'上方' if gap > 0 else '下方'}） |")
    lines += ["", f"排列：{describe('alignment', states['alignment'])}。", "", "### 动量", "",
              f"- RSI(14)（相对强弱指数，Relative Strength Index）= {ind['rsi']:.1f}："
              f"{RSI_WORDS.get(states['rsi'], '无可靠数据')}。",
              f"- MACD(12, 26, 9)（Moving Average Convergence Divergence）：MACD 线 {m['macd']:.2f}，信号线 {m['signal']:.2f}，"
              f"柱 = {m['macd']:.2f} − {m['signal']:.2f} = {m['hist']:.2f}（前一天 {m['hist_prev']:.2f}）。"
              f"{describe('macd_signal', states['macd_signal'])}，{describe('macd_zero', states['macd_zero'])}。", "",
              "### 成交量和波动", "",
              f"- 量比（volume ratio）= 今天成交量 ÷ 前 20 个交易日平均 = {ind['volume']:,.0f} ÷ {ind['volume_avg20']:,.0f} = "
              f"{ind['volume_ratio']:.2f}：{describe('volume', states['volume'])}。",
              f"- ATR(14)（平均真实波幅，Average True Range）= {ind['atr']:.2f} 美元，占收盘的 {ind['atr_pct']:.2%}"
              f"（{ind['atr']:.2f} ÷ {close:.2f}）；过去一年 ATR% 的中位数 {ind['atr_pct_median_1y']:.2%}："
              f"{describe('atr', states['atr'])}。", "",
              "### 前高前低（支撑和压力，support / resistance）", ""]
    for name, lv in ind["levels"].items():
        lines.append(f"- {name}：最高 {lv['high']:.2f}（{lv['high_date']}），收盘比它低 {abs(lv['below_high']):.2%}；"
                     f"最低 {lv['low']:.2f}（{lv['low_date']}），收盘比它高 {lv['above_low']:.2%}。")
    lines += ["", "### 相对强弱（relative strength）", "",
              f"| 区间 | {t} | {_fund_name(sector)} | {_fund_name(market)} |", "| --- | --- | --- | --- |"]
    for n, r in ind["relative"].items():
        lines.append(f"| 过去 {n} 个交易日 | {_pct(r['stock'], 1, True)} | {_pct(r[sector], 1, True)} | {_pct(r[market], 1, True)} |")
    gaps = []
    for name in (sector, market):
        parts = [f"{n} 日 {_pp(r['stock'] - r[name])}" for n, r in ind["relative"].items()
                 if r["stock"] is not None and r[name] is not None]
        if parts:
            gaps.append(f"比 {name}：" + "、".join(parts))
    if gaps:
        lines += ["", f"{t} 的涨跌减去对方的涨跌（正数 = 跑赢）：" + "；".join(gaps) + "。"]
    return "\n".join(lines)


def _noise_example(prep: dict) -> str:
    rows = [r for r in prep["evidence"]["2w"] if r.get("noise")]
    if not rows:
        return ""
    r = next((x for x in rows if x["signal"] == "close_ma20"), rows[0])
    p0, n = r["base_p_up"], r["independent"]
    return (f"例：「{_signal_words(prep, r)}」，2 周：随机波动 = √({p0:.3f} × {1 - p0:.3f} ÷ {n}) = {r['noise'] / 2:.1%}，"
            f"2 倍是 {r['noise']:.1%}；实际差 {r['p_up']:.1%} − {p0:.1%} = {_pp(r['diff'])}，所以判断是「{r['verdict']}」。")


def _evidence_section(prep: dict) -> str:
    t, base = prep["ticker"], prep["baseline"]
    ends = "、".join(f"{HORIZON_NAMES[h]}到 {base[h]['end']}" for h in HORIZONS)
    lines = ["## 每个信号的历史证据", "",
             f"把 {t} 从 {base['2w']['start']} 起的每一天都当作「今天」，挑出和今天信号相同的日子，看之后 10 个交易日（约 2 周）、"
             f"126 个交易日（约半年）收盘是涨是跌（{prep['price_source']} 复权日线）。最后能算的日子：{ends}，更晚的日子还没走完"
             "整个窗口。", "",
             "- **样本**：「天」是这个信号出现过多少天。相邻的日子共用大部分走势，不算独立的证据，所以再数「独立段」，"
             f"即互不重叠的窗口；少于 {MIN_INDEPENDENT} 段写「证据不足」。",
             f"- **没用**：上涨比例和平常的差别在 2 倍随机波动以内。随机波动 = √(平常比例 × (1 − 平常比例) ÷ 独立段数)。"
             f"{_noise_example(prep)}",
             f"- **弱偏涨 / 弱偏跌**：差别是随机波动的 2–3 倍，可能是巧合（同时查 {len(STATES)} 个信号，碰巧超线一两个很正常）；"
             "**偏涨 / 偏跌**：3 倍以上。",
             "- 「平常」= 这个信号能算出来的所有日子；各信号起点不同（例如 MA200 要先攒满 200 天），所以括号里的平常比例略有不同。"]
    for h, (days, _) in HORIZONS.items():
        b = base[h]
        lines += ["", f"### {HORIZON_NAMES[h]}（之后 {days} 个交易日）", "",
                  "| 今天的信号 | 之后上涨的比例（平常） | 涨跌中位数（平常） | 样本：天 / 独立段 | 判断 |",
                  "| --- | --- | --- | --- | --- |",
                  f"| 所有日子（基准） | {_pct(b['base_p_up'])} | {_pct(b['base_median'], 1, True)} | "
                  f"{b['base_days']:,} / {b['base_independent']} | — |"]
        for r in prep["evidence"][h]:
            if r["state"] is None:
                lines.append(f"| {r['signal']} | 无可靠数据 | — | — | 无可靠数据 |")
                continue
            lines.append(f"| {_signal_words(prep, r)} | {_pct(r['p_up'])}（{_pct(r['base_p_up'])}） | "
                         f"{_pct(r['median'], 1, True)}（{_pct(r['base_median'], 1, True)}） | {r['days']:,} / {r['independent']} | "
                         f"{r['verdict']} |")
    return "\n".join(lines)


def _record(prep: dict, source: str, h: str) -> tuple[int, float | None]:
    board = prep["board"]
    if board is None or board.empty:
        return 0, None
    hit = board[(board["source"] == source) & (board["horizon"] == h)]
    return (int(hit["scored"].iloc[0]), float(hit["brier"].iloc[0])) if len(hit) else (0, None)


def _agreement_line(prep: dict, h: str) -> str:
    judged = prep.get("jev") or {}
    current = {"hist_trend": prep["history"][h]["trend"].get("p_up"), "options": (prep["options"].get(h) or {}).get("p_up"),
               "jev": judged.get(h)}
    recorded = []
    for source, p in current.items():
        scored, brier = _record(prep, source, h)
        if p is not None and scored >= MIN_SCORED:
            recorded.append((source, p, scored, brier))
    if len(recorded) < 2:
        have = f"只有 {len(recorded)} 个" if recorded else "一个都没有"
        return (f"- 一致与否：不判断「一致」。规则是只在有成绩（到期记分满 {MIN_SCORED} 次）的来源之间比，现在{have}；"
                f"这一期的预测 {_target(prep, h)} 到期记分。")
    names = "、".join(f"{SOURCE_WORDS[s]} {n} 次 Brier {b:.3f}" for s, _, n, b in recorded)
    return f"- 一致与否：{forecast.agreement([p for _, p, _, _ in recorded])}（只比有成绩的来源：{names}）。"


def _odds_section(prep: dict) -> str:
    lines = ["## 上涨概率：三个来源", "",
             f"「上涨」= 到期那天收盘高于今天的 {prep['close']:.2f} 美元。每个概率都记进预测日志 journal/forecasts.csv，"
             "到期按收盘记分（Brier 分数，越低越准，抛硬币是 0.25）。", ""]
    for h, (days, _) in HORIZONS.items():
        odds = prep["history"][h]
        trend, everything, ten = odds["trend"], odds["all"], odds["last10y"]
        ten_words = _pct(ten["p_up"]) if ten.get("p_up") is not None else f"无可靠数据（{ten.get('note', '')}）"
        if trend.get("p_up") is not None:
            side = "上方" if trend["state"] == "above" else "下方"
            history = (f"- 历史基准率（base rate）{_pct(trend['p_up'])}：只看过去和今天一样收盘在 200 日均线{side}的日子，持有 "
                       f"{days} 个交易日后上涨的比例（{trend['windows']:,} 个窗口，{trend['start']} 到 {trend['end']}）。"
                       f"另两种算法：全部历史 {_pct(everything['p_up'])}，近 10 年 {ten_words}。")
        else:
            history = (f"- 历史基准率（base rate）{_pct(everything['p_up'])}：全部历史里持有 {days} 个交易日后上涨的比例"
                       f"（{trend.get('note', '算不出趋势状态')}）。近 10 年 {ten_words}。")
        o = prep["options"].get(h) or {}
        option = (f"- 期权 {_pct(o['p_up'])}：风险中性概率（risk-neutral probability），基本只反映波动大小、不反映看好看空，"
                  "所以总在 50% 附近（算法见「期权」一节）。" if o.get("p_up") is not None
                  else f"- 期权：无可靠数据（{o.get('note', '')}）。")
        judged = prep.get("jev")
        jev_line = (f"- Jev {_pct(judged[h])}：把本页的数据交给 Jev 模型得到的综合概率（{_stamp(judged.get('as_of'))}）。"
                    if judged and judged.get(h) is not None else "- Jev：待你确认费用后调用（费用见文末「费用」）。")
        lines += [f"### {HORIZON_NAMES[h]}（到 {_target(prep, h)}）", "", history, option, jev_line,
                  _agreement_line(prep, h), ""]
    return "\n".join(lines).rstrip()


def _options_section(prep: dict) -> str:
    rate, rate_date = prep["rate"]
    close, t = prep["close"], prep["ticker"]
    lines = ["## 期权：正常波动区间", "",
             f"用 {t} 自己的期权（yfinance 期权链）：取 {prep['valued_on']} 同一交易日的最后成交价，反推隐含波动率"
             f"（implied volatility，IV），再换算到每个期限；无风险利率用 13 周美国国债收益率（^IRX）{rate:.2%}（{rate_date}）。"
             "期权成交价和 Moomoo 同一收盘前的成交核对（见文末）；Moomoo 自己算的 IV 只作参考（它按读取时的剩余天数重算）。", ""]
    for h, (days, calendar_days) in HORIZONS.items():
        o, target = prep["options"].get(h) or {}, _target(prep, h)
        if o.get("iv") is None:
            lines.append(f"- {HORIZON_NAMES[h]}（到 {target}）：无可靠数据（{o.get('note', '')}）。")
            continue
        root = math.sqrt(calendar_days / 365)
        width = o["iv"] * root
        check = (f"（Moomoo {o['moomoo_iv']:.2%}，差 {(o['moomoo_iv'] - o['iv']) * 100:+.2f} 个百分点）"
                 if o.get("moomoo_iv") is not None else "")
        kinds = "和".join(o.get("used") or [])
        lines += [f"- {HORIZON_NAMES[h]}（到 {target}，{calendar_days} 天）：{o['expiry']} 到期、行权价 {o['strike']:.2f} 美元的"
                  f"{kinds}期权，IV {o['iv']:.2%}{check}。",
                  f"    - 宽度 = {o['iv']:.2%} × √({calendar_days} ÷ 365) = {o['iv']:.2%} × {root:.4f} = {width:.2%}",
                  f"    - 上限 = {close:.2f} × e^{width:.4f} = {o['hi']:.2f} 美元；下限 = {close:.2f} ÷ e^{width:.4f} = "
                  f"{o['lo']:.2f} 美元",
                  f"    - 按模型，到 {target} 有 68% 的可能落在 {o['lo']:.2f}–{o['hi']:.2f} 美元之间，比下限更低、比上限更高的"
                  "可能各 16%。"]
        past = _history_view(prep, h)
        if past.get("p10") is not None:
            lines.append(f"    - 历史对照：过去同类的 {days} 日窗口里，中间 80% 的涨跌落在 {_pct(past['p10'], 1, True)} 到 "
                         f"{_pct(past['p90'], 1, True)}，按今天收盘是 {close * (1 + past['p10']):.2f}–{close * (1 + past['p90']):.2f} 美元"
                         "（最差和最好的各 10% 在这之外）。")
    event = prep["options"].get("event") or {}
    if event.get("move"):
        m, a2, b2 = event["move"], event["iv_after"] ** 2, event["iv_before"] ** 2
        years = event["days_after"] / 365
        lines += [f"- 财报那一天：{_earnings_when(prep)}。",
                  f"    - 财报前最后一个到期日 {event['before']}（{event['days_before']} 天）IV {event['iv_before']:.2%}；"
                  f"财报后第一个到期日 {event['after']}（{event['days_after']} 天）IV {event['iv_after']:.2%}。后者多出来的波动，"
                  "就是市场为财报那一天定的价。",
                  f"    - 财报当天的波动 = √(({event['iv_after']:.2%}² − {event['iv_before']:.2%}²) × {event['days_after']} ÷ 365)"
                  f" = √(({a2:.4f} − {b2:.4f}) × {years:.4f}) = {m:.2%}",
                  f"    - 按今天收盘 {close:.2f}：1 个标准差是 {close * math.exp(-m):.2f}–{close * math.exp(m):.2f} 美元"
                  "（68% 的可能）。这只是财报那一天的跳动，不含其他日子的正常波动。"]
    elif event.get("note"):
        lines.append(f"- 财报那一天：无可靠数据（{event['note']}）。")
    return "\n".join(lines)


def _fy_span(fy: int, fy_end_month: int) -> str:
    start_month = fy_end_month % 12 + 1
    start_year = fy - 1 if fy_end_month < 12 else fy
    return f"{start_year} 年 {start_month} 月到 {fy} 年 {fy_end_month} 月"


def _fundamentals_section(prep: dict) -> str:
    f, a, close, t = prep["fundamentals"], prep["analysts"], prep["close"], prep["ticker"]
    lines = ["## 半年看基本面", ""]
    if f.get("available") is False:
        lines.append(f"- 基本面：无可靠数据（{f.get('note', '')}）。")
    else:
        periods, fy_end = f.get("periods") or {}, f["fy_end_month"]
        span = ""
        if periods.get("0y"):
            fy = int(periods["0y"][2:])
            span = f"{t} 的财年在每年 {fy_end} 月底结束：{periods['0y']} = {_fy_span(fy, fy_end)}。"
        lines += [f"数据：yfinance（季度和年度财报、分析师预期），{f['as_of']} 读取；分析师内容原样引用。{span}", "",
                  "### 营收和利润（亿美元；EPS 单位是美元）", ""]
        if f["quarters"]:
            lines += ["| 季度 | 营收 | 营业利润 | 净利润 | 摊薄 EPS |", "| --- | --- | --- | --- | --- |"]
            for q in f["quarters"]:
                cells = [_yi(q[k]) if q[k] is not None else "—" for k in ("revenue", "operating_income", "net_income")]
                eps = f"{q['eps']:.2f}" if q["eps"] is not None else "—"
                lines.append(f"| {q['label']} | {' | '.join(cells)} | {eps} |")
            q0 = f["quarters"][0]
            growth = []
            if q0.get("revenue_qoq") is not None:
                growth.append(f"比上一季 {_pct(q0['revenue_qoq'], 1, True)}（{_yi(q0['revenue'])} ÷ "
                              f"{_yi(f['quarters'][1]['revenue'])} − 1）")
            if q0.get("revenue_yoy") is not None:
                growth.append(f"比去年同季 {_pct(q0['revenue_yoy'], 1, True)}（÷ {_yi(f['quarters'][4]['revenue'])} − 1）")
            if growth:
                lines += ["", f"- 最近一季 {q0['label']}的营收" + "，".join(growth) + "。"]
            above = [q["label"] for q in f["quarters"] if q["net_income"] is not None and q["operating_income"] is not None
                     and q["net_income"] > q["operating_income"]]
            if above:
                lines.append(f"- {'、'.join(above)}的净利润高于营业利润。净利润 = 营业利润 + 营业外收支 − 所得税，"
                             f"所以{'这一季' if len(above) == 1 else '这几季'}的营业外收支是大额正数（yfinance 没有写明来源）。")
        else:
            lines.append("- 季度财报：无可靠数据（yfinance 没有返回）。")
        years = [y for y in f["years"] if y.get("revenue_yoy") is not None]
        if years:
            lines.append("- 年度：" + "；".join(
                f"{y['label']} 营收 {_yi(y['revenue'])} 亿（{_pct(y['revenue_yoy'], 1, True)}），净利润 "
                f"{_yi(y['net_income'])} 亿（{_pct(y['net_income_yoy'], 1, True)}）" for y in years) + "。括号里是比上一财年。")
        v = f["valuation"]
        lines += ["", f"### 估值（按 {prep['valued_on']} 收盘 {close:.2f} 美元）", ""]
        if v.get("pe_ttm") is not None:
            parts = " + ".join(f"{e:.2f}" for e in f["ttm"]["eps_parts"])
            lines.append(f"- 市盈率 TTM（P/E，按过去四个季度）= 股价 ÷ 近四季摊薄 EPS 之和 = {close:.2f} ÷ ({parts}) = "
                         f"{close:.2f} ÷ {f['ttm']['eps']:.2f} = {v['pe_ttm']:.2f} 倍。")
        else:
            lines.append("- 市盈率 TTM：无可靠数据（缺少近四季 EPS）。")
        forward = []
        for p in ("0y", "+1y"):
            e = f["estimates"].get(p) or {}
            if p in v["pe_forward"]:
                forward.append(f"{periods.get(p, p)} EPS 预期均值 {e['avg']:.2f}（{e['n']:.0f} 位分析师）→ "
                               f"{close:.2f} ÷ {e['avg']:.2f} = {v['pe_forward'][p]:.2f} 倍")
        if forward:
            lines.append("- 预期市盈率（forward P/E）= 股价 ÷ 分析师 EPS 预期：" + "；".join(forward) + "。")
        if v.get("market_cap") and v.get("ps_ttm"):
            lines.append(f"- 市值 = {close:.2f} × {v['shares'] / 1e8:,.2f} 亿股 = {v['market_cap'] / 1e8:,.0f} 亿美元；"
                         f"市销率 TTM（P/S）= 市值 ÷ 近四季营收 {_yi(f['ttm']['revenue'])} 亿 = {v['ps_ttm']:.2f} 倍。")
        lines += ["", f"### 分析师预期修正（yfinance eps_trend、eps_revisions，{f['as_of']}）", ""]
        names = {"0q": "本季", "+1q": "下季", "0y": "本财年", "+1y": "下财年"}
        for p, tr in f["trend"].items():
            if tr.get("current") is None:
                continue
            lines.append(f"- {periods.get(p, p)}（{names.get(p, p)}）EPS 预期：现在 {tr['current']:.2f}，30 天前 {tr['30d']:.2f}，"
                         f"90 天前 {tr['90d']:.2f} → 30 天 {_pct(tr['change_30d'], 2, True)}，90 天 {_pct(tr['change_90d'], 2, True)}")
        if f["revisions"]:
            lines.append("- 近 30 天上调 / 下调 EPS 预期的分析师人数：" + "，".join(
                f"{names.get(p, p)} {r['up_30d']:.0f} / {r['down_30d']:.0f}" for p, r in f["revisions"].items()))
    lines += ["", f"### 分析师评级和目标价（yfinance，{a.get('as_of', '')}，原样引用）", ""]
    if a.get("available") is False:
        lines.append(f"- {a.get('note', '没有数据')}")
    else:
        if a.get("ratings"):
            lines.append("- 评级（recommendations_summary）：" + "、".join(
                f"{forecast.RATING_NAMES[k]} {n}" for k, n in a["ratings"].items()))
        targets = a.get("targets") or {}
        if targets.get("mean"):
            lines.append(f"- 目标价（analyst_price_targets，12 个月口径）：均值 {targets['mean']:.2f}、中位 {targets.get('median', 0):.2f}、"
                         f"最高 {targets.get('high', 0):.2f}、最低 {targets.get('low', 0):.2f} 美元")
        if a.get("changes_90d"):
            lines.append(f"- 近 {forecast.RECENT_DAYS} 天评级动作（upgrades_downgrades）：" + "、".join(
                f"{forecast.ACTIONS.get(k, k)} {n}" for k, n in a["changes_90d"].items()))
        for c in a.get("recent", []):
            grade = f"{c['from']} → {c['to']}" if isinstance(c["from"], str) and c["from"] and c["from"] != c["to"] else c["to"]
            known = [v for v in (c.get("prior_target"), c.get("target")) if v is not None and not pd.isna(v) and v > 0]
            price = (f"，目标价 {c['prior_target']:.0f} → {c['target']:.0f}" if len(known) == 2 and c["prior_target"] != c["target"]
                     else f"，目标价 {known[-1]:.0f}" if known else "")
            lines.append(f"    - {c['date']} {c['firm']}：{c['action']} {grade}{price}")
    lines += ["", "### 下一次财报", ""]
    e = prep["earnings"]
    official = e.get("official") or {}
    label = e.get("label", "下一季")
    if (e.get("fund") or {}).get("kind") == "index":
        lines.append(_card_event(prep).replace("- 下个事件：", "- ") + "。")
    elif official.get("date"):
        lines.append(f"- {label}财报：{official['date']}（[公司公告]({official.get('url', '')})）。")
    else:
        precedent = (f"{official['precedent']}（[去年的公告]({official['precedent_url']})）。"
                     if official.get("precedent") else "")
        page = f"（[公司新闻页]({official['page']})，{official.get('checked', '')} 查看）" if official.get("page") else ""
        lines.append(f"- {label}财报：公司还没公布日期{page}。{precedent}")
    sources = e.get("sources") or []
    if sources:
        distinct = sorted({d for d, _ in sources})
        lines.append("- yfinance 给的日期：" + "、".join(f"{d}（{s.split()[-1]}）" for d, s in sources)
                     + ("；两处不一致，按未确认处理。" if len(distinct) > 1 else "；未经公司确认。"))
    if f.get("available") is not False:
        eps, revenue = (f.get("estimates") or {}).get("0q") or {}, (f.get("revenue_estimates") or {}).get("0q") or {}
        if eps.get("avg") is not None and revenue.get("avg") is not None:
            lines.append(f"- 分析师预期（yfinance）：EPS {eps['avg']:.2f} 美元（{eps['low']:.2f}–{eps['high']:.2f}，{eps['n']:.0f} 位）；"
                         f"营收 {_yi(revenue['avg'])} 亿美元（{_yi(revenue['low'])}–{_yi(revenue['high'])} 亿，{revenue['n']:.0f} 位）。")
    return "\n".join(lines)


def _lessons(prep: dict) -> str:
    ind, close, t = prep["indicators"], prep["close"], prep["ticker"]
    ma, states, sector, market = ind["ma"], prep["states"], prep["sector"], prep["market"]
    recent = ind["levels"]["近 20 个交易日"]
    base = prep["baseline"]["2w"]["base_p_up"]
    option = prep["options"]["2w"]
    iv = f"{t} 2 周期权的 IV 是 {option['iv']:.2%}。" if option.get("iv") is not None else ""
    f = prep["fundamentals"]
    value = (f.get("valuation") or {}) if f.get("available") is not False else {}
    pe = (f"{t} 现在的市盈率 TTM 是 {value['pe_ttm']:.2f} 倍" if value.get("pe_ttm") else "")
    if pe and value["pe_forward"].get("0y"):
        pe += f"，按 {f['periods'].get('0y', '本财年')} 的预期算是 {value['pe_forward']['0y']:.2f} 倍"
    pe = f"{pe}。" if pe else ""
    trend = (f.get("trend") or {}).get("+1y") if f.get("available") is not False else None
    revision = (f"{t} 下财年（{f['periods'].get('+1y', '')}）的 EPS 预期 90 天里变了 {_pct(trend['change_90d'], 2, True)}。"
                if trend and trend.get("change_90d") is not None else "")
    weight = (f"（占 {prep['sector_weight']:.1%}，yfinance）" if prep.get("sector_weight") else "")
    side10, side20 = ("上方" if close > ma[10] else "下方"), ("上方" if close > ma[20] else "下方")
    topics = [
        ("均线（moving average，MA）",
         f"过去 N 个交易日收盘价的平均数，把每天的跳动抹平，用来看方向。{t} 今天的 MA20 = {ma[20]:.2f} 美元，"
         "就是过去 20 个交易日收盘价的平均。",
         f"收盘在 MA5、MA10、MA20 上方，说明最近一到四周买入的人平均是赚的，走势偏强；跌到下方常被当作短线转弱。"
         f"今天收盘 {close:.2f} 在 MA10 {side10}、MA20 {side20}。",
         "MA50、MA200 看几个月到一年的方向：收盘和 MA50 都在 MA200 上方，通常算长期上升趋势。",
         f"均线用的是过去的价格，总是慢半拍。「跌破就跌、站上就涨」在 {t} 自己的历史里大多看不出作用，证据表里判断为「没用」的"
         "均线信号就是例子。"),
        ("均线排列",
         f"把 MA5、MA10、MA20、MA50、MA200 从短到长排：短的都在长的上方叫多头排列，反过来叫空头排列，其余叫交织。"
         f"{t} 今天是{describe('alignment', states['alignment'])}。",
         "多头排列说明短期涨得比长期快，动能向上；交织说明短期方向和长期不一致，常见于回调或横盘。",
         "持续的多头排列对应上涨阶段，但往往要到趋势走了很久才翻成空头排列。",
         "排列只是把均线换个说法，和均线一样滞后，描述的是已经发生的走势。"),
        ("RSI(14)（相对强弱指数，Relative Strength Index）",
         f"过去 14 个交易日里，上涨幅度占全部涨跌幅度的比例，换算成 0–100（Wilder 平滑）。{t} 今天是 {ind['rsi']:.1f}。",
         "70 以上叫超买（涨得急），30 以下叫超卖（跌得急），常被拿来找短线回落或反弹的时点。",
         "对长线意义不大：强势股可以几个月一直待在 50–80 之间。",
         f"「超买就要跌、超卖就要涨」是最常见的误读；强趋势里超买可以持续很久，要看 {t} 自己的历史证据。"),
        ("MACD（Moving Average Convergence Divergence）",
         f"MACD 线 = 12 日指数均线 − 26 日指数均线；信号线 = MACD 线的 9 日指数均线；柱 = MACD 线 − 信号线。"
         f"{t} 今天的柱是 {ind['macd']['hist']:.2f}。",
         "柱由负转正（MACD 上穿信号线）常被看作短线动能转强，由正转负则转弱。",
         "MACD 在零轴上方，说明 12 日均线高于 26 日均线，中期动能向上。",
         "它由均线算出来，一样滞后；在横盘震荡里会反复交叉，给出很多假信号。"),
        ("量比（今天成交量 ÷ 前 20 个交易日平均）",
         f"今天的成交量是前 20 个交易日平均成交量的几倍。{t} 今天是 {ind['volume_ratio']:.2f} 倍。",
         "放量（1.5 倍以上）说明当天参与的人多，这一天的涨跌更引人注意；缩量（0.7 倍以下）说明观望的人多。",
         "一天的成交量和公司价值关系不大，长线基本可以不看。",
         "放量不等于要涨或要跌：同样的大成交量可能是大买，也可能是大卖；财报日、指数调整日、期权到期日的成交量天然偏大。"),
        ("ATR(14)（平均真实波幅，Average True Range）",
         f"每天的真实波幅 = 当天最高减最低，跳空时算到前一天收盘为止；ATR 是它的 14 日平均（Wilder 平滑）。{t} 今天 "
         f"{ind['atr']:.2f} 美元，约是股价的 {ind['atr_pct']:.2%}。",
         f"它告诉你一天「正常」会动多少：{t} 一天动 {ind['atr']:.2f} 美元左右是常态，比这小得多的涨跌多半只是噪音。",
         f"用 ATR 占股价的比例（ATR%）比较不同时期：{t} 现在 {ind['atr_pct']:.2%}，"
         f"{_higher(ind['atr_pct'], ind['atr_pct_median_1y'])}过去一年的中位数 {ind['atr_pct_median_1y']:.2%}，说明这段时间比平常"
         f"{'颠簸' if ind['atr_pct'] > ind['atr_pct_median_1y'] else '平静'}。",
         "ATR 只说幅度不说方向，波动大不代表要跌；它也往往在大涨大跌之后才变大。"),
        ("前高前低（支撑和压力）",
         f"一段时间里出现过的最高价和最低价。{t} 近 20 个交易日最高 {recent['high']:.2f}、最低 {recent['low']:.2f} 美元。",
         "不少人会在前高附近卖、在前低附近买，价格到这些位置常会停一停；收盘越过前高或跌破前低，常被当作新一段走势的开始。",
         "看 52 周最高和最低：离 52 周高点越近，说明越处在强势区间。",
         "这些价位只是很多人盯着的参考点，不是「一定反弹」或「一定回落」的线，也不是目标价。"),
        ("相对强弱（relative strength）",
         f"同一段时间里 {t} 的涨跌减去板块（{sector}）或大盘（{market}）的涨跌；正数表示跑赢。",
         "跑赢板块说明资金更偏向它；跑输说明它比同行弱。",
         "长期跑赢大盘的股票通常有自己的基本面原因（比如利润增长更快）。" + (
             f"{sector} 本身持有 {t}{weight}，和 {sector} 比时，一部分涨跌是 {t} 自己带来的，差距会被缩小。" if weight else ""),
         "相对强弱只描述过去；跑赢的可以继续跑赢，也可以补跌，要看证据表。"),
        ("历史基准率和独立段",
         f"基准率是「过去所有同样长度的持有期里，最后是涨的占几成」。{t} 持有 10 个交易日上涨的比例是 {_pct(base)}。",
         f"判断信号有没有用，要和基准率比，不是和 50% 比：{t} 本来就有 {_pct(base)} 的 10 日窗口是涨的，信号日必须明显高于"
         "或低于它才算有用。",
         "半年（126 个交易日）的窗口少，互不重叠的只有几十段，所以半年的证据更容易「不足」。",
         f"把相邻的日子当成独立样本会高估证据：连续 20 天都在 MA20 上方，之后的走势几乎是同一段，只能算一段。另外 {t} 是事后看的"
         "大赢家，它的历史比一般股票乐观。"),
        ("隐含波动率（implied volatility，IV）和风险中性概率",
         f"从期权价格倒推出来的「市场预计一年会波动多大」。{iv}",
         "IV 换算到 2 周，就是期权给的正常波动区间：按模型，到期价格有 68% 的可能落在区间里。",
         "半年的 IV 包含了期间的财报；财报前 IV 常常偏高，财报后回落。",
         "期权给出的上涨概率是风险中性概率（risk-neutral probability），基本只反映波动大小、不反映看好看空，所以总在 50% "
         "附近；不要把它当成市场在押涨跌。"),
        ("市盈率（P/E）和预期市盈率（forward P/E）",
         f"股价是每股盈利（EPS）的多少倍：按过去四个季度算叫市盈率 TTM，按分析师的预期算叫预期市盈率。{pe}",
         "几乎不影响 2 周的涨跌；财报公布后，EPS 变了，市盈率会跟着跳一下。",
         "市盈率高说明市场为未来增长付了更多钱：增长兑现，高市盈率会慢慢被消化；兑现不了，股价容易下跌。",
         "只看市盈率高低判断贵不贵：增长快的公司市盈率天然高；预期市盈率依赖分析师的预测，而预测会变（见下一条）。"),
        ("分析师预期修正",
         f"分析师对未来 EPS 的预测，最近 30 天、90 天里改高了还是改低了。{revision}",
         "财报前预期被上调，说明分析师更乐观，但财报要超过更高的预期才算「好」。",
         "预期持续上调通常伴随基本面变好；预期下调往往先于股价走弱。",
         "分析师也会错，而且常常追着股价改预测；这里只原样引用数字，不是结论。"),
    ]
    lines = ["## 讲解：每个指标怎么读", "", "每个指标四句：一句话、对短线意味着什么、对长线意味着什么、常见误区。数字都是本页的。"]
    for name, one, short, long, pitfall in topics:
        lines += ["", f"### {name}", "", f"- 一句话：{one}", f"- 短线：{short}", f"- 长线：{long}", f"- 常见误区：{pitfall}"]
    return "\n".join(lines)


def _check_line(row: dict) -> str:
    if row.get("note"):
        return f"- {row['what']}：{row['note']}。"
    (a_source, a), (b_source, b, when) = row["ours"], row["theirs"]
    fmt = row.get("fmt", ",.2f")
    diff = (f"差 {row['diff'] * 100:+.2f} 个百分点" if row["absolute"] else
            "相同" if row["diff"] == 0 else f"差 {row['diff']:+.2%}")
    verdict = "一致" if row["ok"] else "对不上"
    return f"- {row['what']}：{a_source} {a:{fmt}}，{b_source} {b:{fmt}}（{when}）→ {diff}，{verdict}。"


def _sources_section(prep: dict) -> str:
    t, sector, market = prep["ticker"], prep["sector"], prep["market"]
    starts = prep["benchmark_starts"]
    rate, rate_date = prep["rate"]
    moomoo = prep["checks"].get("moomoo") or {}
    official = (prep["earnings"].get("official") or {})
    lines = ["## 数据源、费用和局限", "", "### 数据源", "",
             f"- 日线：{prep['price_source']} 复权日线（{t} {prep['start']} 起，{sector} {starts.get(sector, '')} 起，{market} "
             f"{starts.get(market, '')} 起），截至 {prep['valued_on']}。",
             (f"- 实时核对：Moomoo OpenAPI 只读行情（美股 {moomoo['rights'].get('us_qot_right')}、美股期权 "
              f"{moomoo['rights'].get('us_option_qot_right')}），{t} 时间戳 {moomoo['time'][:19]} 美东。" if moomoo.get("rights")
              else f"- 实时核对：Moomoo 没有核对（{moomoo.get('note', 'OpenD 没开')}）。"),
             f"- 期权链、基本面、分析师、财报日期：yfinance，{prep['made_on']} 读取；无风险利率：^IRX {rate:.2%}（{rate_date}）。"]
    if official.get("page"):
        lines.append(f"- 官方：[公司新闻页]({official['page']})（{official.get('checked', '')} 查看）。")
    lines += ["", "### 两个来源对得上吗", ""]
    rows = prep["checks"].get("rows") or []
    lines += [_check_line(r) for r in rows] or ["- 没有可核对的第二来源。"]
    if any(not r.get("ok", True) for r in rows if not r.get("note")):
        lines.append("- 对不上的项：原因没有核实；本页的指标一律用 Treg · Tiingo 的数，前后口径一致。")
    costs, judged = prep.get("costs") or {}, prep.get("jev")
    lines += ["", "### 费用", "",
              f"- Treg：今天 {'、'.join(costs.get('treg_tickers') or []) or '这几个代码'} 的 Tiingo 日线共调用 "
              f"{costs.get('treg_calls', 0)} 次，{costs.get('treg_usd', 0.0):.2f} 美元（免费额度每队每天 20 次；下一个美股收盘之前再用直接读"
              "已下载的文件，不再调用）。",
              "- yfinance、Moomoo：免费。"]
    quoted = prep.get("jev_quote")
    if judged:
        cap = f"；确认的封顶 {quoted['ceiling']:.6f} 美元" if quoted else ""
        lines.append(f"- Jev：1 次 POST /v1/evaluate，实际 {judged.get('cost_usd', 0):.6f} 美元"
                     f"（{_stamp(judged.get('as_of'))}{cap}）。")
    elif quoted:
        lines.append(f"- Jev：还没调用，等你确认。计划 {quoted['calls']} 次 {quoted['endpoint']}（{quoted['model']}）：单次最多 "
                     f"{quoted['max_tokens']:,} 个输入 token × {quoted['price_per_token']:.9f} 美元 = {quoted['ceiling']:.6f} 美元封顶，"
                     "实际按用量收费。")
    else:
        lines.append("- Jev：还没调用，等你确认费用。")
    lines += ["", "### 局限", "",
              f"- 历史证据只用 {t} 一只股票，而且它是事后看的大赢家，「平常」的上涨比例本身偏高；表里比的是信号日和平常的差别。",
              f"- {len(STATES)} 个信号同时检验，碰巧有一两个超线很正常；也没有在别的股票或别的年代上验证过。",
              "- 期权概率是风险中性的；区间假设对数正态分布，现实里大跌比模型更常见。期权价格取最后一笔成交，可能比收盘早几分钟。",
              "- 基本面数字来自 yfinance 的整理，没有逐条对照公司向 SEC 提交的原文。",
              "- 2 周和半年的概率都记进了 journal/forecasts.csv，到期记分；现在没有一个来源有成绩。"]
    return "\n".join(lines)


def sections(prep: dict) -> list[tuple[str, str]]:
    """The report's sections in order, each (title, Markdown starting with "## title")."""
    parts = [("一屏结论", _conclusion(prep)), ("价格和均线（近半年）", _chart_section(prep)), ("指标看板", _dashboard(prep)),
             ("每个信号的历史证据", _evidence_section(prep)), ("上涨概率：三个来源", _odds_section(prep)),
             ("期权：正常波动区间", _options_section(prep)), ("半年看基本面", _fundamentals_section(prep)),
             ("讲解：每个指标怎么读", _lessons(prep)), ("数据源、费用和局限", _sources_section(prep))]
    return parts


def title(prep: dict) -> str:
    return f"{prep['ticker']} 分析 v1：{prep['valued_on']} 收盘"


def lead(prep: dict) -> str:
    return (f"数据截至 {prep['valued_on']} 美股收盘（美东时间），{prep['made_on']} 生成。只讲 {prep['ticker']} 本身：不含个人账户"
            "数据，不给买卖指令和目标价；关键价位只来自均线和前高前低。")


def report_markdown(prep: dict) -> str:
    return "\n\n".join([f"# {title(prep)}", lead(prep), *[md for _, md in sections(prep)], DISCLAIMER]) + "\n"


# --- Jev and the forecast log ------------------------------------------------------------------------------

def _clean(value):
    """Plain JSON values: numpy numbers to Python, NaN to None, dates to text, floats to 6 significant digits."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if math.isnan(value) else float(f"{float(value):.6g}")
    if isinstance(value, (dt.date, pd.Timestamp)):
        return str(value.date()) if isinstance(value, pd.Timestamp) else str(value)
    return value


TREND_WORDS = {"上升": "up", "下降": "down", "转换中": "mixed", "向上": "up", "向下": "down", "震荡": "sideways",
               "转弱": "weakening (close below MA10 and MA20, MA10 still above MA20)",
               "转强": "strengthening (close above MA10 and MA20, MA10 still below MA20)"}
VERDICT_WORDS = {"证据不足": "too few independent windows", "没用": "no different from all days",
                 "弱偏涨": "weakly higher (2-3x noise)", "弱偏跌": "weakly lower (2-3x noise)",
                 "偏涨": "higher (over 3x noise)", "偏跌": "lower (over 3x noise)", "无可靠数据": "no data"}


def jev_request(prep: dict) -> tuple[dict, dict]:
    """State = the computed page (indicators, each signal's history, base rates, options, fundamentals, analysts);
    one boolean question per horizon: will the stock close above today's close at the target date?"""
    ind, t, close = prep["indicators"], prep["ticker"], prep["close"]
    f = prep["fundamentals"] if prep["fundamentals"].get("available") is not False else {}
    state = {
        "ticker": t, "as_of": prep["valued_on"], "close": close, "currency": "USD",
        "trend": {"long_term": TREND_WORDS[prep["trend"]["long"]], "short_term": TREND_WORDS[prep["trend"]["short"]]},
        "moving_averages": {f"MA{n}": v for n, v in ind["ma"].items()},
        "close_vs_moving_averages": {f"MA{n}": v for n, v in ind["vs_ma"].items()},
        "rsi14": ind["rsi"], "macd": ind["macd"], "volume_vs_prior_20d_average": ind["volume_ratio"],
        "atr14": ind["atr"], "atr_pct_of_close": ind["atr_pct"], "atr_pct_median_past_year": ind["atr_pct_median_1y"],
        "highs_and_lows": {lv["days"]: {k: lv[k] for k in ("high", "high_date", "low", "low_date")}
                           for lv in ind["levels"].values()},
        "returns_over_trading_days": {n: r for n, r in ind["relative"].items()},
        "signal_history": [{"signal": describe(r["signal"], r["state"], prep["sector"], prep["market"]) if r["state"] else r["signal"],
                            "horizon_trading_days": HORIZONS[r["horizon"]][0], "share_up_after": r.get("p_up"),
                            "share_up_all_days": r.get("base_p_up"), "median_return_after": r.get("median"),
                            "independent_windows": r.get("independent"), "verdict": VERDICT_WORDS[r["verdict"]]}
                           for h in HORIZONS for r in prep["evidence"][h]],
        "history_base_rates": {h: {"same_side_of_200d_average": prep["history"][h]["trend"].get("p_up"),
                                   "all_history": prep["history"][h]["all"].get("p_up"),
                                   "last_10_years": prep["history"][h]["last10y"].get("p_up")} for h in HORIZONS},
        "options": {h: {k: (prep["options"].get(h) or {}).get(k) for k in ("iv", "expiry", "p_up", "lo", "hi")} for h in HORIZONS},
        "earnings_day_move_1sd": (prep["options"].get("event") or {}).get("move"),
        "vix_fear_gauge": prep.get("vix"),
        "vxn_nasdaq100_fear_gauge": prep.get("vxn"),
        "next_earnings": {"official_date": (prep["earnings"].get("official") or {}).get("date"),
                          "estimated_dates": prep["earnings"].get("dates")},
        "fundamentals": {"pe_ttm": (f.get("valuation") or {}).get("pe_ttm"),
                         "pe_forward": (f.get("valuation") or {}).get("pe_forward"),
                         "ps_ttm": (f.get("valuation") or {}).get("ps_ttm"),
                         "latest_quarter_revenue_growth_qoq": (f.get("quarters") or [{}])[0].get("revenue_qoq"),
                         "latest_quarter_revenue_growth_yoy": (f.get("quarters") or [{}])[0].get("revenue_yoy"),
                         "eps_estimate_trend": f.get("trend"), "eps_revisions_30d": f.get("revisions")},
        "analysts": {k: v for k, v in prep["analysts"].items() if k in ("targets", "ratings", "changes_90d", "recent")},
        "note": "Share-up figures are fractions of past windows of this stock that ended higher (a long-run winner, so "
                "they lean high). Option odds are risk-neutral. Analyst targets are 12-month figures.",
    }
    questions = {}
    for h in HORIZONS:
        target = _target(prep, h).isoformat()
        questions[f"{t}_{h}"] = {
            "type": "boolean",
            "instructions": f"Based only on the data in the state, will {t} close above {close:.2f} USD on the first "
                            f"trading day on or after {target}?",
            "criteria": {"true": f"{t} closes above {close:.2f} at the horizon",
                         "false": f"{t} closes at or below {close:.2f} at the horizon"}}
    return _clean(state), questions


def jev_quote() -> dict:
    """The planned Jev call priced from the free model list: one call, ceiling = the most a call can cost."""
    table = jev.quote(1)
    row = table.iloc[0]
    return {"calls": int(row["次数"]), "endpoint": row["端点"], "model": row["模型"],
            "ceiling": float(row["合计上限（美元）"]), "max_tokens": jev.MAX_REQUEST_TOKENS,
            "price_per_token": float(row["单价上限（美元）"]) / jev.MAX_REQUEST_TOKENS}


def jev_odds(prep: dict, max_cost_usd: float, save_as=None) -> dict:
    """One Jev call for both horizons. max_cost_usd: jev.auto_budget() within the monthly limit, otherwise what the
    user confirmed from jev.quote(1)."""
    state, questions = jev_request(prep)
    probs, result = forecast.jev_odds(state, questions, max_cost_usd=max_cost_usd, save_as=save_as)
    return {**{h: probs[f"{prep['ticker']}_{h}"] for h in HORIZONS}, "cost_usd": result.cost_usd,
            "generation_id": result.generation_id, "as_of": dt.datetime.now().astimezone().isoformat(timespec="seconds")}


def journal_rows(prep: dict) -> list[dict]:
    """Rows for forecast.log_forecasts: one per horizon with every source's odds."""
    rows = []
    judged = prep.get("jev") or {}
    for h in HORIZONS:
        odds = prep["history"][h]
        rows.append({"ticker": prep["ticker"], "horizon": h, "as_of": prep["valued_on"], "target_date": _target(prep, h),
                     "close": prep["close"], "p_hist_all": odds["all"]["p_up"], "p_hist_10y": odds["last10y"]["p_up"],
                     "p_hist_trend": odds["trend"].get("p_up"), "p_options": (prep["options"].get(h) or {}).get("p_up"),
                     "p_jev": judged.get(h)})
    return rows


# --- the Decision card (v2): one screen per stock ----------------------------------------------------------

# The card's lines, in order. Each name maps to a builder in CARD_BUILDERS; add or drop a line here. "terms" (the
# card's words not yet marked 懂了 in the glossary, finclaude.glossary) can be passed in `fields`; it left the default
# on 2026-10-20 at the user's request: repeated on every card, while the Decision tab's first line already points to
# the glossary.
CARD_FIELDS = ("summary", "2w", "1m", "3m", "6m", "levels", "vix", "event", "signals", "fundamentals", "sources")
LONG_WORDS = {"上升": "长线在上升", "下降": "长线在下降", "转换中": "长线方向不明（收盘和 MA50 在 MA200 两边）"}
SHORT_WORDS = {"向上": "短线在上升", "转强": "短线刚转强", "震荡": "短线在横盘", "转弱": "短线在转弱", "向下": "短线在下降"}
NOT_USEFUL = ("没用", "证据不足", "无可靠数据")


def trend_sentence(trend: dict) -> str:
    """The trend in plain words, for the card's first sentence."""
    long, short = trend["long"], trend["short"]
    if long == "上升" and short in ("转弱", "向下"):
        return "长线还在上升，短线在回调"
    if long == "下降" and short in ("转强", "向上"):
        return "长线还在下降，短线在反弹"
    if long == "上升" and short == "向上":
        return "长线在上升，短线也在上升"
    if long == "下降" and short == "向下":
        return "长线在下降，短线也在下降"
    return f"{LONG_WORDS[long]}，{SHORT_WORDS[short]}"


def _whole(p) -> str:
    return "无可靠数据" if p is None or (isinstance(p, float) and math.isnan(p)) else f"{p:.0%}"


def _card_summary(prep: dict) -> str:
    o = prep["options"].get("2w") or {}
    if o.get("lo") is not None:
        span = f"未来 2 周有 68% 的可能在 {o['lo']:.0f}–{o['hi']:.0f} 美元之间"
    elif f"没有 {prep['valued_on']} 当天的成交" in (o.get("note") or ""):
        span = (f"期权区间暂缺：期权的最新成交不在 {prep['valued_on']}（美股开盘后会这样），"
                "等日线更新到美股收盘后再出卡就有")
    else:
        span = "未来 2 周的期权区间无可靠数据"
    return f"一句话：{trend_sentence(prep['trend'])}；{span}。"


def _card_horizon(prep: dict, h: str) -> str:
    o = prep["options"].get(h) or {}
    span = f"区间 {o['lo']:.0f}–{o['hi']:.0f}" if o.get("lo") is not None else "区间 无可靠数据"
    judged = (prep.get("jev") or {}).get(h)
    odds = " · ".join([f"历史 {_whole(_history_view(prep, h)['p_up'])}", f"期权 {_whole(o.get('p_up'))}",
                       f"Jev {_whole(judged)}" if judged is not None else "Jev 未调用"])
    return f"- {HORIZON_NAMES[h]}·{HORIZON_ROLES[h]}（到 {_target(prep, h)}）：{span}｜上涨概率 {odds}"


def _card_levels(prep: dict) -> str:
    ma, half = prep["indicators"]["ma"], prep["indicators"]["levels"]["近半年（126 个交易日）"]
    return (f"- 参考价位：MA20 {ma[20]:.0f} · MA200 {ma[200]:.0f} · 近半年高 {half['high']:.0f} / 低 {half['low']:.0f}"
            "（只是参考点，不是预测）")


def _card_event(prep: dict) -> str:
    e, close = prep["earnings"], prep["close"]
    fund = e.get("fund") or {}
    if fund.get("kind") == "index":
        holdings, weeks = f"指数前 {len(fund['holdings'])} 大成分", FUND_EVENT_DAYS // 7
        coming = "、".join(f"{h['ticker']} {h['date'][5:]}" for h in e.get("holdings") or [])
        if not coming:
            return f"- 下个事件：基金没有自己的财报；{holdings}未来 {weeks} 周没有已公布的财报日"
        return f"- 下个事件：基金没有自己的财报；{holdings}里，未来 {weeks} 周已公布财报日的有 {coming}（公司已公布）"
    official = e.get("official") or {}
    if official.get("date"):
        when = f"{official['date']}{' ' + official['time'] if official.get('time') else ''}（公司已公布）"
    elif e.get("dates"):
        when = f"约 {' / '.join(e['dates'])}（未官宣）"
    else:
        when = "日期无可靠数据"
    move = (prep["options"].get("event") or {}).get("move")
    priced = (f"；期权预期当天 ±{move:.1%}（约 {close * math.exp(-move):.0f}–{close * math.exp(move):.0f} 美元）"
              if move else "")
    return f"- 下个事件：{e.get('label', '下一季')}财报 {when}{priced}"


GAUGE_NAMES = {"vix": ("VIX", "VIX（恐慌指数）"), "vxn": ("VXN", "VXN（纳指恐慌指数）")}


def _card_vix(prep: dict) -> str:
    """The market mood: VIX for the whole market, VXN for the Nasdaq-100 (tech and chips)."""
    parts, dates = [], []
    for key, (short, name) in GAUGE_NAMES.items():
        v = prep.get(key)
        if not v:
            parts.append(f"{short} 无可靠数据")
            continue
        parts.append(f"{name}{v['close']:.1f}，比前一天 {v['change_1d']:+.0%}，比过去一年 {v['pct_1y']:.0%} 的日子高")
        dates.append(v["date"])
    return "- 市场情绪：" + "；".join(parts) + (f"（{dates[0]} 收盘，yfinance）" if dates else "")


def _useful_signals(prep: dict) -> list[dict]:
    return [r for r in prep["key_signals"] if r["verdict"] not in NOT_USEFUL]


def _card_signals(prep: dict) -> str | None:
    useful = _useful_signals(prep)
    if not useful:
        return None
    return "- 有用的信号：" + "；".join(
        f"「{_signal_words(prep, r)}」（{HORIZON_NAMES[r['horizon']]}：之后上涨 {_whole(r['p_up'])}，平常 "
        f"{_whole(r['base_p_up'])}，{r['verdict']}）" for r in useful)


def _card_fundamentals(prep: dict) -> str:
    f = prep["fundamentals"]
    fund = f.get("fund")
    if fund:
        follows = fund["index"] if fund["kind"] == "index" else fund["stock"]
        return f"- 基本面：基金没有自己的财报和分析师预期；跟踪 {follows}，每天 {fund['leverage']} 倍杠杆"
    if f.get("available") is False:
        return f"- 基本面：无可靠数据（{f.get('note', '')}）"
    parts = []
    latest = (f.get("quarters") or [{}])[0]
    if latest.get("revenue_yoy") is not None:
        parts.append(f"最近一季营收同比 {latest['revenue_yoy']:+.0%}")
    revised = ((f.get("trend") or {}).get("+1y") or {}).get("change_90d")
    if revised is not None:
        parts.append(f"分析师 90 天把下财年 EPS 预期{'上调' if revised >= 0 else '下调'} {abs(revised):.0%}")
    return "- 基本面：" + ("；".join(parts) if parts else "没有财报数据（ETF 或数据缺失）")


def _check_words(prep: dict) -> str:
    """How the second sources agreed, short names only; a row that could not be compared does not count."""
    checks = prep["checks"]
    rows = [r for r in checks.get("rows", []) if "ok" in r]
    if not rows:
        note = (checks.get("moomoo") or {}).get("note")
        return "没有交叉核对" + (f"（{note}）" if note else "")
    bad = [r["what"].split("（")[0] for r in rows if not r["ok"]]
    if not bad:
        return f"交叉核对 {len(rows)} 项都一致"
    return f"交叉核对 {len(rows)} 项，{len(bad)} 项不一致：{'、'.join(bad)}"


def _card_sources(prep: dict) -> str:
    fund = prep["fundamentals"].get("fund")
    parts = [f"{prep['price_source']}（日线）", "yfinance（期权）" if fund else "yfinance（期权、基本面）"]
    if fund and fund["kind"] == "index":
        parts.append(f"{fund['source']}（指数成分，{fund['as_of']}）")
    if (prep["checks"].get("moomoo") or {}).get("rights"):
        parts.append("Moomoo（核对）")
    judged = prep.get("jev")
    if judged:
        parts.append(f"Jev（{_stamp(judged.get('as_of'))}）")
    notes = [_check_words(prep)]
    if all(_record(prep, s, h)[0] < MIN_SCORED for s in SOURCE_WORDS for h in HORIZONS):
        notes.append("三个概率都还没有到期记分的成绩，先看区间")
    if not _useful_signals(prep):
        notes.append(f"均线、RSI、MACD 等 {len(STATES)} 个信号在 {prep['ticker']} 历史上没有预测力，不列")
    return f"来源：截至 {prep['valued_on']} 收盘，" + "、".join(parts) + "。" + "".join(f"{n}。" for n in notes)


CARD_BUILDERS = {"summary": _card_summary, **{h: (lambda p, h=h: _card_horizon(p, h)) for h in HORIZONS},
                 "levels": _card_levels, "vix": _card_vix, "event": _card_event, "signals": _card_signals,
                 "fundamentals": _card_fundamentals, "sources": _card_sources}


def card_markdown(prep: dict, fields=CARD_FIELDS, marks: dict | None = None) -> str:
    """The Decision card: a heading, one sentence, a few lines of numbers and the sources; nothing to work through.
    `marks`: the glossary marks (default: the ones kept in profile/glossary.json)."""
    lines = [f"### {prep['ticker']}　{prep['close']:.2f} 美元（{prep['valued_on']} 收盘）"]
    for field in fields:
        line = glossary.pointer("\n".join(lines), marks) if field == "terms" else CARD_BUILDERS[field](prep)
        if not line:
            continue
        if not (line.startswith("- ") and lines[-1].startswith("- ")):
            lines.append("")  # a paragraph and a list need a blank line between them
        lines.append(line)
    return "\n".join(lines) + "\n"
