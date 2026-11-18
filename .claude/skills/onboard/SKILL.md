---
name: onboard
description: Interviews me to build or update my investor profile and investment policy statement (IPS). Use when I say 「建档」 (set up my profile), 「更新画像」 (update my profile) or 「重新评估风险偏好」 (reassess my risk tolerance).
---
# Onboarding interview

First read the holdings snapshot with finclaude.account (read-only) so the questions rest on my real portfolio; if OpenD is not connected, use profile/holdings.csv; if that does not exist either, skip it and say so.
Ask only 1–2 questions at a time, offer options where possible and let me add detail. Cover, in order:
1. Cash flow: monthly income range, monthly spending, savings rate, income stability
2. Safety net: how many months of spending the emergency fund covers; any high-interest debt such as credit cards or personal loans
3. Goals: the amount, year and importance of each goal (e.g. a home deposit in 10 years, retirement in 30)
4. Experience: what I have invested in (US stocks, Hong Kong stocks, ETFs, options, regular investing), my largest loss and what I did then
5. Risk tolerance (scenarios): if the portfolio fell 10% / 20% / 35% within a year, would I buy more, hold, reduce or sell everything
6. Accounts and tax: tax residency, base currency, whether the W-8BEN has been filed, whether I can accept currency swings
7. Constraints: time and attention available, sectors I avoid, kinds of instruments I want to try

Output:
- profile/investor_profile.md: facts and my own words, following the structure of profile/investor_profile.example.md (no account numbers)
- profile/ips.md: filled in from the profile/ips.example.md template. The allocation is mine to decide: offer only 2–3 reference allocations consistent with my answers, each with its historical maximum drawdown and longest recovery computed with finclaude.backtest.
- CLAUDE.local.md: my account, markets, base currency and local time zone, following CLAUDE.local.example.md, plus the weekly doc link if I have one.
- profile/private_terms.txt: ask whether I want a list of private terms (names, e-mail addresses, account figures that identify me), one per line; tests/test_privacy.py uses it to block accidental commits.
- Read everything back to me and write the files only after I confirm. All these files stay on this computer and never go into git.
