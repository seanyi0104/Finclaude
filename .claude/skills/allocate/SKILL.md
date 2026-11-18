---
name: allocate
description: Allocation lab. Backtests the candidate allocations in my IPS and simulates scenarios to compare risk and return. Use when I say 「回测」 (backtest), 「比较配置」 (compare allocations) or 「我的配置合不合理」 (is my allocation reasonable).
---
# Allocation lab
Role: fetch data, compute, visualise. No buy or sell conclusions. All maths in Python. Stop and report on any failure.
Jev is not used: the core allocation is mine to set in the IPS.

0. Ask me first: the candidate allocations (2–3, with assets and weights), how money goes in (lump sum / monthly amount) and the rebalancing rule. Start only after I confirm.
1. Data: pull the longest daily history available for each asset with finclaude.data, ideally covering drawdown years such as 2008, 2020 and 2022; convert every asset to AUD with get_fx("AUDUSD=X"); if histories differ in length, state how they were aligned.
2. Metrics (per allocation, with finclaude.backtest and finclaude.metrics): annualised return, annualised volatility, maximum drawdown, longest time to recover, worst 12-month return, Sharpe ratio.
3. Scenarios: 1,000 simulations with block_bootstrap; report the probability of reaching the IPS target amount and the 10% / 50% / 90% percentile end values.
4. Baselines: 100% broad index, and 60/40 rebalanced yearly.
5. Against the IPS: flag in red any allocation whose historical maximum drawdown exceeds the drawdown I can tolerate.
Output: key conclusions go into this week's Decision page of the weekly doc; files go to reports/<week>/_附件/allocate/: report.html (built with finclaude.render.save_report), metrics.csv and dashboard.html (equity curve, drawdown curve, risk–return scatter; footer with the as-of date, data sources and 「非投资建议」 (not investment advice)).
