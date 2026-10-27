"""Treg gateway client (https://treg.to) over HTTP.

Every request goes through call():
- the token is read from .env and sent only in the X-Treg-Token header; it is never printed or saved
- the raw answer is saved byte for byte under data/raw/treg/, and every request is appended to
  data/raw/treg/calls.csv with the provider and the charge Treg reports (X-Treg-Cost-Micro)
- the same request reads the saved file instead of calling again: daily bars until the next US close is out
  (finclaude.sessions; asked again half an hour later while Tiingo has not published that day), anything else
  for the rest of the day
- a paid endpoint is refused unless max_cost_usd covers its catalog price. Skills show quote() to the
  user and wait for confirmation first. The ceiling is also sent as X-Treg-Route-Max-Cost, so Treg
  refuses anything dearer; the default ceiling is 0, i.e. free endpoints only.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

from . import sessions
from .config import RAW_DIR, CostNotApproved, DataUnavailable, NotConfigured

BASE_URL = "https://treg.to"
TIMEOUT = 120

# Daily history: Tiingo through Treg. Split- and dividend-adjusted, AAPL back to 1980-12-12. $0 per call
# from a trial pool of 20 calls per team per day (Treg catalog, checked 2026-10-05). Pinned to one
# provider on purpose: the routed treg.stocks.eod.history bills every provider it tries.
EOD_ENDPOINT = "tiingo.daily.prices"
EOD_SOURCE = "Treg · Tiingo"
EOD_START = "1900-01-01"  # without startDate Tiingo returns only the latest day

LOG_FIELDS = ["as_of", "endpoint", "provider", "params", "status", "cost_usd", "call_id", "raw_file"]


@dataclass
class Result:
    data: object
    provider: str
    cost_usd: float
    call_id: str
    raw_path: Path
    cached: bool = False


def is_configured() -> bool:
    return bool(os.environ.get("TREG_TOKEN"))


def _token() -> str:
    token = os.environ.get("TREG_TOKEN")
    if not token:
        raise NotConfigured("TREG_TOKEN 未设置：在 .env 里填入 Treg token（Procedures and QA 流程 2 第 3 步）")
    return token


def _send(method: str, url: str, **kwargs) -> requests.Response:
    try:
        return requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise DataUnavailable(f"连不上 Treg（{type(e).__name__}）") from e


def _get(path: str, token: str | None = None) -> dict:
    headers = {"X-Treg-Token": token} if token else {}
    r = _send("GET", BASE_URL + path, headers=headers)
    _check(r, path, token)
    return r.json()


def _describe(r: requests.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    if not isinstance(body, dict):
        return str(body)[:200]
    detail = body.get("error") or body.get("detail") or body.get("message") or body
    if isinstance(detail, dict):
        detail = detail.get("message") or detail
    return str(detail)[:200]


def _check(r: requests.Response, what: str, token: str | None) -> None:
    """Turn a refusal into an error that says what happened; the token never appears in it."""
    if r.ok:
        return
    detail = _describe(r)
    ref = r.headers.get("X-Treg-Call-Id", "")
    if r.status_code in (401, 403):
        message = f"Treg 拒绝了 token（HTTP {r.status_code}）：检查 .env 里的 TREG_TOKEN 是否完整、是否已撤销"
        error = NotConfigured
    elif r.status_code == 402:
        try:
            body = r.json()
        except ValueError:
            body = {}
        amounts = "；".join(f"{label} {body[key] / 1_000_000} 美元" for key, label in
                           (("estimated_cost_micro", "Treg 预估本次"), ("balance_micro", "余额"))
                           if isinstance(body, dict) and isinstance(body.get(key), int))
        message = f"{what}：余额不足或超过费用上限，Treg 未扣费（{detail}；{amounts}；call id {ref}）"
        error = DataUnavailable
    elif r.status_code == 429:
        reason = "Tiingo 免费额度今天已用完（每队每天 20 次），明天再试" if "trial_allowance" in r.text else "请求太频繁"
        message = f"{what}：{reason}（HTTP 429；call id {ref}）"
        error = DataUnavailable
    else:
        message = f"{what}：HTTP {r.status_code} {detail}（call id {ref}）"
        error = DataUnavailable
    if token:
        message = message.replace(token, "***")
    raise error(message)


@lru_cache(maxsize=None)
def endpoint_info(endpoint_id: str) -> dict:
    """Catalog entry for one endpoint (open, free): provider, method, parameters and price."""
    return _get(f"/catalog/endpoints/{endpoint_id}")["endpoint"]


def _price_micro(endpoint_id: str) -> int:
    usd = endpoint_info(endpoint_id)["cost"].get("usd")
    if usd is None:
        raise DataUnavailable(f"{endpoint_id} 只能用自己在数据商的账户 key，Treg 不代付")
    return round(float(usd) * 1_000_000)


def quote(plan: dict[str, int]) -> pd.DataFrame:
    """端点 | 数据商 | 次数 | 单价 | 合计 for planned calls, priced from the catalog; the last row is the total."""
    rows, total = [], 0
    for endpoint_id, count in plan.items():
        price = _price_micro(endpoint_id)
        total += price * count
        rows.append({"端点": endpoint_id, "数据商": endpoint_info(endpoint_id)["provider"], "次数": count,
                     "单价（美元）": price / 1_000_000, "合计（美元）": price * count / 1_000_000})
    rows.append({"端点": "合计", "数据商": "", "次数": sum(plan.values()), "单价（美元）": None,
                 "合计（美元）": total / 1_000_000})
    return pd.DataFrame(rows)


def balance() -> dict:
    """The team's prepaid balance (free). Also proves the token works."""
    token = _token()
    me = _get("/auth/me", token)
    funds = _get(f"/orgs/{me['org_id']}/balance", token)
    return {"team": me["org"], "balance_usd": funds["balance_micro"] / 1_000_000,
            "as_of": dt.datetime.now().astimezone().isoformat(timespec="seconds")}


def _treg_dir() -> Path:
    return RAW_DIR / "treg"


def _raw_path(endpoint_id: str, params: dict, close: dt.date | None = None) -> Path:
    """The saved answer: for a market close (close2026-10-19) when given, else for the day."""
    key = "_".join(f"{k}={params[k]}" for k in sorted(params))
    safe = re.sub(r"[^A-Za-z0-9._=-]", "_", f"{endpoint_id}_{key}")
    day = f"close{close.isoformat()}" if close else dt.date.today().isoformat()
    return _treg_dir() / f"{safe}_{day}.json"


def _log(row: dict) -> None:
    path = _treg_dir() / "calls.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def call_log() -> pd.DataFrame:
    """Every request sent to Treg (not same-day file reads): for a report's cost section and reconciling with the website."""
    path = _treg_dir() / "calls.csv"
    if not path.exists():
        return pd.DataFrame(columns=LOG_FIELDS)
    return pd.read_csv(path, encoding="utf-8")


def call(endpoint_id: str, params: dict, max_cost_usd: float = 0.0, close: dt.date | None = None,
         refresh: bool = False) -> Result:
    """Call one catalog endpoint, e.g. call("tiingo.daily.prices", {"ticker": "aapl"}). The answer is saved for the
    day, or for a market close when `close` is given; `refresh` asks again although an answer is saved."""
    token = _token()
    path = _raw_path(endpoint_id, params, close)
    if path.exists() and not refresh:
        provider = endpoint_id.split(".")[0]  # catalog ids start with the provider: tiingo.daily.prices
        return Result(json.loads(path.read_bytes()), provider, 0.0, "", path, cached=True)

    info = endpoint_info(endpoint_id)
    price = _price_micro(endpoint_id)
    if price > round(max_cost_usd * 1_000_000):
        table = quote({endpoint_id: 1}).to_string(index=False)
        raise CostNotApproved(f"{endpoint_id} 是付费端点，先确认费用：\n{table}\n"
                              f"确认后用 max_cost_usd={price / 1_000_000} 再调用")

    headers = {"X-Treg-Token": token, "X-Treg-Route-Max-Cost": f"{max_cost_usd:.6f}"}
    r = _send(info["method"], f"{BASE_URL}/call/{endpoint_id}", params=params, headers=headers)
    cost = int(r.headers.get("X-Treg-Cost-Micro") or 0) / 1_000_000
    call_id = r.headers.get("X-Treg-Call-Id", "")
    if r.ok:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    _log({"as_of": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "endpoint": endpoint_id,
          "provider": info["provider"], "params": json.dumps(params, sort_keys=True), "status": r.status_code,
          "cost_usd": cost, "call_id": call_id, "raw_file": path.name if r.ok else ""})
    _check(r, endpoint_id, token)
    return Result(r.json(), info["provider"], cost, call_id, path)


def _last_day(rows) -> dt.date | None:
    return max((dt.date.fromisoformat(r["date"][:10]) for r in rows), default=None) if isinstance(rows, list) else None


def _eod_rows(ticker: str) -> list:
    """Tiingo's full daily history, saved for the latest US close: asked again once the next close is out, or while
    Tiingo has not published that day yet (finclaude.sessions)."""
    params = {"ticker": ticker.strip().lower(), "startDate": EOD_START}
    close = sessions.last_close("US")
    result = call(EOD_ENDPOINT, params, close=close)
    if result.cached and sessions.fetch_again(result.raw_path, "US", close, _last_day(result.data)):
        try:
            result = call(EOD_ENDPOINT, params, close=close, refresh=True)
        except DataUnavailable:  # a second try that fails (e.g. no free calls left today) keeps the saved answer
            pass
    return result.data


def _eod_frame(ticker: str) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    rows = _eod_rows(ticker)
    if not rows:
        raise DataUnavailable(f"Tiingo 没有返回 {ticker} 的日线（代码有误或已退市？）")
    frame = pd.DataFrame(rows)
    index = pd.DatetimeIndex(pd.to_datetime(frame["date"], utc=True).dt.tz_localize(None).dt.normalize(), name="date")
    return frame, index


def get_eod_history(ticker: str) -> pd.DataFrame:
    """Full daily history for a US ticker: adjusted close and volume indexed by date (total-return basis)."""
    frame, index = _eod_frame(ticker)
    return pd.DataFrame({"close": frame["adjClose"].to_numpy(), "volume": frame["adjVolume"].to_numpy()}, index=index)


def get_eod_bars(ticker: str) -> pd.DataFrame:
    """Full daily history with adjusted open, high, low, close and volume, plus `raw_close`, the close as traded
    (to check against a live quote). Same request and same saved file as get_eod_history: no extra call."""
    frame, index = _eod_frame(ticker)
    needed = ["adjOpen", "adjHigh", "adjLow", "adjClose", "adjVolume", "close"]
    if missing := [c for c in needed if c not in frame.columns]:
        raise DataUnavailable(f"Tiingo 的 {ticker} 日线缺少 {', '.join(missing)}，算不了最高价、最低价")
    return pd.DataFrame({"open": frame["adjOpen"].to_numpy(), "high": frame["adjHigh"].to_numpy(),
                         "low": frame["adjLow"].to_numpy(), "close": frame["adjClose"].to_numpy(),
                         "volume": frame["adjVolume"].to_numpy(), "raw_close": frame["close"].to_numpy()}, index=index)
