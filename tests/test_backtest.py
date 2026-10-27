import numpy as np
import pandas as pd
import pytest

from finclaude.backtest import backtest_portfolio, block_bootstrap, lump_sum_vs_monthly, lump_sum_win_rate


def frame(**columns):
    n = len(next(iter(columns.values())))
    return pd.DataFrame(columns, index=pd.bdate_range("2020-01-01", periods=n), dtype=float)


def test_buy_and_hold_value_is_weighted_growth():
    px = frame(a=[100, 110, 121], b=[50, 50, 25])
    value = backtest_portfolio(px, {"a": 0.5, "b": 0.5}, rebalance=None)
    assert value.iloc[-1] == pytest.approx(0.5 * 1.21 + 0.5 * 0.5)


def test_band_rebalance_restores_target_weights():
    px = frame(a=[100, 200, 200], b=[100, 100, 100])
    value = backtest_portfolio(px, {"a": 0.5, "b": 0.5}, rebalance=None, band=0.05)
    # Day 1 drifts to 2/3 vs 1/3 and is rebalanced at a value of 1.5; day 2 has no price change.
    assert value.iloc[1] == pytest.approx(1.5)
    assert value.iloc[2] == pytest.approx(1.5)


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        backtest_portfolio(frame(a=[1, 2]), {"a": 0.9})


def rising(days=4000):
    index = pd.bdate_range("2000-01-03", periods=days)
    return pd.Series(100 * 1.0003 ** np.arange(days), index=index)


def test_lump_sum_wins_when_prices_only_rise():
    assert lump_sum_vs_monthly(rising(), 12_000, months=12)["lump_sum_wins"]
    assert lump_sum_win_rate(rising(), months=12, horizon_years=10) == 1.0


def test_block_bootstrap_draws_from_history():
    history = pd.Series(np.arange(100) / 1000)
    paths = block_bootstrap(history, horizon=50, n_paths=20, block=10, seed=1)
    assert paths.shape == (20, 50)
    assert np.isin(paths, history.to_numpy()).all()
