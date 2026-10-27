"""Return, risk and relationship metrics.

Inputs are price Series indexed by date unless noted. Annualisation uses 252 trading days.
"""
from __future__ import annotations

import math

import pandas as pd

TRADING_DAYS = 252


def _align(*series: pd.Series) -> tuple[pd.Series, ...]:
    """Keep only the dates every series has."""
    frame = pd.concat(series, axis=1, join="inner").dropna()
    return tuple(frame.iloc[:, i] for i in range(frame.shape[1]))


def simple_returns(prices: pd.Series) -> pd.Series:
    """r_t = P_t / P_(t-1) - 1."""
    return prices.pct_change().dropna()


def annualized_return(prices: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """CAGR = (P_end / P_start) ** (periods_per_year / n) - 1, n = number of periods."""
    p = prices.dropna()
    n = len(p) - 1
    if n <= 0:
        raise ValueError("至少需要两个价格点")
    return float((p.iloc[-1] / p.iloc[0]) ** (periods_per_year / n) - 1)


def annualized_volatility(prices: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """stdev(simple returns, ddof=1) * sqrt(periods_per_year)."""
    return float(simple_returns(prices).std(ddof=1) * math.sqrt(periods_per_year))


def drawdown(prices: pd.Series) -> pd.Series:
    """P_t / max(P_0..P_t) - 1: zero at a new high, negative below it."""
    return prices / prices.cummax() - 1


def max_drawdown(prices: pd.Series) -> float:
    return float(drawdown(prices).min())


def review_line(prices: pd.Series, first_buy, line: float = -0.30) -> dict | None:
    """IPS 论点复查线: the last close against the highest close since the first buy, and the price that would
    reach the line. Closes before the buy never count. None without a buy date (e.g. shares from a stock grant)."""
    if first_buy is None or pd.isna(first_buy):
        return None
    p = prices.dropna()
    since = p[p.index >= pd.Timestamp(first_buy)]
    if since.empty:
        raise ValueError(f"{first_buy} 之后没有价格")
    peak, now = float(since.max()), float(since.iloc[-1])
    worst = worst_drawdown(since)
    return {"first_buy": str(pd.Timestamp(first_buy).date()), "peak": peak, "peak_date": str(since.idxmax().date()),
            "now": now, "now_date": str(since.index[-1].date()), "now_vs_peak": now / peak - 1,
            "worst": worst["depth"], "worst_peak_date": worst["peak_date"], "worst_peak": worst["peak"],
            "worst_trough_date": worst["trough_date"], "worst_trough": worst["trough"],
            "trigger_price": peak * (1 + line), "triggered": now / peak - 1 <= line}


def worst_drawdown(prices: pd.Series) -> dict:
    """The deepest fall from a high: the high (date, price), the low after it, the depth, and the first close back
    at or above that high (None if it has not come back, or if there was no fall)."""
    p = prices.dropna()
    dd = drawdown(p)
    trough = dd.idxmin()
    peak = p[:trough].idxmax()
    back = p[trough:][p[trough:] >= p[peak]]
    fell = float(dd.min()) < 0
    return {"depth": float(dd.min()), "peak_date": str(peak.date()), "peak": float(p[peak]),
            "trough_date": str(trough.date()), "trough": float(p[trough]),
            "recovered_date": str(back.index[0].date()) if fell and not back.empty else None}


def longest_underwater(prices: pd.Series) -> int:
    """Longest run of consecutive periods below an earlier high, i.e. the longest wait to recover."""
    longest = run = 0
    for below in drawdown(prices) < 0:
        run = run + 1 if below else 0
        longest = max(longest, run)
    return longest


def worst_rolling_return(prices: pd.Series, window: int = TRADING_DAYS) -> float:
    """Worst P_t / P_(t-window) - 1, e.g. the worst 12 months with window=252."""
    return float((prices / prices.shift(window) - 1).min())


def sharpe_ratio(prices: pd.Series, risk_free: float = 0.0, periods_per_year: int = TRADING_DAYS) -> float:
    """Annualised mean excess return / annualised volatility; risk_free is an annual rate."""
    excess = simple_returns(prices) - risk_free / periods_per_year
    return float(excess.mean() * periods_per_year / (excess.std(ddof=1) * math.sqrt(periods_per_year)))


def period_return(prices: pd.Series, n: int) -> float:
    """close[t] / close[t-n] - 1 at the last date."""
    return float(prices.iloc[-1] / prices.iloc[-1 - n] - 1)


def relative_strength(prices: pd.Series, benchmark: pd.Series, n: int) -> float:
    """ret(n) - benchmark ret(n) on common dates, as a decimal (x100 for percentage points)."""
    a, b = _align(prices, benchmark)
    return period_return(a, n) - period_return(b, n)


def _rsi(avg_gain: float, avg_loss: float) -> float:
    return 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)


def rsi_wilder(prices: pd.Series, n: int = 14) -> pd.Series:
    """RSI with Wilder smoothing: first average = mean of n changes, then avg = (prev * (n-1) + x) / n."""
    delta = prices.diff().dropna()
    if len(delta) < n:
        raise ValueError(f"RSI{n} 至少需要 {n + 1} 个价格点")
    gains = delta.clip(lower=0).to_numpy()
    losses = (-delta.clip(upper=0)).to_numpy()
    avg_gain, avg_loss = gains[:n].mean(), losses[:n].mean()
    values = [_rsi(avg_gain, avg_loss)]
    for gain, loss in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
        values.append(_rsi(avg_gain, avg_loss))
    return pd.Series(values, index=delta.index[n - 1 :], name=f"rsi{n}")


def correlation(a: pd.Series, b: pd.Series) -> float:
    """Pearson correlation of daily simple returns on common dates."""
    ra, rb = _align(simple_returns(a), simple_returns(b))
    return float(ra.corr(rb))


def stress_correlation(a: pd.Series, b: pd.Series, benchmark: pd.Series, quantile: float = 0.10) -> float:
    """Correlation of a and b on the benchmark's worst `quantile` of days only."""
    ra, rb, rm = _align(simple_returns(a), simple_returns(b), simple_returns(benchmark))
    worst = rm <= rm.quantile(quantile)
    return float(ra[worst].corr(rb[worst]))


def tracking_error(a: pd.Series, b: pd.Series, periods_per_year: int = TRADING_DAYS) -> float:
    """stdev(r_a - r_b, ddof=1) * sqrt(periods_per_year)."""
    ra, rb = _align(simple_returns(a), simple_returns(b))
    return float((ra - rb).std(ddof=1) * math.sqrt(periods_per_year))


def holdings_overlap(weights_a: dict, weights_b: dict) -> float:
    """Sum of min(w_a, w_b) over holdings both funds own; weights as decimals."""
    return float(sum(min(weights_a[k], weights_b[k]) for k in weights_a.keys() & weights_b.keys()))
