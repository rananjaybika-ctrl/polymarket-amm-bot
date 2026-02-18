# Whale Actionable Signals - Complete Analysis

## ⛔ DEPRECATED - February 5, 2026

**Status:** ABANDONED - Unable to profitably replicate whale strategies.

**Backtest Results:**
- Direction accuracy: 70.2% (good)
- Hourly rate: **-$2.93/hr** (losing money)
- Entry cap & BTC filters made it worse

**Conclusion:** Baguette's edge likely comes from order flow visibility, execution speed, or market making advantages we cannot replicate. The signals identified here are not sufficient for profitability.

---

**Date:** February 5, 2026
**Data Sources:** OOS6 (3,539 trades) + OOS9 (83,003 trades) = 86,542 total whale trades analyzed

---

## ⚠️ CRITICAL CORRECTION: Baguette's ACTUAL Strategy

**Previous assumption:** Baguette hedges by buying opposite side (pair building)
**Actual data:** Baguette sells SAME side 92% of time (profit-taking/scalping)

| Metric | Value |
|--------|-------|
| Avg BUY price | $0.58 |
| Avg SELL price | $0.66 |
| **Profit per share** | **$0.08** |
| **Exit win rate** | **95%** |
| Position sold (partial exit) | 64% |
| Position held to resolution | 36% |

### Baguette's Actual Flow:
```
1. BUY directional position at ~$0.58
2. Price moves favorably (+$0.08)
3. SELL 64% of position (same side!) to lock profit
4. Hold remaining 36% to resolution
5. 95% of partial exits are profitable
```

**This is SCALPING + PARTIAL HOLD, not pair arbitrage!**

### ⚠️ Period Bias Warning
BTC was bullish 85.6% of OOS9 period. The "90% trades when BTC > 1h EMA21"
is likely period bias, NOT a deliberate filter.

---

## Executive Summary: 6 Actionable Signals

| Signal | Implementation | Expected Impact |
|--------|----------------|-----------------|
| 1. Buy Expensive Side | `up_ask > down_ask → buy UP` | +7% accuracy baseline |
| 2. OBI Contrarian | `obi > 0 → buy DOWN` | +5-8% when combined |
| 3. Volatility Gate | `market_vol > 2.0` | Filter low-quality setups |
| 4. 1h BTC Trend | `btc > 1h_ema21` | 90% of Baguette trades |
| 5. 30m EMA Cross | `ema9 > ema21 on 30m` | 92% bullish structure |
| 6. Time Window | `300-600s remaining` | Peak signal zone |

---

## Signal 1: Buy Expensive Side (STRONGEST)

**The side with higher ask price is the likely winner.**

| Dataset | Gabagool | Baguette |
|---------|----------|----------|
| OOS6 | 53.6% | **66.2%** |
| OOS9 | 46.0% | **63.2%** |
| **Average** | 49.8% | **64.7%** |

**Implementation:**
```python
def get_prediction(up_ask, down_ask):
    if down_ask > up_ask:
        return "DOWN"  # DOWN expensive = buy DOWN
    return "UP"        # UP expensive = buy UP
```

**Expected Accuracy:** 57-65% (vs 50% random)

---

## Signal 2: OBI Contrarian Filter (CONFIRMATION)

**Trade OPPOSITE of orderbook imbalance.**

| Dataset | Gabagool Contrarian | Baguette Contrarian |
|---------|---------------------|---------------------|
| OOS6 | 51.1% | **59.6%** |
| OOS9 | 47.3% | **56.6%** |
| **Average** | 49.2% | **58.1%** |

**Baguette's OBI pattern (OOS9):**
- Buy UP when OBI bearish (negative): **54.5%**
- Buy DOWN when OBI bullish (positive): **58.5%**

**Implementation:**
```python
def obi_confirms(predicted_side, net_obi):
    if predicted_side == "DOWN":
        return net_obi > 0  # OBI bullish → contrarian DOWN
    return net_obi < 0      # OBI bearish → contrarian UP
```

**Combined with expensive side:** 62-68% accuracy

---

## Signal 3: Volatility Gate (QUALITY FILTER)

**Baguette trades in 30% higher volatility conditions.**

| Dataset | Gabagool Avg Vol | Baguette Avg Vol | Difference |
|---------|------------------|------------------|------------|
| OOS6 | 1.28 | **1.65** | +29% |
| OOS9 | 1.70 | **2.22** | +31% |
| **Average** | 1.49 | **1.94** | +30% |

**Implementation:**
```python
def volatility_gate(market_vol, threshold=2.0):
    return market_vol >= threshold
```

**Effect:** Filters out ~40% of low-confidence entries

---

## Signal 4: BTC 1h Trend Alignment (HIGH IMPACT)

**Baguette trades almost exclusively when BTC is above 1h EMA21.**

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Trades when BTC > 1h EMA21 | 65.8% | **89.7%** |
| Trades when BTC > 1h EMA9 | 57.3% | **72.1%** |

**Implementation:**
```python
def btc_trend_filter(btc_price, btc_ema21_1h):
    return btc_price > btc_ema21_1h
```

**Effect:** Filters ~35% of counter-trend entries

---

## Signal 5: BTC 30m EMA Cross (STRUCTURE FILTER)

**Baguette requires bullish EMA structure on 30m+ timeframes.**

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| 30m EMA9 > EMA21 | 76.2% | **92.5%** |
| 1h EMA9 > EMA21 | 74.3% | **91.9%** |
| 15m EMA21 > EMA50 | 74.9% | **93.1%** |

**Implementation:**
```python
def btc_structure_filter(ema9_30m, ema21_30m):
    return ema9_30m > ema21_30m  # Bullish cross
```

**Combined with 1h trend:** Very strong trend alignment

---

## Signal 6: Time Window (TIMING FILTER)

**Both whales concentrate entries in 300-900s window.**

| Time Remaining | Gabagool | Baguette |
|----------------|----------|----------|
| 0-120s | 4.5% | 11.8% |
| 120-300s | 22.1% | 20.8% |
| **300-600s** | **37.9%** | **33.7%** |
| 600-900s | 35.5% | 33.7% |

**Sweet spot:** 300-600s remaining (high signal, time for price discovery)

**Implementation:**
```python
def time_window_filter(time_remaining):
    return 300 <= time_remaining <= 600
```

---

## Combined Entry Logic: MOMENTUM-CONTRARIAN Strategy

```python
def should_enter(market_state, btc_state):
    # Signal 1: Expensive side prediction
    if market_state.down_ask > market_state.up_ask:
        predicted_side = "DOWN"
    else:
        predicted_side = "UP"

    # Signal 2: OBI contrarian confirmation
    if predicted_side == "DOWN":
        obi_confirms = market_state.net_obi > 0
    else:
        obi_confirms = market_state.net_obi < 0

    if not obi_confirms:
        return False

    # Signal 3: Volatility gate
    if market_state.volatility < 2.0:
        return False

    # Signal 4: BTC 1h trend
    if btc_state.price < btc_state.ema21_1h:
        return False

    # Signal 5: BTC 30m structure
    if btc_state.ema9_30m < btc_state.ema21_30m:
        return False

    # Signal 6: Time window
    if not (300 <= market_state.time_remaining <= 600):
        return False

    return True, predicted_side
```

---

## Expected Accuracy Ladder

| Signals Applied | Est. Accuracy | Filter Rate |
|-----------------|---------------|-------------|
| Random baseline | 50% | 0% |
| + Expensive side | 57% | 0% |
| + OBI contrarian | 62-65% | ~40% |
| + Volatility > 2.0 | 65-68% | ~60% |
| + 1h BTC trend | 68-72% | ~75% |
| + 30m EMA cross | 70-75% | ~80% |
| **Baguette actual** | **82.5%** | Unknown |

**Gap to Baguette:** ~8-10pp unexplained (likely execution timing, order flow visibility)

---

## Direction Alignment with BTC

**When BTC is bullish on 15m, Baguette buys UP more.**

| Condition | Gabagool | Baguette |
|-----------|----------|----------|
| Buy UP when BTC > 15m EMA21 | 47.8% | **60.6%** |
| Buy DOWN when BTC > 15m EMA21 | 52.9% | 48.7% |
| **Difference** | -5.1pp | **+11.9pp** |

**Implementation for direction alignment:**
```python
def align_with_btc_trend(predicted_side, btc_above_15m_ema21):
    if btc_above_15m_ema21:
        return predicted_side == "UP"  # Favor UP in BTC uptrend
    return predicted_side == "DOWN"    # Favor DOWN in BTC downtrend
```

---

## Baguette's Position Sizing (NEW)

**Finding: Baguette uses FIXED share sizing, NOT inverse sizing**

| Price Range | Avg Size | Median | Count |
|-------------|----------|--------|-------|
| $0.00-0.40 | 5.8 | **5** | 1,884 |
| $0.40-0.50 | 7.5 | **5** | 986 |
| $0.50-0.60 | 7.3 | **5** | 1,012 |
| $0.60-0.70 | 7.3 | **5** | 960 |
| $0.70-0.80 | 7.8 | **5** | 1,038 |
| $0.80-0.90 | 7.7 | **5** | 1,213 |
| $0.90-1.00 | 7.9 | **5** | 810 |

**Key Stats:**
- Price vs Size Correlation: **+0.10** (weak positive, NOT inverse)
- Median size: **5 shares** across ALL price levels
- Share count CV: 0.97 (more consistent than dollar amount)

**Conclusion:** Use fixed 5 shares per order, not inverse sizing.

---

## Baguette's Entry Price Distribution (NEW)

| Price Range | % of Trades |
|-------------|-------------|
| $0.00-0.40 | **23.8%** |
| $0.40-0.50 | 12.5% |
| $0.50-0.60 | 12.8% |
| $0.60-0.70 | 12.1% |
| $0.70-0.80 | 13.1% |
| $0.80-0.90 | 15.3% |
| $0.90-1.00 | **10.2%** |

Baguette trades at ALL price levels, including 10% at $0.90+.
Mean: $0.58, Median: $0.60

---

## What Does NOT Work

| Signal | Correlation | Notes |
|--------|-------------|-------|
| BTC velocity (60Hz) | r = 0.055 | No latency edge |
| BTC 1m-5m EMAs | ~50% | Too noisy |
| Size vs indicators | r < 0.05 | Sizing not signal-based |
| RSI at entry | ~50 mean | No clear pattern |
| Inverse sizing | r = +0.10 | Baguette uses fixed 5 shares |

---

## Whale Comparison Summary

| Metric | Gabagool | Baguette | Our Target |
|--------|----------|----------|------------|
| Accuracy | 67-70% | **82.5%** | 70%+ |
| Buy expensive | 50% | **65%** | 60%+ |
| Contrarian | 49% | **58%** | 55%+ |
| Avg volatility | 1.5 | **1.9** | >2.0 |
| 1h BTC alignment | 66% | **90%** | 85%+ |
| Entry timing | 300-900s | 300-900s | 300-600s |

---

## EXIT LOGIC (NEW - Critical Finding)

### Baguette's Exit Pattern

| Timing | Action |
|--------|--------|
| Entry | BUY at avg $0.58, time_remaining ~428s |
| Exit | SELL same side at avg $0.66, time_remaining ~374s |
| Delay | ~54 seconds between entry and partial exit |
| Exit size | 64% of position (sell 64%, hold 36%) |

### Exit Decision Logic (Inferred)

```python
def should_exit(entry_price, current_price, time_since_entry):
    # Take profit if price moved favorably
    profit = current_price - entry_price

    if profit >= 0.08:  # ~$0.08 profit target
        return True, 0.64  # Exit 64% of position

    # Or exit after ~54 seconds regardless
    if time_since_entry >= 54:
        if profit > 0:
            return True, 0.64

    return False, 0
```

### Exit Conditions Summary
- **Profit target:** ~$0.08 per share
- **Time target:** ~54 seconds after entry
- **Exit size:** 64% of position
- **Hold to resolution:** 36% of position
- **Exit win rate:** 95%

---

## Implementation Priority

### Phase 1: Core Signals (Highest Impact)
1. ✅ Expensive side prediction
2. ✅ OBI contrarian filter
3. ✅ Volatility > 2.0 gate

### Phase 2: Exit Logic (Critical - NEW)
4. 🔧 Partial exit at +$0.08 profit
5. 🔧 64% position exit, hold 36%

### Phase 3: BTC Trend Filters (⚠️ May be period bias)
6. ⚠️ BTC > 1h EMA21 filter (85% of period was bullish)
7. ⚠️ 30m EMA9 > EMA21 filter

### Phase 4: Refinements (Lower Impact)
8. Time window 300-600s
9. Direction alignment with 15m BTC trend

---

## Data Files

| File | Trades | Coverage |
|------|--------|----------|
| `whale_crossref_gabagool_oos6.csv` | 2,630 | Jan 28 |
| `whale_crossref_baguette_oos6.csv` | 909 | Jan 28 |
| `whale_crossref_gabagool_oos9_btctf.csv` | 75,100 | Feb 1-3 |
| `whale_crossref_baguette_oos9_btctf.csv` | 7,903 | Feb 1-3 |

---

*Created: February 5, 2026*
*Updated: February 5, 2026 - Added sizing analysis, entry price distribution*
*Based on: 86,542 whale trades across OOS6 + OOS9*
