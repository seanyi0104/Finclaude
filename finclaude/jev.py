"""Jev, TypeSafe AI's probabilistic decision model, called through Vercel AI Gateway over HTTP.

Every request goes through _send():
- the key is read from .env and sent only in the Authorization header; it is never printed or saved
- preflight() is free: GET /v1/credits (key accepted, balance above zero) and GET /v1/models (Jev listed,
  live per-token price; this list is public, so the key is not sent)
- a call's price depends on its input tokens, which only Vercel counts. quote() prices each call at the
  ceiling, a full 64,000-token request, i.e. the most it can cost. evaluate() refuses unless max_cost_usd covers
  that ceiling. Cards get it from auto_budget() while the month's Jev spending stays within MONTHLY_AUTO_USD (the
  user's choice, 2026-10-19); past that, and for other skills, quote() is shown to the user first
- every answer must carry valid probabilities; anything missing stops with NoProbability, never a substitute
- request (no key) and response are saved under data/raw/jev/, and every request is appended to
  data/raw/jev/calls.csv with the cost Vercel reports
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import requests

from .config import RAW_DIR, CostNotApproved, DataUnavailable, NotConfigured

GATEWAY = "https://ai-gateway.vercel.sh/v1"
MODEL_ID = "typesafe-ai/jev"
ENDPOINT = "POST /v1/evaluate"
TIMEOUT = 60

# Jev's limit per request: 64,000 tokens in total, 32,000 for the state plus the longest question
# (model description in GET /v1/models, checked 2026-10-05). Output tokens are free.
MAX_REQUEST_TOKENS = 64_000
MAX_CHOICES = 255

LOG_FIELDS = ["as_of", "endpoint", "model", "status", "cost_usd", "generation_id", "input_tokens", "raw_file"]
# Cards call Jev without asking while a calendar month's Jev calls stay within this (USD; the IPS allows 2 USD a
# month for Treg and Jev together). Chosen by the user on 2026-10-19.
MONTHLY_AUTO_USD = 1.0


class NoProbability(DataUnavailable):
    """Jev answered without usable probabilities. The skill stops; nothing is filled in by hand."""


@dataclass
class Result:
    answers: dict
    model: str
    cost_usd: float
    generation_id: str
    raw_path: Path


def is_configured() -> bool:
    return bool(os.environ.get("AI_GATEWAY_API_KEY"))


def _key() -> str:
    key = os.environ.get("AI_GATEWAY_API_KEY")
    if not key:
        raise NotConfigured("AI_GATEWAY_API_KEY 未设置：在 .env 里填入 Vercel AI Gateway 的 key（Procedures and QA 流程 3 第 2 步）")
    return key


def _send(method: str, url: str, **kwargs) -> requests.Response:
    try:
        return requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise DataUnavailable(f"连不上 Vercel AI Gateway（{type(e).__name__}）") from e


def _describe(r: requests.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    detail = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("message") or detail
    return str(detail)[:200]


def _check(r: requests.Response, what: str, key: str | None) -> None:
    """Turn a refusal into an error that says what happened; the key never appears in it."""
    if r.ok:
        return
    detail = _describe(r)
    if r.status_code in (401, 403):
        message = f"Vercel 拒绝了 key（HTTP {r.status_code}）：检查 .env 里的 AI_GATEWAY_API_KEY 是否完整、是否已吊销"
        error = NotConfigured
    elif r.status_code == 402:
        message = f"{what}：key 的每月预算已用完或余额不足，Vercel 拒绝了这次请求（{detail}）"
        error = DataUnavailable
    elif r.status_code == 429:
        message = f"{what}：请求太频繁（HTTP 429），稍后再试"
        error = DataUnavailable
    else:
        message = f"{what}：HTTP {r.status_code} {detail}"
        error = DataUnavailable
    if key:
        message = message.replace(key, "***")
    raise error(message)


def model_info() -> dict:
    """Jev's entry in the public model list (free): pricing, context window, description."""
    r = _send("GET", f"{GATEWAY}/models")
    _check(r, "GET /v1/models", None)
    for model in r.json().get("data", []):
        if model.get("id") == MODEL_ID:
            return model
    raise DataUnavailable(f"Vercel 的模型列表里没有 {MODEL_ID}（下架或改名？）：先停下核实")


def _price_per_token(info: dict) -> Decimal:
    price = (info.get("pricing") or {}).get("input")
    if price is None:
        raise DataUnavailable(f"模型列表没有给出 {MODEL_ID} 的单价")
    return Decimal(str(price))


def _ceiling(price: Decimal) -> Decimal:
    return price * MAX_REQUEST_TOKENS


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def preflight() -> dict:
    """Free checks before any paid call: key accepted, balance above zero, Jev listed with a price."""
    key = _key()
    r = _send("GET", f"{GATEWAY}/credits", headers={"Authorization": f"Bearer {key}"})
    _check(r, "GET /v1/credits", key)
    credits = r.json()
    balance = Decimal(str(credits["balance"]))
    if balance <= 0:
        raise DataUnavailable("Vercel AI Gateway 余额为 0：先充值（Procedures and QA 流程 3 第 4 步）")
    price = _price_per_token(model_info())
    return {"model": MODEL_ID, "balance_usd": float(balance),
            "total_used_usd": float(Decimal(str(credits.get("total_used", "0")))),
            "price_per_token_usd": float(price), "max_cost_per_call_usd": float(_ceiling(price)), "as_of": _now()}


def quote(calls: int = 1) -> pd.DataFrame:
    """端点 | 模型 | 次数 | 单价上限 | 合计上限 for planned Jev calls, priced live; the last row is the total."""
    ceiling = _ceiling(_price_per_token(model_info()))
    return pd.DataFrame([
        {"端点": ENDPOINT, "模型": MODEL_ID, "次数": calls,
         "单价上限（美元）": float(ceiling), "合计上限（美元）": float(ceiling * calls)},
        {"端点": "合计", "模型": "", "次数": calls, "单价上限（美元）": None, "合计上限（美元）": float(ceiling * calls)},
    ])


def _check_questions(questions: dict) -> None:
    """Catch a malformed question before paying for it."""
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions 至少要有一题")
    for name, q in questions.items():
        kind, criteria = q.get("type"), q.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_CHOICES:
                raise ValueError(f"{name}：choice 题要有 2–{MAX_CHOICES} 个选项（criteria 是 选项 → 说明）")
        elif kind == "score":
            if not isinstance(criteria, list) or len(criteria) < 2:
                raise ValueError(f"{name}：score 题要有至少 2 级（criteria 是从低到高的列表）")
        elif kind != "boolean":
            raise ValueError(f"{name}：题型只能是 choice、score、boolean，收到 {kind!r}")


def _is_probability(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def _check_answers(questions: dict, response: dict) -> dict:
    """Every question answered with probabilities that fit its options; otherwise NoProbability."""
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise NoProbability("Jev 的返回里没有 answers，停下")
    for name, q in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise NoProbability(f"Jev 没有回答 {name}，停下")
        if q["type"] == "boolean":
            if not _is_probability(answer.get("probability")):
                raise NoProbability(f"{name}：Jev 没有给出 0–1 之间的概率，停下")
            continue
        options = list(q["criteria"]) if q["type"] == "choice" else [str(i) for i in range(len(q["criteria"]))]
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(options):
            raise NoProbability(f"{name}：Jev 返回的概率和选项对不上，停下")
        if not all(_is_probability(p) for p in probabilities.values()):
            raise NoProbability(f"{name}：Jev 返回的概率不在 0–1 之间，停下")
        if q["type"] == "choice" and answer.get("choice") not in options:
            raise NoProbability(f"{name}：Jev 选中的 {answer.get('choice')!r} 不是选项之一，停下")
    return answers


def _jev_dir() -> Path:
    return RAW_DIR / "jev"


def _log(row: dict) -> None:
    path = _jev_dir() / "calls.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def call_log() -> pd.DataFrame:
    """Every request sent to /v1/evaluate: for a report's cost section and the monthly /checkup."""
    path = _jev_dir() / "calls.csv"
    if not path.exists():
        return pd.DataFrame(columns=LOG_FIELDS)
    return pd.read_csv(path, encoding="utf-8")


def month_spent(month: str | None = None) -> float:
    """What /v1/evaluate cost in a calendar month (YYYY-MM by the local time in calls.csv; default this month)."""
    month = month or dt.date.today().strftime("%Y-%m")
    log = call_log()
    if log.empty:
        return 0.0
    return float(log.loc[log["as_of"].astype(str).str[:7] == month, "cost_usd"].fillna(0).sum())


def auto_budget(calls: int = 1) -> float:
    """The max_cost_usd a card may pass without asking: the ceiling of `calls` calls, as long as this month's spending
    plus that ceiling stays within MONTHLY_AUTO_USD. Past it CostNotApproved: show quote() and wait, as before."""
    ceiling = float(_ceiling(_price_per_token(model_info()))) * calls
    spent = month_spent()
    if spent + ceiling > MONTHLY_AUTO_USD:
        raise CostNotApproved(f"本月 Jev 已用 {spent:.4f} 美元，再调用 {calls} 次最多 {ceiling:.4f} 美元，会超过每月自动调用的上限 "
                              f"{MONTHLY_AUTO_USD:.0f} 美元：先列费用，等你确认")
    return ceiling


def _write_json(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def evaluate(state, questions: dict, max_cost_usd: float = 0.0, save_as: Path | None = None) -> Result:
    """Per-option probabilities from Jev for each question. Never substituted with generated text or self-rated confidence.

    state: a string, object or list (the indicator table). questions: {name: {"type", "instructions", "criteria"}}.
    max_cost_usd: what the user confirmed from quote(1). save_as: e.g. reports/<topic>/<date>/jev_result.json.
    """
    key = _key()
    _check_questions(questions)
    ceiling = _ceiling(_price_per_token(model_info()))
    if ceiling > Decimal(str(max_cost_usd)):
        table = quote(1).to_string(index=False)
        raise CostNotApproved(f"Jev 是付费调用，先确认费用：\n{table}\n确认后用 max_cost_usd={float(ceiling)} 再调用")

    request = {"model": MODEL_ID, "state": state, "questions": questions}
    r = _send("POST", f"{GATEWAY}/evaluate", headers={"Authorization": f"Bearer {key}"}, json=request)
    as_of = _now()
    body = r.json() if r.ok else {}
    gateway = (body.get("providerMetadata") or {}).get("gateway") or {}
    cost = float(gateway.get("cost") or 0)
    generation_id = gateway.get("generationId", "")
    raw_path = _jev_dir() / f"{dt.datetime.now():%Y%m%d-%H%M%S}_{generation_id or 'error'}.json"
    record = {"as_of": as_of, "model": body.get("model", MODEL_ID), "cost_usd": cost, "generation_id": generation_id,
              "request": request, "response": body}
    if r.ok:
        _write_json(raw_path, record)
    _log({"as_of": as_of, "endpoint": ENDPOINT, "model": record["model"], "status": r.status_code, "cost_usd": cost,
          "generation_id": generation_id, "input_tokens": (body.get("usage") or {}).get("inputTokens", ""),
          "raw_file": raw_path.name if r.ok else ""})
    _check(r, ENDPOINT, key)

    answers = _check_answers(questions, body)
    if save_as is not None:
        _write_json(Path(save_as), record)
    return Result(answers, record["model"], cost, generation_id, raw_path)


def ranking(answer: dict) -> list:
    """A choice answer's options, most probable first (ties keep Jev's order)."""
    probabilities = answer["probabilities"]
    return sorted(probabilities, key=lambda option: -probabilities[option])


def compare(jev_top: list, baseline_top: list) -> dict:
    """Whether Jev's top picks match the rule baseline, as a set and in order."""
    return {
        "same_set": set(jev_top) == set(baseline_top),
        "same_order": list(jev_top) == list(baseline_top),
    }
