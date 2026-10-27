---
name: sector-pick
description: 行业相对强弱筛选，只供卫星仓位参考。用 11 只美股行业 ETF 计算相对 SPY 的强弱等指标，由 Jev 给出各行业概率，并与规则基线对照。我问「最近哪个行业强」「卫星仓位看哪个行业」时使用。
---
# 行业筛选
> 方法和阈值改编自 chia Y《You Buy Because Other People are Buying》（2026-09-29）里的「选行业」提示词：https://withchia.substack.com/p/you-buy-because-other-people-are

角色：取数、计算、可视化。不给观点、不用买卖用语。所有数学用 Python，不估算。失败即停并报告。

0. 预检（免费）：Treg 余额；finclaude.jev.preflight()。任一不通过就停。
1. 数据：XLK XLV XLF XLE XLI XLY XLP XLU XLB XLRE XLC，基准 SPY，经 Treg 取一年日线（复权价，只算价格收益）。先列出 端点 | 次数 | 单价 | 合计，等我确认。原始数据存 data/raw/。少于 250 根或最后日期早于 SPY 的剔除并记录。
2. 指标（最后共同交易日，两位小数，用 finclaude.metrics）：
   - rs_1m / rs_3m / rs_6m = 区间收益 − SPY 同期收益，n = 21 / 63 / 126 个交易日
   - px_vs_ma200 = 收盘价 / 200 日均线 − 1；ma200_slope = 200 日均线 20 日变化率
   - rsi14（Wilder 平滑）；vol20 = 20 日收益标准差 × √252
   - dd_from_high = 收盘价 / 区间最高收盘价 − 1（列名写明实际窗口）
3. Jev：一次调用。先用 finclaude.jev.quote(1) 列出费用，等我确认后调用 finclaude.jev.evaluate(state, questions, max_cost_usd=确认的金额, save_as=报告文件夹/jev_result.json)。choice 题「按这些指标，哪个行业相对 SPY 的强势最明显」，选项 = 11 个行业；state = as-of 日期 + 每个行业一行指标。用 finclaude.jev.ranking 取概率前 3。
4. 基线：按 rs_6m 排名前 3；用 finclaude.jev.compare 报告集合与顺序是否一致。
产出：结论要点写进当周周报文档的 Decision；文件存到 reports/<周>/_附件/sector-pick/：indicators.csv、jev_result.json、dashboard.html（前三横幅；rs_6m × rs_3m 四象限散点，颜色 = 概率；行业 × 指标热力图；页脚写 as-of、数据源、Jev 模型 id、「非投资建议」）、report.html（调用次数与费用、剔除项、基线一致性；用 finclaude.render.save_report 生成）。
港股：需要对照时，用 yfinance 跑我指定的港股行业 ETF，结果单独成节，不与美股混排。
