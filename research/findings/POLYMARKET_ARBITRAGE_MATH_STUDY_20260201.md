# Research: AGGRESSIVE Strategy vs Polymarket Arbitrage Math

**Date:** February 1, 2026
**Type:** Research & Strategy Analysis
**Status:** Reference Study (for future consideration)
**Source:** Roan on X - "The Math Needed for Trading on Polymarket (Complete Roadmap)"

---

## Executive Summary

This document compares our AGGRESSIVE spike-detection strategy with the sophisticated arbitrage methods described in the Roan article ($40M extracted). The goal is to identify **profitable opportunities across different time horizons** that we could implement.

**Key Finding:** Our strategy and the article's arbitrage operate in **completely different domains**:
- **Ours:** Exploits BTC→Polymarket price lag (2 seconds, intra-market)
- **Theirs:** Exploits cross-condition probability mispricing (days, multi-market)

Both can coexist. The article reveals opportunities we're not capturing.

---

## Part I: Our Current AGGRESSIVE Strategy (Deep Analysis)

### 1.1 Signal Detection Pipeline

```
BTC 60Hz Price Feed
    ↓
┌─────────────────────────────────────────┐
│ Z-SCORE VOLATILITY FILTER               │
│ - EWMA window: 60 ticks (1s)            │
│ - Normalization: 300 ticks (5s)         │
│ - Accept: z ∈ [0.0, 1.5]                │
│ - Reject: low vol (z<0) or high (z>1.5) │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ SPIKE MAGNITUDE DETECTION               │
│ - Lookback: 72 ticks (1200ms)           │
│ - Threshold: OU adaptive (0.015-0.10%)  │
│ - Base: 0.02% × sigmoid(z)              │
│ - Output: direction (UP/DOWN), magnitude│
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ VELOCITY CONFIRMATION FILTER            │
│ - UP spike + velocity > -0.10: ACCEPT   │
│ - DOWN spike + velocity < +0.10: ACCEPT │
│ - Contradictory: REJECT                 │
│ - Improvement: +218% ($2.37→$7.54/hr)   │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ COMPOSITE SCORE                         │
│ score = 0.40×mag + 0.30×vel + 0.20×confirm + 0.10×urgency │
│ - Threshold: 0.40                       │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ OBI (ORDERBOOK IMBALANCE) FILTER        │
│ - UP spike: requires up_imbalance > 0   │
│ - DOWN spike: requires down_imbalance > 0│
│ - Improvement: +18pp win rate           │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ ENTRY FILTERS                           │
│ - Time remaining > 240s (min_time)      │
│ - Entry price < $0.90 (Turkey problem)  │
└─────────────────────────────────────────┘
    ↓
✓ ENTRY SIGNAL ACCEPTED
```

### 1.2 Position Management

| Component | Formula | Current Value |
|-----------|---------|---------------|
| **Winner Entry** | `min(bid + 0.01, ask - 0.01)` | Aggressive fill |
| **Loser Bid** | `0.50 × magnitude + 0.08` | 9-15¢ below ask |
| **Target Pair Cost** | Winner + Loser | < $0.99 |
| **Position Size** | Fixed | 50 shares |
| **Time-Stop** | Exit if unprofitable after N seconds | 180s |

### 1.3 Exit Logic Priority

1. **Stop-Loss** (DISABLED): Winner drops ≥12% → Market exit
2. **Time-Stop** (ACTIVE): 180s elapsed AND not in profit → Market exit
3. **Passive Hedge**: Loser ask drops to our bid → Maker fill
4. **Resolution**: Market settles → Redemption

### 1.4 Fee Model

```
Polymarket Taker Fee = 1.56% × (1 - |2×price - 1|)

Price    Fee Rate
──────   ────────
$0.01    0.003%   (near-zero at extremes)
$0.50    1.56%    (maximum at midpoint)
$0.90    0.315%   (low at extremes)
```

**Our strategy exploits this**: Winner at $0.50-0.60, Loser at $0.40-0.50 → moderate fees
But NOT at $0.50/$0.50 which would be maximum fee territory.

### 1.5 Current Performance

| Metric | Value | Source |
|--------|-------|--------|
| Win Rate | 54.3% (single-cycle) | OOS3+4 backtest |
| Hourly Rate | +$1.37/hr (conservative) | Grid search |
| Expected (50 shares) | +$9.00/hr | TRADING_CONFIGS |
| Trades/Hour | 2.9 independent | Spike clustering analysis |
| Trade Duration | ~90s avg (time-stop caps at 180s) | Backtest |

---

## Part II: Article's Arbitrage Math (Detailed Analysis)

### 2.1 The Problem They Solve

**Single market looks fine:**
- "Will Trump win PA?" YES: $0.48, NO: $0.52 → Sum: $1.00 ✓

**But add a dependent market:**
- "Will GOP win PA by 5+?" YES: $0.32, NO: $0.68 → Sum: $1.00 ✓

**Logical dependency creates arbitrage:**
- If GOP wins by 5+, Trump MUST win PA
- These markets aren't independent
- Prices assuming 4 outcomes when only 3 exist = mispricing

### 2.2 Key Mathematical Concepts

#### Marginal Polytope (M)
- Set of arbitrage-free price vectors
- Defined as convex hull of valid payoff outcomes
- Prices outside M are exploitable

**Why it matters:** Checking if prices are arbitrage-free requires solving whether they lie in M, which has exponentially many vertices (2^n outcomes).

#### Integer Programming Solution
Instead of checking 2^63 outcomes (NCAA tournament), describe valid outcomes with linear constraints:
```
Sum of z(team_i, wins) = 1  for each team
z(teamA, 5+) + z(teamB, 5+) ≤ 1  if they can't both win 5+
```
3 constraints replace 16,384 brute-force checks.

#### Bregman Divergence
For LMSR markets, the optimal arbitrage trade equals the Bregman projection of current prices onto M:
```
D(μ||θ) = R(μ) + C(θ) - θ·μ
```
Where R(μ) is negative entropy (KL divergence for probabilities).

**What this gives you:**
1. **Optimal positions** (which conditions to trade)
2. **Expected profit** (divergence magnitude)
3. **Trade direction** (gradient of divergence)

#### Frank-Wolfe Algorithm
Makes Bregman projection tractable on exponential spaces:
1. Start with small vertex set
2. Each iteration: solve IP to find new descent vertex
3. Add vertex to active set
4. Repeat until convergence gap < ε

**Performance:** 50-150 iterations for thousands of conditions.

### 2.3 Results From Article

| Category | Extracted | Details |
|----------|-----------|---------|
| **Single Condition** | $10.6M | YES+NO < $1.00 (41% of conditions) |
| **Market Rebalancing** | $29.0M | All YES/NO conditions within market |
| **Combinatorial** | $0.1M | Cross-market dependencies |
| **TOTAL** | $39.7M | Apr 2024 - Apr 2025 |

**Top Trader:** $2,009,632 from 4,049 trades = **$496 avg profit/trade**

### 2.4 Execution Insights

**Non-Atomic Problem:**
```
Plan: Buy YES $0.30, Buy NO $0.30 → Profit $0.40
Reality: YES fills $0.30, price moves, NO fills $0.78 → LOSS $0.08
```
Solution: Submit all legs within 30ms to confirm in same block.

**Latency Hierarchy:**
| Trader Type | Latency | Advantage |
|-------------|---------|-----------|
| Retail | 2,650ms | None |
| Sophisticated | 2,040ms | 600ms faster |
| **Gap** | 610ms | Enough for arb to disappear |

**Copy-Trading Fails:**
- By time you see their trade (Block N), they detected at Block N-1
- Price already moved by Block N+1 when you copy
- You provide exit liquidity, not capture arbitrage

---

## Part III: Comparison - Two Different Games

### 3.1 Side-by-Side

| Dimension | Our AGGRESSIVE | Article Arbitrage |
|-----------|----------------|-------------------|
| **Signal Source** | BTC 60Hz price spikes | Cross-condition probability mispricing |
| **Detection** | OU adaptive threshold + velocity | Integer Programming + Frank-Wolfe |
| **Time Horizon** | 2s detection → 180s hold | Days to market resolution |
| **Market Type** | Single binary (BTC 15m) | Multi-condition groups |
| **Edge** | Price lag (~2s) | Probability violations (41%) |
| **Capital/Trade** | $50 | $500+ avg |
| **Trades/Day** | ~70 (2.9/hr × 24h) | ~11 (4,049/year ÷ 365) |
| **Competition** | Low (niche BTC correlation) | High (published math) |
| **Infrastructure** | Python + WebSocket | Gurobi IP solver + parallel exec |

### 3.2 Why Both Can Coexist

**Our edge is orthogonal:**
- We don't compete with arbitrageurs—they're fixing probability violations
- We're exploiting a temporal lag between BTC and Polymarket
- Different markets (BTC 15m vs election/sports multi-condition)
- Different time scales (seconds vs days)

**Article's edge requires:**
- Multi-condition markets (elections, tournaments)
- Capital to hold until resolution
- IP solver infrastructure (Gurobi license ~$10K/year)

---

## Part IV: Opportunity Analysis by Time Horizon

### 4.1 SUB-SECOND (Current Domain)

**What We Do:**
- Detect BTC spike at t=0
- Enter Polymarket at t=0.1s
- Polymarket reflects BTC move at t=2s
- We capture the lag

**Potential Enhancement: Parallel Multi-Market Entry**

When BTC spikes, we could enter MULTIPLE correlated markets simultaneously:
- BTC 15m market (current)
- ETH 15m market (if correlated)
- BTC 30m market (if exists)

**Requirements:**
- Correlation analysis between markets
- Parallel WebSocket connections
- Position tracking across markets

**Risk:** Dilutes edge if correlations aren't strong.

### 4.2 MINUTES (Optimized Current Domain)

**What We Do:**
- 180s time-stop
- Fixed 50 shares
- Binary OBI filter

**Potential Enhancement A: Kelly Criterion Position Sizing**

Article mentions Kelly: `f* = (bp - q) / b`

We could scale position by signal strength:
```python
# Current: fixed 50 shares
shares = 50

# Kelly-adjusted:
edge = composite_score - 0.40  # Our score - threshold
confidence = min(edge / 0.30, 1.0)  # 0-1 scale
shares = int(20 + 30 * confidence)  # Range: 20-50 shares
```

**Requirements:**
- Backtest Kelly vs fixed sizing
- Ensure minimum order size met (5 shares)

**Potential Enhancement B: Magnitude-Scaled Time-Stop**

Larger spikes might deserve longer hold times:
```python
# Current: fixed 180s
time_stop = 180

# Magnitude-scaled:
base_time = 120  # seconds
magnitude_bonus = min(spike_magnitude * 1000, 60)  # up to 60s extra
time_stop = base_time + magnitude_bonus  # Range: 120-180s
```

### 4.3 HOURS (New Opportunity - Intra-Market Arbitrage)

**What Article Describes:**
- Within a single market, conditions might misprice
- Example: All YES options sum to $0.85 (should be $1.00)
- Buy all YES for $0.85, guaranteed $1.00 payout

**Application to BTC Markets:**

Check if UP + DOWN < $1.00:
```python
up_ask = 0.48
down_ask = 0.47
total = up_ask + down_ask  # 0.95

if total < 0.98:  # 2% margin for fees
    # Buy both for guaranteed profit
    buy_up(50)
    buy_down(50)
    # Guaranteed profit: (1.00 - 0.95) × 50 = $2.50 minus fees
```

**Reality Check:**
- In our OOS data, UP+DOWN rarely deviates from 1.00 by more than 1%
- When it does, spread is usually wider → execution risk
- Not a primary strategy, but worth monitoring

### 4.4 DAYS (New Opportunity - Cross-Market Arbitrage)

**What Article Describes:**
- Dependent markets misprice relative to each other
- Example: "BTC > 100K by Feb 15" and "BTC > 100K by Feb 28"
- If Feb 28 YES is cheaper than Feb 15 YES → arbitrage (Feb 15 implies Feb 28)

**Potential Application:**

Monitor BTC milestone markets for logical inconsistencies:
```
Market A: "BTC > 100K by Feb 15" → YES $0.40
Market B: "BTC > 100K by Feb 28" → YES $0.35  ← WRONG! Should be ≥ $0.40
```

**Requirements:**
- Market discovery for related markets
- Dependency graph construction
- Capital to hold until resolution
- Different from our fast-turnaround approach

**Assessment:** High complexity, capital-intensive, outside our current expertise.

---

## Part V: Actionable Recommendations

### 5.1 Keep Current Strategy (High Priority)

Our AGGRESSIVE strategy is **validated and profitable**:
- 54.3% win rate, +$1.37/hr conservative
- Single-cycle mode is optimal (documented in SINGLE_CYCLE_OPTIMAL_20260131.md)
- Don't over-optimize what works

### 5.2 Low-Hanging Fruit Enhancements (Medium Priority)

| Enhancement | Effort | Expected Impact | Risk |
|-------------|--------|-----------------|------|
| **A. Kelly Position Sizing** | Low | +10-20% hourly rate | Low |
| **B. Magnitude-Scaled Time-Stop** | Low | +5-10% win rate | Low |
| **C. Intra-Market Sum Monitoring** | Medium | Alert-based, rare | Low |

### 5.3 New Strategies (Low Priority - Research Phase)

| Strategy | Effort | Expected Impact | Risk |
|----------|--------|-----------------|------|
| **D. Parallel Multi-Market Entry** | High | Unknown | Medium |
| **E. Cross-Market Arbitrage** | Very High | Unknown | High |
| **F. Resolution Timing Plays** | High | Unknown | Medium |

### 5.4 What NOT To Do

1. **Don't chase the $40M arbitrage** - Requires Gurobi, $500K+ capital, different expertise
2. **Don't abandon spike detection** - Our edge is orthogonal and validated
3. **Don't add complexity without backtest** - Every enhancement needs OOS validation

---

## Part VI: Detailed Concepts from Article (Reference)

### 6.1 Marginal Polytope Mathematics

For n conditions, valid payoff vectors form set Z:
```
Z = {φ(ω) : ω ∈ Ω}
```
Where φ(ω) is binary vector showing which condition is TRUE in outcome ω.

Marginal polytope M = conv(Z) (convex hull of valid vectors).

**Arbitrage exists if prices θ lie outside M.**

### 6.2 Bregman Projection

For LMSR cost function C(θ), the Bregman divergence is:
```
D(μ||θ) = R(μ) + C(θ) - θ·μ
```

Where R(μ) = Σ μᵢ ln(μᵢ) (negative entropy).

**Maximum arbitrage profit = D(μ*||θ)** where μ* is projection of θ onto M.

### 6.3 Frank-Wolfe Algorithm

```
1. Initialize: Z₀ = small set of known vertices
2. For iteration t:
   a. Solve: μₜ = argmin over μ ∈ conv(Zₜ₋₁) of F(μ)
   b. Find descent vertex: zₜ = argmin over z ∈ Z of ∇F(μₜ)·z  [IP SOLVE]
   c. Update: Zₜ = Zₜ₋₁ ∪ {zₜ}
   d. Check: g(μₜ) = ∇F(μₜ)·(μₜ - zₜ)
   e. Stop if g(μₜ) ≤ ε
3. Return μₜ
```

**Complexity:** O(n) iterations, each iteration requires IP solve.

### 6.4 Execution Constraints

**VWAP Analysis:**
```
VWAP = Σ(priceᵢ × volumeᵢ) / Σ(volumeᵢ)
```
Use per-block VWAP (2s windows) for realistic fill estimation.

**Minimum Profit Threshold:** $0.05 to cover execution risk.

**Liquidity Cap:**
```
max_profit = price_deviation × min(volume_across_all_positions)
```

---

## Part VII: Future Implementation Checklist

### If Implementing Kelly Sizing (Enhancement A):

- [ ] Add `calculate_kelly_shares(score, base_shares=50)` to trading_utils.py
- [ ] Backtest on OOS3+4 and OOS7 datasets
- [ ] Compare: fixed 50 vs Kelly 20-50 range
- [ ] If positive, update TRADING_CONFIGS.py with `use_kelly_sizing=True`
- [ ] Update run_paper_bot.py to use Kelly sizing

### If Implementing Sum Monitoring (Enhancement C):

- [ ] Add `check_intra_market_arb(up_ask, down_ask)` to trading_utils.py
- [ ] Threshold: alert if UP+DOWN < 0.98
- [ ] Log opportunities without auto-trading (manual review first)
- [ ] Track frequency in production data

---

## Verification Protocol

After implementation of any enhancement:

1. **Backtest** on OOS datasets (minimum 20 hours)
2. **Paper trade** for 24 hours minimum
3. **Compare metrics** to baseline (current AGGRESSIVE)
4. **Only deploy** if improvement is statistically significant (>10% hourly rate)

---

## Files Referenced

| File | Purpose |
|------|---------|
| `src/strategies/enhanced_spike.py` | Main strategy (lines 920-977: spike detection) |
| `src/core/trading_utils.py` | Shared logic (fees, filters, calculations) |
| `research/reference/TRADING_CONFIGS.py` | Parameter source of truth |
| `research/optimizers/aggressive_grid_search.py` | Backtester |
| `research/findings/SINGLE_CYCLE_OPTIMAL_20260131.md` | Why single-cycle works |
| PDF Article | Roan on X - Polymarket arbitrage math |

---

## Article References

- **Research paper:** "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets" (arXiv:2508.03474v1)
- **Theory foundation:** "Arbitrage-Free Combinatorial Market Making via Integer Programming" (arXiv:1606.02825v2)
- **IP solver:** Gurobi Optimizer
- **LLM for dependencies:** DeepSeek-R1-Distill-Qwen-32B
- **Data source:** Alchemy Polygon node API

---

*"Investigate thoroughly. Implement surgically. Validate rigorously."*
