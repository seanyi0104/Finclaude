"""The watchlist (关注池): the companies the user follows, sorted into the areas they care about (2026-10-20: the M7,
semiconductors, energy, the industries AI reaches, plus the long-term index funds). It grows as they chat: when a
stock outside it comes up, they are asked whether to add it and where it belongs. The weekly doc's 关注池 tab shows
the same table, one section per area, so they can see the map of what they follow.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from . import account, data
from .config import PROFILE_DIR

WATCHLIST_CSV = PROFILE_DIR / "watchlist.csv"
FIELDS = ["ticker", "name", "bucket", "industry", "tags", "added", "note"]
# The areas the user named, in their order, each with one plain line; areas added later follow them.
BUCKETS = {
    "M7": "美股市值最大的 7 家科技公司：苹果、微软、谷歌、亚马逊、Meta、英伟达、特斯拉",
    "半导体": "设计、制造芯片和造芯片设备的公司",
    "能源": "石油天然气、电力和核能等；AI 数据中心很耗电",
    "AI 延伸行业": "AI 带动的下游：云和数据中心、AI 应用、广告等",
    "宽基 ETF（长期仓）": "一次买下整个指数的基金，放在长期仓",
}


def load() -> pd.DataFrame:
    if not WATCHLIST_CSV.exists():
        return pd.DataFrame(columns=FIELDS)
    return pd.read_csv(WATCHLIST_CSV, dtype=str, keep_default_na=False)


def is_watched(ticker: str) -> bool:
    return ticker.upper() in set(load()["ticker"].str.upper())


def add(ticker: str, name: str, bucket: str, industry: str, tags: str = "", note: str = "",
        added: str | None = None) -> bool:
    """Add one company; False (and nothing changes) when it is already there."""
    if not name.strip() or not bucket.strip():
        raise ValueError(f"{ticker}：加入关注池要有公司名和分类")
    table = load()
    if ticker.upper() in set(table["ticker"].str.upper()):
        return False
    row = {"ticker": ticker.upper(), "name": name, "bucket": bucket, "industry": industry, "tags": tags,
           "added": added or dt.date.today().isoformat(), "note": note}
    WATCHLIST_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.concat([table, pd.DataFrame([row])], ignore_index=True)[FIELDS].to_csv(WATCHLIST_CSV, index=False,
                                                                              encoding="utf-8")
    return True


def held_tickers() -> set[str]:
    """Data tickers of the last saved holdings (profile/holdings.csv); empty when there is none."""
    try:
        return {data.ticker_from_code(code).upper() for code in account.load_holdings()["code"]}
    except Exception:  # noqa: BLE001 - the table is still shown, without the 持仓 marks
        return set()


def markdown(held: set[str] | None = None) -> str:
    """The 关注池 tab: one section per area with a one-line description and a table; an area the user named but has no
    company yet says so, as a reminder of where they meant to look."""
    table, held = load(), {t.upper() for t in (held_tickers() if held is None else held)}
    order = list(BUCKETS) + [b for b in dict.fromkeys(table["bucket"]) if b not in BUCKETS]
    lines = [f"按你关心的方向归类，共 {len(table)} 家。「持仓」按最近一次读到的账户标出；聊到新公司时我会问要不要加进来。", ""]
    for bucket in order:
        rows = table[table["bucket"] == bucket]
        lines += [f"## {bucket}（{len(rows)} 家）", ""]
        if bucket in BUCKETS:
            lines += [BUCKETS[bucket], ""]
        if rows.empty:
            lines += ["还没有，聊到再加。", ""]
            continue
        lines += ["| 代码 | 公司 | 细分 | 标签 | 加入 |", "| --- | --- | --- | --- | --- |"]
        for r in rows.itertuples(index=False):
            tags = " · ".join(t for t in ("持仓" if r.ticker.upper() in held else "", r.tags) if t)
            lines.append(f"| {r.ticker} | {r.name} | {r.industry} | {tags} | {r.added} |")
        lines.append("")
    return "\n".join(lines)
