# Strategy Pivot: Taker → Maker (February 2026)

**Date:** February 5, 2026 (Updated: February 6, 2026)
**Status:** CONTRARIAN MAKER VALIDATED
**Previous:** AGGRESSIVE (taker-based latency arbitrage)
**New:** CONTRARIAN MAKER (fade spikes when market uncertain)

## UPDATE (Feb 6, 2026): CONTRARIAN MAKER Strategy Validated

A new approach emerged from the maker study: **FADE spikes when the fade_side remains expensive**.

| Finding | Value |
|---------|-------|
| FADE accuracy (fade_side >= $0.65) | **90%** |
| Expected value per trade | **$1.15** (vs $0.35 taker) |
| Datasets validated | IS+OOS2, OOS7, OOS8 |

**Key insight:** When AGGRESSIVE detects a spike but Polymarket doesn't move (fade_side stays expensive), the market is right 90% of the time. FADE the spike.

**See:** [CONTRARIAN_MAKER.md](CONTRARIAN_MAKER.md) for full strategy spec.

---

---

## Executive Summary

The AGGRESSIVE strategy is being pivoted from **taker-based latency arbitrage** to **maker-based prediction trading** due to:

1. **Latency arbitrage not viable**: BTC velocity has NO predictive power (r < 0.06)
2. **Taker fees hurt profits**: 2% taker fee on every entry
3. **Pair building unprofitable**: Sequential pair builder loses money (avg pair cost > $1.00)
4. **Prediction has edge**: "Expensive side = winner" has 57% baseline accuracy (Gabagool achieves 67-70%)

---

## Why the Pivot?

### Evidence Against Latency Arbitrage

| Analysis | Finding |
|----------|---------|
| BTC velocity vs Gabagool direction | r = 0.055 (explains 0.3% variance) |
| Logistic regression (BTC features) | 51.4% accuracy (coin flip) |
| Combined model (BTC + market) | 52.9% accuracy (+0.1% over baseline) |
| 60Hz Binance data value | **NONE** - no latency advantage |

*Source: `PolyClaude/research/findings/gabagool_btc_correlation_findings.md`*

### Evidence Against Pair Building

| Config | PnL | Pair Cost | Result |
|--------|-----|-----------|--------|
| Best of 108 configs | **-$8.03** | $1.0876 | LOSING |
| All configs | Negative | > $1.00 | UNPROFITABLE |

*Source: `PolyClaude/research/optimizers/results/grid_search_results_latest.csv`*

### Evidence FOR Prediction

| Trader | Prediction Accuracy | Strategy |
|--------|---------------------|----------|
| Gabagool | **67-70%** | Expensive side + proprietary |
| Baguette | **82.5%** | Strong contrarian + momentum |
| Baseline (expensive side) | **57%** | Simple price following |

*Source: `research/findings/WHALE_OBI_ANALYSIS.md`*

---

## New Strategy: MAKER-PREDICTION

### Core Concept

Instead of:
- ❌ Taker entry at best ask (2% fee)
- ❌ Relying on BTC velocity for direction

We will:
- ✅ **Maker entry** via limit orders (0% fee)
- ✅ **Prediction signal** to choose direction (expensive side = likely winner)
- ✅ **Directional bias** toward predicted winner (not equal hedge)

### Entry Logic (Path B: Prediction)

```python
def get_prediction_signal(up_ask, down_ask, obi=None, momentum=None):
    """
    Predict which side will win.

    Baseline: expensive_side = likely_winner (57% accuracy)
    Enhanced: Add OBI confirmation, momentum filters
    """
    expensive_side = "UP" if up_ask > down_ask else "DOWN"

    # Optional: OBI confirmation (contrarian = better)
    if obi is not None:
        # Baguette fades OBI with -0.638 correlation
        if expensive_side == "UP" and obi < 0:
            confidence = "HIGH"  # OBI bearish but UP expensive = buy UP
        elif expensive_side == "DOWN" and obi > 0:
            confidence = "HIGH"  # OBI bullish but DOWN expensive = buy DOWN
        else:
            confidence = "LOW"

    return expensive_side, confidence
```

### Order Type: MAKER

```python
# Instead of taker (market order):
# order = create_market_order(side, size)  # 2% fee

# Use maker (limit order):
order = create_limit_order(
    side=predicted_winner,
    price=best_bid + 0.01,  # Penny above best bid
    size=shares,
)  # 0% fee
```

### Position Sizing (Path C: Frank-Wolfe/Bregman)

Frank-Wolfe optimization for position sizing based on:
1. **Coherence detection**: KL divergence to find price incoherence
2. **Optimal allocation**: Maximize expected profit given prediction confidence

```python
# From PolyClaude/polyclaude/strategies/arbitrage/optimizer.py
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

---

## Implementation Plan

### Phase 1: Backtest Maker-Prediction Strategy
- [ ] Create `research/backtests/maker_prediction_backtest.py`
- [ ] Test expensive_side baseline (57% expected)
- [ ] Add OBI confirmation filter
- [ ] Add momentum filter (60s price change)
- [ ] Simulate maker fill delays (2s)

### Phase 2: Add Frank-Wolfe Position Sizing
- [ ] Import from PolyClaude: `optimizer.py`, `coherence.py`
- [ ] Integrate coherence check into entry logic
- [ ] Test FW position sizing vs fixed sizing
- [ ] Validate on OOS data

### Phase 3: Paper Trade
- [ ] Update `run_paper_bot.py` for maker orders
- [ ] Add prediction signal to strategy
- [ ] Monitor fill rates (maker may not fill)
- [ ] Compare to old AGGRESSIVE performance

### Phase 4: Production
- [ ] Deploy with small size (10 shares)
- [ ] Monitor and tune parameters
- [ ] Scale if profitable

---

## Configuration Changes

### Old AGGRESSIVE (Taker)
```python
AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",
    spike_method="EWMA_1000",
    entry_type="TAKER",           # ← 2% fee
    hedge_type="PASSIVE",
    time_stop_seconds=30.0,
    # ... BTC velocity based
)
```

### New MAKER-PREDICTION
```python
MAKER_PREDICTION = TradingConfig(
    name="MAKER_PREDICTION",
    entry_type="MAKER",           # ← 0% fee
    prediction_method="expensive_side",
    use_obi_filter=True,          # Contrarian OBI
    use_momentum_filter=True,     # 60s momentum
    maker_delay_ms=2000,          # 2s fill simulation
    use_frank_wolfe=False,        # Enable later
    # ... no BTC velocity
)
```

---

## Expected Performance

| Metric | Old AGGRESSIVE | New MAKER-PREDICTION (est) |
|--------|----------------|----------------------------|
| Entry fee | 2% | **0%** |
| Direction accuracy | 50% | **57-65%** (prediction) |
| Fill rate | 100% (taker) | ~70% (maker may not fill) |
| Avg win | $4.18 | Similar |
| Avg loss | $2.45 | Similar |
| Net edge | ~$15/hr | TBD (need backtest) |

**Key trade-off:** Lower fill rate but no entry fees + better direction.

---

## Files to Create/Modify

### New Files
| File | Purpose |
|------|---------|
| `research/backtests/maker_prediction_backtest.py` | Backtest new strategy |
| `research/strategies/MAKER_PREDICTION.md` | Full strategy spec |
| `src/strategies/maker_prediction.py` | Live strategy implementation |

### Files to Update
| File | Change |
|------|--------|
| `research/MASTER_PLAN.md` | Document pivot, add Path B/C |
| `research/strategies/AGGRESSIVE.md` | Mark as deprecated (taker-based) |
| `CLAUDE.md` | Update key files reference |

### Files to Import from PolyClaude
| Source | Destination | Purpose |
|--------|-------------|---------|
| `polyclaude/strategies/arbitrage/optimizer.py` | `src/core/frank_wolfe.py` | FW position sizing |
| `polyclaude/strategies/arbitrage/coherence.py` | `src/core/coherence.py` | Price coherence check |
| `polyclaude/core/fees.py` | (already in src/core) | Fee calculation |

---

## Research References

### Gabagool Analysis
- `research/findings/gabagool_strategy_decoded.md` - Full strategy breakdown
- `research/findings/WHALE_OBI_ANALYSIS.md` - OBI correlation analysis
- `PolyClaude/research/findings/gabagool_btc_correlation_findings.md` - BTC velocity analysis

### Frank-Wolfe/Bregman
- `PolyClaude/polyclaude/strategies/arbitrage/optimizer.py` - FW optimizer
- `PolyClaude/polyclaude/strategies/arbitrage/coherence.py` - Coherence checker
- `research/plans/GABAGOOL_FRANK_WOLFE_BREGMAN_ANALYSIS.md` - FW analysis plan

### Pair Builder Failure
- `PolyClaude/research/optimizers/results/grid_search_results_latest.csv` - All configs unprofitable
- `PolyClaude/.claude/plans/glistening-drifting-lantern.md` - Pair builder plan

---

## Success Criteria

1. **Backtest profitable** after maker delays and 0% fees
2. **Direction accuracy** > 55% (better than 50% AGGRESSIVE)
3. **Fill rate** > 60% (maker orders may not fill)
4. **Paper trade** positive PnL over 24+ hours
5. **Sharpe** > 1.0

---

*Created: February 5, 2026*
*Author: Claude Code*
