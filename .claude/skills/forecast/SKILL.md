---
name: forecast
description: 预测卡。对我的每只持仓（或我指定的美股代码）给出未来 2 周（短线）、1 个月和 3 个月（中长线）、半年（长线）的上涨概率和波动区间，来源是历史统计、期权市场、分析师数据和 Jev，到期自动记分。我说「预测」「出预测卡」「这几只接下来怎么看」时使用。
---
# 预测卡
个股分析不带账面和 IPS 提醒（见 CLAUDE.md）：本技能只出预测，仓位和规则留给 /checkup。

角色：预测员。概率只来自数据和模型（finclaude.forecast 的历史统计、期权定价、Jev），不用 Claude 自己估的数，不用自评信心。不给买入 / 卖出或仓位指令；第三方评级原样引用（英文原词），注明来源和日期。所有数学用 Python。拿不到就写「无可靠数据」和原因，不补数。

0. 记分（免费）：先运行 finclaude.forecast.score()，再用 scoreboard() 取各来源的记分表。
1. 标的：默认 finclaude.account 读到的全部持仓（美股、港股、澳股）；OpenD 没开就用 profile/holdings.csv，并注明快照日期。代码用 data.ticker_from_code 转成数据代码（AU.IVV → IVV.AX，不是美股 IVV）。报告第一段写明覆盖了哪些市场；读不到的市场必须写出来，不能悄悄跳过。
2. 数据与预检（免费）：forecast.risk_free() 取 ^IRX；对每只运行 forecast.prepare(ticker, rate)。美股日线经 Treg · Tiingo（0 美元，每天 20 次）；港股、澳股日线和期权、分析师经 yfinance。港股、澳股没有 yfinance 期权，ETF 通常没有分析师覆盖；美股交易时段内运行时美股期权栏也会写「无可靠数据」（价格不同步）。都照实写。再运行 finclaude.jev.preflight()。
3. Jev（付费）：用 forecast.card_rows 先出不含 Jev 的行，再 forecast.jev_request(preps, rows) 组成一次请求（每只 × 4 个期限，各一道 boolean 题）。max_cost_usd 用 finclaude.jev.auto_budget()（每月 1 美元内自动调用，不用问我）；它抛 CostNotApproved 时才先用 finclaude.jev.quote(1) 列出 端点 | 次数 | 单价 | 合计，等我确认；然后 forecast.jev_odds(state, questions, max_cost_usd=那个金额, save_as=报告文件夹/jev_result.json)。Jev 没返回概率就停在这一步，卡上不填 Jev。
4. 出卡：用 Jev 概率重算 forecast.card_rows，再用 forecast.card_markdown 生成每只的卡；forecast.primer_markdown（用一只有期权数据的持仓的 1 个月那一行，讲清三个来源、期权是什么）写进当周 Lesson，不放 Decision；forecast.log_forecasts(rows) 记入 journal/forecasts.csv。
产出：卡片要点写进当周周报文档的 Decision；完整报告存到 reports/<周>/_附件/forecast/：report.html（用 finclaude.render.save_report 生成；第一段：本次几只、几个来源一致；然后小课堂、每只一张卡；再附记分表、数据源与 as-of、费用、局限）、cards.csv（card_rows 的全部列）、jev_result.json。
局限必须写：历史统计偏向活下来的公司；期权概率是风险中性概率；分析师目标价是 12 个月口径；记分要攒到几十次以上才看得出明显的好坏，差别小的来源要几百次独立预测才分得清；同一天出的卡押的是同一段行情，不算独立样本。
结尾：「教育用途，非投资建议；决定权在你。」
