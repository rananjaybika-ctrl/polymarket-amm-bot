# Time-Stop & Loser Offset Optimization Study

**Date:** January 31, 2026
**Status:** Complete - Pending Live Validation

---

## Study Overview

Tested combinations of:
- **Loser Offset Presets:** TIGHTER, TIGHT, CURRENT, WIDE
- **Time-Stops:** 30s, 180s, 240s, 300s
- **Datasets:** IS+OOS2 (5Hz), OOS3+4, OOS5, OOS7 (60Hz + OBI)

### Loser Bid Formula
```python
expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
loser_bid = (1.0 - winner_entry) - expected_drop
```

### Offset Presets Tested
| Preset | DROP_MULT | DROP_INT | Description |
|--------|-----------|----------|-------------|
| TIGHTER | 0.15 | 0.03 | Very aggressive, fastest fill |
| TIGHT | 0.30 | 0.05 | Aggressive, faster fill |
| CURRENT | 0.50 | 0.08 | Baseline (live config) |
| WIDE | 0.70 | 0.10 | Conservative, slower fill |

---

## Test 1: Full Grid (9 configs × 4 datasets = 36 runs)

**Configs:** TIGHT, CURRENT, WIDE × TS180, TS240, TS300
**Data:** 176 hours total

### Results (All Data)

| Rank | Config | $/hr | Trades | Win% | Passive% |
|------|--------|------|--------|------|----------|
| 1 | CURRENT_TS300 | $0.82 | 1373 | 56.7% | 73.3% |
| 2 | CURRENT_TS240 | $0.32 | 1465 | 55.1% | 71.1% |
| 3 | WIDE_TS300 | $0.18 | 1196 | 56.0% | 65.7% |
| 4 | CURRENT_TS180 | $0.07 | 1595 | 55.1% | 67.6% |
| 5-9 | All others | negative | - | - | - |

**Problem:** IS+OOS2 (5Hz data) was dragging down all results.

### Results (OOS Only - 107h, excluding IS+OOS2)

| Rank | Config | $/hr | Trades | Win% | Passive% |
|------|--------|------|--------|------|----------|
| 1 | **CURRENT_TS180** | **$5.51** | 647 | 53.7% | 70.3% |
| 2 | CURRENT_TS240 | $4.86 | 608 | 53.7% | 73.6% |
| 3 | TIGHT_TS180 | $4.16 | 771 | 54.3% | 80.1% |
| 4 | CURRENT_TS300 | $2.87 | 578 | 55.2% | 75.2% |

**Finding:** On 60Hz OOS data, CURRENT_TS180 wins.

---

## Test 2: OOS7 Only (19h, OBI ON)

**Configs:** TIGHTER, TIGHT, CURRENT × TS30, TS180, TS240

### Results

| Rank | Config | $/hr | Trades | Win% | Passive% |
|------|--------|------|--------|------|----------|
| 1 | **TIGHT_TS180** | **$13.92** | 303 | 55.8% | 83.2% |
| 2 | **CURRENT_TS180** | **$13.31** | 246 | 53.3% | 71.5% |
| 3 | CURRENT_TS240 | $11.17 | 235 | 54.5% | 74.5% |
| 4 | TIGHT_TS240 | $9.94 | 289 | 56.7% | 84.8% |
| 5 | TIGHT_TS30 | $9.35 | 398 | 52.5% | 58.3% |
| 6 | CURRENT_TS30 | $8.61 | 364 | 50.5% | 43.1% |
| 7 | TIGHTER_TS180 | $5.55 | 329 | 54.4% | 86.9% |
| 8 | TIGHTER_TS240 | $2.42 | 314 | 55.4% | 87.6% |
| 9 | TIGHTER_TS30 | $2.00 | 432 | 50.7% | 68.1% |

---

## Key Findings

### 1. Winner Configs (OOS7 with OBI)
- **#1 CURRENT_TS180** - Baseline, proven, $13.31/hr
- **#2 TIGHT_TS180** - Higher $/hr ($13.92) but needs validation

### 2. TIGHTER Offset Failed
- Too aggressive (0.15/0.03)
- Fills on marginal/bad trades even with OBI filter
- Highest trade count but lowest $/hr

### 3. TS30 Underperformed
- More trades but lower win rate (~50-52%)
- Low passive% (43-68%) = forced exits before trades mature
- TS180 is the sweet spot

### 4. IS+OOS2 (5Hz) Data Issue
- All configs lost money on IS+OOS2
- 5Hz sampling rate doesn't match 60Hz production
- Should exclude from future backtests or weight lower

### 5. OBI Filter Impact
- With OBI ON (OOS7), TIGHT offset becomes viable
- OBI filters bad entries → aggressive loser bids only fill on quality trades
- Without OBI, TIGHT loses money (fills on bad trades)

---

## Recommendations

### Immediate (After Paper Trading Ends)
1. Deploy **CURRENT_TS180** with conservative settings:
   - `TARGET_SHARES = 10` (reduced from 50)
   - `HIGH_ENTRY_THRESHOLD = 0.80` (reduced from 0.90)
   - `time_stop_seconds = 180`
   - `DROP_MULTIPLIER = 0.50, DROP_INTERCEPT = 0.08`

2. Validate for 1-2 days, then if positive:
   - Increase to `TARGET_SHARES = 50`
   - Increase to `HIGH_ENTRY_THRESHOLD = 0.90`

### Future Testing
- TIGHT_TS180 needs validation on more OOS data with OBI
- Consider dynamic offset based on OBI availability

---

## Config Changes Summary

### TRADING_CONFIGS.py Updates
```python
# AGGRESSIVE config
time_stop_seconds = 180.0    # was 20.0 (testing)

# Loser bid params (no change)
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
```

### run_paper_bot.py Updates (for testing phase)
```python
TARGET_SHARES = 10           # reduced for testing
HIGH_ENTRY_THRESHOLD = 0.80  # reduced for testing
```

---

## Data Files

- **Test 1 Results:** `research/findings/data/timestop_offset_results.csv`
- **Test 2 Results:** `research/findings/data/timestop_offset_v2_results.csv`
- **Analysis Scripts:** `entry_spike_magnitude_test.py`

---

## Next Steps

1. [ ] Paper trading ends → collect data for backtest
2. [ ] Deploy CURRENT_TS180 with 10sh/0.80 skip
3. [ ] Validate 24-48h
4. [ ] If positive: scale to 50sh/0.90 skip
5. [ ] Run TIGHT_TS180 A/B test when more OBI data available
