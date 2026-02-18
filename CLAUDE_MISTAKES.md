# CLAUDE MISTAKES LOG

**READ THIS BEFORE EVERY SESSION. DO NOT REPEAT THESE MISTAKES.**

---

## ⚠️ MANDATORY SELF-CHECK AT SESSION START

**Before doing ANY work, run this command:**

```
Use the Task tool with subagent_type=Explore to thoroughly read and internalize
every mistake in this file. Do not skim. Understand the ROOT CAUSE of each mistake
and how to avoid it. The user is DONE with repeated mistakes.
```

If you skip this step and repeat a documented mistake, you have failed.

---

## MANDATORY ANALYSIS METRICS

Whenever running backtest analysis, ALWAYS report:
- **Sharpe ratio** (> 1.0 minimum, > 1.5 strong)
- **Profitable market %** (> 50% minimum)
- **Worst single trade** (> -$10)
- **Worst single market**
- **Unhedged %** (< 20%)

Don't report $/hr alone - these metrics assess STABILITY for autonomous trading.

---

## BEFORE ANY SSH/RSYNC/DEPLOY COMMAND
1. Read deploy.sh for IP and key path
2. **IP: 54.170.244.221**
3. **Key: $HOME/Downloads/polymarket-key.pem**

## KILLING AWS FRONTEND
When user asks to kill the frontend, use **systemctl** not kill:
```bash
ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221 'sudo systemctl stop polymarket-bot'
```
**DO NOT** use `kill -9 <PID>` - the systemd service will auto-restart it.
Keep frontend dead until user explicitly asks to restart it.

---

## CRITICAL MISTAKES - Feb 4, 2026

### EVENT-DRIVEN SPIKE BYPASSED min_time_remaining CHECK
**What happened:** Event-driven spike detection (`_process_spike_signals`) didn't re-check `min_time_remaining` before executing queued signals. A signal could be queued when time_remaining=95s (passes SpikeEventHandler check at 90s) but executed when time_remaining=85s (after trading loop delay).
**Symptoms:** Trades entered in the last 90 seconds despite `min_time_remaining=90` config.
**Root cause:** `SpikeEventHandler.on_spike_detected()` checks time_remaining when queuing, but `_process_spike_signals()` didn't re-check before executing.
**Fix:** Added time_remaining re-check in `_process_spike_signals()` at line ~5107 before orderbook fetch.
**Lesson:** When implementing async queuing patterns, always re-validate time-sensitive conditions at execution time, not just queue time.

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

## MANDATORY CHECKLIST FOR LISTS AND TASK TRACKING

- [ ] When revising a list, review ALL items first (not just the one being changed)
- [ ] Count items before and after: "Original had N, revised has M - why?"
- [ ] If correcting one item, explicitly verify other items remain unchanged
- [ ] Don't let focused corrections cause collateral deletions

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

**Feb 4, 2026 UPDATE - REPEATED THIS MISTAKE:**
User asked about trades in market 1770200100. I said "no trades yet" without checking logs. If I had checked logs FIRST, I would have immediately seen the `await` bug causing all spike signals to fail.

**EXPANDED FIX:**
1. When something isn't working → **CHECK LOGS FIRST**, don't assume
2. When user reports an issue → **BELIEVE THEM** and investigate, don't assume they're wrong
3. The user is NOT talking out of their ass. If they say something is broken, IT IS BROKEN. Find out why.

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

### 37. MISSED DATASET IN PLAN IMPLEMENTATION
**What happened:** Plan explicitly said "Run on OOS7+OOS8" but I only implemented OOS7 in DATASETS. User had to catch the missing OOS8.

**Root cause:**
- Copied existing DATASETS structure without reading the plan carefully
- Plan specified OOS7+OOS8 but I didn't verify all datasets were included
- Didn't cross-check implementation against plan requirements

**Cost:**
- Had to run OOS8 separately after main run completed
- User caught the mistake, not me

**FIX:**
1. When implementing a plan, explicitly verify ALL specified items are included
2. Cross-check: if plan says "X + Y", ensure BOTH X AND Y are in the code
3. Read plan requirements BEFORE copying existing code structures

**Source:** Feb 1, 2026 - Loss mechanism grid search

---

### 38. DROPPED ITEMS FROM LIST WITHOUT CHECKING THOROUGHLY
**What happened:** Had 6-item auto-merge flaws list. User corrected one item (delay not required per docs). When I revised the list, I carelessly dropped the race condition flaw entirely - reducing from 5 valid items to 3.

**Root cause:**
- Focused narrowly on the correction (delay item)
- Didn't review the full list before presenting the revised version
- No systematic check: "did I keep all valid items?"

**Cost:**
- User had to catch the missing item
- Demonstrated carelessness
- Had to explain WHY I made the mistake (no good reason)

**FIX:**
1. When revising ANY list, review ALL items before finalizing
2. If correcting one item, explicitly verify other items remain
3. Before presenting revised list, count items: "Original had N, revised has M - why the difference?"
4. Don't let focused corrections cause collateral deletions

**Source:** Feb 1, 2026 - Auto-merge investigation fixes list

---

---

### 39. DID NOT CHECK CODEBASE FOR SSH/DEPLOY CONFIGURATION BEFORE DEBUGGING
**What happened:** SSH to AWS failed with "Permission denied (publickey)". I tried `~/.ssh/id_ed25519` multiple times, gave up, and told user "SSH is not working, can you try manually?" - when the deploy.sh script in the repo CLEARLY shows the correct key:

```bash
AWS_KEY="$HOME/Downloads/polymarket-key.pem"  # Line 11 of deploy.sh
```

**Root cause:**
- Assumed SSH key was at standard location (`~/.ssh/`)
- Didn't check deploy scripts or codebase for SSH configuration
- Kept retrying same wrong key instead of researching

**Cost:**
- Multiple failed SSH attempts
- Wasted user's time
- Made user point out the obvious (check our own code)
- Appeared incompetent

**FIX:**
1. When SSH/deploy fails, **FIRST** check the repo for deploy scripts: `find . -name "deploy*.sh"`
2. Grep for key paths: `grep -r "\.pem\|ssh.*-i\|AWS_KEY" --include="*.sh"`
3. Check common config locations: `.env`, `deploy/`, `scripts/`, project root
4. DON'T keep retrying the same failed approach - research first

**Source:** Feb 2, 2026 - Paper trading debugging session

---

### 40. ADDED EARLY-RETURN GUARD WITHOUT UNDERSTANDING FUNCTION'S FULL LOGIC FLOW
**What happened:** Positions were ending up unhedged at market resolution despite time-stop being configured (180s). User correctly identified: "our time stop should have triggered."

**Root cause:** The `min_time_remaining` check was added at the TOP of `get_quotes()` (line 1528) without understanding the function's full logic flow:

```python
def get_quotes(...):
    # Line 1528 - EARLY RETURN added here
    if time_remaining < self.min_time_remaining:
        return []  # ← Exits function immediately, no condition on position state

    # ... 200 lines of code ...

    # Line 1724 - TIME-STOP CHECK (never reached if above returns!)
    if s.first_fill_side is not None and elapsed >= self.time_stop_seconds:
        return [time_stop_hedge_order]  # ← Never executes when time < 240s
```

The guard was intended to block NEW ENTRIES when market is ending soon. But by placing it at the function's entry point with no condition check on position state, it blocked ALL code paths including:
- Time-stop hedges for existing positions (the critical bug)
- Stop-loss hedges
- Any other exit logic that needed to run

This is the classic mistake of adding code "in the first convenient spot" without tracing through all scenarios it affects.

**Cost:**
- Positions ending unhedged at resolution = direct losses on every affected market
- Multiple markets affected before bug was caught
- Time wasted debugging "why isn't time-stop working" when the answer was trivial: code never reached the time-stop check

**FIX (Feb 2, 2026):**
```python
# Only block NEW ENTRIES, not exits for existing positions
if time_remaining < self.min_time_remaining and s.first_fill_side is None:
    return []  # Now allows time-stop/stop-loss for existing positions
```

**PREVENTION:**
1. Before adding early-return guards, TRACE the entire function to understand what code paths they block
2. Early returns should explicitly check ALL conditions they might affect (e.g., "am I in a position?")
3. When adding "block entries when time is low", ask: "what about EXITS for existing positions?"
4. The fix was trivial (`and s.first_fill_side is None`) - the bug was not reading the function before modifying it

**Source:** Feb 2, 2026 - Paper trading time-stop bug investigation

---

---

### 41. HEDGE BID FORMULA USED WRONG BASE PRICE - FUNDAMENTAL BACKTEST/LIVE MISMATCH
**What happened:** Paper trading was consistently losing money while backtests showed profitability. User asked "why were backtests so profitable but paper trading so ass?"

**Root cause:** The hedge bid calculation in live code used a DIFFERENT formula than backtest:

**BACKTEST (correct):**
```python
loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
```

**LIVE (wrong):**
```python
loser_bid = min(loser_ask - expected_drop, max_loser)
```

The backtest uses `(1.0 - winner_entry)` = theoretical loser price (since UP + DOWN = $1.00 in binary market).
The live code used `loser_ask` = actual market ask, which includes spread/premium.

**Example:**
- Entry UP at $0.60
- Theoretical loser: $0.40 (1.0 - 0.60)
- Actual loser ask: $0.45 (5 cent premium)
- Backtest bids: $0.40 - drop = $0.32
- Live was bidding: $0.45 - drop = $0.37 (5 CENTS HIGHER!)

This caused live pair costs to be systematically WORSE than backtest predicted.

**Cost:**
- Every hedge was 3-5 cents more expensive than backtest assumed
- $-7.36 session loss vs profitable backtest expectations
- Fundamental mismatch making all backtests unreliable for live performance

**FIX (Feb 2, 2026):**
Fixed in 3 locations:
1. `enhanced_spike.py:calculate_magnitude_loser_bid()` (class method)
2. `enhanced_spike.py:calculate_magnitude_loser_bid()` (standalone function)
3. `latency_arb.py:calculate_loser_bid()`

All now use: `theoretical_loser = 1.0 - winner_entry`

**PREVENTION:**
1. When porting formulas from backtest to live, COPY EXACTLY - don't "improve"
2. Any deviation between backtest and live formula is a critical bug
3. The `loser_ask` parameter was a red herring - it's available but shouldn't be used as the base

**Source:** Feb 2, 2026 - Paper trading discrepancy investigation

---

---

### 42. VELOCITY FILTER BOUNDARY CONDITION MISMATCH
**What happened:** User asked "is this correct?" about velocity filter comparison in my verification table. I had claimed it matched when it DIDN'T.

**Root cause:** Live code used strict inequality (`<` and `>`) while core function used inclusive boundary (`>` and `<` with opposite return logic):

**CORE (returns TRUE to ALLOW):**
```python
if spike_dir == "UP":
    return velocity_bps > -threshold  # At -0.10: FALSE (REJECT)
```

**LIVE (used to REJECT):**
```python
if spike_dir == "UP" and velocity_bps < -0.10:  # At -0.10: FALSE (ALLOW)
    return REJECT
```

At exactly `velocity_bps = -0.10`:
- Core: REJECTS (because -0.10 is NOT > -0.10)
- Live: ALLOWED (because -0.10 is NOT < -0.10)

**Cost:**
- Live allowed trades at the exact boundary that backtest would reject
- Subtle mismatch that I claimed was "matching" in my verification
- User had to catch my error by questioning the table

**FIX (Feb 2, 2026):**
Changed live code from `<` to `<=` and `>` to `>=`:
```python
if spike_dir == "UP" and velocity_bps <= -0.10:  # Now matches core
if spike_dir == "DOWN" and velocity_bps >= 0.10:  # Now matches core
```

**PREVENTION:**
1. When verifying "matching" logic, trace through BOUNDARY VALUES explicitly
2. Different logical structures (return TRUE vs if-reject) can have subtle boundary differences
3. Don't claim code "matches" without checking edge cases: `==`, `<`, `<=`, `>`, `>=`

**Source:** Feb 2, 2026 - User caught verification error in my table

---

---

### 43. Z-SCORE FILTER NOT PORTED TO NEW BACKTEST FILES DURING REFACTOR
**What happened:** On Jan 31, 2026, the src/core refactor created new "main" backtest files (`aggressive_main_backtest.py`, `aggressive_grid_search.py`). The z-score volatility filter [0.0, 1.5] was NOT ported from the original validated backtest (`fixed_cycling_grid_backtest.py` line 675).

**Root cause:**
- Original backtest (`fixed_cycling_grid_backtest.py`) had z-score filtering: `if zscore < config.z_lo or zscore > config.z_hi: continue`
- New main backtest files were created to import from src/core
- Z-score filtering logic was simply OMITTED - not intentionally removed, just not copied
- The refactor focused on fee model and signal filters, not volatility gating

**Cost:**
- Validated results ($9/hr) came from OLD backtest WITH z-score filter
- NEW backtest results are MORE OPTIMISTIC (include trades old backtest would skip)
- Live code HAS z-score filter (enhanced_spike.py:1555-1561)
- Backtest/live mismatch undetected for 2 days

**Files affected:**
- `research/backtests/aggressive_main_backtest.py` - missing z-score filter
- `research/optimizers/aggressive_grid_search.py` - missing z-score filter
- `research/backtests/fixed_cycling_grid_backtest.py` - HAS z-score filter (original)

**FIX:**
Add z-score filter to new backtest files:
```python
# After spike detection, before entry:
z_score = spike_row.get('z_score', 0.0)
if z_score < Z_LO or z_score > Z_HI:
    spike_idx += 1
    continue
```

**PREVENTION:**
1. When refactoring/creating new backtest files, DIFF against validated original
2. Check TRADING_CONFIGS.py for ALL filter parameters (z_lo, z_hi, etc.)
3. Grep for filter logic: `grep -r "z_lo\|z_hi" research/backtests/`
4. Validate new backtest produces SAME results as original on same data

**Source:** Feb 2, 2026 - Audit discovered z-score filter missing from main backtest

---

---

### 44. CHANGED FIRST GREP RESULT WITHOUT CHECKING ALL OCCURRENCES (REPEAT OF #30)
**What happened:** User asked to fix `high_entry_threshold` from 0.90 to 0.80. I grepped, found the value at line 107 (class default) and line 211 (AGGRESSIVE instance). I ONLY fixed line 107 and called it done. Line 211 (the ACTUAL production value) was still 0.90.

**This is a REPEAT of mistake #30** (Updated config values inconsistently across files).

**Root cause:**
- Grep returned multiple results
- Fixed the FIRST result without checking ALL results
- Didn't verify the fix by grepping AFTER the change
- Didn't understand that class defaults vs instance values are DIFFERENT

**Cost:**
- Bot was running with WRONG threshold (0.90 instead of 0.80)
- User had to ask me to "CHECK THOROUGHLY" to catch my own mistake
- Entries at $0.85 were still being allowed

**The pattern:**
```
Line 107: high_entry_threshold: float = 0.80  # Class DEFAULT (edited)
Line 211: high_entry_threshold=0.90,          # AGGRESSIVE INSTANCE (MISSED!)
```

**FIX - ALREADY DOCUMENTED IN #30 BUT I DIDN'T FOLLOW IT:**
1. Grep ALL occurrences BEFORE changing
2. Understand WHAT each occurrence is (class default vs instance)
3. Change ALL relevant occurrences
4. Grep AFTER to verify ALL are fixed
5. Don't declare "done" after changing the first match

**MANDATORY SELF-CHECK:**
When editing config values:
```bash
grep -n "high_entry_threshold" research/reference/TRADING_CONFIGS.py
# Count results, understand each one, fix ALL relevant ones
```

**Source:** Feb 4, 2026 - skip_high_entry threshold fix

---

### 45. BACKTEST vs PAPER TRADING DISCREPANCY - EWMA DEDUPLICATION MISMATCH ✅ FIXED
**What happened:** Feb 5 paper trading lost money while backtest showed +$10/hr profit on IDENTICAL data (OOS10.1).

**ROOT CAUSE - EWMA Deduplication Mismatch:**
- Backtest deduplicates BTC prices by `timestamp_ms` (72% removed) BEFORE EWMA calculation
- Live was deduplicating by consecutive price value (WRONG approach)
- Result: Backtest produces 346 trades, live got different spike signals

**The Problem:**
- Backtest: `df.drop_duplicates(subset=['timestamp_ms'])` - one tick per millisecond
- Live: `if price != last_price` - only skips if price exactly same as previous
- These are DIFFERENT deduplication approaches with different EWMA values

**NOT A BUG (clarified):**
- Fill price logic is CORRECT: when market ask drops to our bid, we fill at our bid price
- This is correct limit order behavior

**FIX APPLIED (Feb 5, 2026):**
File: `/src/api/binance_client.py` lines 155-165, 232-256
- Added `_last_ewma_timestamp_ms` field to track last processed timestamp
- Changed deduplication from consecutive price to timestamp_ms based:
  ```python
  timestamp_ms = int(now.timestamp() * 1000)
  if timestamp_ms != self._last_ewma_timestamp_ms:
      # Only update EWMA once per unique millisecond
  ```
- Now matches backtest behavior exactly

**Source:** Feb 5, 2026 - OOS10.1 paper trading validation failure
**Details:** research/findings/BACKTEST_LIVE_DISCREPANCY_FEB5.md

---

### 46. MAX LOSS LIMIT NOT ENFORCED IN AGGRESSIVE/CONTRARIAN/SPREADCAP ✅ FIXED
**What happened:** Feb 5 paper trading lost $79.71 despite MAX_LOSS=$10 limit. The loss limit warning was logged repeatedly but trading continued.

**ROOT CAUSE:**
- `loss_limit_reached` flag was checked ONLY in the ACCUM strategy path (line ~3751)
- AGGRESSIVE, CONTRARIAN, and SPREADCAP have dedicated `_run_*_cycle()` functions
- These dedicated functions had NO check for `loss_limit_reached`
- Result: Loss limit warning logged but trading continued indefinitely

**Impact:**
- Loss of $79.71 instead of stopping at $10
- ~8x more loss than configured limit
- All dedicated strategy modes affected

**FIX APPLIED (Feb 5, 2026):**
Added `loss_limit_reached` check to all dedicated cycle functions:
- `_run_aggressive_cycle()` - line ~5770
- `_run_contrarian_cycle()` - line ~6597
- `_run_spread_capture_cycle()` - line ~5293

Check allows hedging to complete but blocks new entries when limit hit.

**Source:** Feb 5, 2026 - Paper trading lost $79.71 with $10 max loss configured

---

### 47. DATA COLLECTION: OBSERVER AND LOGGER NOT COUPLED ✅ FIXED
**What happened:** Observer stopped at 02:42 UTC but Binance logger continued until 03:35 UTC. Result: partial data useless for backtesting.

**ROOT CAUSE:**
- `run_data_collection.py` line 207 had explicit comment: "Don't set self.running = False, let price logger continue"
- Price logger ignores `self.running` flag, runs for its own duration
- When observer crashes/finishes, logger keeps going alone

**FIX APPLIED (Feb 5, 2026):**
- When observer crashes without auto-restart: NOW cancel price_logger_task
- When observer completes normally: NOW cancel price_logger_task
- Both must run/stop together for complete data

---

### 48. CSV LOGGING FAILS SILENTLY ✅ FIXED
**What happened:** Paper trading CSV stopped at 02:39 UTC (159 cycles) but frontend showed 394 cycles and trading continued.

**ROOT CAUSE:**
- `_log_event_csv()` had NO exception handling around file write
- If disk full, permission error, etc. - exception bubbled up and was swallowed
- Trading continued but CSV logging stopped = lost data

**FIX APPLIED (Feb 5, 2026):**
- Added try/except around CSV write
- Track consecutive failures with `_csv_write_failures`
- After 3 failures: set `loss_limit_reached = True` to stop trading
- Log critical error so it's obvious what happened

**Source:** Feb 5, 2026 - 235 cycles lost (394 traded but only 159 logged)

---

### 49. HARDCODED WRONG BREAKEVEN VALUE IN SAME COMMIT THAT DOCUMENTED CORRECT VALUE ✅ FIXED
**What happened:** In commit a025ba9 (Feb 4, 2026), I created the breakeven exit feature. **IN THE SAME COMMIT:**
- TRADING_CONFIGS.py: `breakeven_min_hold_ms = 10000` (correct - 10s)
- BREAKEVEN_SWEEP_FINDINGS.md: "BE_10000ms WINNER, BE_2000ms worse" (correct - 10s)
- aggressive_main_backtest.py: `BREAKEVEN_MIN_HOLD_MS = 2000` (WRONG - 2s hardcoded)

The findings document I created EXPLICITLY says:
> "TESTED: 0ms=DISASTER (98% taker), 2s=worse, 5s=good, 10s=BEST"

Yet I hardcoded 2000ms instead of importing from TRADING_CONFIGS.

**This is a REPEAT of mistakes #30 and #35:**
- #30: Updated config values inconsistently across files
- #35: TRADING_CONFIGS.py NOT WIRED - config had correct value but code hardcoded wrong value

**Cost:**
- Backtest showed -$3.72/hr on OOS10.2 (with 2s hold)
- After fix: +$18.20/hr on OOS10.2 (with 10s hold)
- **Every backtest run since Feb 4 was using WRONG parameters**
- All "validation" runs were invalid

**Root cause:**
- Hardcoded value instead of importing from TRADING_CONFIGS
- Didn't verify backtest params matched TRADING_CONFIGS after implementation
- Commit message said "10s hold" but I typed 2000 instead of 10000

**FIX (Feb 5, 2026):**
```python
# FROM:
BREAKEVEN_MIN_HOLD_MS = 2000

# TO:
BREAKEVEN_MIN_HOLD_MS = getattr(AGGRESSIVE_CONFIG, 'breakeven_min_hold_ms', 10000)
```

**PREVENTION (ADDING TO MANDATORY CHECKLIST):**
1. NEVER hardcode config values in backtests - ALWAYS import from TRADING_CONFIGS
2. After implementing feature, grep to verify ALL files use same value
3. Read your own findings document before hardcoding values
4. If commit message says "10s", verify code actually has 10000ms not 2000ms

**Source:** Feb 5, 2026 - User asked to investigate discrepancy, found Claude's mistake

---

---

### 50. BACKTEST STUDY MISSING SIGNAL DEDUPLICATION (REPEAT PATTERN OF #29)
**What happened:** AGGRESSIVE_M V2 study (`aggressive_m_v2_ewma_study.py`) had NO cooldown deduplication. Reported 39,221 signals when actual deduplicated count is ~476 (30s cooldown). User caught the mistake.

**Root cause:**
- Spike signals cluster in bursts (98.5% within 5s of each other)
- Study counted EVERY spike that passed filters, no cooldown
- This inflated signal counts ~80x (1,500+/hr vs actual ~30/hr)
- Same pattern as mistake #29 (missing dedup on API data)

**Cost:**
- Updated findings with WRONG numbers
- Wasted time on invalid analysis
- Had to redo study with proper deduplication

**Evidence of clustering:**
```
Median gap between signals in same market: 0 seconds
98.5% of gaps < 5 seconds
Signals come in BURSTS, not independent events
```

**FIX:**
1. ALL backtest studies MUST implement cooldown deduplication
2. Standard cooldown: 30 seconds per (market, direction) pair
3. Track `last_signal_ts` and skip if `current_ts - last_signal_ts < cooldown_ms`

**MANDATORY FOR NEW BACKTESTS:**
```python
COOLDOWN_SECONDS = 30
cooldown_ms = COOLDOWN_SECONDS * 1000

# Track last signal time per (direction) for deduplication
last_signal_ts = {'UP': 0, 'DOWN': 0}

# In signal loop:
if spike_ts - last_signal_ts[spike_dir] >= cooldown_ms:
    results.append(signal_data)
    last_signal_ts[spike_dir] = spike_ts
```

**Source:** Feb 6, 2026 - AGGRESSIVE_M V2 OBI study revealed original study was missing dedup

---

---

### 51. SIMULATION LOOP ONLY CHECKED POSITIONS ON SPIKE ROWS - WASTED 6-8 HOURS
**What happened:** Created `aggressive_m_v2_grid_search.py` with a simulation loop that iterated through SPIKE ROWS only. Positions were checked for MAKER fills only when hitting a spike timestamp, missing all intermediate observer rows where the ask could have dropped to our bid level.

**Root cause:**
- Loop structure: `for _, spike_row in market_spikes.iterrows():`
- Positions checked ONLY at spike timestamps
- Between spikes (hundreds of rows), positions never processed
- MAKER fills require checking EVERY row to see if ask touches our bid
- Result: 0 trades across all 864 configs (0% fill rate)

**Cost:**
- 6-8 hours of user's sleep/time wasted on useless run
- 864 configs × ~30s = 7+ hours, ALL ZEROS
- Same mistake pattern as #10 (fill simulation unrealistic) and #33 (wrote from scratch)

**The data PROVED fills should happen:**
- 54% of signals show expensive_ask drop ≥ 2c within 50 rows
- 74% show drop ≥ 1c
- But loop never checked those rows!

**FIX:**
- Iterate through ALL observer rows: `for obs_idx in range(len(mdf)):`
- Check positions on EVERY row for fill opportunities
- Only create new positions when row matches spike timestamp

**PREVENTION:**
1. When simulating MAKER orders, the loop MUST iterate through ALL price rows
2. Spike detection identifies WHEN to enter, but fill detection needs ALL rows
3. Test simulation on small data first and verify non-zero trades before long runs
4. If checkpoint shows all zeros after 100 configs, STOP AND DEBUG

**Source:** Feb 6, 2026 - User ran overnight, woke up to 0 trades everywhere

---

### 52. HARDCODED RESOLUTION FILE PATH FOR ALL DATASETS
**What happened:** `aggressive_m_v2_grid_search.py` loaded `market_resolutions.csv` for ALL datasets (IS+OOS2, OOS7, OOS8, OOS9). But `market_resolutions.csv` only contains IS+OOS2 slugs (Jan 16-19). OOS7/8/9 have different slugs (Jan 29 - Feb 3).

**Result:** 0 matched markets → 0 trades for OOS7, OOS8, OOS9. Grid search ran successfully but produced all zeros for 3/4 datasets.

**Root cause:**
```python
# Line 550 - HARDCODED for all datasets
res_path = base_dir / "research/observer/market_resolutions.csv"
```

**The data existed - just in different files:**
- IS+OOS2 → `market_resolutions.csv` (486 resolutions)
- OOS7 → `resolutions_20260129.csv` + `resolutions_20260130.csv` (75 resolutions)
- OOS8 → `resolutions_20260131.csv` (72 resolutions)
- OOS9 → `resolutions_oos9_1.csv` + `resolutions_oos9_2.csv` (143 resolutions)

**Cost:**
- User ran full grid search, got 0 trades on 3/4 datasets
- Wasted compute time
- User frustration ("how the FUCK is this possible, we have been using these datasets 100s of times")

**Why this wasn't caught earlier:**
- Previous backtests used different loading code that worked
- This script was newly created and copied only partial logic
- No validation that matched markets > 0 before running

**FIX:**
1. Added `res_files` to each dataset config in DATASETS dict
2. Updated `load_dataset()` to load dataset-specific resolution files
3. Handle different column formats (`slug`/`winner` vs `market_slug`/`resolution`)

**PREVENTION:**
1. When creating dataset loaders, verify EACH dataset loads correctly before full run
2. Add assertion: `assert len(markets_with_res) > 0, f"No matched markets for {dataset}"`
3. Quick test on ALL datasets, not just the first one
4. Check that resolution slugs match observer slugs (sample comparison)

**Source:** Feb 6, 2026 - FADE grid search returned 0 trades for OOS7/8/9

---

### 53. TRUSTED SUMMARY INSTEAD OF CHECKING LOGS WHEN USER ASKED ABOUT LOST MESSAGE
**What happened:** User asked "what was my last message prev conversation". I looked at the compaction summary and said "summarize for context befor compact". User pushed back: "after this", "you deleted it then", "check .jsonl logs", "search the word idea" - FOUR times before I actually searched the logs.

**The lost message was:**
```
is this idea valid
using the same that you used, tweak it and revert later or copy the file. and make the below changes
if we simply place a limit order at best bid 30s or no order pull 10% SL if price touches 85 hold until resolution
10sh
```

**Root cause:**
- Context compaction happened RIGHT AFTER user sent this message
- The summary didn't include it (timing issue)
- When user asked about their last message, I TRUSTED THE SUMMARY instead of checking the actual .jsonl logs
- User had to push back 4-5 times before I actually searched properly

**Cost:**
- Wasted user's time with back-and-forth
- User frustration ("why the fuck did i have to do so much back and forth")
- Lost the user's actual idea/question that needed addressing

**FIX:**
1. When user asks "what was my last message" after a compact, ALWAYS check .jsonl logs
2. The summary is generated BEFORE the last message is processed - it can miss recent messages
3. Don't argue with user ("nothing happened after X") - just search the logs
4. Use: `grep '"content":"' /path/to/session.jsonl | tail -20` to find recent messages

**Source:** Feb 6, 2026 - User's FADE strategy idea lost after compact

---

### Mistake #54: Writing wrong formula in summary while code is correct

**What happened:**
- Ran AGGRESSIVE_M_V2 grid search with correct code: `entry_bid = expensive_ask - offset`
- When summarizing results, wrote: "bid at best_bid + 3c offset"
- This is COMPLETELY WRONG - opposite direction and wrong variable
- User caught it immediately

**The actual code:**
```python
entry_bid = max(0.01, expensive_ask - config.entry_offset_cents)
# 3c offset means: bid at expensive_ask - 0.03
```

**What I wrote (WRONG):**
```
bid at best_bid + 3c offset
```

**Root cause:**
- Careless writing when summarizing
- Didn't double-check the formula against the code
- Mixed up variable names (best_bid vs expensive_ask) and direction (+ vs -)

**FIX:**
1. When summarizing trading logic, COPY the actual formula from code
2. Don't paraphrase trading math - quote it exactly
3. Double-check direction (+/-) and variable names before sending

**Source:** Feb 7, 2026 - AGGRESSIVE_M_V2 grid search summary

---

### 55. WROTE FV MM V2 BACKTEST FROM SCRATCH WITH WRONG FILL MODEL (REPEAT OF #33, #51)
**What happened:** Created `fair_value_mm_v2_backtest.py` by writing simulation logic entirely from scratch instead of copying from `aggressive_m_v2_grid_search.py`. The new script:

1. **Used TAKER fills at observed ask** (`entry_price = buy_ask`, line 412) — NO 500ms delay
2. **No capital constraint** — `STARTING_CAPITAL=170` defined but NEVER enforced
3. **No maker simulation** — should use maker entry (0% fee) like FADE, not taker (1.56% fee)
4. **Wrong fill model** — real taker fills happen 542ms later at THEN-current ask, not at order-time ask

**This is the THIRD TIME making Mistake #33** (writing from scratch instead of copying validated code).

**The correct execution architecture (from `paper_trading.py:70-92`):**
```
TAKER (entry): 500ms exchange + 42ms network = 542ms delay, fill at CURRENT ask
MAKER (hedge): 0ms delay, strict price-touch (ask <= our bid), 0% fee + rebate
```

**Cost:**
- FV MM v2 results ($7.42/hr EWMA_WIN_5c_TW) are **COMPLETELY VOID**
- Multiple hours building + running an invalid backtest
- User's time wasted reviewing meaningless results
- Trust further eroded

**Root cause:**
- STILL not following CLAUDE.md: "COPY simulation logic from validated files"
- STILL not following MEMORY.md: execution architecture is immutable
- Despite hooks, skills, and 54 prior documented mistakes

**FIX — PERMANENT RULES:**
1. **Execution engine is SACRED** — copy from `aggressive_m_v2_grid_search.py`
2. **Market logic is FLEXIBLE** — can innovate on signal generation, entry criteria
3. **Fill simulation MUST match `paper_trading.py`** — taker delay, maker price-touch
4. If not sure about execution mechanics → **ASK, don't guess**

**Source:** Feb 9-10, 2026 - FV MM v2 backtest audit revealed entirely wrong fill model

---

---

### Mistake #56: Ignored reference scripts when building HF logger — no stale reconnect

**What happened:**
User explicitly said "we have a data collection wrapper, see if you can learn anything from that script." I reviewed the Polymarket data collection wrapper and adopted some features (duration control, line-buffered CSV, health monitoring, exponential backoff). But I only made the health monitor LOG warnings — I never built the critical feature: force reconnect when feeds go stale.

**Impact:**
- Logger ran for 5+ days on AWS but only collected ~3 hours of data (~148K ticks)
- WebSocket connection stayed alive (pings worked) but trade data silently stopped flowing
- Health monitor printed "WARNING: no ticks for 440840s!" but did nothing about it
- Lost ~5 days of BTC/ETH/SOL/HYPE tick data that cannot be recovered
- User explicitly told me to study the reference scripts and I still missed the most important pattern

**Root cause:**
- Treated "learn from reference scripts" as cosmetic (copy easy features) instead of studying the resilience patterns that make long-running data collection actually work
- Health monitoring without corrective action is useless — it's like a smoke alarm that only beeps but never calls the fire department

**FIX:**
1. When told to reference existing scripts, study the FAILURE HANDLING patterns, not just the happy path
2. Any health monitor MUST have corrective action (reconnect, restart, alert) — not just logging
3. For long-running processes: test failure scenarios (stale connection, silent disconnect) BEFORE deploying

**Source:** Feb 11, 2026 — HF logger on AWS stale for 5 days, only 148K ticks collected

---

**Last updated:** Feb 11, 2026
**Mistakes documented:** 56
**Note:** Use `sudo systemctl stop polymarket-bot` to kill AWS frontend (not kill PID)

**Note:** Multi-cycle findings moved to `research/findings/SINGLE_CYCLE_OPTIMAL_20260131.md` (research finding, not Claude mistake)
**Sources:** CODEBASE_AUDIT_JAN17.md, AWS_7HR_OBSERVER_DEEP_ANALYSIS.md, VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md, PLAN_FIX_ENTRY_FILL_JAN19.md, SPREAD_CAPTURE_FIX_PLAN.md
