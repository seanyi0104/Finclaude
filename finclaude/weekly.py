"""The weekly rhythm (CLAUDE.md「每周交付」): a week is Monday to Friday of US trading, New York time. Each week has one
tab in the weekly doc (News / Decision / Lesson under it) and one folder of trimmed archives, reports/<week>/.

The Friday review of holdings is the one place that reads the account (read-only, finclaude.account): what the
whole account made or lost this week, what each holding did and its normal range for the next two weeks. Each week's
account total is saved in data/weekly/ (not in git) to compare against next week. No account numbers are shown.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from . import account, analysis, data, forecast, jev, quote
from .config import DATA_DIR, DataUnavailable, NotConfigured

GLOSSARY_ORDER = "z0"  # the glossary tab sorts after every week tab
CURRENCY_WORDS = {"USD": "美元", "AUD": "澳元", "HKD": "港元"}
POSITIONS_ONLY = pd.DataFrame(columns=["code", "name", "market", "side", "qty", "price", "time"])
WEEKLY_DIR = DATA_DIR / "weekly"
# Moomoo's names for money moved between the account and a bank (2026-10-19: deposits came as 银行转存). Dividends,
# interest, tax, currency exchange and trade settlement stay inside the account and count in the week's result.
BANK_WORDS = ("银行", "入金", "出金", "提款", "提取")
# While US stocks trade (pre-market, regular, after-hours or overnight) Moomoo values US holdings at the price of the
# moment (e.g. at 01:59 New York on a Monday it uses the overnight price, not Friday's close).
SESSION_WORDS = {"pre": "盘前", "regular": "盘中", "after": "盘后", "overnight": "夜盘"}


def us_day(now: dt.datetime | None = None) -> dt.date:
    """Today's date in New York (US trading days are counted there)."""
    stamp = pd.Timestamp(now or dt.datetime.now().astimezone())
    return stamp.tz_convert("America/New_York").date()


def week_of(day: dt.date) -> tuple[dt.date, dt.date]:
    """Monday and Friday of the week holding `day`; a Saturday or Sunday belongs to the week that just ended."""
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=4)


def close_and_open(day: dt.date) -> tuple[dt.date, dt.date]:
    """Mondays of the week /week closes and the week it opens, run on `day` (a New York date). On a weekday last week
    closes and this week opens; on a weekend the week that just ended closes and next week opens."""
    monday, _ = week_of(day)
    if day.weekday() >= 5:
        return monday, monday + dt.timedelta(days=7)
    return monday - dt.timedelta(days=7), monday


def folder_name(monday: dt.date) -> str:
    """reports/<this>/: 2026-10-19_10-23 (no slashes: Windows folder names cannot hold them)."""
    return f"{monday:%Y-%m-%d}_{monday + dt.timedelta(days=4):%m-%d}"


def tab_name(monday: dt.date) -> str:
    return f"{monday:%m-%d}～{monday + dt.timedelta(days=4):%m-%d}"


def tab_order(monday: dt.date) -> str:
    """Tab order key: later weeks sort first, all before the glossary."""
    return f"b{99999999 - int(f'{monday:%Y%m%d}'):08d}"


def weekly_change(close: pd.Series, monday: dt.date, friday: dt.date) -> dict:
    """From the last close before Monday to the last close of the week (a holiday Friday ends it on Thursday)."""
    close = close.dropna()
    before = close[close.index < pd.Timestamp(monday)]
    during = close[(close.index >= pd.Timestamp(monday)) & (close.index <= pd.Timestamp(friday))]
    if before.empty or during.empty:
        raise DataUnavailable(f"{monday}～{friday} 前后没有收盘价")
    start, end = float(before.iloc[-1]), float(during.iloc[-1])
    return {"start": start, "start_date": str(before.index[-1].date()), "end": end,
            "end_date": str(during.index[-1].date()), "change": end / start - 1}


def _symbol(code: str) -> str:
    return code.split(".", 1)[-1]


def _range_words(rng: dict, unit: str) -> str:
    share = "68%" if rng["kind"] == "期权" else "80%"
    return f"未来 2 周 {share} 区间 {rng['lo']:.0f}–{rng['hi']:.0f} {unit}（{rng['kind']}）"


def save_week(friday: dt.date, funds: dict, folder: Path = WEEKLY_DIR) -> Path:
    """Keep this week's account total for next week's comparison. Figures read earlier than the ones already kept
    for the same Friday (OpenD closed on a second run) never replace them."""
    path = folder / f"{friday}.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8"))["as_of"] > str(funds["as_of"]):
        return path
    folder.mkdir(parents=True, exist_ok=True)
    keep = {key: funds[key] for key in ("total_assets", "cash", "as_of", "read_at", "source") if key in funds}
    path.write_text(json.dumps({"friday": str(friday), **keep}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def previous_week(friday: dt.date, folder: Path = WEEKLY_DIR) -> dict | None:
    """The latest week kept before `friday`."""
    earlier = sorted(p for p in folder.glob("*.json") if p.stem < str(friday)) if folder.exists() else []
    return json.loads(earlier[-1].read_text(encoding="utf-8")) if earlier else None


def _bank_transfers(flows: pd.DataFrame, rates: dict) -> dict:
    """Deposits and withdrawals in AUD (foreign currency at the review's rates) and each one with its date."""
    bank = flows[flows["cashflow_type"].astype(str).map(lambda kind: any(word in kind for word in BANK_WORDS))]
    items = []
    for f in bank.itertuples(index=False):
        amount = float(f.cashflow_amount)
        if f.currency != "AUD":
            if f.currency not in rates:
                raise DataUnavailable(f"没有 AUD{f.currency} 汇率，换算不了 {f.clearing_date} 的{f.cashflow_type}")
            amount /= rates[f.currency]
        items.append({"date": str(f.clearing_date), "type": str(f.cashflow_type), "aud": amount})
    return {"deposits": sum(i["aud"] for i in items if i["aud"] > 0),
            "withdrawals": -sum(i["aud"] for i in items if i["aud"] < 0), "items": items}


def account_week(funds: dict, friday: dt.date, folder: Path = WEEKLY_DIR) -> dict:
    """What the whole account made or lost since the last kept week, in AUD: the change in total assets with money
    moved in from or out to a bank taken out (Moomoo cash flows, read-only). Run before save_week keeps this week."""
    previous = previous_week(friday, folder)
    if previous is None:
        return {"previous": None}
    week = {"previous": previous, "total": float(funds["total_assets"]), "as_of": str(funds["as_of"]),
            "now": {key: funds[key] for key in ("as_of", "read_at") if key in funds}}
    if week["as_of"] <= previous["as_of"]:
        source = funds.get("source", "")
        return {**week, "error": f"账户数据还停在 {week['as_of']}（{source}），没有新数，算不出这周的赚亏"}
    week["change"] = week["total"] - previous["total_assets"]
    try:
        start = dt.date.fromisoformat(previous["as_of"]) + dt.timedelta(days=1)
        moved = _bank_transfers(account.cash_flows(start, dt.date.fromisoformat(week["as_of"])), funds.get("rates", {}))
    except (DataUnavailable, NotConfigured) as e:
        return {**week, "error": f"现金流水读不到（{e}）"}
    return {**week, **moved, "profit": week["change"] - moved["deposits"] + moved["withdrawals"]}


def _read_while_trading(kept: dict) -> str | None:
    """When a total was read while US stocks traded (then it is not a Friday-close total)."""
    if not kept.get("read_at"):
        return None
    session = quote.us_session(dt.datetime.fromisoformat(kept["read_at"]))
    if session not in SESSION_WORDS:
        return None
    return f"{kept['as_of']} 读取时美股在{SESSION_WORDS[session]}交易"


def _week_words(week: dict) -> str:
    trading = [w for w in (_read_while_trading(week["previous"] or {}), _read_while_trading(week.get("now", {}))) if w]
    note = f"注意：{'、'.join(trading)}，Moomoo 的总资产按当时的价格算，不是周五收盘价。" if trading else ""
    return _week_sentence(week) + note


def _week_sentence(week: dict) -> str:
    previous = week["previous"]
    if previous is None:
        return "这周账户赚亏：还没有更早的账户记录，本周的已存下，从下周起对比。"
    if "change" not in week:
        return f"这周账户赚亏：{week['error']}。"
    totals = (f"总资产 {previous['total_assets']:,.0f} → {week['total']:,.0f} 澳元"
              f"（{previous['as_of']} → {week['as_of']} 读取）")
    if week.get("error"):
        return f"这周账户：{totals}，差 {week['change']:+,.0f} 澳元；{week['error']}，差额里可能含入金或出金。"
    verb = "赚" if week["profit"] >= 0 else "亏"
    if not week["items"]:
        return (f"这周账户{verb} {abs(week['profit']):,.0f} 澳元（{week['profit'] / previous['total_assets']:+.1%}）："
                f"{totals}，这期间没有入金或出金（Moomoo 现金流水）。")
    moved = "、".join(f"{'入金' if i['aud'] > 0 else '出金'} {abs(i['aud']):,.0f} 澳元（{i['date']} {i['type']}）"
                      for i in week["items"])
    return f"这周账户{verb} {abs(week['profit']):,.0f} 澳元：{totals}；入金和出金不算赚亏：{moved}（Moomoo 现金流水）。"


def review_markdown(rows: list[dict], funds: dict, monday: dt.date, friday: dt.date, week: dict | None = None) -> str:
    """持仓回顾和展望, written after Friday's close: the account total and the week's result first, then one line per
    holding."""
    converted = f"；外币市值按 {funds['fx']}换算成澳元" if funds.get("fx") else ""
    lines = [f"## 持仓回顾和展望（{tab_name(monday)}，周五盘后）", "",
             f"总资产 {funds['total_assets']:,.0f} 澳元，现金 {funds['cash']:,.0f} 澳元（{funds.get('source', 'Moomoo 只读')}，"
             f"{funds['as_of']}）{converted}。每只：本周涨跌（上周最后一个收盘 → 本周最后一个收盘）、未来 2 周的正常区间、趋势。", ""]
    if week is not None:
        lines[2:2] = [_week_words(week), ""]
    for r in sorted(rows, key=lambda r: -r["market_value_aud"]):  # the largest holdings first
        head = f"- {_symbol(r['code'])}（{r['qty']:g} 股，市值 {r['market_value_aud']:,.0f} 澳元）"
        if r.get("error"):
            lines.append(f"{head}：无可靠数据（{r['error']}）")
            continue
        unit = CURRENCY_WORDS.get(r["currency"], r["currency"])
        c = r["change"]
        parts = [f"本周 {c['change']:+.1%}（{c['start']:.2f} → {c['end']:.2f} {unit}）"]
        if r.get("cost"):  # only what happened since the buy counts for the holder (CLAUDE.md 经验 2026-10-06)
            parts.append(f"你的成本 {r['cost']:.2f} → {c['end']:.2f}（{c['end'] / r['cost'] - 1:+.1%}）")
        elif r.get("cost") == 0:
            parts.append("送股，没有成本")
        if r.get("range"):
            parts.append(_range_words(r["range"], unit))
        parts.append(analysis.trend_sentence(r["trend"]) if r.get("trend") else "历史不足 200 个交易日，算不出长线趋势")
        lines.append(f"{head}：" + "；".join(parts))
    lines += ["", "「68% 区间」来自期权价格，「80% 区间（历史）」来自过去同长度的窗口；都只是正常波动的范围，不是预测涨跌。"]
    return "\n".join(lines) + "\n"


def _two_week_range(ticker: str, close: pd.Series, rate: float) -> dict | None:
    """Options where the ticker has them (US); otherwise the middle 80% of past 10-day moves."""
    spot, valued_on = float(close.iloc[-1]), close.index[-1].date()
    if not data.is_asx(ticker) and not data.is_hk(ticker):
        option = analysis.option_views(ticker, spot, valued_on, rate, [])["2w"]
        if option.get("lo") is not None:
            return {"lo": option["lo"], "hi": option["hi"], "kind": "期权"}
    past = forecast.history_odds(close, analysis.HORIZONS["2w"][0])["all"]
    if past.get("p10") is None:
        return None
    return {"lo": spot * (1 + past["p10"]), "hi": spot * (1 + past["p90"]), "kind": "历史"}


def _account() -> tuple[pd.DataFrame, dict]:
    """Holdings and funds from Moomoo (read-only); the last saved snapshot when OpenD is closed, named as such."""
    try:
        holdings, funds, source = account.snapshot(deals=POSITIONS_ONLY), account.funds().iloc[0], "Moomoo 只读"
        read_at = dt.datetime.now().astimezone().isoformat(timespec="minutes")
    except NotConfigured:
        holdings = account.load_holdings()
        funds = pd.read_csv(account.FUNDS_CSV, encoding="utf-8-sig").iloc[0]
        source, read_at = "OpenD 没开，用上次保存的快照", None
    kept = {"total_assets": float(funds["total_assets"]), "cash": float(funds["cash"]), "as_of": str(funds["as_of"]),
            "source": source}
    return holdings, {**kept, "read_at": read_at} if read_at else kept


def holding_rows(monday: dt.date, friday: dt.date) -> tuple[list[dict], dict]:
    """Every holding in every market with its week and outlook, and the account's total and cash. Market values come
    in each holding's own currency and are turned into AUD at the latest yfinance rate, which the review names."""
    holdings, funds = _account()
    rate = forecast.risk_free()[0]
    fx = {}
    for currency in sorted(set(holdings["currency"]) - {"AUD"}):
        series = data.get_fx(f"AUD{currency}=X")
        fx[currency] = (float(series.iloc[-1]), str(series.index[-1].date()))
    funds["fx"] = "、".join(f"AUD{c} {r:.4f}（yfinance，{d}）" for c, (r, d) in fx.items())
    funds["rates"] = {c: r for c, (r, _) in fx.items()}
    rows = []
    for h in holdings.itertuples(index=False):
        ticker = data.ticker_from_code(h.code)
        value = float(h.market_value)
        row = {"code": h.code, "name": h.name, "qty": float(h.qty),
               "market_value_aud": value / fx[h.currency][0] if h.currency in fx else value,
               "currency": data.currency_of(ticker),
               "cost": float(h.cost_price) if pd.notna(getattr(h, "cost_price", None)) else None}
        try:
            close = data.get_bars(ticker)["close"].loc[:pd.Timestamp(friday)].dropna()
            row["change"] = weekly_change(close, monday, friday)
            ma = analysis.moving_averages(close).iloc[-1]
            if not ma.isna().any():
                row["trend"] = analysis.trend_state(float(close.iloc[-1]), {n: float(ma[n]) for n in analysis.MA_WINDOWS})
            row["range"] = _two_week_range(ticker, close, rate)
        except Exception as e:  # noqa: BLE001 - the review says which holding has no data and why
            row["error"] = str(e)
        rows.append(row)
    return rows, funds


def jev_usage_line(month: str, spent: float, rate: float | None) -> str:
    """This month's Jev spending against the monthly limit for calls made without asking."""
    aud = f"（约 {spent / rate:.4f} 澳元）" if rate else ""
    return f"工具费用：Jev 本月（{month}）已用 {spent:.4f} 美元{aud}，每月自动调用的上限是 {jev.MONTHLY_AUTO_USD:.0f} 美元。"


def friday_review(monday: dt.date, friday: dt.date) -> str:
    """The whole Friday review for /week: holdings, the account's week against the last kept one, then this week kept;
    it ends with this month's Jev spending."""
    rows, funds = holding_rows(monday, friday)
    week = account_week(funds, friday)
    save_week(friday, funds)
    month = dt.date.today().strftime("%Y-%m")
    usage = jev_usage_line(month, jev.month_spent(month), funds.get("rates", {}).get("USD"))
    return review_markdown(rows, funds, monday, friday, week) + "\n" + usage + "\n"
