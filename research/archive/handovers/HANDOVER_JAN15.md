# Handover Document: Spread Capture Strategy Analysis
**Date:** January 15, 2026
**Status:** Analysis Complete - Action Required

---

## Executive Summary

The observer ran for 8.25 hours on AWS collecting live data. Analysis reveals:
- **Current config losing money:** -$21.60 (super aggressive offsets)
- **New conservative config:** -$9.45 (still losing, but better)
- **Root cause:** Low BTC volatility during overnight hours = weak velocity signals = 58% accuracy (vs 77% expected)

---

## 1. Current Situation

### Observer Location
- **AWS Server:** ubuntu@54.170.244.221
- **Key file:** `/Users/rananjaybika/Downloads/polymarket-key.pem`
- **Observer script:** `/home/ubuntu/polymarket-amm-bot/scripts/spread_capture_observer.py`
- **Data output:** `/home/ubuntu/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv`

### Current Configuration (SUPER AGGRESSIVE - LOSING MONEY)
| Zone | Velocity | Winner Offset | Loser Offset | Hedge Target |
|------|----------|---------------|--------------|--------------|
| very_strong (4) | 0.30-0.50 | +0.01 | **-0.12** | $0.28 |
| extreme (5) | 0.50-1.00 | +0.01 | **-0.15** | $0.24 |
| super_strong (6) | 1.00+ | +0.02 | **-0.18** | $0.20 |

### Proposed Configuration (CONSERVATIVE - BETTER)
| Zone | Velocity | Winner Offset | Loser Offset | Hedge Target |
|------|----------|---------------|--------------|--------------|
| very_strong (4) | 0.30-0.50 | +0.01 | **-0.03** | $0.37 |
| extreme (5) | 0.50-1.00 | +0.01 | **-0.04** | $0.35 |
| super_strong (6) | 1.00+ | +0.01 | **-0.05** | $0.33 |

---

## 2. Key Findings

### A. Signal Accuracy is LOW (58%, not 77%)

**Backtest vs Live Comparison:**
| Metric | Backtest (Jan 14) | Live (Jan 15) |
|--------|-------------------|---------------|
| Signal Accuracy | 77% | **58.8%** |
| BTC Volatility | 0.0041% | **0.0020%** (half!) |
| Mean Velocity | 0.21 bps | **0.11 bps** (half!) |
| Zone 4-6 frequency | 25.3% | **8.5%** |
| Trading Hours (UTC) | 0-3 + 13-18 | 0-8 only |

**Root Cause:** Live run was during overnight hours (0-8 UTC) when BTC volatility is LOW. Weak velocity signals = poor predictive accuracy.

### B. Hedge vs Unhedged Mechanics

**Critical Insight:** Unhedged = Wrong Prediction BY DESIGN
```
If prediction CORRECT → loser side drops → hedge fills → HEDGED
If prediction WRONG → loser side rises → hedge never fills → UNHEDGED
```

Therefore: **Hedge rate ≈ Signal accuracy**

### C. Offset Comparison Results

| Configuration | Hedge Rate | Hedged PnL | Unhedged PnL | TOTAL |
|---------------|------------|------------|--------------|-------|
| Super Aggressive | 61.3% | +$55.35 | -$76.95 | **-$21.60** |
| Conservative | 77.4% | +$36.45 | -$45.90 | **-$9.45** |
| Improvement | +16.1pp | -$18.90 | +$31.05 | **+$12.15** |

---

## 3. Files to Modify

### To implement conservative offsets:

**File 1:** `/Users/rananjaybika/polymarket-amm-bot/scripts/spread_capture_observer.py`
**File 2:** `/Users/rananjaybika/polymarket-amm-bot/src/strategies/spread_capture.py`

**Change VELOCITY_ZONES in both files:**
```python
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.04},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.06},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'pair_target': 0.96, 'winner_offset':  0.00, 'loser_offset': -0.08},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'pair_target': 0.95, 'winner_offset': +0.01, 'loser_offset': -0.03},  # Changed
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'pair_target': 0.94, 'winner_offset': +0.01, 'loser_offset': -0.04},  # Changed
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'pair_target': 0.93, 'winner_offset': +0.01, 'loser_offset': -0.05},  # Changed
}
```

---

## 4. Step-by-Step Analysis Process

### To reproduce this analysis:

**Step 1: Download observer data from AWS**
```bash
scp -i /Users/rananjaybika/Downloads/polymarket-key.pem \
    ubuntu@54.170.244.221:/home/ubuntu/polymarket-amm-bot/research/observer/spread_capture_obs_*.csv \
    /Users/rananjaybika/polymarket-amm-bot/research/observer/
```

**Step 2: Filter for complete markets only**
- Complete market = started with >800s remaining AND ended with <60s remaining
- This ensures we see full 15-minute market cycle

**Step 3: For each market, determine:**
1. Entry side (UP or DOWN) based on velocity direction at entry
2. Entry zone (very_strong, extreme, super_strong)
3. Entry price (ask price at entry)
4. Hedge target = pair_target - entry_price + loser_offset

**Step 4: Check if hedge filled**
- Scan all samples after entry
- If loser_ask <= hedge_target at any point → hedge filled
- Record hedge fill price

**Step 5: Determine market resolution**
- final_up_bid >= 0.90 → resolved UP
- final_down_bid >= 0.90 → resolved DOWN
- else → UNCLEAR

**Step 6: Calculate PnL**
- If hedged: PnL = (1.0 - entry_price - hedge_price) * 15 shares
- If unhedged + resolution matches entry_side: PnL = (1.0 - entry_price) * 15
- If unhedged + resolution opposite: PnL = -entry_price * 15

---

## 5. Key Questions Answered

### Q1: Is the velocity signal accurate?
**A:** Only 58.8% during overnight hours (vs 77% in backtest during active hours)

### Q2: Why are all unhedged positions wrong?
**A:** By design. Hedged = correct prediction, Unhedged = wrong prediction.

### Q3: Why is live different from backtest?
**A:** BTC volatility was HALF during live run (overnight hours). Low volatility = weak signals = low accuracy.

### Q4: Do super aggressive offsets help?
**A:** NO. They require 40-55% price drops that don't happen. Conservative offsets (-0.03 to -0.05) are better.

### Q5: Would conservative offsets make us profitable?
**A:** Still losing (-$9.45 vs -$21.60), but much better. The core issue is signal accuracy during low volatility.

---

## 6. Recommendations

### Immediate Actions:
1. **Change offsets** to conservative (-0.03, -0.04, -0.05)
2. **Only trade during high-volatility hours** (13-18 UTC / US market hours)
3. **Or increase velocity threshold** during overnight (0.50+ bps instead of 0.30)

### Config Changes to Deploy:
```python
# In VELOCITY_ZONES for zones 4-6:
'very_strong':  {'loser_offset': -0.03},  # was -0.12
'extreme':      {'loser_offset': -0.04},  # was -0.15
'super_strong': {'loser_offset': -0.05},  # was -0.18
```

### Commands to Deploy:
```bash
# 1. Update local files with new offsets
# 2. Sync to AWS
scp -i /Users/rananjaybika/Downloads/polymarket-key.pem \
    /Users/rananjaybika/polymarket-amm-bot/scripts/spread_capture_observer.py \
    ubuntu@54.170.244.221:/home/ubuntu/polymarket-amm-bot/scripts/

scp -i /Users/rananjaybika/Downloads/polymarket-key.pem \
    /Users/rananjaybika/polymarket-amm-bot/src/strategies/spread_capture.py \
    ubuntu@54.170.244.221:/home/ubuntu/polymarket-amm-bot/src/strategies/

# 3. Restart observer
ssh -i /Users/rananjaybika/Downloads/polymarket-key.pem ubuntu@54.170.244.221 \
    "pkill -f spread_capture_observer; cd /home/ubuntu/polymarket-amm-bot && \
     nohup python3 scripts/spread_capture_observer.py --hours 12 --balance 170 --shares 15 > observer.log 2>&1 &"
```

---

## 7. Data Files

| File | Location | Description |
|------|----------|-------------|
| Live data | `research/observer/spread_capture_obs_20260115_current.csv` | 141k samples, 34 markets |
| Backtest data | `research/observer/spread_capture_obs_20260114_old.csv` | 105k samples, 38 markets |
| This handover | `research/HANDOVER_JAN15_ANALYSIS.md` | This document |
| Plan file | `~/.claude/plans/clever-mixing-lollipop.md` | Full analysis history |

---

## 8. Formulas Reference

```
Entry bid    = best_bid + winner_offset
Hedge target = pair_target - entry_price + loser_offset
Hedged PnL   = (1.0 - entry_price - hedge_price) * shares
Unhedged PnL = (resolution_price - entry_price) * shares
             where resolution_price = 1.0 if correct, 0.0 if wrong
```

---

*Generated: January 15, 2026*
