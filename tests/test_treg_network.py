"""Live Treg checks (need internet and TREG_TOKEN). Free: balance lookup and Tiingo's $0 daily route.

The daily call counts against Tiingo's trial pool (20 per team per day); repeats before the next US close read
data/raw/treg/.
"""
import pytest

from finclaude import data, treg

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(not treg.is_configured(), reason="TREG_TOKEN 未设置"),
]


def test_token_works_and_balance_is_readable():
    assert treg.balance()["balance_usd"] >= 0


def test_us_prices_come_from_treg_with_long_history():
    df = data.get_prices("AAPL")
    assert (df["source"] == "Treg · Tiingo").all()
    assert df.index[0].year <= 1981
    log = treg.call_log()
    assert log.loc[log["endpoint"] == treg.EOD_ENDPOINT, "cost_usd"].sum() == 0
