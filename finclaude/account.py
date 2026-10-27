"""Read-only snapshot of the Moomoo AU account (US, HK and ASX holdings) through moomoo OpenD.

Safety: this module only queries. It never sends, changes or cancels orders and never
unlocks trading; tests/test_safety.py scans the whole package to enforce that.
Needs moomoo OpenD running on this computer and logged in by you.
Run `uv run python -m finclaude.account` to print and save profile/holdings.csv, profile/deals.csv and
profile/funds.csv (cash and total assets in AUD).
"""
from __future__ import annotations

import datetime as dt
import re
import socket
import time
from collections import deque
from pathlib import Path

import pandas as pd
from moomoo import RET_OK, Currency, OpenSecTradeContext, SecurityFirm, TrdEnv, TrdMarket

from .config import PROFILE_DIR, NotConfigured

HOLDINGS_CSV = PROFILE_DIR / "holdings.csv"
DEALS_CSV = PROFILE_DIR / "deals.csv"
FUNDS_CSV = PROFILE_DIR / "funds.csv"
# The SDK rejects (TrdMarket.AU, TrdEnv.REAL) as a context filter, but accepts TrdMarket.NONE (all markets);
# each query then names its market. Dropping AU instead hid every ASX holding until 2026-10-06.
CONTEXT_MARKET = TrdMarket.NONE
MARKETS = (TrdMarket.US, TrdMarket.HK, TrdMarket.AU)

# Output column -> candidate column names in the moomoo position table.
COLUMNS = {
    "code": ["code"],
    "name": ["stock_name"],
    "market": ["position_market"],
    "qty": ["qty"],
    # Average cost, as in the Moomoo app. The SDK's cost_price is the diluted cost, which
    # realized gains push down and can turn negative (e.g. free shares that paid out).
    "cost_price": ["average_cost"],
    "market_value": ["market_val"],
    "currency": ["currency"],
}
HOLDINGS_ORDER = ["code", "name", "market", "qty", "cost_price", "cost_total", "first_buy", "last_buy",
                  "qty_no_deal", "market_value", "currency", "as_of"]

# OpenD refuses longer ranges: "单次查询的时间跨度不能超过 360 天" (2026-10-05); 359 days apart was accepted.
DEAL_WINDOW_DAYS = 359
DEAL_LOOKBACK_DAYS = 3 * 360
# OpenD: 查询历史成交频率太高，每30秒最多10次 (2026-10-06). Stay one under the limit with a little slack.
DEAL_RATE_CALLS, DEAL_RATE_SECONDS = 9, 31.0
DEAL_COLUMNS = {"code": "code", "stock_name": "name", "deal_market": "market", "trd_side": "side",
                "qty": "qty", "price": "price", "create_time": "time"}
BUY_SIDES = {"BUY", "BUY_BACK"}
QTY_EPS = 1e-9
# OpenD: 查询现金流水频率太高，请求失败，每30秒最多20次 (2026-10-19). This account also refuses date ranges
# ("仅支持通过清算日期查询资金流水"), so cash flows are read one clearing date per call.
FLOW_RATE_CALLS, FLOW_RATE_SECONDS = 19, 31.0
FLOW_COLUMNS = ["clearing_date", "currency", "cashflow_type", "cashflow_direction", "cashflow_amount", "cashflow_remark"]
# Account funds in AUD; the per-currency cash columns show where the cash sits.
FUNDS_COLUMNS = ["total_assets", "cash", "market_val", "au_cash", "us_cash", "hk_cash", "avl_withdrawal_cash", "currency"]


def _check(ret, data, what: str):
    if ret == RET_OK:
        return data
    message = re.sub(r"\d{8,}", "****", str(data))  # OpenD names the account number in some refusals
    if "unlock" in message.lower() or "解锁" in message:
        raise PermissionError(f"{what}要求解锁交易。Finclaude 不会解锁，请先停下确认。原始信息：{message}")
    raise NotConfigured(f"{what}失败：{message}（OpenD 是否已启动并登录？）")


def _tidy(positions: pd.DataFrame) -> pd.DataFrame:
    def pick(candidates):
        for name in candidates:
            if name in positions.columns:
                return positions[name].to_numpy()
        return [None] * len(positions)

    return pd.DataFrame({column: pick(candidates) for column, candidates in COLUMNS.items()})


def _opend_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """True if something listens on OpenD's port. The SDK itself retries a closed port every 8 s, forever."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _open(host: str, port: int):
    """One all-markets context and the ids of its real accounts."""
    if not _opend_reachable(host, port):
        raise NotConfigured(f"连不上 moomoo OpenD（{host}:{port}）：请打开 OpenD 并登录后再试")
    ctx = OpenSecTradeContext(filter_trdmarket=CONTEXT_MARKET, host=host, port=port, security_firm=SecurityFirm.FUTUAU)
    try:
        accounts = _check(*ctx.get_acc_list(), "读取账户列表")
    except Exception:
        ctx.close()
        raise
    return ctx, [int(a) for a in accounts.loc[accounts["trd_env"] == TrdEnv.REAL, "acc_id"]]


def _real_accounts(host: str, port: int, markets):
    """Yield (market, context, real account ids) for each market from one all-markets context, closed afterwards."""
    ctx, acc_ids = _open(host, port)
    try:
        for market in markets:
            yield market, ctx, acc_ids
    finally:
        ctx.close()


def funds(host: str = "127.0.0.1", port: int = 11111) -> pd.DataFrame:
    """Total assets and cash of every real account in AUD, one row per account; no account numbers."""
    ctx, acc_ids = _open(host, port)
    try:
        rows = [_check(*ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id, currency=Currency.AUD), "读取资金")
                for acc_id in acc_ids]
    finally:
        ctx.close()
    table = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=FUNDS_COLUMNS)
    return table.reindex(columns=FUNDS_COLUMNS).assign(as_of=dt.date.today().isoformat())


def _windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Back-to-back date ranges covering start..end, each short enough for OpenD."""
    windows = []
    while start <= end:
        stop = min(start + dt.timedelta(days=DEAL_WINDOW_DAYS), end)
        windows.append((start, stop))
        start = stop + dt.timedelta(days=1)
    return windows


def _pace(recent: deque, calls: int, seconds: float) -> None:
    """Wait until fewer than `calls` queries were sent in the last `seconds`, then count this one."""
    if len(recent) >= calls:
        wait = recent[0] + seconds - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        recent.popleft()
    recent.append(time.monotonic())


def deal_history(host: str = "127.0.0.1", port: int = 11111, markets=MARKETS, since: dt.date | None = None) -> pd.DataFrame:
    """Filled deals in every real account since `since` (default: about 3 years), oldest first.

    Times are Moomoo's create_time as returned. No account numbers, deal ids or order ids.
    """
    since = since or dt.date.today() - dt.timedelta(days=DEAL_LOOKBACK_DAYS)
    frames, recent = [], deque()
    for market, ctx, acc_ids in _real_accounts(host, port, markets):
        for acc_id in acc_ids:
            for start, end in _windows(since, dt.date.today()):
                _pace(recent, DEAL_RATE_CALLS, DEAL_RATE_SECONDS)
                deals = _check(
                    *ctx.history_deal_list_query(
                        start=start.isoformat(), end=end.isoformat(), trd_env=TrdEnv.REAL, acc_id=acc_id,
                        deal_market=market,
                    ),
                    "读取历史成交",
                )
                if not deals.empty:
                    frames.append(deals.loc[deals["status"] == "OK", list(DEAL_COLUMNS)].rename(columns=DEAL_COLUMNS))
    if not frames:
        return pd.DataFrame(columns=list(DEAL_COLUMNS.values()))
    return pd.concat(frames, ignore_index=True).sort_values("time", ignore_index=True)


def cash_flows(start: dt.date, end: dt.date, host: str = "127.0.0.1", port: int = 11111) -> pd.DataFrame:
    """Money moved in or out of every real account with clearing dates start..end, as Moomoo names it (bank
    deposits and withdrawals, dividends, interest, tax, currency exchange, trade settlement). Outflows are negative.
    No account numbers or flow ids."""
    ctx, acc_ids = _open(host, port)
    frames, recent = [], deque()
    try:
        for acc_id in acc_ids:
            day = start
            while day <= end:
                _pace(recent, FLOW_RATE_CALLS, FLOW_RATE_SECONDS)
                flows = _check(*ctx.get_acc_cash_flow(clearing_date=day.isoformat(), trd_env=TrdEnv.REAL,
                                                      acc_id=acc_id), "读取现金流水")
                if len(flows):
                    frames.append(flows.reindex(columns=FLOW_COLUMNS))
                day += dt.timedelta(days=1)
    finally:
        ctx.close()
    if not frames:
        return pd.DataFrame(columns=FLOW_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def _holding_period(deals: pd.DataFrame) -> tuple[str | None, str | None, float]:
    """(first buy date, last buy date, quantity explained by deals) of the current holding period.

    A period ends when deals bring the quantity back to zero; the next buy starts a new one.
    """
    held, first, last = 0.0, None, None
    for side, qty, time in deals.sort_values("time")[["side", "qty", "time"]].itertuples(index=False):
        signed = float(qty) if side in BUY_SIDES else -float(qty)
        if signed > 0 and held <= QTY_EPS:
            first = str(time)[:10]
        held += signed
        if signed > 0:
            last = str(time)[:10]
        if held <= QTY_EPS:
            held, first, last = 0.0, None, None
    return first, last, held


def _with_purchases(holdings: pd.DataFrame, deals: pd.DataFrame) -> pd.DataFrame:
    """Add cost_total, first_buy, last_buy and qty_no_deal (shares with no deal, e.g. free shares)."""
    qty = pd.to_numeric(holdings["qty"], errors="coerce")
    cost = pd.to_numeric(holdings["cost_price"], errors="coerce")
    periods = [_holding_period(deals[deals["code"] == code]) for code in holdings["code"]]
    return holdings.assign(
        cost_total=(qty * cost).round(2),
        first_buy=[p[0] for p in periods],
        last_buy=[p[1] for p in periods],
        qty_no_deal=[round(max(float(q) - p[2], 0.0), 4) for q, p in zip(qty, periods)],
    )


def snapshot(
    host: str = "127.0.0.1", port: int = 11111, markets=MARKETS, deals: pd.DataFrame | None = None,
    deals_since: dt.date | None = None,
) -> pd.DataFrame:
    """Positions in every real account, one row per holding, with purchase dates; no account numbers."""
    frames = []
    for market, ctx, acc_ids in _real_accounts(host, port, markets):
        for acc_id in acc_ids:
            positions = _check(
                *ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, position_market=market, currency=Currency.AUD),
                "读取持仓",
            )
            if not positions.empty:
                frames.append(_tidy(positions))
    holdings = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(COLUMNS))
    if deals is None:
        deals = deal_history(host, port, markets, deals_since)
    return _with_purchases(holdings, deals).assign(as_of=dt.date.today().isoformat())[HOLDINGS_ORDER]


def save_snapshot(table: pd.DataFrame, path: Path = HOLDINGS_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def load_holdings(path: Path = HOLDINGS_CSV) -> pd.DataFrame:
    """Last saved snapshot; used when OpenD is not running."""
    if not path.exists():
        raise FileNotFoundError(f"没有持仓快照 {path}：先启动 OpenD 并运行 uv run python -m finclaude.account")
    return pd.read_csv(path, encoding="utf-8-sig")


if __name__ == "__main__":
    deals = deal_history()
    holdings = snapshot(deals=deals)
    cash = funds()
    print(holdings.to_string(index=False))
    print(cash.to_string(index=False))
    print(f"已保存到 {save_snapshot(holdings)}、{save_snapshot(deals, DEALS_CSV)} 和 {save_snapshot(cash, FUNDS_CSV)}")
