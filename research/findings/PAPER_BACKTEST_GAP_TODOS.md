# Paper Trading vs Backtest Gap - Comprehensive Analysis

**Created:** Feb 5, 2026
**Last Updated:** Feb 5, 2026

---

## Executive Summary

| Metric | Paper Trading | Backtest (Before Delay Fix) | Backtest (After Delay Fix) |
|--------|--------------|----------------------------|---------------------------|
| Cycles | 128 | 241-369 | 235 (OOS10.2) |
| Avg Pair Cost | $1.0008 | $0.9926 | ~$1.01 |
| Net PnL | -$0.98 | +$18.20/hr | **-$25.45/hr** |

### ⚠️ CRITICAL FINDING (Feb 5, 2026)
With realistic 542ms entry delay simulated, the strategy is **UNPROFITABLE**:
- OOS10.2: **-$25.45/hr**, 235 trades, 45.5% direction accuracy
- Overall: **-$1.33/hr** across all datasets, 5859 trades, 41.8% win rate

The previous +$18.20/hr result was unrealistic due to instant fills assumption.

---

## ROOT CAUSES IDENTIFIED (Priority Order)

### 1. BACKTEST HAS 0ms ENTRY DELAY (FIXED ✅)
**Status:** ✅ FIXED Feb 5, 2026

**Problem:**
```
BACKTEST (before):  Spike detected → INSTANT fill at current ask
PAPER:              Spike detected → wait 542ms → fill at WHATEVER ask is after delay
```

**Fix Applied:** `research/backtests/aggressive_main_backtest.py` lines 90-102, 799-819
```python
ENTRY_FILL_DELAY_MS = 542  # Total taker delay (500ms exchange + 42ms network)
ENTRY_DELAY_ROWS = 3       # 542ms / 200ms = 2.7 → round to 3 rows (conservative)

# In simulation loop:
delayed_obs_idx = min(obs_idx + ENTRY_DELAY_ROWS, len(mdf) - 1)
delayed_obs_row = mdf.iloc[delayed_obs_idx]
winner_entry = winner_ask_delayed  # USE DELAYED PRICE, not instant
```

**Result:** Strategy now shows NEGATIVE returns, matching paper trading behavior.

---

### 2. PASSIVE FILL PRICE LOGIC (VERIFIED ✅ - NOT A BUG)
**Status:** ✅ VERIFIED CORRECT Feb 5, 2026

**Original Concern:** Thought backtest used wrong fill price for hedge orders.

**Verification:** The logic is CORRECT for CLOB/limit order mechanics:
- **Passive fill (line 554):** `loser_fill = loser_target` (our bid price)
  - When our resting bid is hit by a seller, we fill at OUR limit price
  - This is correct limit order behavior
- **Breakeven exit (line 592):** `loser_fill = loser_ask` (market price)
  - Taking the market = fill at market ask (taker)
- **Time-stop exit (line 639):** `loser_fill = loser_ask` (market price)
  - Taking the market = fill at market ask (taker)

**Conclusion:** No bug here. Fill logic matches real trading mechanics.

---

### 3. EWMA DEDUPLICATION MISMATCH (FIXED ✅)
**Status:** ✅ FIXED Feb 5, 2026

**Problem:** Backtest deduplicated by `timestamp_ms`, live used price-based dedup.

**Fix Applied:** `src/api/binance_client.py` lines 235-256
- Added `_last_ewma_timestamp_ms` field
- EWMA only updates on unique timestamp_ms (matches backtest)

---

### 4. BREAKEVEN_MIN_HOLD_MS HARDCODE (FIXED ✅)
**Status:** ✅ FIXED Feb 5, 2026

**Problem:** Backtest hardcoded 2000ms instead of importing 10000ms from config.

**Fix Applied:** `research/backtests/aggressive_main_backtest.py` line 96
```python
BREAKEVEN_MIN_HOLD_MS = getattr(AGGRESSIVE_CONFIG, 'breakeven_min_hold_ms', 10000)
```

---

### 5. NETWORK LATENCY NOT CONFIGURED (FIXED ✅)
**Status:** ✅ FIXED Feb 5, 2026

**Problem:** Paper trading had `network_latency_ms = 0`, should be 42ms.

**Fix Applied:** `src/services/paper_trading.py` line 78
```python
network_latency_ms: float = 42.0  # AWS Ireland → Polymarket
```

---

### 6. BE/TIME-STOP LOCK BUG (PARTIALLY FIXED)
**Status:** ⚠️ IMPLEMENTED but needs verification

**The Bug:**
1. BE acquires lock on position (`_exited_positions.add()`)
2. BE callback fails/hangs (doesn't execute hedge)
3. Time-stop fires at 30s, sees lock, BLOCKED
4. Position stuck unhedged until resolution

**Fixes Implemented:**
- `force=True` parameter in `try_acquire_for_timestop()` - line 779
- `cleanup_after_cycle()` method added - line 825
- Called from cycle completion paths - lines 5899, 6029, 6236, 6431

**Verification Needed:**
- [ ] Check logs for `[TIMESTOP] Force override` messages
- [ ] Confirm no positions stuck unhedged
- [ ] Verify cleanup happens after every cycle

---

### 7. OBSERVER BUFFER BUG (FIXED ✅)
**Status:** ✅ FIXED Feb 5, 2026

**Problem:** Observer spike_detected was ALWAYS False (buffer 50 < required 73).

**Fix Applied:** `scripts/observer.py` lines 259-263
```python
spike_buffer_size = SPIKE_LOOKBACK + 10  # 82 (was 50)
```

**Note:** Observer uses FIXED lookback (not EWMA). This is intentional - observer runs at 5Hz and is for data collection only, not trading decisions.

---

### 8. CYCLE COUNT GAP (128 vs 241-369)
**Status:** ❓ NEEDS INVESTIGATION

**Possible Causes:**
1. **Entry fill delay** - 542ms delay means fewer opportunities
2. **Position locking** - Must wait for hedge before next entry (avg 27.7s)
3. **Rate limiting** - 500ms event loop vs backtest's continuous simulation
4. **Event-driven mode** - Implemented Feb 4, reduced latency from 5s to 500ms
5. **Filters** - Paper may have filters backtest doesn't simulate

**Investigation Steps:**
- [ ] Compare spike detection counts (backtest vs paper)
- [ ] Analyze time between paper trading cycles
- [ ] Check for blocked entries in logs

---

## UPDATED TODO LIST (Priority Order)

### ✅ COMPLETED

1. **Add entry fill delay to backtest** ✅
   - File: `aggressive_main_backtest.py` lines 90-102, 799-819
   - Simulates 542ms delay (3 rows in observer data)
   - Uses ask price AFTER delay, not at spike moment

2. **Re-run OOS10.2 backtest after fixes** ✅
   - Result: **-$25.45/hr** (vs +$18.20/hr before fix)
   - 235 trades, 45.5% direction accuracy
   - Strategy is UNPROFITABLE with realistic delays

3. **Verify passive fill price logic** ✅
   - Confirmed backtest logic is CORRECT
   - Passive fills use our bid (limit order mechanics)
   - Taker exits (BE/time-stop) use market ask

4. **EWMA deduplication mismatch** ✅
   - Fixed in `src/api/binance_client.py` lines 235-256

5. **BREAKEVEN_MIN_HOLD_MS hardcode** ✅
   - Fixed: now imports from TRADING_CONFIGS (10000ms)

6. **Network latency configured** ✅
   - Set to 42ms in paper_trading.py

7. **Observer buffer bug** ✅
   - Fixed: 50 → 82 (SPIKE_LOOKBACK + 10)

### ⚠️ NEEDS VERIFICATION

8. **BE/time-stop lock bug fixes**
   - Status: Code implemented, needs live verification
   - Check logs for `[TIMESTOP] Force override` messages
   - Confirm no positions stuck unhedged

### ❓ NEEDS INVESTIGATION

9. **Cycle count gap (128 vs 235)**
   - Paper trading: 128 cycles
   - Backtest with delay: 235 cycles
   - Still ~1.8x more in backtest - why?
   - Possible causes:
     - Position locking duration (avg 27.7s)
     - Rate limiting in live (500ms event loop)
     - Filters not simulated in backtest

### 📝 LOW PRIORITY

10. **Document all configurations**
    - AWS latency test results documented in this file
    - TRADING_CONFIGS.py comments could be updated

---

## Configuration Summary

### Paper Trading (Realistic)
```python
entry_fill_delay_ms = 500.0   # Polymarket taker delay
network_latency_ms = 42.0     # AWS Ireland → Polymarket
# Total taker delay: 542ms
```

### Backtest (NOW REALISTIC ✅)
```python
ENTRY_FILL_DELAY_MS = 542  # Total taker delay
ENTRY_DELAY_ROWS = 3       # 542ms / 200ms = 2.7 → 3 rows in observer data

# Entry uses price AFTER delay:
delayed_obs_idx = obs_idx + ENTRY_DELAY_ROWS
winner_entry = delayed_obs_row[f'{winner_side.lower()}_ask']
```

### Latency Test Results (Feb 5, 2026)
```
Binance WS:       107ms avg (price feed latency)
Polymarket REST:  42ms avg (order placement)
Total taker:      542ms (500ms exchange + 42ms network)
```

---

## Files Modified This Session

| File | Change | Status |
|------|--------|--------|
| `aggressive_main_backtest.py` | Import BREAKEVEN from config | ✅ Done |
| `aggressive_main_backtest.py` | Add 542ms entry delay simulation | ✅ Done |
| `src/services/paper_trading.py` | Set network_latency=42ms | ✅ Done |
| `src/api/binance_client.py` | Fix EWMA deduplication (timestamp_ms) | ✅ Done |
| `scripts/observer.py` | Fix buffer 50→82, add documentation | ✅ Done |
| `scripts/run_paper_bot.py` | BE/time-stop lock fixes (force=True, cleanup) | ✅ Done |
| `research/findings/PAPER_BACKTEST_GAP_TODOS.md` | This file | ✅ Updated |

---

## Next Steps

1. ✅ ~~Implement entry delay in backtest~~ **DONE**
2. ✅ ~~Run backtest on OOS10.2 with entry delay~~ **DONE - Shows -$25.45/hr**
3. ✅ ~~Compare new backtest results vs paper trading~~ **DONE - Both negative**
4. ⚠️ **Verify BE/time-stop fixes** with live session (check logs)
5. ❓ **Investigate cycle count gap** (128 vs 235) - why does backtest still trade more?

---

## ⚠️ STRATEGIC CONCLUSION

**The AGGRESSIVE strategy is UNPROFITABLE when simulated with realistic latency.**

### What Changed:
- Before: Instant fills → +$18.20/hr (unrealistic)
- After: 542ms delay → **-$25.45/hr** (realistic)

### Root Cause:
During the 542ms taker delay, prices move against us:
- Spike detected at ask = $0.60
- 542ms later, ask has moved to $0.63-0.65
- We pay 3-5 cents MORE per entry
- This erases the small edge and turns it negative

### Options Going Forward:
1. **Re-optimize parameters** with entry delay baked in (grid search with delay)
2. **Reduce entry delay** - explore passive entry (maker orders) instead of taker
3. **Different strategy** - the latency arbitrage window may be too small for this approach
4. **Accept the finding** - the strategy doesn't work with realistic assumptions

---

*The goal was to make backtest REALISTIC. Mission accomplished - it now matches paper trading (both lose money).*
