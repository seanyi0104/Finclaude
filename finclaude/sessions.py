"""Which market close a saved download of daily data holds, so it is kept until the next close is out instead of for
one Sydney date (a file saved while New York was still trading used to stay in use for the rest of the Sydney day).

A close counts as out SETTLE after the market's closing time on a weekday, by the market's own clock (the time zone
database handles daylight saving on each side). Daily downloads are saved under a name holding that close
(<name>_close2026-10-19): every read until the next close is out reuses the file, on weekends Friday's. When a
provider is late, a file saved after the close that still lacks that day is fetched again RETRY later; GIVE_UP after
the close it is taken as a market holiday and kept. Holidays are not known here.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

# market -> (time zone, closing time)
CLOSES = {"US": ("America/New_York", dt.time(16, 0)), "ASX": ("Australia/Sydney", dt.time(16, 0)),
          "HK": ("Asia/Hong_Kong", dt.time(16, 0))}
SETTLE = dt.timedelta(minutes=30)  # for the providers to publish the close
RETRY = dt.timedelta(minutes=30)
GIVE_UP = dt.timedelta(hours=6)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def market_of(ticker: str) -> str:
    """The close a ticker's daily data follows: ASX for .AX, HK for .HK, US for the rest (US stocks and funds, ^ indices
    such as VIX, FX pairs)."""
    ticker = ticker.upper()
    return "ASX" if ticker.endswith(".AX") else "HK" if ticker.endswith(".HK") else "US"


def out_at(market: str, day: dt.date) -> pd.Timestamp:
    """When `day`'s close counts as out."""
    zone, closing = CLOSES[market]
    return pd.Timestamp(dt.datetime.combine(day, closing)).tz_localize(zone) + SETTLE


def last_close(market: str, at: dt.datetime | None = None) -> dt.date:
    """The latest weekday whose close is out at `at` (now by default)."""
    at = pd.Timestamp(at or now())
    day = at.tz_convert(CLOSES[market][0]).date()
    while day.weekday() >= 5 or out_at(market, day) > at:
        day -= dt.timedelta(days=1)
    return day


def fetch_again(path: Path, market: str, close: dt.date, last_day: dt.date | None, at: dt.datetime | None = None) -> bool:
    """Whether the file saved for `close` must be downloaded (again): it is missing, was written before that close was
    out, or lacks that day (`last_day` is its latest date) while the provider may still publish it."""
    if not path.exists():
        return True
    at = pd.Timestamp(at or now())
    written = pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
    due = out_at(market, close)
    if written < due:
        return True
    if last_day is not None and last_day >= close:
        return False
    return written < due + GIVE_UP and at - written >= RETRY
