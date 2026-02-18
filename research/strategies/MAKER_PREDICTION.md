# MAKER-PREDICTION Strategy (Path B)

**Status:** IN DEVELOPMENT
**Created:** February 5, 2026
**Replaces:** AGGRESSIVE (taker-based, DEPRECATED)

---

## Executive Summary

MAKER-PREDICTION is a prediction-based trading strategy that uses:
- **MAKER entry** (0% fee via limit orders)
- **Prediction signal** (expensive side = likely winner)
- **OBI contrarian filter** (trade against orderbook imbalance)
- **Momentum confirmation** (enter on rising trends)

This strategy replaces the deprecated AGGRESSIVE taker-based strategy after research showed:
1. Latency arbitrage is NOT viable (BTC velocity r = 0.055)
2. Taker fees (2%) eat into profits
3. Prediction signals have proven edge (57-82% accuracy)

---

## Configuration

```python
MAKER_PREDICTION = TradingConfig(
    name="MAKER_PREDICTION",
    # Entry type
    entry_type="MAKER",           # 0% fee (vs 2% taker)

    # Prediction signal
    prediction_method="expensive_side",  # Buy the leading side

    # Filters
    use_obi_contrarian=True,      # Fade OBI (Baguette: -0.638 correlation)
    use_momentum_filter=True,     # 60s momentum confirmation
    momentum_threshold_pct=0.02,  # Minimum 2 cents movement

    # Position management
    time_stop_seconds=60.0,       # Exit if losing after 60s
    min_time_remaining=120.0,     # Don't enter in last 2 minutes
    high_entry_threshold=0.90,    # Skip entries >= $0.90
    low_entry_threshold=0.10,     # Skip entries <= $0.10

    # Cycling
    min_cycle_gap_ms=1000,        # 1s between cycles
    use_cycling=True,
)
```

---

## Core Concepts

### 1. Prediction Signal: "Expensive Side = Winner"

From WHALE_OBI_ANALYSIS.md research:

| Strategy | Prediction Accuracy |
|----------|---------------------|
| Random baseline | 50.0% |
| Expensive side only | **56.9%** |
| Gabagool | 67.5% |
| **Baguette** | **82.5%** |

**Logic:** The side with the higher ask price is currently "winning" the market. This is momentum/trend following on price.

```python
def get_prediction(up_ask, down_ask):
    if up_ask > down_ask:
        return "UP"  # UP is expensive = likely winner
    else:
        return "DOWN"  # DOWN is expensive = likely winner
```

### 2. OBI Contrarian Filter

Baguette's strategy has -0.638 correlation with OBI (strong contrarian).

**Logic:** When orderbook imbalance favors one side, trade the OTHER side.

| Whale | OBI Correlation | Interpretation |
|-------|-----------------|----------------|
| Gabagool | -0.276 | Slight contrarian |
| **Baguette** | **-0.638** | Strong contrarian |

```python
def obi_confirms(predicted_side, net_obi):
    # CONTRARIAN: Buy UP when market is bearish (OBI < 0)
    if predicted_side == "UP":
        return net_obi < 0  # Market bearish, we buy UP
    else:
        return net_obi > 0  # Market bullish, we buy DOWN
```

### 3. Momentum Filter

Baguette is more accurate when catching rising momentum (> 2-3 cents/minute).

```python
def momentum_confirms(predicted_side, momentum_60s):
    if predicted_side == "UP":
        return momentum_60s > 0.02  # Rising UP price
    else:
        return momentum_60s < -0.02  # Rising DOWN price
```

### 4. MAKER Entry (0% Fee)

Instead of taker (market order, 2% fee), we use maker (limit order, 0% fee).

| Entry Type | Fee | Fill Certainty | Fill Timing |
|------------|-----|----------------|-------------|
| TAKER | 2% | ~100% | Immediate (+ network latency) |
| **MAKER** | **0%** | Uncertain | When market reaches limit price |

**How maker orders work:**
- Place limit buy at target price (e.g., $0.48)
- Order fills immediately IF market ask ≤ your limit price
- If market never reaches your price, order never fills
- No inherent delay - it's fill **uncertainty**, not time delay

**Trade-off:** Lower fill certainty but no entry fees + better direction.

---

## Entry Logic

```python
def should_enter(market_data):
    # 1. Get prediction signal
    predicted_side = get_expensive_side(up_ask, down_ask)
    if predicted_side is None:
        return False

    # 2. Skip if price too extreme
    entry_price = up_ask if predicted_side == "UP" else down_ask
    if entry_price >= 0.90 or entry_price <= 0.10:
        return False

    # 3. OBI contrarian filter
    if not obi_confirms(predicted_side, net_obi):
        return False

    # 4. Momentum filter
    momentum = compute_momentum_60s(predicted_side)
    if not momentum_confirms(predicted_side, momentum):
        return False

    # 5. Time check
    if time_remaining < 120:
        return False

    return True
```

---

## Exit Logic

Three exit types:

### 1. Maker Fill (Passive Hedge)
- Place limit order on loser side at calculated bid
- When market ask drops to our bid, we fill at OUR price (0% fee)
- **Best outcome:** Low pair cost, maker exit

### 2. Time-Stop
- If losing after 60 seconds, exit at market (taker)
- Prevents holding losers too long
- **Worst outcome:** Higher pair cost due to taker exit fee

### 3. Resolution
- Hold to market resolution
- Receive $1.00 if correct, $0.00 if wrong
- **High variance:** Full win or full loss

---

## Expected Performance

| Metric | AGGRESSIVE (Old) | MAKER-PREDICTION (New) |
|--------|------------------|------------------------|
| Entry fee | 2% | **0%** |
| Direction accuracy | ~50% | **57-70%** |
| Fill certainty | ~100% (taker) | Uncertain (maker) |
| Avg win | $4.18 | Similar |
| Avg loss | $2.45 | Similar |
| Expected $/hr | $15.20 | **TBD** |

---

## Risk Analysis

### Advantages
1. **No entry fees** - Save 2% on every entry
2. **Better direction** - 57%+ accuracy vs 50%
3. **Proven whale signals** - Based on Gabagool/Baguette analysis

### Risks
1. **Fill uncertainty** - Maker orders may not fill if market moves away from limit price
2. **Adverse selection** - Orders that DO fill may be on the wrong side of momentum
3. **Momentum dependency** - Needs volatile markets for signals

### Mitigations
1. Only place limit orders when prediction signal is strong
2. Use OBI as confirmation filter to reduce adverse selection
3. Order timeout to be determined via grid search

---

## Implementation Files

| File | Purpose | Status |
|------|---------|--------|
| `research/backtests/maker_prediction_backtest.py` | Backtest simulation | CREATED |
| `research/strategies/MAKER_PREDICTION.md` | Strategy spec (this file) | CREATED |
| `src/strategies/maker_prediction.py` | Live implementation | TODO |
| `scripts/run_maker_prediction.py` | Paper trading runner | TODO |

---

## Backtest Command

```bash
cd /Users/rananjaybika/polymarket-amm-bot && python research/backtests/maker_prediction_backtest.py
```

---

## Research References

### Key Findings
- [WHALE_OBI_ANALYSIS.md](../findings/WHALE_OBI_ANALYSIS.md) - Whale trading patterns
- [STRATEGY_PIVOT_FEB2026.md](STRATEGY_PIVOT_FEB2026.md) - Pivot documentation
- [gabagool_btc_correlation_findings.md](../../PolyClaude/research/findings/gabagool_btc_correlation_findings.md) - BTC velocity analysis

### From WHALE_OBI_ANALYSIS.md

**Baguette's Apparent Strategy:**
1. Wait for price momentum > 2-3 cents/minute
2. Bet on the EXPENSIVE side (currently winning)
3. FADE OBI (contrarian to order flow)
4. SEEK volatility (trade more in high vol zones)
5. **Smaller positions when confident** (inverse sizing)

---

## Success Criteria

1. **Backtest profitable** with realistic maker fill simulation and 0% fees
2. **Prediction accuracy** > 55% (better than 50% AGGRESSIVE)
3. **Fill rate** > 60% (maker orders may not fill if market moves away)
4. **Sharpe ratio** > 1.0
5. **Paper trade** positive PnL over 24+ hours

---

## Future Enhancements (Path C)

### Frank-Wolfe Position Sizing

Consider integrating Frank-Wolfe optimization for position sizing:

```python
from polyclaude.strategies.arbitrage.optimizer import FrankWolfeOptimizer
from polyclaude.strategies.arbitrage.coherence import CoherenceChecker

checker = CoherenceChecker()
optimizer = FrankWolfeOptimizer()

# Check if prices are incoherent (arbitrage opportunity)
is_incoherent = checker.check_binary(up_ask, down_ask)

# Optimize position size
if is_incoherent:
    allocation = optimizer.optimize(prices=[up_ask, down_ask], budget=capital)
```

See: `PolyClaude/polyclaude/strategies/arbitrage/`

---

*Created: February 5, 2026*
*Based on: WHALE_OBI_ANALYSIS.md, STRATEGY_PIVOT_FEB2026.md*
