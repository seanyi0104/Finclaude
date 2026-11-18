---
name: fomo-check
description: Herd-buying check. Use when I mention 「大家都在买」 (everyone is buying), 「朋友赚了」 (a friend made money) or 「再不上车就晚了」 (it will be too late if I don't get in now), or when I want to buy something outside my IPS.
---
# Herd-buying check
Ask me these one at a time; compute in Python whatever can be computed:
1. Why do I want to buy now? Who did the idea come from?
2. Can I explain in three sentences how it makes money?
3. What is its largest historical drawdown? At the amount I plan to invest, how many AUD would that be?
4. What share of the portfolio would it be after the purchase? Would it exceed the position limit in my IPS?
5. What does it cost to hold? Is there a cheaper equivalent?
6. How much does it overlap with what I already hold? (call /overlap)
7. Hand it to the devils-advocate subagent for review.
Finally, write the questions and answers into journal/ (using journal/_template.md) and suggest a cooling-off period (its length is set in the IPS) before I decide.
