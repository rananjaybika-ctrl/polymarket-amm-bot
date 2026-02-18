# Whale Strategy Complete Analysis: Gabagool & Baguette

**Date:** February 7, 2026
**Data:** OOS6 (Jan 28-29) + OOS9 (Feb 1-3)
**Total Trades:** Gabagool 76,839 | Baguette 14,446

---

## Executive Summary

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| **Strategy Type** | Pure Pair Arbitrage | Directional + Hedge |
| **Automation** | Fully Algorithmic | Semi-Auto (Bot + Human) |
| **Win Rate** | 48.0% (irrelevant) | 59.3% (predicts direction) |
| **Pair Cost** | $0.998 (guaranteed profit) | $2.23 (loses on pairs) |
| **Edge Source** | Spread capture | Direction prediction |
| **Replicable?** | Yes (need maker fills) | Partially (signal found) |

---

## Part 1: Gabagool - Pure Pair Arbitrage

### Strategy Summary

```
1. Start EARLY (98% start with >600s remaining)
2. Build EQUAL positions on both sides (50/50)
3. Target ~10,000 total shares per market
4. Achieve pair cost < $1.00 (guaranteed arbitrage)
5. NET POSITION ≈ 0 (no directional prediction)
```

### Key Mechanics

| Parameter | Value |
|-----------|-------|
| Target shares per side | 5,000 |
| Primary trade size | 24 shares (35% of trades) |
| Gap between sides | 4s median |
| Pair cost achieved | $0.998 |
| Markets with pair cost < $1 | 59.4% |
| Balance ratio | 98.7% |

### Adverse Selection Protection

Gabagool avoids adverse selection through:

1. **SPEED (3-4 second gap)**
   - Fills BOTH sides within seconds
   - No time for market to move against them
   - Locks in pair cost before prices change

2. **50/50 BALANCE (always)**
   - Maintains strict position balance
   - Adverse selection on one side offset by other
   - Net position stays near zero

3. **MAKER FILLS (60% below ask)**
   - Provides liquidity, doesn't take it
   - Gets filled when counterparties hit orders
   - Counterparties take the adverse selection risk

4. **PAIR COST < $1.00**
   - Guaranteed profit regardless of winner
   - Even if "picked off" on one side, pair is profitable
   - The ONLY metric that matters

5. **NO DIRECTIONAL PREDICTION**
   - Doesn't try to predict winner
   - Adverses selection hurts directional traders
   - Profits from spread, not prediction

**Core Insight:**
> Gabagool converts a DIRECTIONAL game into a SPREAD game.
> They don't care who wins - they profit from buying both for < $1.00

### Fill Quality Analysis

| Entry Price | Win Rate | Interpretation |
|-------------|----------|----------------|
| $0.00-$0.20 | 8.9% | Fair pricing |
| $0.20-$0.30 | 23.3% | Fair pricing |
| $0.40-$0.50 | 45.0% | Fair pricing |
| $0.60-$0.70 | 63.0% | Fair pricing |
| $0.80-$1.00 | 90.6% | Fair pricing |

The market is efficient - win rates match prices. Gabagool profits from the SPREAD, not from beating the market.

### Replication Requirements

```python
GABAGOOL_CONFIG = {
    'target_shares_per_side': 5000,
    'trade_sizes': [24, 5, 10, 20, 15],
    'max_gap_seconds': 10,
    'target_pair_cost': 0.998,
    'start_time_remaining': 850,
    'balance_tolerance': 0.02,
}
```

**Critical:** Must use LIMIT orders (maker) to achieve pair cost < $1.00
- As TAKER: Pair cost = $1.017 → LOSES money
- As MAKER: Pair cost = $0.998 → MAKES money

---

## Part 2: Baguette - Directional Predictor

### Strategy Summary

```
1. Start EARLY (97% start with >600s remaining)
2. Make DIRECTIONAL BET (lean into predicted winner)
3. Hedge with smaller position on other side
4. Target ~650 shares per market (biased distribution)
5. Win 59.3% - actually PREDICTS direction
```

### Key Mechanics

| Parameter | Value |
|-----------|-------|
| Target shares per market | ~650 total |
| Primary trade size | 5 shares (45% of trades) |
| Gap between sides | 35s median |
| Winner share % | 64.3% (biased to winner) |
| Prediction accuracy | 84.2% of markets |
| Pair cost | $2.23 (loses on pure pairs) |

### Manual vs Algorithmic Analysis

**Evidence for AUTOMATED (Bot):**
- 35% of trades < 100ms apart (humanly impossible)
- Up to 24 trades at exact same millisecond
- Trades at 3-4 AM UTC
- Fibonacci-like sizes (5, 8, 13)

**Evidence for HUMAN Input:**
- 15 markets have 60-224 second gaps between first UP and DOWN
- Much fewer trades than Gabagool (96/market vs 735/market)
- More single trades (54%) vs bursts

**Conclusion:** Semi-automated bot with human discretionary input

### The Signal (Reverse-Engineered)

```python
def baguette_signal(btc_ema_trend, net_obi):
    # Core: Follow BTC trend
    signal = 'UP' if btc_ema_trend > 0 else 'DOWN'

    # Confidence: Only trade when OBI is CONTRARIAN
    obi_signal = 'UP' if net_obi > 0 else 'DOWN'

    if obi_signal != signal:
        return signal  # HIGH confidence
    else:
        return None  # LOW confidence - skip
```

### Signal Backtest Results

**Claimed vs Actual:**

| Metric | Claimed | Default Config | Optimized Config |
|--------|---------|----------------|------------------|
| HIGH confidence | 98.1% | 52.4% | **77.3%** |
| LOW confidence | 37.5% | 50.0% | - |

**Optimized Configuration:**
```python
ema_period = 20
obi_threshold = 0.1  # Filter weak signals
entry_window = (700, 800)  # Later entry is better
```

| Metric | Value |
|--------|-------|
| HIGH confidence accuracy | 77.3% |
| Number of signals | 22 per OOS9 |
| PnL ($10/bet) | $50.40 |
| Avg PnL/trade | $2.29 |

### Why Results Differ from Claims

1. **Position-based vs signal-based** - Original analyzed actual positions
2. **Selection bias** - Baguette skips unattractive markets
3. **Different OBI** - Maybe cumulative, not snapshot
4. **Human discretion** - Semi-automated system

---

## Part 3: Head-to-Head Comparison

### Trading Behavior

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Total trades | 76,839 | 14,446 |
| Trades per market | 735 | 96 |
| Trade size (mode) | 24 | 5 |
| Gap between sides | 4s | 35s |
| Alternation rate | 22% | 22% |
| Start time | 850s remaining | 822s remaining |

### Position Management

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Winner share % | 49.8% (50/50) | 64.3% (biased) |
| Net position | ±70 shares | -65 to +229 |
| Markets >60% winner | 0% | 70% |
| Markets >70% winner | 0% | 43% |

### Profitability

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Win rate | 48.0% | 59.3% |
| Pair cost | $0.998 | $2.23 |
| Edge source | Spread | Prediction |
| Guaranteed profit | Yes | No |

### Statistical Tests

| Test | P-value | Interpretation |
|------|---------|----------------|
| Chi-square (side selection) | 0.0000 | DIFFERENT strategies |
| T-test (trade price) | 0.0000 | Different |
| T-test (trade size) | 0.0000 | Different |
| T-test (time remaining) | 0.0000 | Different |
| T-test (net OBI) | 0.0815 | Similar |

---

## Part 4: Replication Recommendations

### For Gabagool (Easier but needs infrastructure)

**Requirements:**
1. Limit order infrastructure (maker fills)
2. Fast execution (<5s for both sides)
3. Order book access for pricing
4. Capital for ~10,000 shares per market

**Expected Performance:**
- Pair cost target: < $1.00
- Guaranteed profit: ~$10-100 per market
- Win rate: N/A (both sides covered)

**Challenge:** Must get MAKER fills. Cannot replicate as TAKER.

### For Baguette (Easier signal but less reliable)

**Requirements:**
1. BTC EMA calculation (period 20)
2. OBI calculation from order book
3. Entry at 700-800s remaining
4. Filter: Only trade when OBI disagrees with BTC

**Expected Performance:**
- Accuracy: ~77% on filtered signals
- Signals: ~22 per OOS9 period
- PnL: ~$50 per dataset at $10/bet

**Challenge:** Lower accuracy than claimed. Semi-discretionary element hard to replicate.

---

## Part 5: Key Findings

### Gabagool Findings

1. **Pure market maker** - No directional prediction
2. **Speed is critical** - 4s between sides
3. **Maker fills required** - 60% below ask
4. **Pair cost < $1.00** - Guaranteed arbitrage
5. **50/50 balance** - No adverse selection exposure

### Baguette Findings

1. **Directional predictor** - 84% market accuracy
2. **Semi-automated** - Bot + human discretion
3. **Signal: BTC trend + OBI contrarian** - 77% with optimization
4. **Smaller positions** - 650 vs 10,000 shares
5. **Higher risk** - Loses money on pair cost

### Why Gabagool Works

> Gabagool converts market-making from a prediction game to a spread game.
> By maintaining 50/50 balance and achieving pair cost < $1.00,
> they profit regardless of which side wins.

### Why Baguette Works

> Baguette uses the "smart money vs dumb money" dynamic.
> When retail (OBI) fades the BTC trend, the trend wins 77%+ of the time.
> They trade WITH the BTC trend AGAINST the crowd.

---

## Output Files Generated

| File | Description |
|------|-------------|
| `whale_basic_profiles.csv` | Trade counts, sizes |
| `whale_entry_conditions.csv` | Entry analysis |
| `whale_win_rates.csv` | Win rates by condition |
| `whale_pair_building.csv` | Pair building stats |
| `gabagool_market_pairs.csv` | Per-market pair details |
| `baguette_market_pairs.csv` | Per-market pair details |
| `whale_btc_correlations.csv` | BTC correlations |
| `whale_statistical_tests.csv` | All test results |
| `whale_comparison.csv` | Head-to-head metrics |
| `whale_hf_latency.csv` | HF latency analysis |
| `gabagool_pair_mechanics.csv` | Pair building mechanics |
| `baguette_pair_mechanics.csv` | Pair building mechanics |
| `gabagool_replication_backtest.csv` | Replication backtest |
| `baguette_signal_backtest_results.csv` | Signal backtest |

---

## Part 6: Baguette Partial Hedging Analysis

### YES, Baguette Uses Partial Hedging

| Metric | Value |
|--------|-------|
| Average hedge ratio | 48.7% of main position |
| Median hedge ratio | 46.7% |
| Min hedge | 15% |
| Max hedge | 98% |

### Hedge Level Distribution

| Level | Count | Percentage |
|-------|-------|------------|
| Light (<25%) | 10 | 13% |
| Moderate (25-50%) | 33 | 43% |
| Heavy (50-75%) | 23 | 30% |
| Full (75-100%) | 10 | 13% |

### Hedge Ratio vs Prediction Outcome

| Outcome | Hedge Ratio | Net Exposure |
|---------|-------------|--------------|
| CORRECT (64 markets) | 46.2% | 222 shares |
| WRONG (12 markets) | 62.1% | 270 shares |

**Key Insight:** When WRONG, they had HIGHER hedge ratios (62% vs 46%).
This suggests they hedge MORE when less confident - and those are the markets they get wrong.

### Hedge Timing Pattern

| Metric | Value |
|--------|-------|
| Hedge trades FIRST | 46% of markets |
| Avg time for hedge | 523s remaining |
| Avg time for main | 482s remaining |
| Difference | +41s (hedge earlier) |

**Pattern:**
1. Start with hedge/probe position (~523s remaining)
2. Wait 40+ seconds for signal confirmation
3. Build main directional position (~482s remaining)
4. Keep hedge as insurance

### Hedge Effectiveness

| Scenario | PnL |
|----------|-----|
| With hedge | $178 |
| Without hedge | $3,829 |
| Hedge cost | -$3,651 |

**On WRONG predictions (12 markets):**
| Scenario | PnL |
|----------|-----|
| With hedge | -$2,463 |
| Without hedge | -$3,962 |
| **Hedge saved** | **$1,499** |

The hedge saved 37.8% of potential losses on wrong predictions.

### Hedge Sizing by Entry Time

| Entry Time | Avg Hedge Ratio |
|------------|-----------------|
| 700-900s | 53.5% |
| 500-700s | 46.7% |
| 300-500s | 45.1% |
| 0-300s | 61.6% |

**Interpretation:**
- Early entries (700-900s): Higher hedge - still uncertain
- Mid entries (300-700s): Lower hedge - more confident
- Late entries (0-300s): Higher hedge again - emergency/adjustment

### The Complete Baguette Pattern

```
TIME: 900s → 0s
      |
      | ← START: Place hedge trades (contrarian to OBI)
      |    Hedge ratio: ~50%
      |    "Testing the waters"
      |
~800s | ← OBSERVE: Wait for BTC trend confirmation
      |    Look for OBI to disagree with trend
      |
~600s | ← BUILD: Add main directional position
      |    If HIGH confidence: larger main, smaller hedge
      |    If LOW confidence: more balanced
      |
~300s | ← ADJUST: Fine-tune if needed
      |    May add more hedge if uncertain
      |
0s    | ← RESOLUTION
      |    Win: Main pays off, hedge is sunk cost
      |    Lose: Hedge recovers ~38% of losses
```

### Comparison to Gabagool

| Aspect | Gabagool | Baguette |
|--------|----------|----------|
| Hedge ratio | ~100% (50/50) | ~48% |
| Hedge purpose | Guarantee profit | Limit losses |
| Timing | Simultaneous | Sequential |
| Net exposure | ~0 | ~250 shares |
| Risk profile | No directional risk | Partial directional |

---

*Analysis completed: February 7, 2026*
*Scripts: whale_deep_analysis.py, whale_pair_mechanics.py, gabagool_replication.py, baguette_signal_backtest.py*
