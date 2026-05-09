# Apex Trader Funding — LEGACY PA $50K Ruleset

Research date: 2026-05-09. Trader: Lauren. Accounts: PA-APEX-158140-06/07/08/09 (4× $50K Legacy PA).
Tag legend: [OFFICIAL] = apextraderfunding.com help-center / support.apextraderfunding.com.
[3RD-PARTY] = third-party blogs, used as cross-check only. [FLAG] = uncertain / contradiction noted.

Apex 4.0 launched 2026-03-01. Accounts purchased BEFORE that date stay on Legacy rules indefinitely; there is NO conversion path between Legacy and 4.0. Legacy and 4.0 have different drawdowns, payout mechanics, and consistency rules — never mix them.

---

## 1. Drawdown (Legacy $50K)

- Type: **Intraday Trailing Drawdown** based on highest unrealized peak balance during a trade, NOT EOD. [OFFICIAL]
- Amount: **$2,500** trailing on the $50K Full plan.
- Trails from the liquidation threshold (initial − $2,500 = $47,500) and follows the highest unrealized account balance reached during any trade. Threshold updates live, including peaks the trade did not actually close at.
- **Stops trailing** when peak unrealized balance reaches the **Safety Net = $52,600** (= $50,000 + $2,500 + $100). After that, the liquidation level is locked at **$50,100**.
- Tradovate quirk during EVAL: trailing drawdown does NOT stop and continues to trail. RITHMIC stops trailing when threshold reaches profit target ($53,000 on a 50K eval). PA behavior is the locked-at-$50,100 rule above.

## 2. Daily Loss Limit

- Legacy PA has **NO Daily Loss Limit**. Intraday Trailing is the only loss control.
- DLL is **4.0 only** ($1,000 on 50K).

## 3. Consistency Rule — 30% Windfall (Legacy)

- Threshold: **30%** (NOT 50%; 50% is 4.0 only).
- Rule: no single trading day's profit may exceed 30% of total profit since last approved payout.
- When checked: at **payout request time only**. EVAL phase has NO consistency rule.
- Formula: `Highest Profit Day ÷ 0.30 = Minimum Total Profit Required`.
- Reset: after each approved payout — only counts profits since the most recent payout.
- Exception: rule applies until the **6th approved payout**, OR until the account moves to a Live Prop Account. From the 6th payout onward the 30% rule no longer applies.

## 4. Payout Structure (Legacy PA)

- Profit split: **100% of first $25,000** per account, then **90/10** thereafter.
- Minimum payout: **$500** (any account size).
- Minimum trading days before first payout: **8 trading days**; of those, **at least 5 must show ≥ $50 profit** ("qualifying days").
- Minimum days between payouts: **8 trading days** since last request.
- Qualifying day = trading day with ≥ **$50** profit.
- Trading day definition: **6:00 PM ET → 4:59 PM ET** the next calendar day.
- Minimum balance to request payout ($50K): **$52,600** (Safety Net).
- Per-payout cap on **first 5** payouts: **$2,000** ($50K). From the **6th payout onward**: NO maximum cap.
- 100% profit eligibility: kicks in starting with the **6th approved payout** (~48 trading days at 8-day cycle ≈ 2 months).
- Safety net rule for first 3 payouts: account must exceed safety net by the requested amount above $500. After **4th payout**, the safety net requirement drops.

## 5. Position Size Rules (Legacy)

- Max contracts in EVAL ($50K): **10 minis** (or micro equivalent).
- Max contracts in PA ($50K): **10 minis** (same as eval).
- **Half-size rule**: until EOD balance > Safety Net ($52,600), only **half** of the max — i.e. **5 contracts** on $50K. After Safety Net hit, full 10 unlocks next session and stays.
- Penalty for over-half violation: close excess immediately; **profits from the violation removed**; **+8 compliant trading days** before next payout.
- Contract limit applies across all instruments simultaneously, NOT per-instrument.

## 6. Restricted Activities (Legacy — STILL APPLIES, removed in 4.0)

- **5:1 Risk-Reward**: stop loss ≤ 5× profit target.
- **30% MAE**: live unrealized open negative PnL ≤ 30% of start-of-day profit balance ($750 on $50K low-profit). Loosens to **50%** at EOD profit ≥ 2× safety net ($5,200+ profit on $50K).
- **One-Direction**: long OR short, never both, no bracket pairs.
- **Hedging**: no offsetting positions on same/correlated instruments — including across micros vs minis.
- **News Trading**: explicitly **allowed**.
- **Position close cutoff**: **4:59 PM ET**.

## 7. Bot / Algo / Copy-Trading Policy

- **Automation in PA**: **strictly prohibited** — AI, autobots, algorithms, fully automated systems, HFT all banned.
- **3rd-party copy trading**: PA + Live must be traded by listed individual ONLY. No copy-trading services / mirroring.
- **Apex's own Copy Trader** (1 leader → 19 followers among the trader's OWN accounts): allowed under separate Apex Copy Trader rules. [3RD-PARTY — verify before relying]
- **Eval automation** [FLAG]: Compliance language sweeps "all account types" but Eval Rules don't explicitly enumerate. Treat as banned.

## 8. Other

- **Metals halt** (since 2026-03-14): GC, SI, QI, QO, MGC, HG, PL, PA all halted across all platforms in BOTH simulated and Performance accounts. **Still in effect** as of 2026-05-09. Applies to Legacy too.
- **Reset policy**:
  - Eval: resettable. **$80 (Rithmic / WealthCharts)** or **$100 (Tradovate)**. Non-refundable. Free reset on monthly auto-renewal if account is failed.
  - **PA: NOT resettable.** Once breached it's gone.
- **Refund policy**: Reset fees non-refundable. Wrong-plan/wrong-platform purchases get no refund.
- **Account migration Legacy ↔ 4.0**: **NO conversion path**. To use 4.0, buy a new 4.0 evaluation as a separate account.
- **Probation**: hard stop-loss orders mandatory; mental stops not allowed.

## 9. Tradovate fee — $3.45 / round-trip trade

Verified 2026-05-09 against PA-APEX-158140-09 actual Net Liquidity $49,228.09 (computed $49,228.17, gap $0.08 rounding). Different from BluSky ($2.20/trade) and Lucid ($1.00/contract).

---

## Quick reference card — $50K Legacy PA

| Field | Value |
|---|---|
| Drawdown type | Intraday Trailing |
| Drawdown $ | $2,500 |
| Safety Net | $52,600 |
| Stops trailing at | Safety Net, then locked at $50,100 |
| DLL | None (Legacy) |
| Consistency | 30% (windfall) at payout request only |
| Consistency exempt | After 6th payout or Live Prop |
| MAE | 30% of SoD profit; → 50% at EOD profit ≥ 2× safety net |
| 5:1 RR | Required |
| One-direction / no hedge | Required |
| News trading | Allowed |
| Automation / HFT / 3rd-party copy | Banned |
| Min payout | $500 |
| Min trading days | 8 |
| Qualifying days needed | 5 of 8 with ≥ $50 profit |
| Min balance for payout | $52,600 |
| Per-payout cap (first 5) | $2,000 |
| 100% split kicks in | Payout #6 |
| Profit split base | 100% first $25K, then 90/10 |
| Max contracts (eval & PA) | 10 minis (5 until safety net) |
| Day cutoff | 4:59 PM ET |
| Metals | Halted since 2026-03-14 |
| PA reset | Not allowed (eval only) |
| Eval reset cost | $80 Rithmic/WC, $100 Tradovate |
| Migration to 4.0 | None — separate purchase |
| Tradovate fee | $3.45 / round-trip trade |

---

## Sources

[OFFICIAL]
- Legacy PA Trading Rules — apextraderfunding.com/help-center/performance-accounts-pa/legacy-performance-account-pa-trading-rules/
- Legacy PA Payout Parameters — apextraderfunding.com/help-center/legacy-payouts/legacy-pa-payout-parameters/
- Legacy PA Compliance — apextraderfunding.com/help-center/performance-accounts-pa/legacy-performance-account-pa-compliance/
- Legacy Evaluation Rules — apextraderfunding.com/help-center/evaluation-accounts-ea/legacy-evaluation-rules/
- Legacy Reset Options — apextraderfunding.com/help-center/everything-billing-subscriptions-cancellations-resets/legacy-reset-options/
- Trading Halt Metals Contracts — apextraderfunding.com/help-center/helpful-items/trading-halt-metals-contracts/

[3RD-PARTY]
- proptradingvibes.com/blog/apex-trader-funding-rules-overview
- proptradingvibes.com/blog/apex-trader-funding-contract-limits
