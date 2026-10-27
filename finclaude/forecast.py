"""Forecast cards: for each holding and horizon, the odds of a higher price, taken from data and models, never from Claude.

Sources (CLAUDE.md, 铁律):
- history: the share of past windows of the same length that ended higher (daily prices via finclaude.data),
  over all history, the last 10 years, and the past days in the same trend state as today (close vs 200-day average)
- options: risk-neutral odds N(d2) and the ±1σ range from at-the-money implied volatility (yfinance chain). The
  volatility is backed out from each option's last trade in the same session as the closing price; yfinance's own
  impliedVolatility is unreliable while the market is closed, so it is not used
- analysts: price targets, rating counts, recent rating and target changes, estimate revisions (yfinance), quoted as
  published. Targets are 12-month figures: context for the card and for Jev, not scored
- Jev: one call per run combines the above (finclaude.jev); the skill shows jev.quote(1) and waits for confirmation
Every probability goes into journal/forecasts.csv and is scored with the Brier score once its target date has passed.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import pandas as pd
import yfinance as yf
from scipy.optimize import brentq
from scipy.stats import norm

from . import data, jev
from .config import RAW_DIR, ROOT, DataUnavailable

# horizon -> (trading days for history windows, calendar days for option expiries and target dates)
# The same four horizons as the Decision card (finclaude.analysis), the user's choice on 2026-10-20:
# 2 weeks for short-term trades, 1 and 3 months for medium to long, half a year for long
HORIZONS = {"2w": (10, 14), "1m": (21, 30), "3m": (63, 91), "6m": (126, 182)}
MIN_WINDOWS = 60
TREND_DAYS = 200
NEUTRAL = (0.45, 0.55)  # odds inside this band read as "about even"
RECENT_DAYS = 90
UNDERLYING = {"ORCX": ("ORCL", 2)}  # leveraged ETF -> (underlying, daily leverage)
ACTIONS = {"up": "上调评级", "down": "下调评级", "main": "维持", "reit": "重申", "init": "首次覆盖"}

JOURNAL = ROOT / "journal" / "forecasts.csv"
JOURNAL_FIELDS = ["created", "as_of", "ticker", "horizon", "target_date", "source", "p_up", "base_price",
                  "resolved_date", "resolved_price", "outcome", "brier"]
SCORED_SOURCES = ("hist_all", "hist_10y", "hist_trend", "options", "jev")
SOURCE_NAMES = {"hist_all": "历史（全部）", "hist_10y": "历史（近 10 年）", "hist_trend": "历史（同趋势）",
                "options": "期权市场", "jev": "Jev 综合"}


# --- history -----------------------------------------------------------------------------------------------

def _window_stats(fwd: pd.Series) -> dict:
    if len(fwd) < MIN_WINDOWS:
        return {"p_up": None, "windows": int(len(fwd)), "note": f"只有 {len(fwd)} 个窗口，少于 {MIN_WINDOWS}"}
    return {"p_up": float((fwd > 0).mean()), "windows": int(len(fwd)),
            "p10": float(fwd.quantile(0.1)), "p50": float(fwd.quantile(0.5)), "p90": float(fwd.quantile(0.9)),
            "start": str(fwd.index[0].date()), "end": str(fwd.index[-1].date())}


def history_odds(close: pd.Series, days: int, breaks=()) -> dict:
    """Share of past `days`-trading-day windows that ended higher, with the 10th/50th/90th percentile returns.

    `breaks`: dates where the price scale changes (data.level_breaks, e.g. IVV.AX on 2011-01-04). Windows that
    cross a break are left out and the 200-day average is taken on each side separately; both sides still count.
    """
    close = close.dropna()
    part = pd.Series(0, index=close.index)  # which side of each break a day sits on
    for b in breaks:
        part.iloc[close.index.searchsorted(pd.Timestamp(b)):] += 1
    fwd = (close.shift(-days) / close - 1)
    fwd = fwd[(part.shift(-days) == part)].dropna()
    ten_years_ago = close.index[-1] - pd.DateOffset(years=10)
    average = close.groupby(part).transform(lambda s: s.rolling(TREND_DAYS).mean())
    above = (close > average)[average.notna()]
    odds = {"all": _window_stats(fwd),
            "last10y": _window_stats(fwd[fwd.index >= ten_years_ago]) if close.index[0] < ten_years_ago
            else {"p_up": None, "windows": 0, "note": "历史不足 10 年，同「全部」"}}
    if above.empty:
        odds["trend"] = {"p_up": None, "windows": 0, "note": f"历史不足 {TREND_DAYS} 个交易日，算不出趋势状态"}
    else:
        now = bool(above.iloc[-1])
        same = fwd[fwd.index.isin(above.index[above == now])]
        odds["trend"] = {**_window_stats(same), "state": "above" if now else "below"}
    return odds


# --- options -----------------------------------------------------------------------------------------------

def bs_price(spot: float, strike: float, years: float, rate: float, vol: float, call: bool) -> float:
    """Black-Scholes price of a European option (no dividends)."""
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if call:
        return spot * norm.cdf(d1) - strike * math.exp(-rate * years) * norm.cdf(d2)
    return strike * math.exp(-rate * years) * norm.cdf(-d2) - spot * norm.cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, years: float, rate: float, call: bool) -> float:
    """The volatility at which bs_price equals `price`; ValueError when no volatility between 0.01% and 500% fits."""
    return brentq(lambda vol: bs_price(spot, strike, years, rate, vol, call) - price, 1e-4, 5.0)


def pick_expiry(expiries: list[str], valued_on: dt.date, days: int) -> str:
    """The listed expiry closest to `days` calendar days ahead, ignoring the coming week."""
    future = [e for e in expiries if dt.date.fromisoformat(e) > valued_on + dt.timedelta(days=7)]
    if not future:
        raise DataUnavailable(f"没有 {valued_on} 一周之后到期的期权")
    return min(future, key=lambda e: abs((dt.date.fromisoformat(e) - valued_on).days - days))


def _same_session_price(row: pd.Series, valued_on: dt.date) -> float | None:
    """The option's last trade price, if that trade happened in the session whose close is the spot price."""
    when = row.get("lastTradeDate")
    if when is None or pd.isna(when) or not row["lastPrice"] > 0:
        return None
    when = pd.Timestamp(when)
    when = when.tz_localize("UTC") if when.tzinfo is None else when
    return float(row["lastPrice"]) if when.tz_convert("America/New_York").date() == valued_on else None


def option_view(spot: float, vol: float, rate: float, days: int) -> dict:
    """From an annual volatility to `days` calendar days: width = vol × √(days/365), the ±1σ range spot × e^(∓width)
    (68% of outcomes under the model) and the risk-neutral odds of finishing above spot, N((rate − vol²/2)√T / vol)."""
    years = days / 365
    width = vol * math.sqrt(years)
    return {"p_up": float(norm.cdf((rate - vol * vol / 2) * math.sqrt(years) / vol)), "width": width,
            "lo": spot * math.exp(-width), "hi": spot * math.exp(width)}


def option_odds(chain: pd.DataFrame, spot: float, valued_on: dt.date, expiry: str, rate: float,
                horizon_days: int | None = None) -> dict:
    """Risk-neutral odds of finishing above today's close, and the ±1σ range, from at-the-money implied volatility.

    The volatility is backed out with the option's own time to expiry; the odds and the range are then taken to
    `horizon_days` (the card's target date), because the nearest listed expiry is rarely exactly 30 or 91 days out.
    """
    calls, puts = chain[chain["type"] == "call"], chain[chain["type"] == "put"]
    strikes = sorted(set(calls["strike"]) & set(puts["strike"]))
    if not strikes:
        raise DataUnavailable(f"{expiry} 的期权链没有同时挂出看涨和看跌的行权价")
    strike = min(strikes, key=lambda k: abs(k - spot))
    years = (dt.date.fromisoformat(expiry) - valued_on).days / 365
    vols, used, prices = [], [], {}
    for call, rows in ((True, calls), (False, puts)):
        price = _same_session_price(rows[rows["strike"] == strike].iloc[0], valued_on)
        if price is None:
            continue
        try:
            vol = implied_vol(price, spot, strike, years, rate, call)
        except ValueError:
            continue
        if 0.01 <= vol <= 5:
            vols.append(vol)
            used.append("看涨" if call else "看跌")
            prices["call" if call else "put"] = price
    if not vols:
        raise DataUnavailable(f"{expiry} 行权价 {strike} 没有 {valued_on} 当天的成交，无可靠期权数据")
    vol = sum(vols) / len(vols)
    days = (dt.date.fromisoformat(expiry) - valued_on).days
    horizon_days = horizon_days or days
    return {**option_view(spot, vol, rate, horizon_days), "iv": vol, "expiry": expiry, "days": days,
            "horizon_days": horizon_days, "strike": float(strike), "basis": "last", "used": used, "prices": prices}


def risk_free() -> tuple[float, str]:
    """13-week US Treasury bill yield (^IRX, yfinance) as a decimal, with its date."""
    irx = data.get_prices("^IRX")["close"]
    return float(irx.iloc[-1]) / 100, str(irx.index[-1].date())


# --- analysts ----------------------------------------------------------------------------------------------

def _fetch_analyst(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    raw = {"ticker": ticker, "targets": t.analyst_price_targets, "summary": t.recommendations_summary,
           "changes": t.upgrades_downgrades, "revisions": t.eps_revisions}
    path = RAW_DIR / "yfinance" / f"{ticker}_analyst_{dt.date.today().isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = {k: v.reset_index().to_dict(orient="records") if isinstance(v, pd.DataFrame) else v for k, v in raw.items()}
    path.write_text(json.dumps(frames, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    return raw


def analyst_view(ticker: str, today: dt.date | None = None) -> dict:
    """Published analyst data, quoted as is: targets, rating counts, recent changes, estimate revisions."""
    today = today or dt.date.today()
    underlying = UNDERLYING.get(ticker)
    name = underlying[0] if underlying else ticker
    raw = _fetch_analyst(name)
    targets = dict(raw.get("targets") or {})
    summary = raw.get("summary")
    if not targets.get("mean") and not (isinstance(summary, pd.DataFrame) and not summary.empty):
        return {"available": False, "ticker": name, "for": ticker, "source": "yfinance", "as_of": str(today),
                "note": f"yfinance 没有 {name} 的分析师数据（ETF 通常没有分析师覆盖）"}
    view = {"ticker": name, "for": ticker, "source": "yfinance", "as_of": str(today),
            "note": f"{ticker} 是 {underlying[1]} 倍杠杆 ETF，没有分析师覆盖；以下是标的 {name} 的数据" if underlying else "",
            "targets": targets}
    if targets.get("mean") and targets.get("current"):
        view["upside_mean"] = targets["mean"] / targets["current"] - 1
    if isinstance(summary, pd.DataFrame) and not summary.empty:
        now = summary[summary["period"] == "0m"].iloc[0]
        view["ratings"] = {k: int(now[k]) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    changes = raw.get("changes")
    if isinstance(changes, pd.DataFrame) and not changes.empty:
        recent = changes[changes.index >= pd.Timestamp(today - dt.timedelta(days=RECENT_DAYS))].sort_index(ascending=False)
        view["changes_90d"] = {k: int(v) for k, v in recent["Action"].value_counts().items()}
        view["recent"] = [{"date": str(i.date()), "firm": r["Firm"], "action": ACTIONS.get(r["Action"], r["Action"]),
                           "from": r["FromGrade"], "to": r["ToGrade"], "target": r.get("currentPriceTarget"),
                           "prior_target": r.get("priorPriceTarget")} for i, r in recent.head(5).iterrows()]
    revisions = raw.get("revisions")
    if isinstance(revisions, pd.DataFrame) and not revisions.empty:
        view["revisions"] = {p: {"up_30d": int(r["upLast30days"]), "down_30d": int(r["downLast30days"])}
                             for p, r in revisions.iterrows() if p in ("0q", "+1q")}
    return view


# --- cards -------------------------------------------------------------------------------------------------

def prepare(ticker: str, rate: float) -> dict:
    """Everything a card needs for one ticker; a source that cannot be read is recorded with its reason."""
    prices = data.get_prices(ticker)
    close = prices["close"]
    spot, valued_on = float(close.iloc[-1]), close.index[-1].date()
    prep = {"ticker": ticker, "close": spot, "valued_on": valued_on, "price_source": prices["source"].iloc[-1],
            "currency": data.currency_of(ticker),
            "level_breaks": [pd.Timestamp(b) for b in prices.attrs.get("level_breaks", [])],
            "history": {}, "options": {}}
    prep["history"] = {h: history_odds(close, days, breaks=prep["level_breaks"]) for h, (days, _) in HORIZONS.items()}
    try:
        expiries = data.option_expiries(ticker)
    except Exception as e:  # noqa: BLE001 - the card says why the source is missing
        expiries, reason = [], f"读不到期权到期日（{type(e).__name__}）"
    else:
        reason = "没有挂牌期权"
    for h, (_, calendar_days) in HORIZONS.items():
        try:
            if not expiries:
                raise DataUnavailable(reason)
            expiry = pick_expiry(expiries, valued_on, calendar_days)
            prep["options"][h] = option_odds(data.get_option_chain(ticker, expiry), spot, valued_on, expiry, rate,
                                             horizon_days=calendar_days)
        except DataUnavailable as e:
            prep["options"][h] = {"p_up": None, "note": str(e)}
    try:
        prep["analysts"] = analyst_view(ticker)
    except Exception as e:  # noqa: BLE001
        prep["analysts"] = {"available": False, "note": f"读不到分析师数据（{type(e).__name__}）"}
    return prep


def lean(p: float) -> str:
    return "偏涨" if p > NEUTRAL[1] else "偏跌" if p < NEUTRAL[0] else "接近一半"


def agreement(probs: list) -> str:
    """Whether the available sources point the same way (history, options, Jev)."""
    leans = [lean(p) for p in probs if p is not None]
    if len(leans) < 2:
        return "来源不足"
    return f"一致：{leans[0]}" if len(set(leans)) == 1 else "不一致"


def card_rows(prep: dict, holding_aud: float, jev_probs: dict | None = None) -> list[dict]:
    """One row per horizon: every source's odds, the option range in USD and in AUD for this holding."""
    rows, ticker, spot = [], prep["ticker"], prep["close"]
    for h, (_, calendar_days) in HORIZONS.items():
        if h not in prep["history"]:  # a card prepared before a horizon was added
            continue
        hist, opt = prep["history"][h], prep["options"].get(h) or {}
        p = {"hist_all": hist["all"]["p_up"], "hist_10y": hist["last10y"]["p_up"], "hist_trend": hist["trend"]["p_up"],
             "options": opt.get("p_up"), "jev": (jev_probs or {}).get(f"{ticker}_{h}")}
        row = {"ticker": ticker, "horizon": h, "as_of": prep["valued_on"],
               "target_date": prep["valued_on"] + dt.timedelta(days=calendar_days), "close": spot,
               **{f"p_{k}": v for k, v in p.items()}, "trend_state": hist["trend"].get("state"),
               "iv": opt.get("iv"), "expiry": opt.get("expiry"), "strike": opt.get("strike"),
               "option_days": opt.get("horizon_days"), "option_types": "和".join(opt.get("used") or []) or None,
               "range_lo": opt.get("lo"), "range_hi": opt.get("hi"),
               "holding_aud": holding_aud}
        row["aud_lo"] = holding_aud * (row["range_lo"] / spot - 1) if row["range_lo"] else None
        row["aud_hi"] = holding_aud * (row["range_hi"] / spot - 1) if row["range_hi"] else None
        all_hist = hist["all"]
        row["hist_aud_p10"] = holding_aud * all_hist["p10"] if all_hist.get("p10") is not None else None
        row["hist_aud_p90"] = holding_aud * all_hist["p90"] if all_hist.get("p90") is not None else None
        history_view = p["hist_trend"] if p["hist_trend"] is not None else p["hist_all"]
        row["agreement"] = agreement([history_view, p["options"], p["jev"]])
        rows.append(row)
    return rows


RATING_NAMES = {"strongBuy": "Strong Buy", "buy": "Buy", "hold": "Hold", "sell": "Sell", "strongSell": "Strong Sell"}
CURRENCY_NAMES = {"USD": "美元", "AUD": "澳元", "HKD": "港元"}


def _pct(p) -> str:
    return "无可靠数据" if p is None or pd.isna(p) else f"{p:.1%}"


HORIZON_WORDS = {"2w": "2 周", "6m": "半年"}  # names that are not "N 个月"


def _horizon_name(h: str) -> str:
    return HORIZON_WORDS.get(h) or h.replace("m", " 个月")


def _compared(r: dict) -> list[tuple[str, float | None]]:
    """The three sources the agreement compares, under the short names the card uses."""
    history = r["p_hist_trend"] if r["p_hist_trend"] is not None else r["p_hist_all"]
    return [("历史", history), ("期权", r["p_options"]), ("Jev", r["p_jev"])]


def _verdict(r: dict) -> str:
    name, verdict = _horizon_name(r["horizon"]), r["agreement"]
    present = [(n, p) for n, p in _compared(r) if p is not None]
    if verdict.startswith("一致"):
        return f"{name}{verdict.split('：')[1]}（{len(present)} 个来源一致）"
    if verdict == "不一致":
        return f"{name}来源不一致（" + "、".join(f"{n}{' ' if n.isascii() else ''}{lean(p)}" for n, p in present) + "）"
    return f"{name}来源不足"


def card_markdown(prep: dict, rows: list[dict], board: pd.DataFrame) -> str:
    """One card per holding, read line by line: the verdict, the odds per horizon, the price ranges (about the stock
    only, no holding amounts), the score once there is one, and the analyst data as published."""
    by_h = {r["horizon"]: r for r in rows}
    hs = [h for h in HORIZONS if h in by_h]
    held = " / ".join(_horizon_name(h) for h in hs)
    first = by_h[hs[0]]
    unit = CURRENCY_NAMES.get(prep.get("currency", "USD"), prep.get("currency", ""))
    lines = [f"### {prep['ticker']}　收盘 {prep['close']:.2f} {unit}（{prep['valued_on']}，{prep['price_source']}）", "",
             "**结论**：" + "；".join(_verdict(by_h[h]) for h in hs) + "。", "",
             f"**上涨概率**（到期那天收盘高于 {prep['close']:.2f} {unit}的概率；高于 55% 算偏涨，低于 45% 算偏跌）"]
    for h in hs:
        r = by_h[h]
        odds = "，".join(f"{n} {_pct(p)}" if p is not None or n != "Jev" else "Jev 无数据" for n, p in _compared(r))
        lines.append(f"- {_horizon_name(h)}（到 {r['target_date']}）：{odds} → {r['agreement']}")
    history = {h: prep["history"][h] for h in hs}
    ten = ("近 10 年 " + " / ".join(_pct(history[h]["last10y"]["p_up"]) for h in hs) if first["p_hist_10y"] is not None
           else f"近 10 年（{history[hs[0]]['last10y'].get('note', '无可靠数据')}）")
    others = f"全部历史 {' / '.join(_pct(history[h]['all']['p_up']) for h in hs)}；{ten}"
    state = {"above": "在 200 日均线上方", "below": "在 200 日均线下方"}.get(first["trend_state"])
    if state and first["p_hist_trend"] is not None:
        lines.append(f"- 「历史」只看过去和今天一样{state}的日子，从那天起持有 {held}后上涨的比例。"
                     f"另两种算法：{others}。")
    else:
        lines.append(f"- 「历史」= 全部历史里持有 {held}后上涨的比例"
                     f"（{history[hs[0]]['trend'].get('note', '算不出趋势状态')}）。{ten}。")
    if any(by_h[h]["iv"] is not None for h in hs):
        for h in hs:
            r = by_h[h]
            if r["iv"] is None:
                lines.append(f"- 期权 {_horizon_name(h)}：无可靠数据（{(prep['options'].get(h) or {}).get('note', '')}）。")
                continue
            lines.append(f"- 期权 {_horizon_name(h)}：用 {prep['ticker']} 自己的期权，{r['expiry']} 到期（挂牌日期里最接近 "
                         f"{_horizon_name(h)}的）、行权价 {r['strike']:.2f} {unit}（最接近今天收盘）的"
                         f"{r['option_types'] or ''}期权，从成交价反推隐含波动率（implied volatility）{r['iv']:.1%}，"
                         f"再按到 {r['target_date']} 的 {r['option_days']} 天换算。")
    else:
        lines.append(f"- 期权：{(prep['options'].get(hs[0]) or {}).get('note', '无可靠数据')}。")
    lines += ["", "**到期时的价格范围**（只讲标的本身，不按持仓算账面）"]
    for h in hs:
        r, past = by_h[h], history[h]["all"]
        option = ("无可靠数据" if r["range_lo"] is None else
                  f"68% 的可能在 {r['range_lo']:.2f}–{r['range_hi']:.2f} {unit}之间；比这更低、更高的可能各 16%")
        spread = ("无可靠数据" if past.get("p10") is None else
                  f"过去同长度窗口里，中间 80% 落在 {prep['close'] * (1 + past['p10']):.2f}–{prep['close'] * (1 + past['p90']):.2f} "
                  f"{unit}（按今天收盘换算），最差和最好的各 10% 在这之外")
        lines += [f"- {_horizon_name(h)}（到 {r['target_date']}）", f"    - 期权：{option}。", f"    - 历史：{spread}。"]
    scored = board[board["source"].isin(SCORED_SOURCES)] if not board.empty else board
    if not scored.empty:
        lines += ["", "**过去准不准**（Brier 分数越低越准，抛硬币是 0.25）：" + "；".join(
            f"{SOURCE_NAMES[s.source]} {_horizon_name(s.horizon)}：{int(s.scored)} 次，Brier {s.brier:.3f}"
            for s in scored.itertuples())]
    a = prep["analysts"]
    lines += ["", f"**分析师**（{a.get('source', 'yfinance')}，{a.get('as_of', '')}，原样引用，目标价是 12 个月口径）"]
    if a.get("available") is False:
        lines.append(f"- {a.get('note', '没有数据')}")
        return "\n".join(lines)
    if a.get("note"):
        lines.append(f"- {a['note']}")
    t = a.get("targets") or {}
    if t.get("mean"):
        lines.append(f"- 目标价：均值 {t['mean']:.2f}、中位 {t.get('median', float('nan')):.2f}、最高 {t.get('high', float('nan')):.2f}、"
                     f"最低 {t.get('low', float('nan')):.2f} 美元；均值比 {a['ticker']} 当前价 {t.get('current', float('nan')):.2f} "
                     f"{'高' if a.get('upside_mean', 0) >= 0 else '低'} {abs(a.get('upside_mean', 0)):.1%}")
    if a.get("ratings"):
        lines.append("- 评级：" + "、".join(f"{RATING_NAMES[k]} {v}" for k, v in a["ratings"].items()))
    if a.get("changes_90d"):
        lines.append(f"- 近 {RECENT_DAYS} 天评级动作：" + "、".join(f"{ACTIONS.get(k, k)} {v}" for k, v in a["changes_90d"].items()))
    for c in a.get("recent", []):
        grade = f"{c['from']} → {c['to']}" if isinstance(c["from"], str) and c["from"] else c["to"]
        known = lambda v: v is not None and not pd.isna(v) and v > 0  # noqa: E731 - yfinance writes 0 for "no prior target"
        target = ("" if not known(c["target"]) else f"，目标价 {c['prior_target']:.0f} → {c['target']:.0f}"
                  if known(c["prior_target"]) else f"，目标价 {c['target']:.0f}")
        lines.append(f"    - {c['date']} {c['firm']}：{c['action']} {grade}{target}")
    for period, label in (("0q", "本季"), ("+1q", "下季")):
        rev = (a.get("revisions") or {}).get(period)
        if rev:
            lines.append(f"- {label} EPS 预期近 30 天：上调 {rev['up_30d']} 次、下调 {rev['down_30d']} 次")
    return "\n".join(lines)


def primer_markdown(prep: dict, row: dict) -> str:
    """读卡前的小课堂, worked through with one holding's real numbers: `row` is its 1-month card row, with options."""
    t, unit, spot = prep["ticker"], CURRENCY_NAMES.get(prep.get("currency", "USD"), ""), prep["close"]
    iv, days, target = row["iv"], row["option_days"], row["target_date"]
    root = math.sqrt(days / 365)
    width = iv * root
    up, down = math.exp(width) - 1, 1 - math.exp(-width)
    expiry_days = (dt.date.fromisoformat(row["expiry"]) - row["as_of"]).days
    history = row["p_hist_trend"] if row["p_hist_trend"] is not None else row["p_hist_all"]
    side = {"above": "上方", "below": "下方"}.get(row["trend_state"], "同一侧")
    example = next((p for p in (row["p_jev"], history) if p is not None), None)
    lines = [
        '<details markdown="1" open>', "<summary>读卡前的小课堂：三个来源各是什么，期权和你的持仓有什么关系（可收起）</summary>", "",
        f"**一句话：你不用买卖期权。** 卡上的「期权」只是借用别人买卖期权的价格，读出市场预计 {t} 接下来会波动多大。"
        f"下面用 {t} 的真实数字走一遍。", "",
        "**1. 期权（option）是什么**", "",
        "- 期权是一份合约：在到期日（expiry）之前，有权按约定的行权价（strike）买或卖一只股票。买这份权利要付钱，"
        "叫权利金（premium）；过了到期日，权利作废。",
        "- 看涨期权（call）：有权按行权价**买入**。股价涨得越多越值钱，相当于押它涨。",
        "- 看跌期权（put）：有权按行权价**卖出**。股价跌得越多越值钱，相当于给股票买下跌保险。",
        f"- 你持有的 {t} 是份额本身：涨了赚、跌了亏，没有到期日，也不付权利金。期权是另一群人对同一只 {t} 的押注。",
        *([f"- {t} 每天做 {UNDERLYING[t][0]} 涨跌的 {UNDERLYING[t][1]} 倍，它自己也有挂牌期权；卡上用的是 {t} 的期权，"
           f"不是 {UNDERLYING[t][0]} 的，所以波动率里已经包含了杠杆放大的部分。"] if t in UNDERLYING else []), "",
        "**2. 期权价格怎么告诉我们「会波动多大」**", "",
        "- 股价可能波动越大，期权到期时越有机会值钱，卖方就要价越高。反过来，从期权的成交价能倒推出市场认为的波动幅度，"
        "叫隐含波动率（implied volatility，IV），按一年算。",
        f"- 这张卡用的是 {t} 自己的期权：{row['expiry']} 到期、行权价 {row['strike']:.2f} {unit}（最接近今天收盘 "
        f"{spot:.2f} {unit}）的{row['option_types'] or ''}期权，取 {row['as_of']} 当天的最后成交价倒推，平均隐含波动率 {iv:.1%}。",
        f"- 为什么不用 {target} 到期的：期权只在固定日期到期，挂牌日期里离 {days} 天最近的是 {row['expiry']}"
        f"（{expiry_days} 天后）。我们只借它的波动率，再换算到 {target}。", "",
        f"**3. 从波动率到卡上的价格区间**（{t} 1 个月）", "",
        f"- 波动按时间的平方根变大：{days} 天的波动幅度 = {iv:.1%} × √({days} ÷ 365) = {iv:.1%} × {root:.4f} = {width:.1%}。",
        f"- 上限 = {spot:.2f} × e^{width:.4f} = {row['range_hi']:.2f} {unit}（涨 {up:.1%}）；"
        f"下限 = {spot:.2f} ÷ e^{width:.4f} = {row['range_lo']:.2f} {unit}（跌 {down:.1%}）。"
        f"e 是 2.718…，用它是因为涨跌按复利算：涨 {up:.1%} 和跌 {down:.1%} 是同样大小的一步。",
        f"- 按模型，到 {target} 有 68% 的可能落在 {row['range_lo']:.2f}–{row['range_hi']:.2f} {unit}之间，"
        "比下限更低、比上限更高的可能各 16%。",
        "- 读法：这是「正常波动」的范围。跌到下限附近不代表预测错了，只是落在了 16% 那一侧。", "",
        "**4. 「期权上涨概率」为什么总在 50% 附近**", "",
        f"- 把同样的波动率放进期权定价公式（Black-Scholes），算到期价高于今天收盘的概率：{t} 是 {_pct(row['p_options'])}。",
        "- 这是风险中性概率（risk-neutral probability）：假设市场上的人都不要求额外风险补偿时的概率。它基本只反映波动大小，"
        "不反映市场看好还是看空，所以常在 50% 附近。",
        "- 波动越大，它越低于 50%：涨一倍和跌一半在复利上是对称的两步，所以「收得比今天高」略少于一半。",
        "- 所以期权这一栏更值得看的是区间（会波动多大），概率只作参考。", "",
        "**5. 另外两个来源**", "",
        f"- 历史：把过去每一天都当作「今天」，看持有同样天数后是涨是跌，数涨的比例。「同趋势」只数和今天一样在 200 日均线"
        f"（过去 200 个交易日收盘价的平均）{side}的日子。{t} 1 个月是 {_pct(history)}。历史只包括活到今天的公司，偏乐观。",
        "- Jev：把历史、期权和分析师数据一起交给 Jev 模型，它返回一个综合概率。",
    ]
    if example is not None:
        lines.append(f"- 概率 {example:.0%} 的意思：类似的情况重复 100 次，到期收涨的有 {example * 100:.0f} 次左右，不是「一定涨」或「一定跌」。"
                     "哪个来源更准，要等很多次到期记分（Brier 分数）才看得出来：差别小的来源，往往要几百次独立预测才分得清。")
    lines += ["", "</details>"]
    return "\n".join(lines)


# --- Jev ---------------------------------------------------------------------------------------------------

def jev_request(preps: list[dict], rows: list[dict]) -> tuple[dict, dict]:
    """State = the computed sources for every holding; one boolean question per holding and horizon."""
    holdings = []
    for prep in preps:
        mine = [r for r in rows if r["ticker"] == prep["ticker"]]
        analysts = {k: v for k, v in prep["analysts"].items() if k in ("ticker", "note", "targets", "upside_mean",
                                                                       "ratings", "changes_90d", "recent", "revisions")}
        holdings.append({"ticker": prep["ticker"], "close": prep["close"], "currency": prep.get("currency", "USD"),
                         "close_date": str(prep["valued_on"]),
                         "horizons": [{"horizon": r["horizon"], "target_date": str(r["target_date"]),
                                       **{SOURCE_NAMES[s]: r[f"p_{s}"] for s in SCORED_SOURCES if s != "jev"},
                                       "trend_vs_200d_average": r["trend_state"], "option_implied_vol": r["iv"]}
                                      for r in mine],
                         "analysts": analysts})
    state = {"as_of": str(max(p["valued_on"] for p in preps)), "holdings": holdings,
             "note": "History shares are fractions of past windows that ended higher; option odds are risk-neutral; "
                     "analyst targets are 12-month figures."}
    questions = {}
    for r in rows:
        target = r["target_date"].isoformat()
        questions[f"{r['ticker']}_{r['horizon']}"] = {
            "type": "boolean",
            "instructions": f"Based only on the data in the state, will {r['ticker']} close above {r['close']:.2f} "
                            f"on the first trading day on or after {target}?",
            "criteria": {"true": f"{r['ticker']} closes above {r['close']:.2f} at the horizon",
                         "false": f"{r['ticker']} closes at or below {r['close']:.2f} at the horizon"}}
    return state, questions


def jev_odds(state: dict, questions: dict, max_cost_usd: float, save_as: Path | None = None) -> tuple[dict, object]:
    """One Jev call for all questions; the probability of 'true' for each."""
    result = jev.evaluate(state, questions, max_cost_usd=max_cost_usd, save_as=save_as)
    return {k: float(result.answers[k]["probability"]) for k in questions}, result


# --- journal and scoring -----------------------------------------------------------------------------------

def _read_journal() -> pd.DataFrame:
    if not JOURNAL.exists():
        return pd.DataFrame(columns=JOURNAL_FIELDS)
    log = pd.read_csv(JOURNAL, encoding="utf-8")
    for field in JOURNAL_FIELDS:
        if field not in log.columns:
            log[field] = None
    return log[JOURNAL_FIELDS]


def _write_journal(log: pd.DataFrame) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    log[JOURNAL_FIELDS].to_csv(JOURNAL, index=False, encoding="utf-8")


def log_forecasts(rows: list[dict], created: dt.date | None = None) -> int:
    """Append every available probability to the journal, once per (as_of, ticker, horizon, source)."""
    log = _read_journal()
    seen = set(zip(log["as_of"].astype(str), log["ticker"], log["horizon"], log["source"]))
    new = []
    for r in rows:
        for source in SCORED_SOURCES:
            p = r[f"p_{source}"]
            key = (str(r["as_of"]), r["ticker"], r["horizon"], source)
            if p is None or key in seen:
                continue
            seen.add(key)
            new.append({"created": str(created or dt.date.today()), "as_of": str(r["as_of"]), "ticker": r["ticker"],
                        "horizon": r["horizon"], "target_date": str(r["target_date"]), "source": source,
                        "p_up": round(float(p), 6), "base_price": r["close"]})
    if new:
        frames = [f for f in (log, pd.DataFrame(new, columns=JOURNAL_FIELDS)) if not f.empty]
        _write_journal(pd.concat(frames, ignore_index=True))
    return len(new)


def score(load=data.get_prices) -> int:
    """Resolve forecasts whose target date has a close: outcome 1 if the first close on or after it beat the base price."""
    log = _read_journal()
    open_rows = log[log["outcome"].isna()]
    resolved = 0
    for ticker in open_rows["ticker"].unique():
        close = load(ticker)["close"]
        for i, r in open_rows[open_rows["ticker"] == ticker].iterrows():
            after = close[close.index >= pd.Timestamp(r["target_date"])]
            if after.empty:
                continue
            outcome = int(after.iloc[0] > r["base_price"])
            log.loc[i, ["resolved_date", "resolved_price", "outcome", "brier"]] = [
                str(after.index[0].date()), float(after.iloc[0]), outcome, (float(r["p_up"]) - outcome) ** 2]
            resolved += 1
    if resolved:
        _write_journal(log)
    return resolved


def scoreboard() -> pd.DataFrame:
    """Per source and horizon: forecasts scored, mean Brier score (a coin flip scores 0.25), hit rate."""
    log = _read_journal().dropna(subset=["brier"])
    if log.empty:
        return pd.DataFrame(columns=["source", "horizon", "scored", "brier", "coin_flip", "hit_rate"])
    log = log.assign(p_up=log["p_up"].astype(float), outcome=log["outcome"].astype(float), brier=log["brier"].astype(float))
    called = log[log["p_up"] != 0.5]
    hits = ((called["p_up"] > 0.5) == (called["outcome"] == 1)).groupby([called["source"], called["horizon"]]).mean()
    board = log.groupby(["source", "horizon"]).agg(scored=("brier", "size"), brier=("brier", "mean")).reset_index()
    board["coin_flip"] = 0.25
    board["hit_rate"] = [hits.get((s, h)) for s, h in zip(board["source"], board["horizon"])]
    return board
