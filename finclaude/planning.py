"""Compounding and goal arithmetic for monthly investing."""
from __future__ import annotations

from scipy.optimize import brentq


def principal(monthly: float, years: float) -> float:
    """Money put in: monthly * 12 * years."""
    return monthly * 12 * years


def future_value_monthly(
    monthly: float, annual_return: float, years: float, annual_fee: float = 0.0, initial: float = 0.0
) -> float:
    """End-of-month contributions compounding monthly at (annual_return - annual_fee) / 12.

    FV = initial * (1 + i)^m + P * ((1 + i)^m - 1) / i,  i = (r - fee) / 12,  m = 12 * years.
    """
    i = (annual_return - annual_fee) / 12
    m = round(12 * years)
    growth = (1 + i) ** m
    contributions = monthly * m if i == 0 else monthly * (growth - 1) / i
    return initial * growth + contributions


def fee_drag(monthly: float, annual_return: float, years: float, low_fee: float, high_fee: float) -> float:
    """Ending-value gap between paying `low_fee` and `high_fee` per year."""
    return future_value_monthly(monthly, annual_return, years, low_fee) - future_value_monthly(
        monthly, annual_return, years, high_fee
    )


def required_return(target: float, monthly: float, years: float, initial: float = 0.0) -> float:
    """Annual return (after fees) that makes future_value_monthly reach `target`."""

    def gap(r: float) -> float:
        return future_value_monthly(monthly, r, years, initial=initial) - target

    if gap(0.0) >= 0:
        return 0.0
    return float(brentq(gap, 0.0, 1.0))
