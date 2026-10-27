"""Portfolio backtests: rebalanced allocations, lump sum vs monthly, block bootstrap scenarios."""
from __future__ import annotations

import numpy as np
import pandas as pd

_PERIODS = {"monthly": "M", "quarterly": "Q", "annual": "Y", None: None}


def backtest_portfolio(
    prices: pd.DataFrame, weights: dict, rebalance: str | None = "annual", band: float | None = None, initial: float = 1.0
) -> pd.Series:
    """Daily value of a portfolio holding `weights` (summing to 1) of the columns of `prices`.

    rebalance: 'monthly' | 'quarterly' | 'annual' | None, back to target on the first trading day of each period.
    band: if set (e.g. 0.05), also rebalance whenever any weight drifts more than `band` from target.
    No trading costs or taxes are modelled.
    """
    target = pd.Series(weights, dtype=float)
    if not np.isclose(target.sum(), 1.0):
        raise ValueError("权重之和必须为 1")
    px = prices[list(target.index)].dropna()
    rule = _PERIODS[rebalance]
    periods = px.index.to_period(rule) if rule else None

    units = initial * target / px.iloc[0]
    values = []
    for i in range(len(px)):
        row = px.iloc[i]
        holding = units * row
        total = holding.sum()
        if i > 0:
            due = rule is not None and periods[i] != periods[i - 1]
            if band is not None:
                due = due or bool(((holding / total) - target).abs().gt(band).any())
            if due:
                units = total * target / row
        values.append(total)
    return pd.Series(values, index=px.index, name="portfolio")


def _first_trading_days(prices: pd.Series) -> pd.Series:
    return prices.groupby(prices.index.to_period("M")).head(1)


def lump_sum_vs_monthly(prices: pd.Series, total: float, months: int = 12) -> dict:
    """Invest `total` on the first day vs in `months` equal parts on each month's first trading day.

    Both are valued at the last date of `prices`.
    """
    px = prices.dropna()
    buys = _first_trading_days(px).iloc[:months]
    if len(buys) < months:
        raise ValueError(f"至少需要 {months} 个月的数据")
    last = px.iloc[-1]
    lump = total / px.iloc[0] * last
    monthly = sum(total / months / price for price in buys) * last
    return {"lump_sum": float(lump), "monthly": float(monthly), "lump_sum_wins": bool(lump > monthly)}


def lump_sum_win_rate(prices: pd.Series, months: int = 12, horizon_years: int = 10) -> float:
    """Share of monthly start dates where lump sum beat a `months` phase-in, each held `horizon_years`."""
    px = prices.dropna()
    starts = _first_trading_days(px).index
    horizon = 12 * horizon_years
    wins = [
        lump_sum_vs_monthly(px.loc[starts[k] : starts[k + horizon]], 1.0, months)["lump_sum_wins"]
        for k in range(len(starts) - horizon)
    ]
    if not wins:
        raise ValueError(f"历史太短：需要超过 {horizon_years} 年的数据")
    return sum(wins) / len(wins)


def block_bootstrap(
    returns: pd.Series, horizon: int, n_paths: int = 1000, block: int = 21, seed: int = 0
) -> np.ndarray:
    """Resample `horizon` daily returns per path by stitching random contiguous blocks of history.

    Blocks keep short-run patterns such as volatility clustering. Returns shape (n_paths, horizon).
    """
    r = returns.dropna().to_numpy()
    if len(r) < block:
        raise ValueError("历史长度短于区块长度")
    rng = np.random.default_rng(seed)
    n_blocks = -(-horizon // block)
    starts = rng.integers(0, len(r) - block + 1, size=(n_paths, n_blocks))
    paths = r[starts[..., None] + np.arange(block)]
    return paths.reshape(n_paths, -1)[:, :horizon]
