"""Unified market-data entry point.

Routing (CLAUDE.md, 数据优先级):
- Hong Kong (*.HK), ASX (*.AX), indices (^...), FX (...=X): yfinance
- US tickers: Treg (Tiingo) when TREG_TOKEN is set, otherwise yfinance; the `source` column says which
- VIX and VXN closes as published: Cboe's own history files (get_cboe_history), the official source yfinance is checked against
Raw downloads are saved under data/raw/<source>/. Daily history is reused until its market's next close is out
(finclaude.sessions), so a file saved while the market was still trading is not used once it has closed.
Prices are adjusted for splits and dividends, i.e. a total-return basis.
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd
import requests
import yfinance as yf

from . import sessions, treg
from .config import RAW_DIR, DataUnavailable, NotConfigured


def is_hk(ticker: str) -> bool:
    return ticker.upper().endswith(".HK")


def is_asx(ticker: str) -> bool:
    return ticker.upper().endswith(".AX")


def _yfinance_only(ticker: str) -> bool:
    return is_hk(ticker) or is_asx(ticker) or ticker.startswith("^") or ticker.upper().endswith("=X")


def ticker_from_code(code: str) -> str:
    """Moomoo code -> data ticker: US.AAPL -> AAPL, AU.IVV -> IVV.AX, HK.00700 -> 0700.HK.

    ASX codes must keep the .AX suffix: AU.IVV is iShares S&P 500 ETF in AUD, not the NYSE-listed IVV.
    """
    market, _, symbol = code.strip().upper().partition(".")
    if market == "US":
        return symbol
    if market == "AU":
        return f"{symbol}.AX"
    if market == "HK":
        return f"{str(int(symbol)).zfill(4)}.HK"
    raise ValueError(f"不认识的 Moomoo 代码 {code}")


def currency_of(ticker: str) -> str:
    """Trading currency of a data ticker."""
    return "AUD" if is_asx(ticker) else "HKD" if is_hk(ticker) else "USD"


def to_aud(amount: float, currency: str) -> float:
    """Convert an amount to AUD at the latest yfinance rate (AUDUSD=X, AUDHKD=X)."""
    currency = currency.upper()
    if currency == "AUD":
        return float(amount)
    if currency in ("USD", "HKD"):
        return float(amount) / float(get_fx(f"AUD{currency}=X").iloc[-1])
    raise DataUnavailable(f"不支持把 {currency} 换算成澳元")


def _raw_path(source: str, name: str, close: dt.date | None = None):
    """data/raw/<source>/<name>_<key>: the market close a daily download holds (close2026-10-19), or the day a
    one-off download such as an option chain was saved."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    key = f"close{close.isoformat()}" if close else dt.date.today().isoformat()
    return RAW_DIR / source / f"{safe}_{key}.csv"


def _kept(path, market: str, close: dt.date, read, fetch):
    """What `path` holds for `close`, or a new download from fetch() (which saves it) when sessions.fetch_again says
    so. read(path) and fetch() give data indexed by date."""
    saved = read(path) if path.exists() else None
    last_day = saved.index.max().date() if saved is not None and len(saved) else None
    if not sessions.fetch_again(path, market, close, last_day):
        return saved
    try:
        return fetch()
    except Exception:  # noqa: BLE001 - a second try for the same close failed: what was saved for it still holds
        if saved is None:
            raise
        return saved


def _read_frame(path) -> pd.DataFrame:
    return pd.read_csv(path, index_col="date", parse_dates=["date"])


def _yfinance_daily(ticker: str, name: str, columns: tuple[str, ...]) -> pd.DataFrame:
    """yfinance's full daily history of `ticker` (these columns, lower-cased), kept for its market's latest close."""
    market = sessions.market_of(ticker)
    close = sessions.last_close(market)
    path = _raw_path("yfinance", name, close)

    def fetch() -> pd.DataFrame:
        hist = yf.Ticker(ticker).history(period="max", auto_adjust=True)
        if hist.empty:
            raise DataUnavailable(f"yfinance 没有返回 {ticker} 的数据（代码有误或已退市？）")
        index = pd.DatetimeIndex(hist.index.tz_localize(None).normalize(), name="date")
        df = pd.DataFrame({column.lower(): hist[column].to_numpy() for column in columns}, index=index)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path)
        return df

    return _kept(path, market, close, _read_frame, fetch)


def _yfinance_history(ticker: str) -> pd.DataFrame:
    return _yfinance_daily(ticker, ticker, ("Close", "Volume"))


CBOE_HISTORY = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CBOE_VIX = CBOE_HISTORY.format(name="VIX")


def _read_cboe(path) -> pd.Series:
    table = pd.read_csv(path)
    index = pd.DatetimeIndex(pd.to_datetime(table["DATE"], format="%m/%d/%Y"), name="date")
    return pd.Series(table["CLOSE"].to_numpy(dtype=float), index=index, name="close")


def get_cboe_history(name: str) -> pd.Series:
    """Closes of a Cboe volatility index (VIX, VXN) as Cboe, which calculates and publishes it, puts them out: one row
    per finished day (no intraday value), kept under data/raw/cboe/ until the next US close is out."""
    close = sessions.last_close("US")
    path = _raw_path("cboe", f"{name}_History", close)

    def fetch() -> pd.Series:
        r = requests.get(CBOE_HISTORY.format(name=name), timeout=30)
        if r.status_code != 200 or not r.text.startswith("DATE"):
            raise DataUnavailable(f"Cboe 的 {name} 历史文件读不到（HTTP {r.status_code}）")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text, encoding="utf-8")
        return _read_cboe(path)

    return _kept(path, "US", close, _read_cboe, fetch)


def get_vix_official() -> pd.Series:
    """VIX closes from Cboe: News quotes these, and the cards' yfinance ^VIX is checked against them."""
    return get_cboe_history("VIX")


BAD_TICK_JUMP = 0.5  # a one-day move beyond +50% (or its mirror, -33%) that reverses the next day


def drop_bad_ticks(close: pd.Series, jump: float = BAD_TICK_JUMP) -> tuple[pd.Series, list]:
    """Remove single-day spikes that reverse the next day (e.g. yfinance IVV.AX 16.26 -> 241.51 -> 16.15 on 2015-12-28).

    A lasting level change is kept; so is any real move that does not snap back within one day.
    """
    up, down = 1 + jump, 1 / (1 + jump)
    ratio_in = close / close.shift(1)
    ratio_out = close.shift(-1) / close
    spike = ((ratio_in > up) & (ratio_out < down)) | ((ratio_in < down) & (ratio_out > up))
    return close[~spike], list(close.index[spike])


def level_breaks(close: pd.Series, jump: float = BAD_TICK_JUMP) -> list:
    """Dates of one-day moves beyond +50% / -33% that last (e.g. yfinance IVV.AX 99.60 -> 6.73 on 2011-01-04).

    Usually an unadjusted split. Flagged only: the caller decides (e.g. use history after the last break).
    """
    ratio = close / close.shift(1)
    return list(close.index[(ratio > 1 + jump) | (ratio < 1 / (1 + jump))])


def get_prices(ticker: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Daily close and volume for `ticker`, with a `source` column naming the provider.

    yfinance series (not ^ indices or yields) are cleaned of one-day bad ticks, listed in `.attrs["dropped_ticks"]`;
    lasting one-day breaks (likely unadjusted splits) are flagged in `.attrs["level_breaks"]`, not changed.
    """
    ticker = ticker.strip().upper()
    dropped = []
    if not _yfinance_only(ticker) and treg.is_configured():
        df, source = treg.get_eod_history(ticker), treg.EOD_SOURCE
    else:
        df = _yfinance_history(ticker)
        source = "yfinance" if _yfinance_only(ticker) else "yfinance（Treg 未接入）"
        if not ticker.startswith("^"):  # index levels and yields near zero swing by large ratios for real
            _, dropped = drop_bad_ticks(df["close"])
            df = df.drop(index=dropped)
    breaks = level_breaks(df["close"]) if source.startswith("yfinance") and not ticker.startswith("^") else []
    df = df.loc[start:end]
    if df.empty:
        raise DataUnavailable(f"{ticker} 在 {start} 至 {end} 之间没有数据")
    df = df.assign(source=source)
    df.attrs["dropped_ticks"] = [str(d.date()) for d in dropped]
    df.attrs["level_breaks"] = [str(d.date()) for d in breaks if df.index[0] <= d <= df.index[-1]]
    return df


def _yfinance_bars(ticker: str) -> pd.DataFrame:
    return _yfinance_daily(ticker, f"{ticker}_bars", ("Open", "High", "Low", "Close", "Volume"))


def get_bars(ticker: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Daily adjusted open, high, low, close and volume, with `raw_close` (the close as traded; Tiingo only, empty
    from yfinance) and a `source` column. Routed like get_prices; for US tickers it reuses the same Treg file."""
    ticker = ticker.strip().upper()
    if not _yfinance_only(ticker) and treg.is_configured():
        df, source = treg.get_eod_bars(ticker), treg.EOD_SOURCE
    else:
        df = _yfinance_bars(ticker)
        source = "yfinance" if _yfinance_only(ticker) else "yfinance（Treg 未接入）"
        if not ticker.startswith("^"):
            _, dropped = drop_bad_ticks(df["close"])
            df = df.drop(index=dropped)
        if "raw_close" not in df.columns:
            df = df.assign(raw_close=float("nan"))
    df = df.loc[start:end]
    if df.empty:
        raise DataUnavailable(f"{ticker} 在 {start} 至 {end} 之间没有数据")
    return df.assign(source=source)


def get_fx(pair: str = "AUDUSD=X", start: str | None = None, end: str | None = None) -> pd.Series:
    """FX close series, e.g. AUDUSD=X = US dollars per Australian dollar."""
    return get_prices(pair, start, end)["close"].rename(pair)


def option_expiries(ticker: str) -> list[str]:
    """Listed option expiry dates for a US ticker (yfinance), earliest first."""
    ticker = ticker.strip().upper()
    if is_hk(ticker):
        raise NotConfigured("港股期权需要 Moomoo OpenD 行情，接入 Moomoo 时实现")
    return list(yf.Ticker(ticker).options)


def get_option_chain(ticker: str, expiry: str | None = None) -> pd.DataFrame:
    """Current option chain (calls and puts). US via yfinance; no historical option prices."""
    ticker = ticker.strip().upper()
    if is_hk(ticker):
        raise NotConfigured("港股期权需要 Moomoo OpenD 行情，接入 Moomoo 时实现")
    t = yf.Ticker(ticker)
    expiries = t.options
    if not expiries:
        raise DataUnavailable(f"yfinance 没有 {ticker} 的期权链")
    expiry = expiry or expiries[0]
    chain = t.option_chain(expiry)
    out = pd.concat([chain.calls.assign(type="call"), chain.puts.assign(type="put")], ignore_index=True)
    out = out.assign(expiry=expiry, source="yfinance")
    path = _raw_path("yfinance", f"{ticker}_options_{expiry}")
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return out
