# Backtest vs Live Trading Discrepancy Analysis - Feb 5, 2026

## Problem Statement

**Critical Issue:** Backtest shows +$10.32/hr profit but paper trading lost money on the SAME Feb 5, 2026 data (OOS10.1).

| Metric | Backtest (10sh, 0.80) | Paper Trading |
|--------|----------------------|---------------|
| **Hourly Rate** | **+$10.32/hr** | **LOSS (~-$32)** |
| **Trades/Cycles** | 346 | 144 |
| **Win Rate** | **70.0%** | **25.0%** |
| **Avg Pair Cost** | **$0.9845** | **$1.0194** |
| **Direction Accuracy** | 38.4% | - |

This represents a **fundamental mismatch** between backtest simulation and live execution that invalidates all previous backtest results.

---

## Root Cause Analysis (3 Agents)

### 1. SPIKE DETECTION MISMATCH (Agent: aa676d8)

**Source Files:**
- Backtest: `/research/backtests/aggressive_main_backtest.py` (lines 339-420)
- Live: `/src/strategies/enhanced_spike.py` (lines 983-1040)
- BinanceClient: `/src/api/binance_client.py`

**Finding:** Backtest and live compute EWMA differently due to deduplication:

| Aspect | Backtest | Live |
|--------|----------|------|
| **Deduplication** | YES - removes 72% duplicate ticks at same timestamp | NO - processes every tick |
| **Input ticks** | 975,886 unique timestamps | 3.5M+ raw ticks |
| **EWMA behavior** | Converges faster, gaps create more deviation | More frequent but smaller updates |
| **Spikes produced** | MORE (faster deviation from EWMA) | FEWER (duplicate ticks don't move EWMA) |

**Result:** Backtest generates 346 trades, paper trading only did 144 cycles - a **2.4x difference**.

**Observer Data Anomaly:**
- `spike_detected` column: ALL FALSE (47,192 rows)
- `spike_magnitude`: ALL 0.0
- This suggests live spike detection was NOT recording to observer during Feb 5 session

---

### 2. PAIR COST DISCREPANCY - NOT A BUG (Agent: af045b1)

**CORRECTION (Feb 5, 2026):** The fill simulation logic is CORRECT.

When `loser_ask <= loser_target` (market ask drops to our bid), we fill at **OUR BID price** (loser_target). This is correct limit order behavior - you fill at your price, not the market price.

**The REAL issue causing pair cost difference:**
- Backtest: Gets 77 passive fills at `loser_target` (our bid price)
- Paper trading: May have different fill rates due to timing/execution

**Pair cost difference ($0.9845 vs $1.0194) likely caused by:**
1. Different trade selection (346 vs 144 trades)
2. Different market conditions when trades execute
3. Live execution delays causing worse fills

---

### 3. DOCUMENTED MISTAKES RELEVANT TO THIS ISSUE (Agent: a88f893)

**Source File:** `/CLAUDE_MISTAKES.md`

**Previously Fixed Issues:**
1. **Mistake #41 (Feb 2):** Hedge bid formula used `loser_ask` instead of `1.0 - winner_entry` - FIXED
2. **Mistake #42 (Feb 2):** Velocity filter boundary mismatch (`<` vs `<=`) - FIXED
3. **Mistake #43 (Jan 31):** Z-score filter missing from new backtests - FIXED
4. **Mistake #40 (Feb 2):** Time-stop early return guard blocked exits - FIXED
5. **Mistake #51 (Feb 4):** Event-driven spike bypassed time check - FIXED

**Expected Performance (validated):**
- +$15.35/hr with breakeven exit
- Sharpe: 1.03
- Win rate: 46.1%

**Key Insight:** Despite all these fixes, Feb 5 paper trading STILL shows massive discrepancy, indicating MORE unfixed issues.

---

## Issues Status

### Issue 1: EWMA Deduplication Mismatch (PRIMARY CAUSE) ✅ FIXED
**Files:**
- Backtest: `/research/backtests/aggressive_main_backtest.py` lines 339-420 (deduplicates by timestamp_ms)
- Live: `/src/api/binance_client.py` lines 155-165, 232-256

**Fix Applied (Feb 5, 2026):**
Added timestamp-based deduplication to BinanceClient to match backtest behavior:
- Added `_last_ewma_timestamp_ms` field
- Changed from consecutive price deduplication to timestamp_ms deduplication
- EWMA now only updates once per unique millisecond timestamp
- This matches backtest's `df.drop_duplicates(subset=['timestamp_ms'])` behavior exactly

### Issue 3: Observer Not Recording Spikes
**File:** `/scripts/observer.py` (spike detection output)
**Evidence:** Feb 5 observer data has all spike fields = 0/False/NaN
**Fix Required:** Ensure spike detection state is recorded during paper trading

---

## Immediate Action Plan

### Task 1: Fix Backtest Fill Price Bug
**Priority:** CRITICAL
**File:** `/research/backtests/aggressive_main_backtest.py`
**Change:** Line ~520: `loser_fill = loser_target` → `loser_fill = loser_ask`

### Task 2: Verify Deduplication Alignment
**Priority:** HIGH
**Files:** Backtest precompute vs BinanceClient EWMA
**Decision needed:** Should BOTH deduplicate or NEITHER?

### Task 3: Re-run Backtest on OOS10.1 After Fixes
**Priority:** HIGH
**Expected outcome:** Backtest results should be CLOSER to paper trading reality

### Task 4: Add Spike Recording to Observer
**Priority:** MEDIUM
**File:** `/scripts/observer.py`
**Change:** Ensure spike_detected, spike_magnitude, spike_direction are populated

### Task 5: Validate All Previous Backtest Results
**Priority:** HIGH
**Impact:** If fill simulation was wrong, ALL historical backtest results are invalid

---

## Data Files Reference

| File | Description |
|------|-------------|
| `/paper_trades_aggressive_2026-02-05.csv` | Paper trading results (144 cycles, $1.02 avg pair) |
| `/research/observer/grid_obs_20260205.csv` | Observer data (47K rows, spike fields blank) |
| `/research/binance_hf/btc_prices_20260204_190733.csv` | 60Hz BTC prices (8.5M rows) |
| `/research/observer/resolutions_20260205.csv` | Market resolutions (11 markets: 8 DOWN, 3 UP) |

---

## Questions to Answer

1. **Why does backtest show 346 trades but paper only did 144?**
   - Answer: Deduplication mismatch in EWMA computation

2. **Why is backtest pair cost $0.98 but paper $1.02?**
   - Answer: Backtest fills at `loser_target` (bid) instead of `loser_ask` (market)

3. **Can we trust ANY previous backtest results?**
   - Answer: NO - fill simulation bug affects all historical results

4. **What should the ACTUAL expected performance be?**
   - Answer: Unknown until backtest is fixed and re-run

---

## Next Steps

1. [ ] Fix backtest fill price bug (loser_target → loser_ask)
2. [ ] Decide on deduplication strategy (both or neither)
3. [ ] Re-run backtest on OOS10.1
4. [ ] Re-run backtest on ALL datasets with corrected simulation
5. [ ] Compare new backtest results to paper trading
6. [ ] If still mismatch, investigate deeper (slippage, timing, etc.)

---

**Created:** Feb 5, 2026
**Status:** IN PROGRESS
**Agent IDs for context:** aa676d8, af045b1, a88f893
