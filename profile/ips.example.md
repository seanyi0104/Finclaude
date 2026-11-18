# Investment Policy Statement (IPS) v1 — effective <date>

> /onboard creates profile/ips.md from this template (it stays on this computer and is not tracked by git). "Portfolio" means total account assets (holdings plus the cash in the account); the emergency fund is not included. The allocation and the rules are yours to decide; Claude only checks against this document and reminds you.
>
> Every number in this document is a **reminder line**, not a ban (except the prohibited actions): when a line is exceeded, Claude reminds you only in the monthly check-up (/checkup) and when you say you want to buy or sell, until it is back inside the line or you change the rule. When you change something, write down why and bump the version number.

## Goals
- Main goal: <amount, year, purpose>
- Money in: <how much has been deposited; how much per month from now on, or "to be decided">
- Long-term annual return needed: <backed out with finclaude.planning; recomputed at every check-up from actual deposits>

## Risk budget
- Maximum drawdown you can tolerate: <e.g. 30%, also written as an amount based on today's portfolio>
- Emergency fund: <months of spending, kept outside the portfolio>
- Reference backtest: <candidate allocations computed with finclaude.backtest: maximum drawdown, longest recovery; for reference only>

## Buckets
| Bucket | What goes in | Target / limit |
|---|---|---|
| Long-term | Broad index ETFs, bought gradually and held for the long run | <e.g. 60%, or "to be decided"> |
| Short-term | Individual stocks, leveraged ETFs, options | <limit, or "none"> |
| Cash | Cash in the account | — |

- Rebalancing method (how the long-term bucket gets back to its target): <e.g. the monthly check-up shows the gap and I decide on the spot>

## Holding classification (which holding goes in which bucket)
| Holding | Market | Bucket | Notes |
|---|---|---|---|
| <ticker> | <market, currency> | <long-term / short-term> | <what it tracks; leverage> |
| Any new purchase | — | Short-term by default | Broad index ETFs move to long-term after I confirm |

## Execution rules
- Deposits: <amount per month, or "none for now">
- Rebalancing: <when to look, and how to remind me>
- Fund selection thresholds: size ≥ USD 100 million, at least 1 year old, average daily traded value ≥ USD 1 million, spread ≤ 0.30% (chia Y's defaults; change them as you like)
- Before an order: write a one-page decision note (why buy, what would prove me wrong, the most I am willing to lose), save it in journal/ (template: journal/_template.md) and review it when it is due
- Adding to a position: <e.g. in 2–3 tranches at least 48 hours apart>
- Cooling-off period: <e.g. unplanned purchases wait 48 hours, or "not required">

## Jev usage
- Used for choice questions (/sector-pick, /etf-pick, /overlap) and for the combined probability on forecast cards; the long-term allocation is set by this IPS, never by Jev
- Called automatically for cards within a monthly budget (finclaude.jev.auto_budget, default USD 1); above that, ask me first; choice questions show the cost and wait for my confirmation
- When Jev disagrees with the rule-based baseline: <follow the baseline, or report 「不一致」 (disagree) as it is without choosing for me>
- Monthly cap on Treg + Jev costs: <amount>

## Tax and currency
- Base currency, tax residency, whether the W-8BEN has been filed (write "to be checked" where unsure, with when and whom to ask):

## Prohibited actions
- Investing with borrowed money, margin trading; selling uncovered options
- Buying because "others are buying" (run /fomo-check first)

## Risk reminders (listed in the monthly check-up and when I say I want to buy or sell; they inform, they do not restrict)
1. Each bucket's share, and the gap to its target
2. Stress loss: each holding's "market value × worst historical drawdown", and the total, against the maximum drawdown you can tolerate
3. Leveraged ETFs counted at equivalent exposure (e.g. a 3x ETF = 3 × market value), noting the long-term decay of daily leverage
4. Whether staged buying followed the rules

## Reviews
- When a review is required: <e.g. a large deposit, a big change in income or life, a change of base currency>
- Version history: v1 (<date>): created
