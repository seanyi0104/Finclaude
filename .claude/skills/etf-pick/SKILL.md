---
name: etf-pick
description: Picks among ETFs in the same category (e.g. several S&P 500 ETFs, or several Hong Kong broad-market ETFs). Jev gives the probabilities and the result is compared with a lowest-fee baseline. Use when I give candidate tickers and ask 「选哪只」 (which one) or 「比较这几只」 (compare these).
---
# Choosing within a fund category
> Method and thresholds adapted from the 「选 ETF」 (ETF selection) prompt in chia Y's "You Buy Because Other People are Buying" (2026-09-29): https://withchia.substack.com/p/you-buy-because-other-people-are

Role: fetch data, compute, organise. No opinions and no buy or sell wording. All maths in Python, no estimates. Stop and report on any failure.

0. Ask me first for the candidate tickers (at least 2) and the benchmark; after I confirm, run finclaude.jev.preflight().
1. Data: US funds via Treg, Hong Kong funds via yfinance. Three years of daily prices, fund size, expense ratio, inception date, type (active / passive) and bid–ask spread. Fields that cannot be fetched are set to null and listed in the report; without quotes, approximate the spread from the high–low range and mark it spread_proxy. List the Treg cost first and wait for my confirmation.
2. Metrics (two decimals, aligned to the benchmark's trading days): 1-year and 3-year annualised return, excess return over the benchmark, 3-year maximum drawdown, 1-year volatility, tracking error (passive funds only), expense ratio, fund size, years since inception, 20-day average daily traded value, spread.
3. Hard filters (applied in code): drop funds that fail the thresholds in profile/ips.md; if none are set, use chia Y's defaults (size below USD 100 million, younger than 1 year, daily traded value below USD 1 million, spread above 0.30%). Hong Kong amounts are converted to USD at the day's rate before comparing. Null fields are not filtered but are flagged. Stop if fewer than 2 funds survive.
4. Jev: one call. Show the cost with finclaude.jev.quote(1), wait for my confirmation, then call finclaude.jev.evaluate(..., max_cost_usd=the confirmed amount, save_as=<report folder>/jev_result.json). A choice question whose options are the surviving funds, with state = one line of metrics per fund; take the top 1–2 by probability with finclaude.jev.ranking.
5. Baseline: the fund with the lowest expense ratio; report whether Jev agrees with the baseline.
Output: key conclusions go into this week's Decision page; files go to reports/<week>/_附件/etf-pick/: metrics.csv (each field names its data provider), jev_result.json and report.html (costs, missing fields, approximations, excluded funds, agreement with the baseline; built with finclaude.render.save_report).
