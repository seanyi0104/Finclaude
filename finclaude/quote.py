"""Read-only Moomoo quotes through OpenD: quote rights, market snapshots and option implied volatility, used to check
the day's close and the option data against a second source.

Safety: only the quote context is opened; nothing here can trade (tests/test_safety.py scans the package). The user
info call is asked for quote rights only, and only the rights are returned: nickname, user id and keys are dropped.
Times are as Moomoo gives them: US Eastern for US codes.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
from moomoo import RET_OK, OpenQuoteContext
from moomoo.common.constant import UserInfoField

from . import account
from .config import DataUnavailable, NotConfigured

HOST, PORT = "127.0.0.1", 11111
RIGHTS = ("us_qot_right", "us_option_qot_right", "hk_qot_right")
SNAPSHOT_FIELDS = ["code", "update_time", "last_price", "prev_close_price", "open_price", "high_price", "low_price",
                   "volume", "after_price", "highest52weeks_price", "lowest52weeks_price", "pe_ttm_ratio",
                   "total_market_val", "option_implied_volatility"]


def _check(ret, data, what: str):
    if ret == RET_OK:
        return data
    raise DataUnavailable(f"Moomoo {what}失败：{data}")


def _query(host: str, port: int, ask):
    """Open a quote context, run `ask(ctx)`, always close it."""
    if not account._opend_reachable(host, port):
        raise NotConfigured(f"连不上 moomoo OpenD（{host}:{port}）：打开 OpenD 并登录后再试")
    ctx = OpenQuoteContext(host=host, port=port)
    try:
        return ask(ctx)
    finally:
        ctx.close()


def quote_rights(host: str = HOST, port: int = PORT) -> dict:
    """Quote levels of this login, e.g. {"us_qot_right": "LV3", ...}; nothing personal."""
    info = _query(host, port, lambda ctx: _check(*ctx.get_user_info([UserInfoField.QOTRIGHT]), "读取行情权限"))
    return {k: info.get(k, "N/A") for k in RIGHTS}


def snapshot(codes: list[str], host: str = HOST, port: int = PORT) -> pd.DataFrame:
    """Market snapshot per code (e.g. US.NVDA): last price with its time stamp, previous close, volume, 52-week range."""
    table = _query(host, port, lambda ctx: _check(*ctx.get_market_snapshot(list(codes)), "读取快照"))
    return table[[c for c in SNAPSHOT_FIELDS if c in table.columns]].reset_index(drop=True)


EXTENDED = {"pre": ("盘前", "pre_price", "pre_change_rate"), "after": ("盘后", "after_price", "after_change_rate"),
            "overnight": ("夜盘", "overnight_price", "overnight_change_rate")}


def us_session(now: dt.datetime) -> str:
    """The US session trading at `now` (any time zone), by the New York clock: pre (04:00–09:30), regular, after
    (16:00–20:00), overnight (20:00–04:00, Sunday night to Friday morning) or closed. Holidays are not known here."""
    et = pd.Timestamp(now).tz_convert("America/New_York")
    minutes, weekday = et.hour * 60 + et.minute, et.weekday()
    if weekday < 5 and 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre"
    if weekday < 5 and 9 * 60 + 30 <= minutes < 16 * 60:
        return "regular"
    if weekday < 5 and 16 * 60 <= minutes < 20 * 60:
        return "after"
    if (weekday in (6, 0, 1, 2, 3) and minutes >= 20 * 60) or (weekday < 5 and minutes < 4 * 60):
        return "overnight"
    return "closed"


def _measured_from_last_close(price: float, rate: float, last_close: float) -> bool:
    """Moomoo keeps an earlier day's pre-market, after-hours or overnight price until that session trades again; such
    a price's change is measured against an older close. A price of the session now is measured against the last
    regular close (Moomoo gives the change to 0.001 %, so the close it implies matches to well within 0.05 %)."""
    return price > 0 and abs(price / (1 + rate) / last_close - 1) < 0.0005


def extended_hours(codes: list[str], now: dt.datetime | None = None, host: str = HOST, port: int = PORT) -> pd.DataFrame:
    """The price trading right now for each US code and its change as Moomoo gives it (against the last regular
    close), with the snapshot's time stamp (US Eastern). Only the session trading now is read, and a price left over
    from an earlier day is dropped (price and change empty, the note says why). Outside every session (and on
    holidays, which show no new trades): the last close, marked 休市."""
    session = us_session(now or dt.datetime.now().astimezone())
    table = _query(host, port, lambda ctx: _check(*ctx.get_market_snapshot(list(codes)), "读取快照"))
    rows = []
    for _, r in table.iterrows():
        last, note = float(r["last_price"]), ""
        if session in EXTENDED:
            name, price_field, rate_field = EXTENDED[session]
            price, rate = float(r.get(price_field, float("nan"))), float(r.get(rate_field, float("nan"))) / 100
            if not _measured_from_last_close(price, rate, last):
                price, rate, note = None, None, f"{name}还没有成交（快照里是更早一天的价，已略过）"
            row = {"session": name, "price": price, "change": rate}
        elif session == "regular":
            row = {"session": "盘中", "price": last, "change": last / float(r["prev_close_price"]) - 1}
        else:
            row, note = {"session": "休市", "price": last, "change": None}, "休市，价格是最近一个常规时段收盘价"
        rows.append({"code": r["code"], **row, "update_time": str(r["update_time"]), "note": note})
    return pd.DataFrame(rows)


def option_ivs(code: str, expiry: str, strike: float, host: str = HOST, port: int = PORT) -> dict:
    """Moomoo's own implied volatility (as a decimal) of the call and the put at one strike and expiry."""
    def ask(ctx):
        chain = _check(*ctx.get_option_chain(code, start=expiry, end=expiry), "读取期权链")
        rows = chain[chain["strike_price"] == strike]
        if rows.empty:
            raise DataUnavailable(f"Moomoo 的 {code} {expiry} 期权链里没有行权价 {strike}")
        quotes = _check(*ctx.get_market_snapshot(list(rows["code"])), "读取期权快照").set_index("code")
        return rows, quotes

    rows, quotes = _query(host, port, ask)
    out = {}
    for option_code, kind in zip(rows["code"], rows["option_type"]):
        q = quotes.loc[option_code]
        out[str(kind).lower()] = {"code": option_code, "iv": float(q["option_implied_volatility"]) / 100,
                                  "last_price": float(q["last_price"]), "update_time": str(q["update_time"])}
    if not {"call", "put"} & set(out):
        raise DataUnavailable(f"Moomoo 没有返回 {code} {expiry} 行权价 {strike} 的看涨或看跌期权")
    ivs = [out[k]["iv"] for k in ("call", "put") if k in out]
    return {**out, "iv": sum(ivs) / len(ivs), "expiry": expiry, "strike": float(strike)}
