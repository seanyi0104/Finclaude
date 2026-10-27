"""Jev client, offline: free preflight, cost gate, probability checks, saved results and the call log."""
import json

import pytest

from finclaude import jev
from finclaude.config import CostNotApproved, DataUnavailable, NotConfigured

KEY = "vck_test-key-not-real"
JEV = {"id": "typesafe-ai/jev", "type": "evaluation", "context_window": 32000,
       "pricing": {"input": "0.000000042", "output": "0"}}
OTHER = {"id": "liquid/d1", "type": "evaluation", "pricing": {"input": "0.00000004", "output": "0"}}
STATE = "as_of 2026-10-02; VOO expense 0.03%; IVV 0.03%; SPY 0.0945%"
QUESTIONS = {"cheapest": {"type": "choice", "instructions": "Which fund has the lowest expense ratio?",
                          "criteria": {"VOO": "Vanguard S&P 500 ETF", "IVV": "iShares Core S&P 500 ETF",
                                       "SPY": "SPDR S&P 500 ETF Trust"}}}


def answered(answers, cost="0.00001155"):
    """A /v1/evaluate body shaped like the official example."""
    return {"model": "typesafe-ai/jev", "answers": answers,
            "usage": {"inputTokens": 275, "outputTokens": 20},
            "providerMetadata": {"gateway": {"cost": cost, "generationId": "gen_test1"}}}


GOOD = answered({"cheapest": {"type": "choice", "choice": "VOO",
                              "probabilities": {"VOO": 0.6, "IVV": 0.39, "SPY": 0.01}}})


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = {}
        self.ok = status < 400

    def json(self):
        return json.loads(self.content)


class FakeGateway:
    """Stands in for ai-gateway.vercel.sh: credits, the model list and one canned evaluate answer."""

    def __init__(self, answer=None, balance="10", models=(JEV, OTHER), credits_status=200):
        self.answer = answer or FakeResponse(200, GOOD)
        self.balance = balance
        self.models = list(models)
        self.credits_status = credits_status
        self.calls = []

    def __call__(self, method, url, **kwargs):
        path = url.removeprefix(jev.GATEWAY)
        self.calls.append({"method": method, "path": path, **kwargs})
        if path == "/credits":
            if self.credits_status != 200:
                return FakeResponse(self.credits_status, {"error": {"message": f"bad key {KEY}"}})
            return FakeResponse(200, {"balance": self.balance, "total_used": "0"})
        if path == "/models":
            return FakeResponse(200, {"object": "list", "data": self.models})
        return self.answer

    def sent(self, path):
        return [c for c in self.calls if c["path"] == path]


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No test here may reach Vercel with the real key from .env."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", KEY)
    monkeypatch.setattr(jev, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(jev, "_send", FakeGateway())


def use(monkeypatch, fake):
    monkeypatch.setattr(jev, "_send", fake)
    return fake


def saved_text(root):
    return "".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())


def test_preflight_checks_balance_and_lists_jev_with_its_live_price(monkeypatch):
    fake = use(monkeypatch, FakeGateway())
    check = jev.preflight()
    assert check["balance_usd"] == 10.0
    assert check["price_per_token_usd"] == 0.000000042
    assert check["max_cost_per_call_usd"] == 0.002688
    (credits,) = fake.sent("/credits")
    assert credits["method"] == "GET" and credits["headers"]["Authorization"] == f"Bearer {KEY}"
    (models,) = fake.sent("/models")
    assert "Authorization" not in models.get("headers", {})  # the model list is public; the key stays home
    assert fake.sent("/evaluate") == []


def test_preflight_stops_when_the_balance_is_zero(monkeypatch):
    use(monkeypatch, FakeGateway(balance="0"))
    with pytest.raises(DataUnavailable, match="充值"):
        jev.preflight()


def test_preflight_stops_when_jev_is_not_listed(monkeypatch):
    use(monkeypatch, FakeGateway(models=[OTHER]))
    with pytest.raises(DataUnavailable, match="typesafe-ai/jev"):
        jev.preflight()


def test_rejected_key_is_reported_without_the_key(monkeypatch):
    use(monkeypatch, FakeGateway(credits_status=401))
    with pytest.raises(NotConfigured) as raised:
        jev.preflight()
    assert "AI_GATEWAY_API_KEY" in str(raised.value)
    assert KEY not in str(raised.value)


def test_no_key_stops_before_any_request(monkeypatch):
    fake = use(monkeypatch, FakeGateway())
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    assert not jev.is_configured()
    with pytest.raises(NotConfigured):
        jev.preflight()
    with pytest.raises(NotConfigured):
        jev.evaluate(STATE, QUESTIONS, max_cost_usd=1.0)
    assert fake.calls == []


def test_quote_prices_each_call_at_the_request_ceiling():
    table = jev.quote(2)
    assert list(table.columns) == ["端点", "模型", "次数", "单价上限（美元）", "合计上限（美元）"]
    assert table["端点"].iloc[0] == "POST /v1/evaluate"
    assert table["单价上限（美元）"].iloc[0] == 0.002688
    assert table["合计上限（美元）"].iloc[-1] == 0.005376


def test_evaluate_is_refused_until_the_cost_is_confirmed(monkeypatch):
    fake = use(monkeypatch, FakeGateway())
    with pytest.raises(CostNotApproved) as refused:
        jev.evaluate(STATE, QUESTIONS)
    assert "0.002688" in str(refused.value) and "次数" in str(refused.value)
    assert fake.sent("/evaluate") == []


def test_evaluate_sends_model_state_and_questions_and_returns_probabilities(monkeypatch):
    fake = use(monkeypatch, FakeGateway())
    result = jev.evaluate(STATE, QUESTIONS, max_cost_usd=0.002688)
    (sent,) = fake.sent("/evaluate")
    assert sent["method"] == "POST"
    assert sent["json"] == {"model": "typesafe-ai/jev", "state": STATE, "questions": QUESTIONS}
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(sent["json"])
    assert result.answers["cheapest"]["probabilities"] == {"VOO": 0.6, "IVV": 0.39, "SPY": 0.01}
    assert result.model == "typesafe-ai/jev"
    assert result.cost_usd == 0.00001155
    assert result.generation_id == "gen_test1"


def test_each_call_is_saved_and_logged_without_the_key(monkeypatch, tmp_path):
    use(monkeypatch, FakeGateway())
    report = tmp_path / "reports" / "2026-10-05-jev-check" / "jev_result.json"
    result = jev.evaluate(STATE, QUESTIONS, max_cost_usd=0.01, save_as=report)
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["request"] == {"model": "typesafe-ai/jev", "state": STATE, "questions": QUESTIONS}
    assert saved["response"] == GOOD
    assert saved["model"] == "typesafe-ai/jev" and saved["cost_usd"] == 0.00001155
    assert json.loads(result.raw_path.read_text(encoding="utf-8"))["response"] == GOOD
    log = jev.call_log()
    assert log[["endpoint", "model", "status", "cost_usd", "generation_id", "input_tokens"]].iloc[0].tolist() == [
        "POST /v1/evaluate", "typesafe-ai/jev", 200, 0.00001155, "gen_test1", 275]
    assert KEY not in saved_text(tmp_path)


@pytest.mark.parametrize("answers", [
    {},                                                                          # question not answered
    {"cheapest": {"type": "choice", "choice": "VOO"}},                           # no probabilities
    {"cheapest": {"type": "choice", "choice": "VOO",
                  "probabilities": {"VOO": 0.9, "IVV": 0.1}}},                   # an option missing
    {"cheapest": {"type": "choice", "choice": "VOO",
                  "probabilities": {"VOO": 1.2, "IVV": 0.0, "SPY": 0.0}}},       # outside 0-1
    {"cheapest": {"type": "choice", "choice": "VOO",
                  "probabilities": {"VOO": "high", "IVV": 0.0, "SPY": 0.0}}},    # not a number
    {"cheapest": {"type": "choice", "choice": "QQQ",
                  "probabilities": {"VOO": 0.5, "IVV": 0.3, "SPY": 0.2}}},       # choice not an option
])
def test_answers_without_valid_probabilities_stop_and_write_no_result(monkeypatch, tmp_path, answers):
    use(monkeypatch, FakeGateway(FakeResponse(200, answered(answers))))
    report = tmp_path / "jev_result.json"
    with pytest.raises(jev.NoProbability):
        jev.evaluate(STATE, QUESTIONS, max_cost_usd=0.01, save_as=report)
    assert not report.exists()
    assert len(jev.call_log()) == 1  # the paid call is still on record


def test_boolean_and_score_answers_are_checked_too(monkeypatch):
    questions = {"cheap": {"type": "boolean", "instructions": "Is the fee under 0.05%?"},
                 "size": {"type": "score", "instructions": "How large is the fund?",
                          "criteria": ["small", "medium", "large"]}}
    good = answered({"cheap": {"type": "boolean", "probability": 0.97},
                     "size": {"type": "score", "score": 1.98, "probabilities": {"0": 0, "1": 0.02, "2": 0.98}}})
    use(monkeypatch, FakeGateway(FakeResponse(200, good)))
    result = jev.evaluate(STATE, questions, max_cost_usd=0.01)
    assert result.answers["cheap"]["probability"] == 0.97
    bad = answered({"cheap": {"type": "boolean"},
                    "size": {"type": "score", "score": 1.98, "probabilities": {"0": 0, "1": 0.02, "2": 0.98}}})
    use(monkeypatch, FakeGateway(FakeResponse(200, bad)))
    with pytest.raises(jev.NoProbability):
        jev.evaluate(STATE, questions, max_cost_usd=0.01)


@pytest.mark.parametrize("questions", [
    {"pick": {"type": "choice", "instructions": "?", "criteria": {"VOO": "only one"}}},
    {"pick": {"type": "score", "instructions": "?", "criteria": ["one rung"]}},
    {"pick": {"type": "guess", "instructions": "?"}},
    {},
])
def test_malformed_questions_are_rejected_before_anything_is_sent(monkeypatch, questions):
    fake = use(monkeypatch, FakeGateway())
    with pytest.raises(ValueError):
        jev.evaluate(STATE, questions, max_cost_usd=0.01)
    assert fake.sent("/evaluate") == []


@pytest.mark.parametrize("status, body, error, words", [
    (402, {"error": {"message": "Quota limit exceeded. Current spend: $2.00, limit: $2.00.",
                     "type": "quota_for_entity_exceeded"}}, DataUnavailable, "预算"),
    (429, {"error": {"message": "rate limited"}}, DataUnavailable, "429"),
    (500, {"error": {"message": "internal"}}, DataUnavailable, "500"),
])
def test_refusals_become_clear_errors(monkeypatch, status, body, error, words):
    use(monkeypatch, FakeGateway(FakeResponse(status, body)))
    with pytest.raises(error) as raised:
        jev.evaluate(STATE, QUESTIONS, max_cost_usd=0.01)
    assert words in str(raised.value) and KEY not in str(raised.value)
    assert jev.call_log()["status"].tolist() == [status]


def test_ranking_orders_options_by_probability():
    answer = GOOD["answers"]["cheapest"]
    assert jev.ranking(answer) == ["VOO", "IVV", "SPY"]
    assert jev.compare(jev.ranking(answer)[:1], ["VOO"]) == {"same_set": True, "same_order": True}


# --- cards call Jev on their own within a monthly limit (user's choice, 2026-10-19) ------------------------

def logged(as_of, cost):
    return {"as_of": as_of, "endpoint": jev.ENDPOINT, "model": jev.MODEL_ID, "status": 200, "cost_usd": cost,
            "generation_id": "g", "input_tokens": 5883, "raw_file": ""}


def test_cards_call_jev_on_their_own_until_the_monthly_limit(monkeypatch, tmp_path):
    from decimal import Decimal
    import datetime as dt
    monkeypatch.setattr(jev, "RAW_DIR", tmp_path)
    monkeypatch.setattr(jev, "model_info", lambda: {})
    monkeypatch.setattr(jev, "_price_per_token", lambda info: Decimal("0.000000042"))  # ceiling 0.002688 a call
    this_month = dt.date.today().strftime("%Y-%m")
    jev._log(logged(f"{this_month}-01T10:00:00+11:00", 0.3))
    jev._log(logged("2000-01-05T10:00:00+11:00", 5.0))  # another month does not count
    assert jev.month_spent() == pytest.approx(0.3)
    assert jev.auto_budget() == pytest.approx(0.002688)
    jev._log(logged(f"{this_month}-01T11:00:00+11:00", 0.6995))
    with pytest.raises(CostNotApproved, match="每月自动调用的上限"):
        jev.auto_budget()  # 0.9995 spent + 0.002688 would pass 1 USD: list the cost and ask
