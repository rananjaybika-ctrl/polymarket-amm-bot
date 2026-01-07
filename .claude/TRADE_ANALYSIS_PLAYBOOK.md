# Trade Analysis Playbook

Reference guide for analyzing Polymarket AMM Bot paper trading sessions.
Use this playbook whenever the user requests trade analysis.

---

## Data Sources

```
paper_trades_standard.csv         - Standard accumulation strategy trades
paper_trades_volume_weighted.csv  - Volume-weighted (Gabagool) strategy trades
paper_trades_directional.csv      - Directional strategy trades
```

**Web UI copies (updated in real-time):**
```
web/paper_trades_standard.csv
web/paper_trades_volume_weighted.csv
web/paper_trades_directional.csv
```

### CSV Columns

**Common columns:**
- `timestamp` - UTC timestamp
- `market_slug` - Market identifier
- `event_type` - BUY, RESOLUTION, etc.
- `trade_side` - UP or DOWN
- `pnl_realized` - Realized PNL at resolution
- `balance_after` - Balance after event
- `pos_up_size` - Number of UP shares held
- `pos_down_size` - Number of DOWN shares held
- `pos_pair_cost` - Cost per hedged pair
- `pos_hedged_pairs` - Number of hedged pairs
- `pos_imbalance` - Position imbalance (ratio)

**Derived metrics:**
- `unhedged_shares` - abs(pos_up_size - pos_down_size) at resolution

**Directional-only columns:**
- `bias` - UP, DOWN, or NEUTRAL
- `flip_count` - Number of bias flips
- `btc_price` - Current BTC price
- `btc_strike` - Strike price
- `btc_change_pct` - BTC % change vs strike

---

## Session Filtering

Convert IST to UTC: `IST - 5:30 hours = UTC`

Example session boundaries:
```
Session 1 (1:00 AM - 11:00 AM IST):
  End UTC: 2025-12-21 05:30:00

Session 2 (11:15 AM - 3:15 PM IST):
  Start UTC: 2025-12-21 05:45:00
  End UTC: 2025-12-21 09:45:00
```

---

## Analysis Structure

### 1. Quantitative Comparison Table

Generate this table for each session:

```
┌─────────────────────────────────┬────────────────────┬────────────────────┐
│ METRIC                          │ ACCUMULATION       │ DIRECTIONAL        │
├─────────────────────────────────┼────────────────────┼────────────────────┤
│ Markets Resolved                │ [count]            │ [count]            │
│ Win Rate                        │ [%]                │ [%]                │
│ Total PNL                       │ $[value]           │ $[value]           │
│ Avg PNL/Market                  │ $[value]           │ $[value]           │
├─────────────────────────────────┼────────────────────┼────────────────────┤
│ Max Win                         │ $[value]           │ $[value]           │
│ Max Loss                        │ $[value]           │ $[value]           │
│ Avg Win Size                    │ $[value]           │ $[value]           │
│ Avg Loss Size                   │ $[value]           │ $[value]           │
├─────────────────────────────────┼────────────────────┼────────────────────┤
│ Avg Pair Cost                   │ $[value]           │ N/A                │
│ Pair Cost < $1.00               │ [x/y]              │ N/A                │
├─────────────────────────────────┼────────────────────┼────────────────────┤
│ Avg Unhedged Shares             │ [value]            │ [value]            │
│ Max Unhedged Shares             │ [value]            │ [value]            │
│ Fully Hedged Markets            │ [x/y]              │ [x/y]              │
└─────────────────────────────────┴────────────────────┴────────────────────┘
```

### 2. Accumulation Strategy Details

**Pair Cost Distribution:**
- Average, Min, Max pair cost
- % below $1.00 threshold (profitable zone)
- PNL by pair cost bucket:
  - $0.00-$0.95: [count] markets, PNL: $[value]
  - $0.95-$0.98: [count] markets, PNL: $[value]
  - $0.98-$1.00: [count] markets, PNL: $[value]
  - $1.00-$1.05: [count] markets, PNL: $[value]

**Position Metrics:**
- Avg/Max imbalance
- Avg hedged pairs

**Unhedged Shares Analysis:**
- Avg unhedged shares at resolution
- Max unhedged shares (worst case exposure)
- Fully hedged rate: % of markets with 0 unhedged shares
- PNL correlation with unhedged shares:
  - 0 unhedged: [count] markets, Avg PNL: $[value]
  - 1-5 unhedged: [count] markets, Avg PNL: $[value]
  - 6-10 unhedged: [count] markets, Avg PNL: $[value]
  - 11+ unhedged: [count] markets, Avg PNL: $[value]

### 3. Directional Strategy Details

**Bias Distribution:**
- UP bias count and accuracy %
- DOWN bias count and accuracy %
- NEUTRAL bias count

**Flip Analysis:**
- Total flips across session
- Avg flips per market
- Max flips in single market

### 4. PNL Distribution

For each strategy:
```
PNL Range:    $[min] to $[max]
Mean PNL:     $[value]
Median PNL:   $[value]
Std Dev:      $[value]

Big Wins (>$2):     [count] trades, Total: $[value]
Small Wins:         [count] trades, Total: $[value]
Small Losses:       [count] trades, Total: $[value]
Big Losses (<-$2):  [count] trades, Total: $[value]
```

### 5. Statistical Analysis

Generate these statistical metrics for each strategy:

#### Return Distribution Metrics
| Metric | Formula | Interpretation |
|--------|---------|----------------|
| Mean Return | `pnls.mean()` | Average profit per market |
| Median Return | `pnls.median()` | Typical profit (less affected by outliers) |
| Std Deviation | `pnls.std()` | Return volatility |
| Variance | `pnls.var()` | Squared deviation from mean |
| Skewness | `pnls.skew()` | Negative = left tail (more losses) |
| Kurtosis | `pnls.kurtosis()` | High = fat tails (outlier risk) |

#### Volatility Metrics
| Metric | Formula | Interpretation |
|--------|---------|----------------|
| Coefficient of Variation (CV) | `std / abs(mean) * 100` | Lower = more consistent |
| Range | `max - min` | Total spread of outcomes |

#### Risk-Adjusted Returns
| Metric | Formula | Interpretation |
|--------|---------|----------------|
| Sharpe Ratio | `mean / std` | Higher = better risk-adjusted return (>1 good, >2 excellent) |
| Sortino Ratio | `mean / downside_std` | Higher = better (only penalizes losses) |
| Max Consecutive Losses | Count streaks | Risk of drawdown |

#### Percentile Analysis
| Percentile | Meaning |
|------------|---------|
| 5th | Worst-case scenario (95% of outcomes are better) |
| 25th (Q1) | Lower quartile boundary |
| 50th (Median) | Typical outcome |
| 75th (Q3) | Upper quartile boundary |
| 95th | Best-case scenario (5% of outcomes are better) |

#### Python Code for Statistical Analysis
```python
import pandas as pd
import numpy as np

def calculate_statistics(pnls):
    """Calculate comprehensive statistics for PNL series."""
    return {
        # Central tendency
        'mean': pnls.mean(),
        'median': pnls.median(),

        # Dispersion
        'std': pnls.std(),
        'variance': pnls.var(),
        'cv_pct': (pnls.std() / abs(pnls.mean()) * 100) if pnls.mean() != 0 else 0,

        # Distribution shape
        'skewness': pnls.skew(),
        'kurtosis': pnls.kurtosis(),

        # Range
        'min': pnls.min(),
        'max': pnls.max(),
        'range': pnls.max() - pnls.min(),

        # Percentiles
        'p5': pnls.quantile(0.05),
        'p25': pnls.quantile(0.25),
        'p75': pnls.quantile(0.75),
        'p95': pnls.quantile(0.95),

        # Risk-adjusted
        'sharpe': pnls.mean() / pnls.std() if pnls.std() != 0 else 0,
        'sortino': pnls.mean() / pnls[pnls < 0].std() if len(pnls[pnls < 0]) > 0 else float('inf'),
    }
```

### 6. Correlation Analysis

#### Inter-Strategy Correlation
Measure how strategies move together (by market_slug):

```python
# Merge PNLs by market_slug
merged = std_res[['market_slug', 'pnl']].merge(
    vw_res[['market_slug', 'pnl']], on='market_slug', suffixes=('_std', '_vw')
).merge(
    dir_res[['market_slug', 'pnl']], on='market_slug'
).rename(columns={'pnl': 'pnl_dir'})

# Calculate correlations
corr_std_vw = merged['pnl_std'].corr(merged['pnl_vw'])
corr_std_dir = merged['pnl_std'].corr(merged['pnl_dir'])
corr_vw_dir = merged['pnl_vw'].corr(merged['pnl_dir'])
```

**Interpretation:**
- Correlation near 0: Strategies are independent (good for diversification)
- Correlation near 1: Strategies move together (redundant)
- Correlation near -1: Strategies move opposite (natural hedge)

#### BTC Price Correlation
```python
# From directional data with btc_price column
btc_pnl_corr = dir_res['pnl'].corr(dir_res['btc_price'])
btc_change_corr = dir_res['pnl'].corr(dir_res['btc_change_pct'])
```

**Interpretation:**
- Near 0: Hedging is working (no directional exposure)
- Positive: Profits when BTC rises
- Negative: Profits when BTC falls

### 7. Three-Strategy Comparison Table

For comparing Standard, VW, and Directional:

```
┌─────────────────────────┬──────────────┬──────────────┬──────────────────────┐
│ METRIC                  │   STANDARD   │     VW       │    DIRECTIONAL       │
├─────────────────────────┼──────────────┼──────────────┼──────────────────────┤
│ Markets Resolved        │   [count]    │   [count]    │      [count]         │
│ Total Trades            │   [count]    │   [count]    │      [count]         │
│ Win Rate                │   [%]        │   [%]        │      [%]             │
│ Total PNL               │   $[value]   │   $[value]   │      $[value]        │
│ Avg PNL/Market          │   $[value]   │   $[value]   │      $[value]        │
├─────────────────────────┼──────────────┼──────────────┼──────────────────────┤
│ Mean Return             │   $[value]   │   $[value]   │      $[value]        │
│ Std Deviation           │   $[value]   │   $[value]   │      $[value]        │
│ Volatility (CV%)        │   [value]%   │   [value]%   │      [value]%        │
│ Sharpe Ratio            │   [value]    │   [value]    │      [value]         │
│ Sortino Ratio           │   [value]    │   [value]    │      [value]         │
├─────────────────────────┼──────────────┼──────────────┼──────────────────────┤
│ Max Win                 │   $[value]   │   $[value]   │      $[value]        │
│ Max Loss                │   $[value]   │   $[value]   │      $[value]        │
│ Avg Pair Cost           │   $[value]   │   $[value]   │      $[value]        │
│ Fully Hedged Markets    │   [x/y]      │   [x/y]      │      [x/y]           │
└─────────────────────────┴──────────────┴──────────────┴──────────────────────┘
```

---

## Data Integrity Checks

Before analyzing performance, run these integrity checks to validate data quality.

### 1. P&L Verification

Compare reported P&L against calculated P&L for each RESOLUTION event.

**Expected P&L Formula:**
```python
def calculate_expected_pnl(row, winner):
    """Calculate expected P&L from position state at resolution."""
    up_size = row['pos_up_size']
    up_avg = row['pos_up_avg']
    down_size = row['pos_down_size']
    down_avg = row['pos_down_avg']

    # Total cost
    total_cost = (up_size * up_avg) + (down_size * down_avg)

    # Payout depends on winner
    if winner == 'UP':
        payout = up_size * 1.0  # Only UP shares pay $1
    else:  # DOWN
        payout = down_size * 1.0  # Only DOWN shares pay $1

    return payout - total_cost

# Find discrepancies
resolutions = df[df['event_type'] == 'RESOLUTION']
for idx, row in resolutions.iterrows():
    winner = row['trade_side']
    expected = calculate_expected_pnl(row, winner)
    reported = row['pnl_realized']
    error = abs(expected - reported)
    if error > 0.01:  # >$0.01 tolerance
        print(f"P&L ERROR: {row['market_slug']}")
        print(f"  Expected: ${expected:.2f}, Reported: ${reported:.2f}")
        print(f"  Error: ${error:.2f}")
```

**Historical Bug Pattern:** If `extra_cost` appears to be subtracted twice, the bug was in an older version of `resolve_market()`. The current code is correct.

### 2. Imbalance Violation Detection

Check if trades exceeded the configured imbalance limits.

```python
def check_imbalance_violations(df, max_imbalance_shares=5):
    """Find trades that exceeded imbalance limit."""
    trades = df[df['event_type'] == 'TRADE']
    violations = []

    for idx, row in trades.iterrows():
        up_size = row.get('pos_up_size', 0)
        down_size = row.get('pos_down_size', 0)
        imbalance = abs(up_size - down_size)

        if imbalance > max_imbalance_shares:
            violations.append({
                'market': row['market_slug'],
                'timestamp': row['timestamp'],
                'up_size': up_size,
                'down_size': down_size,
                'imbalance': imbalance,
                'excess': imbalance - max_imbalance_shares,
                'side': 'UP' if up_size > down_size else 'DOWN',
            })

    return violations

# Run check
violations = check_imbalance_violations(accum_df, max_imbalance_shares=5)
print(f"Imbalance violations: {len(violations)} / {len(trades)} trades")
print(f"Violation rate: {len(violations)/len(trades)*100:.1f}%")

# Severity breakdown
mild = [v for v in violations if v['excess'] <= 5]
moderate = [v for v in violations if 5 < v['excess'] <= 15]
severe = [v for v in violations if v['excess'] > 15]
print(f"  Mild (6-10 shares): {len(mild)}")
print(f"  Moderate (11-20): {len(moderate)}")
print(f"  Severe (21+): {len(severe)}")
```

### 3. Winner Consistency Check

Verify same market has same winner across both strategies.

```python
def check_winner_consistency(accum_df, dir_df):
    """Find markets with conflicting winners between strategies."""
    accum_res = accum_df[accum_df['event_type'] == 'RESOLUTION']
    dir_res = dir_df[dir_df['event_type'] == 'RESOLUTION']

    conflicts = []

    for market in accum_res['market_slug'].unique():
        accum_winner = accum_res[accum_res['market_slug'] == market]['trade_side'].iloc[0]
        dir_match = dir_res[dir_res['market_slug'] == market]

        if len(dir_match) > 0:
            dir_winner = dir_match['trade_side'].iloc[0]
            if accum_winner != dir_winner:
                conflicts.append({
                    'market': market,
                    'accum_winner': accum_winner,
                    'dir_winner': dir_winner,
                })

    return conflicts

# Run check
conflicts = check_winner_consistency(accum_df, dir_df)
if conflicts:
    print(f"CRITICAL: {len(conflicts)} markets with conflicting winners!")
    for c in conflicts:
        print(f"  {c['market']}: Accum={c['accum_winner']}, Dir={c['dir_winner']}")
else:
    print("All markets have consistent winners between strategies")
```

**Root Cause if Conflicts Found:** Each strategy had independent BinanceClient with different strike prices. Fix: Use Polymarket API `get_winning_token()` for actual resolution.

---

## Key Metrics to Calculate

### From RESOLUTION events only:

```python
resolutions = df[df['event_type'] == 'RESOLUTION']
pnls = resolutions['pnl_realized']

# Win/Loss
wins = (pnls > 0).sum()
losses = (pnls < 0).sum()
win_rate = wins / len(resolutions) * 100

# PNL
total_pnl = pnls.sum()
avg_pnl = pnls.mean()
max_win = pnls.max()
max_loss = pnls.min()
avg_win = pnls[pnls > 0].mean()
avg_loss = pnls[pnls < 0].mean()
```

### Pair Cost (Accumulation):

```python
pair_costs = resolutions['pos_pair_cost'].dropna()
pair_costs = pair_costs[pair_costs > 0]

avg_pair_cost = pair_costs.mean()
below_threshold = (pair_costs < 1.0).sum()
above_threshold = (pair_costs >= 1.0).sum()
```

### Unhedged Shares:

```python
resolutions = df[df['event_type'] == 'RESOLUTION']

# Calculate unhedged shares for each resolution
resolutions['unhedged_shares'] = abs(
    resolutions['pos_up_size'] - resolutions['pos_down_size']
)

# Key metrics
avg_unhedged = resolutions['unhedged_shares'].mean()
max_unhedged = resolutions['unhedged_shares'].max()
fully_hedged_count = (resolutions['unhedged_shares'] == 0).sum()
fully_hedged_rate = fully_hedged_count / len(resolutions) * 100

# PNL by unhedged bucket
def analyze_by_unhedged(df):
    buckets = [
        ("0 (fully hedged)", 0, 0),
        ("1-5 shares", 1, 5),
        ("6-10 shares", 6, 10),
        ("11-20 shares", 11, 20),
        ("21+ shares", 21, float('inf')),
    ]

    for label, low, high in buckets:
        mask = (df['unhedged_shares'] >= low) & (df['unhedged_shares'] <= high)
        subset = df[mask]
        if len(subset) > 0:
            avg_pnl = subset['pnl_realized'].mean()
            print(f"  {label}: {len(subset)} markets, Avg PNL: ${avg_pnl:.2f}")
```

---

## Qualitative Analysis Framework

### What Went Well

**Accumulation:**
- High % of markets with pair cost < $1.00
- Low imbalance (< 1.0)
- Consistent small wins
- Big wins outweigh big losses

**Directional:**
- High bias accuracy
- Low flip count (conviction)
- Correct trend identification
- Big wins when right

### What Didn't Work

**Accumulation:**
- Markets with pair cost >= $1.00
- High imbalance
- Large single losses

**Directional:**
- Win rate near 50% (coin flip)
- High flip count (indecision)
- Big losses from wrong bias
- Session-specific underperformance

---

## Recommendations Template

### Accumulation:
- [ ] Pair cost discipline: Enforce $0.99 max limit
- [ ] Imbalance control: Cap at specified threshold
- [ ] Target hedged pairs: [X-Y] range

### Directional:
- [ ] Time-of-day filtering if session variance high
- [ ] Faster flip detection
- [ ] Higher conviction threshold (z-score)
- [ ] Consider hybrid approach

### Capital Allocation:
Based on relative performance:
- Accumulation outperforms: 70-80% Accumulation
- Similar performance: 50-50 split
- Directional outperforms: Investigate conditions

---

## Output Format

Use markdown with:
- Tables for quantitative comparisons
- Bullet points for insights
- Winner indicators (checkmarks/emojis)
- Section headers for organization

Example session output:
```markdown
## SESSION 1: [Time Range]

### Quantitative Comparison
[table]

### What Went Well
- Accumulation: [insight]
- Directional: [insight]

### What Didn't Work
- Accumulation: [issue]
- Directional: [issue]

### Session Winner: [STRATEGY] by $[difference]
```

---

## Python Snippets for Analysis

### Load Data with Timezone Handling:
```python
import pandas as pd

accum_df = pd.read_csv('paper_trades_accumulation.csv')
accum_df['timestamp'] = pd.to_datetime(accum_df['timestamp'], utc=True)

# Filter to session
session_end = pd.Timestamp('2025-12-21 05:30:00', tz='UTC')
session_df = accum_df[accum_df['timestamp'] < session_end]
```

### Calculate All Metrics:
```python
def analyze(df):
    res = df[df['event_type'] == 'RESOLUTION']
    pnls = res['pnl_realized']

    return {
        'markets': len(res),
        'wins': (pnls > 0).sum(),
        'losses': (pnls < 0).sum(),
        'win_rate': (pnls > 0).sum() / len(res) * 100,
        'total_pnl': pnls.sum(),
        'avg_pnl': pnls.mean(),
        'max_win': pnls.max(),
        'max_loss': pnls.min(),
    }
```

---

## Unified Orderbook Note

**Important**: Polymarket uses a **unified orderbook** for binary markets.

In a binary market (UP/DOWN), there's only ONE orderbook because:
- **Selling YES = Buying NO** (at 1 - price)
- **Selling NO = Buying YES** (at 1 - price)

Example: If you sell 10 UP shares at $0.60, this is equivalent to buying 10 DOWN shares at $0.40.

**Implication for trade analysis**: When analyzing a wallet's trades via the API:
- SELL trades are **artifacts of the unified orderbook**, not intentional sells
- Most accumulation bots are **buy-only** - they don't actively sell
- **Ignore SELL trades** when analyzing buy-side accumulation strategies

---

## Gabagool22 Opportunistic MM Strategy

### Analysis Summary (5 Markets, Dec 20-22, 2025)

Based on analysis of wallet `0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d`:

| Parameter | Observed Value |
|-----------|----------------|
| Total BUY trades | 2,500 |
| Total hedged pairs | 13,530 |
| Final pair cost (avg) | $0.9787 |
| Final pair cost (range) | $0.9615 - $0.9877 |
| Max imbalance | 277 shares |
| Max imbalance ratio | 24.62x |
| Avg UP buy price | $0.3886 |
| Avg DOWN buy price | $0.5578 |

### Key Finding: NO Strict Pair Cost Threshold During Accumulation

**Contrary to our assumption**, gabagool22 does NOT enforce a strict pair cost < $1.00 rule during accumulation:

```
Prospective pair costs during accumulation:
  Max observed: $1.0612 (exceeds $1.00!)
  > $1.00: 185 trades (7.4%)
  > $0.999: 220 trades (8.8%)
  > $0.99: 911 trades (36.4%)
```

**However**, the final pair cost is ALWAYS profitable:
- All 5 markets ended with pair cost < $1.00
- Average final pair cost: $0.9787 (guaranteed ~$0.02 profit per pair)

### The Gabagool Strategy: "Buy Cheap, Trust the Average"

1. **Buy whatever is cheap** (relative to 50c mid-point)
   - UP when < $0.50 (avg $0.39)
   - DOWN when < $0.50 (avg $0.56 - higher because often hedging expensive side)

2. **Tolerate large imbalances** during accumulation
   - Up to 277 shares unhedged
   - Ratio up to 24.62x (one side can be 24x larger than other temporarily)

3. **Trust the law of averages**
   - Over 500+ trades per market, prices average out
   - As long as avg buy price per side < $0.50, pair cost < $1.00

4. **End balanced (or close)**
   - Final imbalance typically < 200 shares
   - Most value is in hedged pairs

### Mathematical Basis

For a profitable pair:
```
pair_cost = avg_up_price + avg_down_price < $1.00

If avg_up_price ≈ $0.39 and avg_down_price ≈ $0.56:
  pair_cost ≈ $0.95 → guaranteed $0.05 profit per pair
```

The strategy works because:
- In a 50/50 market, both sides should price around $0.50
- By buying both sides when cheap (< $0.50), you lock in profit
- Temporary pair cost > $1.00 doesn't matter if you keep buying cheap

### Comparison: Gabagool vs Our AsymmetricOpportunity

| Aspect | Gabagool | Our AsymmetricOpportunity |
|--------|----------|---------------------------|
| Pair cost check | None during accumulation | Prospective check before each buy |
| Imbalance tolerance | Very high (277 shares, 24x) | Typically limited |
| Buy price constraint | < ~$0.50 target | Based on prospective pair cost |
| Strategy | Buy cheap, trust average | Constrained optimization |

### CRITICAL: Impulsive/Trending Market Behavior

**Initial assumption was WRONG**: "Buy cheap only" does NOT work in trending markets.

Analysis of 3 impulsive markets (Dec 15, 17, 21) revealed gabagool22 **DOES buy expensive** to maintain hedges:

#### Impulsive Market Data

| Market | Cheap Side | Avg Price | Expensive Side | Avg Price | Max Paid |
|--------|------------|-----------|----------------|-----------|----------|
| Dec 21 9PM | UP | $0.24 | DOWN | $0.76 | **$0.99** |
| Dec 17 9:45AM | UP | $0.37 | DOWN | $0.62 | $0.75 |
| Dec 15 9:45AM | UP | $0.19 | DOWN | $0.80 | **$0.99** |

#### Price Distribution in Trending Markets

```
Dec 21 9PM (UP trending):
  UP trades:   250 cheap (<$0.45) |  0 expensive (>$0.55)
  DOWN trades:   0 cheap          | 205 EXPENSIVE

Dec 15 9:45AM (UP trending):
  UP trades:   245 cheap (<$0.45) |  0 expensive
  DOWN trades:   0 cheap          | 255 EXPENSIVE
```

**Key insight**: They buy 200+ trades at $0.60-$0.99 for the expensive side!

#### Recovery After Max Imbalance

When position becomes heavily imbalanced, they aggressively hedge at ANY price:

| Market | Max Imbalance | Recovery Side | Avg Recovery Price | Expensive Hedges |
|--------|---------------|---------------|-------------------|------------------|
| Dec 21 9PM | 179 shares | DOWN | **$0.87** | 111 trades |
| Dec 15 9:45AM | 223 shares | DOWN | **$0.89** | 103 trades |

#### Why Expensive Hedges Still Work

```
Dec 15 9:45AM Math:
  UP:   3,040 shares × $0.19 avg = $578 cost
  DOWN: 2,994 shares × $0.80 avg = $2,395 cost

  Total cost: $2,973
  Hedged pairs: 2,994
  Pair cost: $0.99 → STILL PROFITABLE!
```

The secret: **Many cheap shares offset few expensive hedges**

If you buy 3,000 shares at $0.19 avg and 3,000 shares at $0.80 avg:
- Pair cost = $0.19 + $0.80 = $0.99
- Guaranteed $0.01 profit per pair × 3,000 = **$30 profit**

### The REAL Gabagool Strategy

**NOT "buy cheap only"** but **"buy both sides, weight toward cheap"**

```
1. Buy cheap side aggressively (many trades at low prices)
2. ALSO buy expensive side to hedge (fewer trades, but at ANY price needed)
3. Accept temporary pair cost > $1.00 during accumulation
4. Final pair cost will be < $1.00 due to averaging
```

### Recommendations for Our Bot

Based on gabagool22's complete strategy (including impulsive markets):

1. **BUY BOTH SIDES - not just cheap**
   - In trending markets, one side will always be expensive
   - You MUST buy expensive side to maintain hedge
   - Don't wait for "cheap" price that never comes

2. **Relax pair cost constraint during accumulation**
   - Allow prospective pair cost up to $1.03 temporarily
   - Trust that averaging brings final cost < $1.00

3. **Aggressive hedging when imbalanced**
   - If imbalance > 150 shares, buy deficit side at ANY available price
   - Better to pay $0.95 for hedge than hold unhedged losers

4. **Increase imbalance tolerance**
   - Allow up to 200-250 shares imbalance
   - But trigger aggressive hedging at high imbalance

5. **Volume matters**
   - Target 500+ trades per market
   - More trades = better averaging = lower final pair cost

### Updated Buy Rule (Gabagool-Style)

```python
def should_buy(
    side: str,
    price: float,
    current_up: float,
    current_down: float,
    max_imbalance: int = 200
) -> bool:
    """
    Gabagool-style buy decision for trending markets.

    Buy if:
    1. Price is cheap (< $0.50), OR
    2. We need to hedge (imbalance too high on this side)
    """
    # Always buy if cheap
    if price < 0.50:
        return True

    # Buy expensive if we need to hedge
    imbalance = abs(current_up - current_down)
    deficit_side = "UP" if current_down > current_up else "DOWN"

    if side == deficit_side and imbalance > max_imbalance * 0.5:
        # Buy deficit side even at expensive prices to reduce imbalance
        return price < 0.99  # Still have some limit

    return False
```

### Key Takeaway

**Q: In trending markets, won't we just accumulate losers?**

**A: Only if you refuse to buy the expensive (winning) side.**

Gabagool buys the expensive side at $0.80-$0.99 to maintain hedges. The math works because they bought enough cheap shares ($0.10-$0.30) to offset the expensive hedges.

---

## Accumulation Mode Toggle

The bot supports two accumulation modes via CLI:

### Usage

```bash
# Standard mode (default) - strict pair cost enforcement
python scripts/run_paper_bot.py --accum-mode standard

# Opportunistic mode (Gabagool-style) - flexible hedging
python scripts/run_paper_bot.py --accum-mode opportunistic
```

### Mode Comparison

| Parameter | Standard Mode | Opportunistic Mode |
|-----------|---------------|-------------------|
| **Imbalance limit** | Absolute (shares) | Percentage (% of position) |
| **Buy decision** | Pair cost < threshold | Price < $0.50 OR need hedge |
| **Expensive buys** | Avoided | Required when imbalance high |
| **Pair cost tolerance** | Strict < $0.995 | Temporary > $1.03 OK |

### Standard Mode Parameters

```
--accum-pair-cost-limit    Max pair cost (default: 0.995)
--accum-max-imbalance      Max share difference (default: 20)
--accum-target-shares      Target per side (default: 50)
--accum-max-share-price    Price ceiling (default: 0.95)
```

### Opportunistic Mode Parameters

Internally configured (not yet CLI-exposed):
```python
opp_imbalance_pct = 0.10      # Max 10% imbalance relative to position
opp_cheap_threshold = 0.50    # Buy if price < this (always)
opp_hedge_trigger_pct = 0.05  # Start hedging when imbalance > 5%
opp_max_hedge_price = 0.99    # Max price for hedge buys
```

### When to Use Each Mode

**Standard Mode:**
- Sideways markets with prices oscillating around $0.50
- When you want strict pair cost discipline
- Conservative approach with guaranteed profitability

**Opportunistic Mode:**
- Trending/impulsive markets with one side consistently expensive
- When you need to buy expensive side to maintain hedges
- Higher volume trading (500+ trades per market)
- When you trust averaging over strict thresholds

### Buy Logic Comparison

**Standard Mode:**
```python
should_buy = prospective_pair_cost < pair_cost_limit
```

**Opportunistic Mode:**
```python
def should_buy_opportunistic(side, price, current_up, current_down):
    # Always buy if cheap
    if price < 0.50:
        return True

    # Check if we need to hedge
    max_position = max(current_up, current_down, 1)
    imbalance = abs(current_up - current_down)
    imbalance_pct = imbalance / max_position

    deficit_side = "UP" if current_down > current_up else "DOWN"

    # Buy expensive if deficit side and imbalance too high
    if side == deficit_side and imbalance_pct > 0.05:
        return price < 0.99

    return False
```

---

## Version History

- v1.4 (2025-12-25): Added Statistical Analysis framework (Sharpe, Sortino, CV, percentiles), Correlation Analysis (inter-strategy, BTC), and Three-Strategy Comparison Table. Updated data sources for Standard/VW/Directional strategies.
- v1.3 (2025-12-22): Added Accumulation Mode Toggle documentation
- v1.2 (2025-12-22): Added impulsive/trending market analysis, corrected "buy cheap only" misconception
- v1.1 (2025-12-22): Added Unified Orderbook note and Gabagool22 strategy analysis
- v1.0 (2025-12-21): Initial playbook created after comprehensive analysis
