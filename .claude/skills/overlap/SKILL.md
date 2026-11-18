---
name: overlap
description: Checks whether funds I hold or plan to buy duplicate each other; Jev judges which pairs are redundant and which fund to keep in each redundant group. Use when I ask 「这几只重不重叠」 (do these overlap) or 「分散够不够」 (am I diversified enough); /checkup also calls it once a quarter.
---
# Fund overlap check
> Method and thresholds adapted from the 「查重叠」 (overlap check) prompt in chia Y's "You Buy Because Other People are Buying" (2026-09-29): https://withchia.substack.com/p/you-buy-because-other-people-are

Role: data engineering. Fetch data, compute, organise; the judgement goes to Jev. No buy or sell wording. Stop and report on any failure.

0. Ask me which funds to compare (at least 2; by default every ETF in profile/holdings.csv) and show the list for my confirmation; then list the Treg cost and wait for my confirmation.
1. Data: each fund's top 50 holdings and weights (the top 10 if that is all that is available), one year of daily prices, expense ratio and size; note the holdings disclosure date.
2. For every pair (two decimals; across markets, align on common trading days; use finclaude.metrics):
   - overlap_pct = sum of the smaller weight of each shared holding
   - shared_top10 = number of top-10 holdings in common
   - corr_1y = correlation of daily returns over one year
   - corr_stress = correlation on the benchmark's worst 10% of days (SPY for US funds, 2800.HK for Hong Kong funds)
   - te_1y = tracking error between the two
3. Jev: one call each for D1 and D2. Show the cost with finclaude.jev.quote(2), wait for my confirmation, then call finclaude.jev.evaluate(..., max_cost_usd=the confirmed unit-price cap, save_as=<report folder>/jev_D1.json or jev_D2.json). D1: for each pair, choose 「重复 / 部分重叠 / 分散」 (redundant / partly overlapping / diversified); pairs with P(redundant) ≥ 0.5 are linked into groups. D2: for each group, choose which fund to keep (state lists each fund's expense ratio, size and tracking error within the group).
4. Baseline: a pair is redundant when correlation ≥ 0.95 or overlap ≥ 50% (thresholds can be changed in the IPS); keep the cheapest fund in each group; report whether Jev agrees.
Output: key conclusions go into this week's Decision page; files go to reports/<week>/_附件/overlap/: pairs.csv, top10_side_by_side.csv (shared holdings marked *), jev_D1.json, jev_D2.json and report.html (built with finclaude.render.save_report).
Closing reminder: correlations rise in a crisis; diversification lowers risk but does not remove it.
