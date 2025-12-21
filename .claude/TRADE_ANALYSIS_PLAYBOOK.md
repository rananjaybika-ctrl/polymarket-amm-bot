# Trade Analysis Playbook

Reference guide for analyzing Polymarket AMM Bot paper trading sessions.
Use this playbook whenever the user requests trade analysis.

---

## Data Sources

```
paper_trades_accumulation.csv  - Accumulation strategy trades
paper_trades_directional.csv   - Directional strategy trades
```

### CSV Columns

**Common columns:**
- `timestamp` - UTC timestamp
- `market_slug` - Market identifier
- `event_type` - BUY, RESOLUTION, etc.
- `trade_side` - UP or DOWN
- `pnl_realized` - Realized PNL at resolution
- `balance_after` - Balance after event
- `pos_pair_cost` - Cost per hedged pair
- `pos_hedged_pairs` - Number of hedged pairs
- `pos_imbalance` - Position imbalance

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

### 3. Directional Strategy Details

**Bias Distribution:**
- UP bias count and accuracy %
- DOWN bias count and accuracy %
- NEUTRAL bias count

**Flip Analysis:**
- Avg flips per market
- Max flips
- PNL by flip count

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

## Version History

- v1.0 (2025-12-21): Initial playbook created after comprehensive analysis
