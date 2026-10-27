"""Live Jev checks (need internet and AI_GATEWAY_API_KEY). Free only: credits and the public model list.

No test here calls /v1/evaluate; the first paid call is made by hand after the user confirms its cost.
"""
import pytest

from finclaude import jev

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(not jev.is_configured(), reason="AI_GATEWAY_API_KEY 未设置"),
]


def test_preflight_passes_with_a_positive_balance_and_a_listed_price():
    check = jev.preflight()
    assert check["balance_usd"] > 0
    assert check["price_per_token_usd"] > 0
    assert check["max_cost_per_call_usd"] == pytest.approx(check["price_per_token_usd"] * jev.MAX_REQUEST_TOKENS)


def test_quote_lists_one_evaluate_call():
    table = jev.quote(1)
    assert table["端点"].iloc[0] == jev.ENDPOINT
    assert table["合计上限（美元）"].iloc[-1] > 0
