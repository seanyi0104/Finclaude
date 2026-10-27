import numpy as np
import pandas as pd
import pytest

from finclaude import metrics
from finclaude.planning import future_value_monthly, required_return


def prices(values):
    return pd.Series(values, index=pd.bdate_range("2024-01-01", periods=len(values)), dtype=float)


def random_walk(seed, n=500):
    rng = np.random.default_rng(seed)
    return prices(100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n)))


def test_annualized_return_of_steady_growth():
    p = prices(100 * 1.001 ** np.arange(253))
    assert metrics.annualized_return(p) == pytest.approx(1.001**252 - 1)


def test_longest_underwater_counts_periods_below_high():
    assert metrics.longest_underwater(prices([100, 90, 95, 99, 101, 100])) == 3


def test_rsi_is_100_when_prices_only_rise():
    assert metrics.rsi_wilder(prices(range(1, 30))).iloc[-1] == 100


def test_rsi_stays_between_0_and_100():
    rsi = metrics.rsi_wilder(random_walk(1))
    assert rsi.between(0, 100).all()


def test_relative_strength_against_itself_is_zero():
    p = random_walk(2)
    assert metrics.relative_strength(p, p, 63) == pytest.approx(0)


def test_identical_series_correlate_perfectly_with_no_tracking_error():
    p = random_walk(3)
    assert metrics.correlation(p, p) == pytest.approx(1)
    assert metrics.stress_correlation(p, p, random_walk(4)) == pytest.approx(1)
    assert metrics.tracking_error(p, p) == pytest.approx(0)


def daily(values, end="2026-10-05"):
    return pd.Series(values, index=pd.bdate_range(end=end, periods=len(values)), dtype=float)


def test_worst_drawdown_names_the_high_the_low_and_the_recovery():
    p = daily([100, 120, 90, 60, 80, 121, 110])
    w = metrics.worst_drawdown(p)
    assert w["depth"] == pytest.approx(60 / 120 - 1)
    assert (w["peak_date"], w["peak"]) == (str(p.index[1].date()), 120)
    assert (w["trough_date"], w["trough"]) == (str(p.index[3].date()), 60)
    assert w["recovered_date"] == str(p.index[5].date())  # first close back at or above 120
    assert metrics.worst_drawdown(daily([100, 80, 90]))["recovered_date"] is None
    flat = metrics.worst_drawdown(daily([100, 101, 102]))
    assert flat["depth"] == 0 and flat["recovered_date"] is None


def test_review_line_looks_only_at_closes_since_the_first_buy():
    p = daily([200, 150, 100, 110, 120, 105, 96])  # the old high of 200 came before the buy and must not count
    r = metrics.review_line(p, str(p.index[2].date()))
    assert r["peak"] == 120 and r["peak_date"] == str(p.index[4].date())
    assert (r["now"], r["now_date"]) == (96, str(p.index[6].date()))
    assert r["now_vs_peak"] == pytest.approx(96 / 120 - 1)
    assert r["worst"] == pytest.approx(96 / 120 - 1)
    assert (r["worst_peak_date"], r["worst_trough"]) == (str(p.index[4].date()), 96)
    assert r["trigger_price"] == pytest.approx(84.0)  # 120 × (1 − 30%)
    assert r["triggered"] is False
    assert metrics.review_line(p, str(p.index[4].date()), line=-0.15)["triggered"] is True


def test_review_line_does_not_apply_without_a_buy_date():
    p = daily([100, 101, 102])
    assert metrics.review_line(p, None) is None
    assert metrics.review_line(p, float("nan")) is None  # read from holdings.csv, a blank first_buy is NaN


def test_required_return_inverts_future_value():
    target = future_value_monthly(3000, 0.07, 25)
    assert required_return(target, 3000, 25) == pytest.approx(0.07, abs=1e-6)
