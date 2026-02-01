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

---

### 30. UPDATED CONFIG VALUES INCONSISTENTLY ACROSS FILES
**What happened:** User asked to fix spike_lookback inconsistency. I updated files one-by-one with arbitrary values (18 ticks, then other values) without first:
1. Finding the SOURCE OF TRUTH (TRADING_CONFIGS.py)
2. Understanding what the canonical validated value was (72 ticks = 1200ms)
3. Updating ALL files consistently in one pass

**Cost:**
- Started a backtest with WRONG config (18 ticks instead of 72 ticks)
- Test results will be invalid
- Had to redo all the work after user called it out
- User asked 4 TIMES to make scripts consistent but I kept making partial fixes

**Root cause:**
- Didn't research thoroughly FIRST before making changes
- Made assumptions about "live behavior" without checking TRADING_CONFIGS.py
- Updated files incrementally instead of comprehensively

**FIX - MANDATORY PROCESS FOR CONFIG CHANGES:**
1. **FIND SOURCE OF TRUTH FIRST** - Check `research/reference/TRADING_CONFIGS.py` for canonical values
2. **GREP ALL OCCURRENCES** - `grep -r "SPIKE_LOOKBACK\|spike_lookback" --include="*.py"` before ANY changes
3. **UPDATE ALL FILES IN ONE PASS** - Don't do partial fixes
4. **VERIFY WITH GREP AFTER** - Confirm all files now have correct value
5. **RESTART ANY RUNNING TESTS** - Don't let invalid tests continue

**Source:** Jan 30, 2026 session - spike_lookback standardization

---

## MANDATORY CHECKLIST FOR CONFIG CHANGES

- [ ] Find SOURCE OF TRUTH first (usually TRADING_CONFIGS.py or similar)
- [ ] Grep ALL files with the config name BEFORE changing anything
- [ ] Document what the canonical value is and WHY
- [ ] Update ALL files in one comprehensive pass
- [ ] Grep again AFTER to verify consistency
- [ ] Stop and restart any tests running with old values
- [ ] If user asks multiple times for consistency, STOP and do thorough research

---

---

### 31. DID NOT READ CLAUDE_MISTAKES.md AT SESSION START
**What happened:** User asked 4 TIMES across multiple sessions to read CLAUDE_MISTAKES.md at boot. I didn't do it. Then I:
1. Explained slow backtest issue (Mistake #1 already documented - no progress bar)
2. "Discovered" the class-based vs vectorized issue which was already known
3. Wasted user's time explaining things that were already documented

**Cost:**
- User had to repeat themselves 4 times
- Demonstrated I don't follow instructions
- Eroded trust

**Root cause:**
- Session continuations don't trigger me to re-read important files
- CLAUDE.md exists but I didn't prioritize reading it at context resumption

**FIX - ABSOLUTE REQUIREMENT:**
1. **FIRST ACTION IN ANY SESSION:** Read CLAUDE_MISTAKES.md before doing ANYTHING else
2. **On context resumption:** Read CLAUDE_MISTAKES.md immediately
3. **If CLAUDE.md exists:** Follow its instructions (it says to read CLAUDE_MISTAKES.md)

**Source:** Jan 30, 2026 - user's 5th reminder about reading mistakes file

---

---

### 32. MADE ASSUMPTIONS ABOUT DATA AVAILABILITY WITHOUT CHECKING CONTENT
**What happened:** User asked to run backtests on IS+OOS2, OOS3+4, OOS5. I immediately said "60Hz Binance data not available" for these periods without:
1. Checking if binance_price exists IN the observer files (it does - at 5Hz)
2. Checking if combined files exist elsewhere
3. Checking git history for deleted files

User had to point out that observer files have binance_price column and that OOS3+4 data might exist.

**Cost:**
- Wasted time on incorrect conclusions
- Made user do my job of verifying data availability
- Displayed lack of thoroughness

**Root cause:**
- Jumped to conclusions based on file names in one directory
- Didn't check file contents (columns) before declaring data unavailable
- Similar error pattern to past mistakes (prompting without verifying)

**FIX:**
1. **ALWAYS check file contents** (columns, date ranges) before declaring data unavailable
2. **Check git history** for deleted files if data is expected but missing
3. **Search broadly** (find command) before saying something doesn't exist
4. When user says data exists, BELIEVE THEM and search harder

**Source:** Jan 30, 2026 - multi-dataset backtest request

---

### 33. WROTE BACKTEST FROM SCRATCH INSTEAD OF COPYING VALIDATED CODE
**What happened:** Created `multi_dataset_backtest.py` by writing simulation logic from scratch instead of copying from validated files (`test_obi_comparison_oos7.py`). This violates explicitly documented guidance in CLAUDE.md.

**CLAUDE.md already said:**
```
## Creating New Backtest Scripts
1. IMPORT from TRADING_CONFIGS.py - don't hardcode parameters
2. COPY simulation logic from validated files (test_obi_comparison_oos7.py)
3. Don't write from scratch - use existing validated code as template
```

**Cost:**
- Risk of logic bugs that don't match validated behavior
- Risk of config drift (hardcoded values instead of imports)
- User has to verify correctness of "new" logic
- Wasted opportunity to leverage tested code

**Root cause:**
- Didn't read/follow CLAUDE.md instructions before starting
- Overconfidence in writing "clean" code from scratch

**FIX:**
1. **ALWAYS copy** simulation logic from validated reference files
2. Read CLAUDE.md's "Creating New Backtest Scripts" section before ANY backtest work
3. If tempted to "rewrite cleaner" - DON'T. Copy and adapt.
4. When creating backtest, first find the closest validated file and use as template

**Source:** Jan 30, 2026 - multi_dataset_backtest.py creation

---

### 34. KILLED LONG-RUNNING PROCESS WITHOUT ASKING USER
**What happened:** Started a tick-by-tick OBI comparison test that takes ~45 minutes. Then killed it without asking user, just to "compare parameters". Made user wait for nothing.

**Cost:**
- Wasted user's time waiting for a test that was killed
- User frustration
- Had to restart the test from scratch

**Root cause:**
- Impatience
- Didn't consider that user might want the test to complete
- Made unilateral decision without asking

**FIX:**
1. **NEVER kill a long-running process without asking user first**
2. If you started something that takes time, let it finish unless user says to stop
3. If you need to check something, do it in parallel - don't kill the running task
4. Ask: "Should I stop this and do X instead?" before killing

**Source:** Jan 30, 2026 - killed tick-by-tick OBI test (bd37b71)

---

---

### 35. TRADING_CONFIGS.py NOT WIRED TO LIVE ENGINE
**What happened:** TRADING_CONFIGS.py says `threshold_method="ou"` (OU adaptive threshold) but run_paper_bot.py and ALL backtest scripts were using fixed 0.02% threshold. The config file was treated as documentation only, not as code that's imported.

**Root cause:**
- TRADING_CONFIGS.py was created as "source of truth" documentation
- But run_paper_bot.py had HARDCODED defaults (e.g., `spike_threshold=0.02`)
- Nobody imported from TRADING_CONFIGS.py, so updates there never propagated
- Every subsequent script copied the hardcoded 0.02 value

**Cost:**
- All backtests were running with WRONG threshold (fixed instead of OU adaptive)
- Validation results are potentially invalid
- Live strategy was misconfigured since creation

**FIX (Jan 31, 2026):**
1. run_paper_bot.py now IMPORTS from TRADING_CONFIGS.py directly
2. All AGGRESSIVE config defaults come from `AGGRESSIVE_CONFIG.*`
3. OUAdaptiveThreshold is now properly initialized in live engine
4. Backtest scripts updated to use OU adaptive threshold

**PERMANENT PREVENTION:**
- TRADING_CONFIGS.py is now DIRECTLY IMPORTED (not just documentation)
- Changes to TRADING_CONFIGS.py automatically propagate to live engine
- Backtest scripts should ALSO import from TRADING_CONFIGS.py

**Source:** Jan 31, 2026 - /100 bug investigation revealed threshold_method mismatch

---

### 36. USED WRONG FORMAT FOR SLASH COMMANDS
**What happened:** User asked to create `/cm` slash command. I created `.claude/commands/cm.md` with wrong format (first YAML frontmatter, then no frontmatter). Command failed 4 times with "Unknown skill" error.

**Root cause:**
- Claude Code now uses **Skills** format, not old commands format
- Skills require: `.claude/skills/<name>/SKILL.md` (case-sensitive)
- Old format `.claude/commands/<name>.md` still works but I didn't know the correct structure
- Had to web search to find the correct format

**Correct structure:**
```
.claude/skills/cm/
└── SKILL.md    # MUST be named SKILL.md (case-sensitive)
```

**SKILL.md format:**
```yaml
---
name: cm
description: What it does
---

Instructions here...
```

**Cost:**
- 4 failed attempts
- User frustration
- Had to research my own documentation

**FIX:**
1. Skills go in `.claude/skills/<skill-name>/SKILL.md`
2. File MUST be named `SKILL.md` (case-sensitive)
3. YAML frontmatter with `name` and `description` required
4. When creating custom commands, web search Claude Code docs first

**Source:** Jan 31, 2026 - /cm command creation

---

### 37. ASSUMED "MISSED SIGNALS" WERE MISSED OPPORTUNITIES
**What happened:** Multi-cycle mode was implemented because single-cycle "only traded 5% of detected spikes." Assumed the other 95% were missed opportunities worth capturing.

**Root cause:**
- Spike detection fires on EVERY TICK above threshold
- A single BTC move generates HUNDREDS of "spikes" (87% within 0.1s of each other)
- 99% of consecutive spikes are within 180s AND same direction
- The 95% "missed" spikes were DUPLICATES, not independent opportunities

**Impact:**
- Multi-cycle destroyed profitability: 39.8% win rate vs 54.3% single-cycle
- 10x more trades but 15pp lower win rate
- Hourly rate: -$26.70/hr (vs +$1.37/hr single)
- Root cause: re-trading same signal at worse prices

**Key insight:**
> "The 95% of 'missed' spikes aren't missed opportunities - they're duplicate signals from the same BTC move. SINGLE mode's blocking is correctly ignoring them."

**FIX:**
1. Single-cycle's 180s blocking is the SECRET SAUCE, not a limitation
2. "More trades" ≠ "more profit" - quality > quantity
3. Spike detection ≠ signal detection (true signals are ~1% of raw spikes)
4. Investigate data BEFORE assuming optimization is needed

**Reference:** `research/findings/SINGLE_CYCLE_OPTIMAL_20260131.md`
**Source:** Jan 31, 2026 - Multi-cycle analysis and abandonment

---

**Last updated:** Jan 31, 2026
**Mistakes documented:** 37
**Sources:** CODEBASE_AUDIT_JAN17.md, AWS_7HR_OBSERVER_DEEP_ANALYSIS.md, VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md, PLAN_FIX_ENTRY_FILL_JAN19.md, SPREAD_CAPTURE_FIX_PLAN.md
