# Whale Pair Building Mechanics - Deep Analysis

*Generated: 2026-02-07*

---

## Executive Summary: TWO COMPLETELY DIFFERENT STRATEGIES

| Metric | Gabagool | Baguette | Interpretation |
|--------|----------|----------|----------------|
| **Strategy Type** | Pure Pair Builder | Directional with Hedge | Fundamentally different |
| **Win Rate** | 48.0% | 59.3% | Baguette predicts direction |
| **Net Position** | ±70 shares (balanced) | -65 to +229 shares (biased) | Baguette leans into winner |
| **Winner Share %** | 49.8% (always 50/50) | 64.3% (biased to winner) | Baguette knows which side wins |
| **Pair Cost** | $0.998 (guaranteed profit) | $2.23 (loses on pair) | Completely different economics |

---

## Strategy #1: GABAGOOL - "Pure Market Maker"

### The Strategy (Simplified)
```
1. Start EARLY (98% start with >600s remaining)
2. Build EQUAL positions on both sides
3. Target ~10,000 total shares per market (5,000 each side)
4. Achieve pair cost < $1.00 (guaranteed arbitrage profit)
5. NET POSITION ≈ 0 (doesn't predict direction)
```

### Key Mechanics

**First Side Selection:**
- UP first: 57%, DOWN first: 43% (roughly random)
- Does NOT buy expensive side first (only 40.6%)
- NOT contrarian (47.5% contrarian rate)
- First side has ZERO correlation with winner

**Gap Between Sides:**
- Mean: 7.6 seconds
- Median: 4.0 seconds
- 73% complete both sides within 10 seconds
- This is SIMULTANEOUS market making

**Size Pattern:**
- Mode: 24 shares (35% of all trades)
- Secondary size: 5 shares (4.7%)
- Uses FIXED SIZES (24, 5, 10, 20, etc.)
- ~735 trades per market (373 UP, 361 DOWN)

**Position Balance:**
- UP shares ≈ DOWN shares (always)
- Winner share %: 49.8% (perfect 50/50)
- NO markets with >60% winner exposure
- Net position uncorrelated with winner (r=-0.36)

**Economics:**
- Mean pair cost: $0.998 (UNDER $1.00!)
- 59.4% of markets achieve pair cost < $1.00
- **GUARANTEED PROFIT** regardless of winner

### Example Trade Flow (Gabagool)
```
Time  Side  Size   Running Position
890s   UP    24     UP: 24, DOWN: 0
886s  DOWN   24     UP: 24, DOWN: 24    ← Second leg in 4s
882s   UP    24     UP: 48, DOWN: 24
878s  DOWN   24     UP: 48, DOWN: 48
...continues alternating with fixed sizes...
```

---

## Strategy #2: BAGUETTE - "Directional Predictor with Hedge"

### The Strategy (Simplified)
```
1. Start EARLY (97% start with >600s remaining)
2. Make DIRECTIONAL BET (lean into predicted winner)
3. Hedge with smaller position on other side
4. Target ~650 shares per market (biased distribution)
5. Win 59.3% of time - PREDICTING direction!
```

### Key Mechanics

**First Side Selection:**
- UP first: 55%, DOWN first: 45%
- Does NOT buy expensive first (only 30.3%)
- STRONGLY contrarian (65.8% contrarian first!)
- First side correlated with winner at r=-0.34 (buys loser first as hedge!)

**Gap Between Sides:**
- Mean: 34.8 seconds (5x slower than Gabagool)
- Median: 16.0 seconds
- Only 32% complete both sides within 10 seconds
- This is SEQUENTIAL positioning, not simultaneous

**Size Pattern:**
- Mode: 5 shares (44.6% of all trades!)
- Secondary sizes: 13, 6, 8 shares
- Much SMALLER than Gabagool
- ~96 trades per market (44 UP, 52 DOWN)

**Position Balance:**
- DOWN shares > UP shares on average (net = -65)
- Winner share %: 64.3% (BIASED to winner!)
- 70% of markets have >60% winner exposure
- 43% of markets have >70% winner exposure
- Net position POSITIVELY correlated with winner (r=+0.49)

**Economics:**
- Mean pair cost: $2.23 (well OVER $1.00)
- 0% of markets achieve pair cost < $1.00
- **LOSES MONEY on pure pair arbitrage**
- **MAKES MONEY by predicting winner correctly**

### Example Trade Flow (Baguette)
```
Time  Side  Size   Running Position    Notes
890s  DOWN   10     UP: 0, DOWN: 10    ← Starts with hedge
888s  DOWN   29     UP: 0, DOWN: 39    ← Doubles down on hedge
886s  DOWN   30     UP: 0, DOWN: 69
...more DOWN buys...
786s   UP    10     UP: 10, DOWN: 130  ← Finally buys UP (100s later!)
782s   UP    10     UP: 20, DOWN: 130
...continues building UP position...

Final: UP: 235, DOWN: 353 (favor DOWN)
Winner: UP ← WRONG prediction, loses $69
```

---

## Head-to-Head Comparison

### Sizing

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Most common size | 24 (35%) | 5 (45%) |
| Mean size | 13.82 | 6.78 |
| Median size | 13.90 | 5.00 |
| Size variability (CV) | 0.66 | 0.73 |
| Trades per market | 735 | 96 |
| Total shares per market | 10,156 | 650 |

**Gabagool uses MUCH larger positions (15x more shares per market)**

### Timing

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Gap to second side (mean) | 7.6s | 34.8s |
| Gap to second side (median) | 4.0s | 16.0s |
| Both sides in <10s | 73% | 32% |

**Gabagool executes 5x faster**

### Alternation Pattern

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Alternation rate | 22.2% | 22.1% |
| Mean run length | 4.46 | 4.43 |
| Max run length | 70 | 146 |

**Both have similar "burst" patterns - NOT perfect UP-DOWN-UP-DOWN**

### Adverse Selection Handling

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Winner share % | 49.8% | 64.3% |
| Avg size on winner | 14.36 | 6.99 |
| Avg size on loser | 13.33 | 6.48 |
| Winner/Loser ratio | 1.08 | 1.08 |

**Same winner/loser size ratio, but Baguette has MORE shares on winner side**

---

## Which Strategy to Replicate?

### Gabagool (Easier to Replicate)

**Pros:**
- Deterministic: Always target 50/50 balance
- No prediction needed
- Guaranteed profit if pair cost < $1.00
- Simple sizing rules (use 24, 5, 10, etc.)

**Cons:**
- Requires FAST execution (4s between sides)
- Need to achieve pair cost < $1.00
- Lower edge per market ($0.002 * 5000 = $10?)
- 48% "win rate" is irrelevant (both sides covered)

**Key Parameters to Match:**
```python
GABAGOOL_CONFIG = {
    'target_shares_per_side': 5000,
    'trade_sizes': [24, 5, 10, 20, 15],  # Most common
    'max_gap_seconds': 10,  # Must get both sides quickly
    'target_pair_cost': 0.998,  # Under $1.00
    'start_time_remaining': 850,  # Start early
    'balance_tolerance': 0.02,  # Stay within 2% of 50/50
}
```

### Baguette (Harder to Replicate)

**Pros:**
- 59.3% win rate - actually predicts direction
- Higher profit potential per market
- Smaller capital required (650 vs 10,000 shares)

**Cons:**
- REQUIRES direction prediction model
- We don't know what signal they use
- Loses money on pure pairs ($2.23 cost)
- Need to understand their contrarian + OBI signal

**Key Signal Clues:**
- 65.8% contrarian first trade (buys against OBI)
- Buys loser side FIRST as hedge
- 34.8s gap suggests they WAIT for signal
- OBI correlation -0.34 for first side

**Unknown Signal Needed:**
```python
# We need to reverse engineer this:
def baguette_predict_winner(market_state):
    """
    Known factors:
    - NOT based on pair cost (they don't achieve <$1.00)
    - NOT based on expensive side (only 30% expensive first)
    - CORRELATED with OBI contrarian (65.8%)
    - Builds position over 35+ seconds (waits for info)

    Unknown:
    - What signal makes them add to one side?
    - Why 59% accuracy?
    """
    pass
```

---

## Recommendation

**Start with Gabagool** - it's a pure market-making strategy that doesn't require prediction.

**Key Requirements:**
1. Fast execution (<10s to get both sides)
2. Target pair cost < $1.00
3. Use fixed sizes (24, 5, 10)
4. Maintain 50/50 balance
5. Trade ~10,000 shares per market

**Next Step for Baguette:**
- Analyze their winning vs losing trades
- Look for signal in velocity, OBI, time patterns
- Build classifier to predict their side selection

---

*End of Analysis*
