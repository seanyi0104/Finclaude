"""Acceptance tests from the guide (Step 4), using chia Y's worked examples."""
import pandas as pd
import pytest

from finclaude.metrics import holdings_overlap, max_drawdown
from finclaude.planning import fee_drag, future_value_monthly, principal


def test_principal_3000_a_month_for_30_years():
    assert principal(3000, 30) == 1_080_000


def test_future_value_at_8_percent_for_30_years():
    assert future_value_monthly(3000, 0.08, 30) == pytest.approx(4_471_000, rel=0.001)


def test_fee_drag_003_vs_1_percent():
    # She wrote "about $790,000"; the exact figure is about $784,000.
    assert fee_drag(3000, 0.08, 30, low_fee=0.0003, high_fee=0.01) == pytest.approx(790_000, rel=0.02)


def test_max_drawdown():
    assert max_drawdown(pd.Series([100, 120, 90, 130])) == pytest.approx(-0.25)


def test_holdings_overlap():
    a = {"X": 0.60, "Y": 0.40}
    b = {"X": 0.30, "Z": 0.70}
    assert holdings_overlap(a, b) == pytest.approx(0.30)
