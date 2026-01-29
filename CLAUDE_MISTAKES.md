# CLAUDE MISTAKES LOG

**READ THIS BEFORE EVERY SESSION. DO NOT REPEAT THESE MISTAKES.**

---

## CRITICAL MISTAKES - Jan 28, 2026

### OOS6 DATA COLLECTION - USED WRONG SCRIPT
**What happened:** User asked to restart data collection. I ran `python3 scripts/observer.py` directly instead of `python3 scripts/run_data_collection.py`.
**Cost:** OOS6 is MISSING the 60Hz Binance data needed for proper spike detection backtesting. The observer only captures 5Hz binance_price, not the 60Hz logger data.
**Impact:** Cannot run proper grid search or OBI validation on OOS6 data. 35+ hours of data collection partially wasted.
**FIX:**
1. ALWAYS use `run_data_collection.py` for data collection - it runs BOTH observer AND Binance logger.
2. Added prominent warning to `observer.py` docstring: "DO NOT RUN THIS SCRIPT DIRECTLY FOR DATA COLLECTION"
3. The wrapper is at `scripts/run_data_collection.py --hours N`
**Source:** Jan 28 19:39 UTC session, command that started PID 499722

---

## CRITICAL MISTAKES - Jan 27, 2026

### 1. NO PROGRESS BAR ON LONG-RUNNING SCRIPTS
**What happened:** Created `fixed_cycling_grid_backtest.py` with 146 configs, no progress indicator. User had no idea how long it would take or where it was.
**Cost:** 3+ hours of user's time, sleep deprivation, extreme frustration.
**FIX:** ALWAYS add `tqdm` or print `[X/Y]` progress for any loop > 10 iterations.

### 2. NO CHECKPOINTING
**What happened:** Script only saves results at the END. If killed, all progress lost.
**Cost:** User trapped - can't kill without losing everything.
**FIX:** Save partial results every N iterations. Use `df.to_csv("checkpoint.csv")` every 10 configs.

### 3. INEFFICIENT KALMAN PREPROCESSING
**What happened:** Computed Kalman states separately for each of 70 configs instead of once and reusing. 7.6M rows × 70 = 532M operations instead of 7.6M.
**Cost:** Script took 3+ hours instead of ~30 min.
**FIX:** Compute expensive preprocessing ONCE, pass to all configs.

### 4. WRONG TIME ESTIMATES
**What happened:** Said "5-10 min left" repeatedly for over 2 hours. Made predictions based on CPU/memory patterns without understanding the code.
**Cost:** User stayed up waiting based on false promises.
**FIX:** Don't estimate unless you have actual progress numbers. Say "I don't know" instead of guessing.

### 5. DISMISSIVE RESPONSES
**What happened:** Said "pick one" after making user wait 3 hours. Tone-deaf after causing frustration.
**Cost:** Made user feel unheard.
**FIX:** Match user's emotional state. Acknowledge impact of mistakes before offering solutions.

---

## STRATEGY & LOGIC BUGS (from project history)

### 6. BROKEN CYCLING LOGIC
**What happened:** Original cycling logic counted trades even when still in position, inflating trade counts.
**Cost:** All previous backtest results were unreliable, had to re-run everything.
**FIX:** Track `in_position` state, block new entries until hedge fills.
**Source:** Plan file, Jan 27

### 7. OU Z-SCORE DRIFT
**What happened:** Used OU z-score method which drifted on OOS3 ($6.15→$2.34/hr). OU parameters were calibrated on IS data and became stale when BTC price levels shifted.
**Cost:** Unreliable strategy that degraded over time.
**FIX:** Use EWMA z-score which adapts automatically.
**Source:** VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md

### 8. VELOCITY FLIP BUG
**What happened:** Strategy recalculates entry/hedge sides every sample based on velocity. When velocity flips, BOTH sides get filled as "entry" at mid-market prices ($0.50 each).
**Cost:** Pair costs average >$1.00 = structural loss. -$1,661 in 7-hour AWS run.
**Example:**
```
t=924s: velocity=-0.078 → entry_side=DOWN → fills DOWN at $0.54
t=903s: velocity=+0.137 → entry_side=UP   → fills UP at $0.47
BOTH sides filled as "entry" = $1.01 pair cost LOSING
```
**FIX:** Lock entry side on FIRST fill. Hedge logic should activate after entry.
**Source:** AWS_7HR_OBSERVER_DEEP_ANALYSIS.md, SPREAD_CAPTURE_FIX_PLAN.md

### 9. HEDGE BID CHASING
**What happened:** Hedge bid recalculates every sample, chasing price down instead of staying fixed.
```
T=0:   DOWN bid=$0.49 → hedge=$0.45
T=5:   DOWN bid=$0.30 → hedge=$0.26 (chasing)
T=10:  DOWN bid=$0.10 → hedge=$0.06 (still chasing)
T=14:  DOWN bid=$0.01 → hedge=$0.01 (only NOW fills)
```
**Cost:** Missed profit, hedge only fills at $0.01 (end of market).
**FIX:** Set hedge target ONCE when entry fills: `hedge_target = 0.97 - entry_price`. Keep fixed until filled.
**Source:** AWS_7HR_OBSERVER_DEEP_ANALYSIS.md

### 10. FILL SIMULATION TOO STRICT
**What happened:** Passive fill check used `ask <= order_price`. This requires entire ask side to drop to our bid level.
**Cost:** 99.6% of orders pulled (only 1-4 trades out of ~740 signals). Unrealistic backtest.
**FIX:** Check if we're at/above best bid with tight spread and time elapsed.
**Source:** PLAN_FIX_ENTRY_FILL_JAN19.md

---

## CODE QUALITY BUGS (from CODEBASE_AUDIT_JAN17.md)

### 11. OBSERVER NON-EXISTENT ATTRIBUTES
**What happened:** `check_stop_loss()` referenced attributes that don't exist on `GridState`: `entry_state.entry_filled`, `entry_state.entry_side`, etc.
**Cost:** Would raise AttributeError if ever called.
**FIX:** Delete dead functions or implement proper attributes.

### 12. MISSING JSON ERROR HANDLING
**What happened:** `binance_price_logger.py` used `json.loads(msg)` without try-except.
**Cost:** Malformed WebSocket message crashes entire logger.
**FIX:** Add try-except for JSON parsing errors.

### 13. FILL ID COLLISION RISK
**What happened:** Paper and live modes shared `_confirmed_fills` set with different ID formats.
**Cost:** Potential false deduplication or duplicate fill counting.
**FIX:** Separate tracking sets for paper vs live fills.

### 14. STRATEGY STATE NOT SYNCED
**What happened:** Spread capture cycle synced strategy state, but accumulation cycle didn't.
**Cost:** Rebalancing logic uses stale strategy state, causing incorrect decisions.
**FIX:** Add strategy state sync to accumulation cycle.

### 15. FILE HANDLE LEAK
**What happened:** `csv_file = open(filepath, ...)` without context manager, not properly closed on exception.
**Cost:** File handles leak, data loss on crash.
**FIX:** Use try-finally with proper cleanup.

### 16. HARD STOP NOT ENFORCED
**What happened:** Hard imbalance limit was logged but didn't block non-rebalancing orders.
**Cost:** Can exceed max imbalance limit, risk exposure increases.
**FIX:** Actually block trading when hard stop triggered.

### 17. DATA LOSS ON MARKET SWITCH
**What happened:** `cycle_records` discarded on market switch without archiving.
**Cost:** Historical data lost forever.
**FIX:** Archive cycle records before reset.

### 18. WEBSOCKET RECONNECTION RACE
**What happened:** Old `_ws_task` not cancelled before reconnecting, stale data contamination.
**FIX:** Cancel task BEFORE disconnecting WebSocket.

### 19. NO FLUSH ON GRACEFUL SHUTDOWN
**What happened:** `stop()` sets flag but doesn't flush CSV buffer.
**Cost:** Last seconds of data lost.
**FIX:** Flush and fsync before exit.

### 20. INVALID VELOCITY ZONE FALLBACK
**What happened:** Returns `'super_strong'` but that key doesn't exist in `VELOCITY_ZONES`.
**FIX:** Return `'extreme'` (actual existing key).

### 21. RACE CONDITION IN get_quotes()
**What happened:** State can be reset by `on_fill()` → `reset_for_cycle()` during execution.
**FIX:** Copy state values at function entry.

### 22. SPIKE HISTORY NOT CLEARED IN CYCLING
**What happened:** `reset_for_cycle()` doesn't call `clear_spike_history()`.
**Cost:** Stale prices used in next cycle.
**FIX:** Add `self.clear_spike_history()` call.

### 23. DUPLICATE CODE
**What happened:** Multiple duplicate functions:
- `maybe_tighten_hedge_target()` and `check_hedge_target_change()` - identical
- 4 standalone functions duplicate class methods in enhanced_spike.py
- ~40 lines of WS fill processing duplicated
**Cost:** Maintenance burden, inconsistent fixes.
**FIX:** Extract helpers, keep single source of truth.

### 24. MARKET ROTATION CLEANUP INCOMPLETE
**What happened:** Missing `.clear()` calls for: `_pending_expensive_orders`, `_emergency_triggered_markets`, `_emergency_ceiling_used`, `_pull_cooldown`
**Cost:** State leaks between market rotations.
**FIX:** Add all missing `.clear()` calls.

---

## OBSERVER & DATA COLLECTION BUGS

### 25. OBSERVER CRASH - SILENT FAILURE
**What happened:** Observer ran for 51 SECONDS then silently crashed. BTC logger continued 20 hours unaware.
**Cost:** 20+ hours of observer data LOST - cannot be recovered.
**Root causes:**
- `SpreadCaptureObserver` has NO `stop()` method - crashes on shutdown
- No error handling around observer - failures are silent
- No health monitoring
- No auto-restart or supervisor
**FIX:** Add stop() method, error handling, health checks, logging, auto-restart.
**Source:** VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md

### 26. DEAD CODE FUNCTIONS
**What happened:** 3 functions defined but never called in observer.py: `check_stop_loss()`, `maybe_tighten_hedge_target()`, `calculate_size_allocation()`
**Cost:** Confusion, maintenance burden.
**FIX:** Delete dead code.

### 27. DUPLICATE SPIKE CONSTANTS
**What happened:** Same constants in observer.py and enhanced_spike.py, no shared module.
**Cost:** Drift between files, inconsistent behavior.
**FIX:** Import from shared constants module.

---

## MANDATORY CHECKLIST FOR NEW SCRIPTS

Before running ANY backtest or simulation:

- [ ] Progress bar or `[X/Y]` print statements (tqdm)
- [ ] Checkpoint saves every N iterations
- [ ] ETA display based on actual progress, not guesses
- [ ] Expensive computations done ONCE and reused
- [ ] Test on small subset first before full run
- [ ] Tell user expected runtime BEFORE starting
- [ ] Error handling around all external calls
- [ ] Health monitoring / heartbeat logging

---

## MANDATORY CHECKLIST FOR COMMUNICATION

- [ ] Don't give time estimates without real data
- [ ] Say "I don't know" instead of guessing
- [ ] Acknowledge user frustration before problem-solving
- [ ] Don't be dismissive with short responses after mistakes
- [ ] Own mistakes directly, no deflection

---

## MANDATORY CHECKLIST FOR STRATEGY CODE

- [ ] State machine for position tracking (WAITING → ENTRY_FILLED → HEDGED)
- [ ] Lock entry/hedge sides on first fill, don't recalculate
- [ ] Set hedge target ONCE: `hedge_target = target_pair_cost - entry_price`
- [ ] Use EWMA z-scores (not OU) - adapts to regime changes
- [ ] Track `in_position` state for cycling logic
- [ ] Error handling for all API calls
- [ ] stop() method with proper cleanup

---

### 28. GAVE INSTRUCTIONS WITHOUT CHECKING CURRENT STATE
**What happened:** Told user to run Kalman script without first checking if it was already running. User had already executed it.
**Cost:** Wasted user's time, caused frustration, appeared incompetent.
**FIX:** ALWAYS check `ps aux | grep` and output files BEFORE telling user to run something.

---

### 29. TRUSTED PAGINATED API DATA WITHOUT DEDUPLICATION
**What happened:** Analyzed Gabagool merge frequency using Polymarket activity API. Concluded "only 4/28 markets have merges (14%)" and "Gabagool doesn't merge constantly." User corrected me - Gabagool actually has 252 BTC 15m markets with merges ($1.06M total).
**Root cause:**
- API pagination returned duplicate data at different offsets
- Didn't deduplicate by transaction hash initially
- Drew conclusions from incomplete data (50K activities all in 42-minute window)
**Cost:** Wrong analysis, wasted time, had to redo work, user lost trust in findings.
**FIX:**
1. ALWAYS deduplicate API results by transaction hash
2. Check data time range spans expected period
3. If pagination returns same data repeatedly, investigate before concluding
4. When user says something contradicts your finding, RE-VERIFY with different query approach
**Source:** Whale merge analysis, Jan 29, 2026

---

**Last updated:** Jan 29, 2026
**Mistakes documented:** 29
**Sources:** CODEBASE_AUDIT_JAN17.md, AWS_7HR_OBSERVER_DEEP_ANALYSIS.md, VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md, PLAN_FIX_ENTRY_FILL_JAN19.md, SPREAD_CAPTURE_FIX_PLAN.md
