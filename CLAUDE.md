# Claude Code Instructions

## CRITICAL: Read Before Every Session

**YOU MUST READ `/Users/rananjaybika/polymarket-amm-bot/CLAUDE_MISTAKES.md` AT THE START OF EVERY SESSION.**

This file contains 30+ documented mistakes that cost significant time and money. Do not repeat them.

## ⚠️ STRATEGY PIVOT (February 5, 2026)

**AGGRESSIVE (taker-based) is DEPRECATED. New focus: MAKER-PREDICTION.**

| Old Strategy | New Strategy | Key Change |
|--------------|--------------|------------|
| AGGRESSIVE (taker) | MAKER-PREDICTION | 0% maker fees, prediction signal |

**Read first:** `research/strategies/STRATEGY_PIVOT_FEB2026.md`

## Key Files to Know

- `research/reference/TRADING_CONFIGS.py` - **SOURCE OF TRUTH** for all trading parameters
- `CLAUDE_MISTAKES.md` - Mistakes log (READ THIS)
- `research/strategies/STRATEGY_PIVOT_FEB2026.md` - **NEW** Strategy pivot plan
- `src/strategies/enhanced_spike.py` - AGGRESSIVE strategy (DEPRECATED)
- `scripts/run_paper_bot.py` - Live paper trading runner

## CONFIG WIRING (Jan 31, 2026 FIX)

**TRADING_CONFIGS.py is now DIRECTLY IMPORTED by run_paper_bot.py!**

Changes to AGGRESSIVE config values in TRADING_CONFIGS.py automatically propagate to live:
- `lookback_ticks` → spike detection lookback
- `time_stop_seconds` → time-stop exit
- `z_lo`, `z_hi` → z-score filter bounds
- `high_entry_threshold` → skip rule ($0.90)
- `min_time_remaining` → entry cutoff (180s)
- `skip_high_entry` → enable/disable skip rule
- `use_cycling` → enable/disable cycling
- `threshold_method="ou"` → OU adaptive threshold (NOT fixed 0.02!)

**Backtest scripts should also import from TRADING_CONFIGS.py** - don't hardcode!

## Mandatory Processes

### Before Changing Any Config Value:
1. **UPDATE TRADING_CONFIGS.py FIRST** - it's the single source of truth
2. Live engine (run_paper_bot.py) will pick up changes automatically
3. For backtest scripts: import from TRADING_CONFIGS.py or update manually
4. Verify with grep AFTER changes
5. Restart any running tests with old values

### Before Running Long Scripts:
- Add tqdm progress bar
- Add checkpoint saves every N iterations
- Test on small subset first
- Tell user expected runtime BEFORE starting

### Before Giving Instructions:
- Check `ps aux | grep` if process might be running
- Check output files exist before telling user to read them
- Say "I don't know" instead of guessing time estimates

## PROTECTED DATA FILES - DO NOT DELETE

Files prefixed with `PROTECTED_` are irreplaceable datasets. **NEVER delete without explicit user confirmation.**

Location: `research/observer/`
- `PROTECTED_grid_obs_is_oos2_combined.csv` - IS+OOS2 observer data (Jan 16-19)
- `PROTECTED_grid_obs_oos3_oos4_combined.csv` - OOS3+4 observer data (Jan 22-24)
- `PROTECTED_btc_prices_oos3_oos4_combined.csv` - OOS3+4 Binance 60Hz data
- `PROTECTED_grid_obs_oos5_recovered.csv` - OOS5 observer data (Jan 26)
- `PROTECTED_grid_obs_20260126_recovered.csv` - OOS5 alternative

If cleanup/deletion is needed:
1. ASK USER explicitly: "Do you want to delete PROTECTED_xxx file?"
2. Only delete with explicit "yes" confirmation
3. Document reason in commit message

## Creating New Backtest Scripts

When creating new backtest scripts:
1. **IMPORT from TRADING_CONFIGS.py** - don't hardcode parameters
2. **COPY simulation logic** from validated files (test_obi_comparison_oos7.py)
3. **Don't write from scratch** - use existing validated code as template

## Function Naming Conventions (TO BE STANDARDIZED)

Current inconsistencies exist - when fixing, use these canonical names:
- `detect_spike(binance_price: float)` - for class methods
- `detect_spikes(btc_df, lookback)` - for DataFrame functions
- `calculate_loser_bid(winner_entry, spike_magnitude)` - for hedge pricing
