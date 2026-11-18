---
name: sector-pick
description: Sector relative-strength screen, for reference on satellite (short-term) positions only. Computes relative strength against SPY and related indicators for the 11 US sector ETFs, has Jev give each sector a probability, and compares the result with a rule-based baseline. Use when I ask 「最近哪个行业强」 (which sector is strong lately) or 「卫星仓位看哪个行业」 (which sector to look at for satellite positions).
---
# Sector screen
> Method and thresholds adapted from the 「选行业」 (sector selection) prompt in chia Y's "You Buy Because Other People are Buying" (2026-09-29): https://withchia.substack.com/p/you-buy-because-other-people-are

Role: fetch data, compute, visualise. No opinions and no buy or sell wording. All maths in Python, no estimates. Stop and report on any failure.

0. Preflight (free): the Treg balance and finclaude.jev.preflight(). Stop if either fails.
1. Data: XLK XLV XLF XLE XLI XLY XLP XLU XLB XLRE XLC, with SPY as the benchmark: one year of daily prices via Treg (adjusted, price return only). List 端点 | 次数 | 单价 | 合计 (endpoint | calls | unit price | total) first and wait for my confirmation. Save the raw data in data/raw/. Drop and record any ETF with fewer than 250 bars or a last date earlier than SPY's.
2. Indicators (on the last common trading day, two decimals, using finclaude.metrics):
   - rs_1m / rs_3m / rs_6m = period return − SPY's return over the same period, n = 21 / 63 / 126 trading days
   - px_vs_ma200 = close / 200-day moving average − 1; ma200_slope = 20-day change of the 200-day moving average
   - rsi14 (Wilder smoothing); vol20 = standard deviation of 20-day returns × √252
   - dd_from_high = close / highest close in the window − 1 (the column name states the actual window)
3. Jev: one call. Show the cost with finclaude.jev.quote(1), wait for my confirmation, then call finclaude.jev.evaluate(state, questions, max_cost_usd=the confirmed amount, save_as=<report folder>/jev_result.json). A choice question, 「按这些指标，哪个行业相对 SPY 的强势最明显」 (on these indicators, which sector is most clearly strong against SPY), with the 11 sectors as options; state = the as-of date plus one line of indicators per sector. Take the top 3 by probability with finclaude.jev.ranking.
4. Baseline: the top 3 by rs_6m; report with finclaude.jev.compare whether the sets and the order agree.
Output: key conclusions go into this week's Decision page; files go to reports/<week>/_附件/sector-pick/: indicators.csv, jev_result.json, dashboard.html (top-3 banner; rs_6m × rs_3m quadrant scatter coloured by probability; sector × indicator heat map; footer with the as-of date, data sources, the Jev model id and 「非投资建议」 (not investment advice)) and report.html (calls and costs, exclusions, agreement with the baseline; built with finclaude.render.save_report).
Hong Kong: when a comparison is needed, run the Hong Kong sector ETFs I name through yfinance and report them in a separate section, not mixed with the US results.
