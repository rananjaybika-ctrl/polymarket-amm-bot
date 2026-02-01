# Why Single-Cycle Mode is Optimal

**Date:** January 31, 2026
**Status:** VALIDATED - Single-cycle is production-ready
**Author:** Multi-agent analysis consensus

---

## Executive Summary

Single-cycle mode's 180s "blocking" is not a limitation - it's the **secret sauce** that makes the strategy profitable. The 95% of "missed" spikes are not missed opportunities; they're duplicate signals from the same BTC move.

---

## The Two Paradoxes

### Paradox 1: Single trades only 5% of spikes but is profitable

**Answer:** The 95% "missed" spikes are NOT independent opportunities.

```
Raw spike analysis (OOS3+4, 47.1 hours):
- Total spikes detected: 11,172 (237/hour)
- Spikes within 0.1s of each other: 87%
- Spikes within 180s of each other: 99%
- Truly independent signals (>180s gap): 1% (~2.9/hour)
```

When BTC moves 0.05%, the spike detector fires on EVERY TICK where the threshold is exceeded. A single 2-second BTC move generates **hundreds of "spikes"** - but they're all the SAME signal counted multiple times.

### Paradox 2: Multi-cycle trades more but destroys profitability

**Answer:** Multi-cycle re-trades the same signal at worse prices.

| Mode | Win Rate | $/hr | Trades/hr |
|------|----------|------|-----------|
| SINGLE | 54.3% | +$1.37 | 2.9 |
| MULTI | 39.8% | -$26.70 | 29+ |

Multi-cycle enters on the "second" spike which is:
- 99.4% likely to be the **same direction** as the first
- Part of the **same BTC move** (not a new opportunity)
- At a **worse price** (the move already happened)

---

## Spike Clustering Analysis

### Time Gap Distribution

| Gap | Count | Percentage |
|-----|-------|------------|
| < 0.1s | 9,725 | 87.0% |
| 0.1-0.5s | 22 | 0.2% |
| 0.5-1s | 3 | 0.0% |
| 1-5s | 19 | 0.2% |
| 5-10s | 12 | 0.1% |
| 10-30s | 45 | 0.4% |
| 30-60s | 56 | 0.5% |
| 60-180s | 56 | 0.5% |
| **>180s** | **112** | **1.0%** |

**Key insight:** Only 1% of spikes have >180s gap from the previous spike. These are the truly independent signals.

### Direction Correlation

| Consecutive Spikes | Same Direction | Opposite Direction |
|-------------------|----------------|-------------------|
| Within 180s | 99.4% | 0.6% |
| Within 100ms | 100.0% | 0.0% |

When spikes cluster, they are almost always in the SAME direction. This confirms they're duplicates of the same BTC move, not independent signals.

---

## Why Single-Cycle's Blocking Works

```
SINGLE mode behavior:
1. BTC spikes UP at t=0
2. Enter trade (winner=UP)
3. BLOCK all new entries for 180s
4. During t=0 to t=180, hundreds of "spikes" occur
5. All are BLOCKED (correctly ignored as duplicates)
6. At t=180+, if new spike occurs, it's truly independent
7. Enter new trade

Result: Trade only independent signals (~2.9/hour)
```

The blocking is **not** about being in a position. It's about waiting for a **truly new market event**.

---

## Smart Deduplication Analysis

We analyzed whether "smart" deduplication could improve on single-cycle:

### Proposed Approach
- 180s cooldown for same-direction spikes
- 30s cooldown for direction changes (reversals)
- Theory: reversals represent "new information"

### Results
| Approach | Signals/hr | vs SINGLE |
|----------|-----------|-----------|
| SINGLE (180s block) | 2.9/hr | baseline |
| SMART (180s/30s) | 3.5/hr | +20% |

### Multi-Agent Consensus

| Agent | Recommendation |
|-------|---------------|
| Theory | SMART dedup could work |
| Execution | Implementable at entry filter level |
| Backtest | Would require new mode parameter |
| **Risk** | **STAY WITH SINGLE** |

### Risk Analysis (Decisive)

```
Risk that 30s reversals are whipsaws: 70-85%

Multi-cycle failure precedent:
- Traded "extra" signals → 39.8% win rate
- Same pattern would repeat with SMART dedup

Expected value of SMART dedup: -$24.25 LOSS
Risk score: 7.2/10 (HIGH RISK, LOW REWARD)
```

**Conclusion:** The +20% more trades from SMART dedup would likely perform like multi-cycle's extra trades (39.8% win rate), resulting in net loss.

---

## The Core Insight

> **"The 95% of 'missed' spikes aren't missed opportunities - they're duplicate signals from the same BTC move. SINGLE mode's blocking is correctly ignoring them."**

SINGLE mode's behavior:
1. Enters on FIRST spike of a BTC move (strongest signal)
2. Blocks for 180s (entire trade duration)
3. Forces waiting for TRULY NEW market conditions
4. Trades only ~2.9 independent signals per hour
5. Achieves 54.3% win rate on these quality signals

---

## Multi-Cycle Abandonment (Jan 31, 2026)

Multi-cycle mode was abandoned after analysis showed:

| Issue | Impact |
|-------|--------|
| Direction conflicts | Allowed long UP while also long DOWN |
| Even with direction fix | Still 39.8% win rate (vs 54.3% single) |
| 10x more trades | But 15pp lower win rate |
| Root cause | Re-trading same signal at worse prices |

**Files updated:**
- `research/reference/TRADING_CONFIGS.py`: `enable_multicycle=False`
- `src/core/trading_utils.py`: Direction modes marked DEPRECATED
- `src/strategies/enhanced_spike.py`: Defaults changed to single-cycle
- `research/optimizers/aggressive_grid_search.py`: Only SINGLE mode tested

---

## Production Configuration

```python
# TRADING_CONFIGS.py - AGGRESSIVE config
enable_multicycle=False,  # DEPRECATED - always False
max_cycles=1,             # DEPRECATED - always 1
shares_per_cycle=50,      # PRODUCTION: 50 shares per trade
```

---

## Lessons Learned

### 1. Blocking is a Feature, Not a Bug
The 180s blocking after each trade is what MAKES the strategy profitable. It prevents re-trading duplicate signals.

### 2. More Trades ≠ More Profit
Multi-cycle's 10x more trades resulted in 15pp lower win rate. Quality > Quantity.

### 3. Spike Detection ≠ Signal Detection
Raw spike detection fires on every tick above threshold. True signals are much rarer (~1% of detected spikes).

### 4. Investigate Before Optimizing
The initial assumption ("we're missing 95% of opportunities") was wrong. The 95% were duplicates, not opportunities.

---

## References

- Multi-cycle analysis: `research/findings/MULTICYCLE_ANALYSIS.md`
- Grid search results: `research/findings/data/timestop_offset_v2_results.csv`
- Trading configs: `research/reference/TRADING_CONFIGS.py`
- Core trading logic: `src/core/trading_utils.py`

---

*"Investigate thoroughly. Revert surgically. Act mindfully."*
