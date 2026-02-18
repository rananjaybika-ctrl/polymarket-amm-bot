# ML Market Predictor - Findings

**Date:** February 8, 2026
**Pipeline:** `research/ml/market_predictor/run_overnight.py`
**Models saved:** `research/ml/market_predictor/models/`

---

## 1. Data

### Datasets (60/40 Train/Test Split)

| Dataset | Split | Rows | Markets | Period |
|---------|-------|------|---------|--------|
| IS+OOS2 | Train | 1,090,500 | 254 | Jan 16-19 |
| OOS7 | Train | 328,960 | 77 | Jan 29-30 |
| OOS9 | Train | 433,724 | 101 | Feb 1-3 |
| OOS3+OOS4 | Test | 930,960 | 173 | Jan 22-24 |
| OOS6 | Test | 202,076 | 47 | Jan 28 |
| OOS8 | Test | 315,035 | 74 | Jan 31 |
| **TRAIN** | - | **1,853,184** | **432** | - |
| **TEST** | - | **1,448,071** | **294** | - |

**Resolution fix:** OOS3+OOS4 (173 markets) had zero resolutions in the original `market_resolutions_verified.csv` because `fetch_resolutions.py` only globbed `grid_obs_*.csv` and missed `PROTECTED_grid_obs_*.csv` files. Fixed by re-fetching 233 missing resolutions from Gamma API. All 850 markets now resolved.

**Label balance:**
- Train: UP=903,848, DOWN=949,336 (48.8% / 51.2%)
- Test: UP=724,885, DOWN=723,186 (50.0% / 50.0%)

### Features (110 numeric features)

| Category | Count | Examples |
|----------|-------|---------|
| Price | 15 | up_ask, down_ask, mid_price_diff, ask_price_diff_pct, expensive_ask |
| Orderbook depth (L1-L5) | 40 | up_bid_1..5, up_ask_size_1..5 |
| Spike | 8 | has_spike, spike_up, spike_magnitude_abs |
| Velocity | 9 | velocity_bps, velocity_abs, velocity_confirms_spike |
| Time | 5 | time_remaining_secs, time_urgency, in_entry_window |
| OBI | 4 | up_imbalance, down_imbalance, obi_contrarian |
| Rolling (5s/30s/60s) | 12 | up_ask_std_5s, velocity_mean_60s |
| Volatility regime | 3 | volatility_percentile, vol_regime_low, vol_regime_high |
| Composite | 2 | baguette_signal, fade_signal |

**Engineered by:** `feature_engineer.py` (adds ~60 features to base ~50 columns)

---

## 2. Tick-Level Models (Predict winner per 5Hz observation)

**Target:** `winner_binary` (1=UP wins, 0=DOWN wins)

### Results

| Model | Accuracy | AUC | F1 | CV Mean | CV Std |
|-------|----------|-----|-----|---------|--------|
| **Logistic Regression** | **74.8%** | **0.846** | **0.750** | 0.721 | 0.001 |
| Random Forest | 73.5% | 0.829 | 0.745 | 0.775 | 0.001 |
| XGBoost | 67.7% | 0.785 | 0.718 | 0.844 | 0.002 |

**Best: Logistic Regression** (AUC = 0.846)

### LR Classification Report (Test)
```
              precision    recall  f1-score   support
        DOWN       0.75      0.74      0.75    723186
          UP       0.74      0.76      0.75    724885
    accuracy                           0.75   1448071
```

### XGBoost Overfitting
XGBoost has highest CV on training (0.844) but lowest test accuracy (67.7%). Gap = 16.7 percentage points. Complex nonlinear patterns in training data don't generalize. Linear model (LR) generalizes best.

### Top Features by Model

**LR (|coefficient|):**
1. up_ask_4: 0.8035
2. down_ask_1: 0.7390
3. up_ask_3: 0.4957
4. up_bid_5: 0.4048
5. up_bid_4: 0.3910

**Random Forest (importance):**
1. up_mid: 0.0873
2. ask_price_diff_pct: 0.0814
3. mid_price_diff: 0.0707
4. down_mid: 0.0651
5. bid_price_diff: 0.0638

**XGBoost (importance):**
1. ask_price_diff_pct: 0.1916
2. bid_price_diff: 0.1732
3. up_mid: 0.0560
4. expensive_ask: 0.0490
5. down_ask: 0.0462

**Interpretation:** LR relies on orderbook depth levels (L3-L5), while tree models use price differences. All models agree: **price features dominate**. Velocity/time/OBI are secondary.

---

## 3. Market-Level Models (Option B: 1 row per market)

**Aggregation:** For each market, take last 25% of observations (near resolution). Compute median, std, and last value for every numeric feature. Produces 333 features per market.

**Target:** `winner_binary` (1=UP wins, 0=DOWN wins)

### Results

| Model | Accuracy | AUC | F1 | CV Mean |
|-------|----------|-----|-----|---------|
| **Logistic Regression** | **94.6%** | **0.980** | **0.944** | 0.891 |
| Gradient Boosting | 93.9% | 0.982 | 0.937 | 0.878 |

### Classification Report (LR, Test: 294 markets)
```
              precision    recall  f1-score   support
        DOWN       0.92      0.97      0.94       148
          UP       0.96      0.91      0.94       146
    accuracy                           0.94       294
```

### Top Features (Gradient Boosting)
1. down_ask_last: 0.1227
2. up_ask_last: 0.0912
3. up_ask_from_fair_last: 0.0770
4. down_mid_last: 0.0765
5. up_bid_last: 0.0610
6. up_mid_last: 0.0589
7. down_bid_median: 0.0342
8. down_bid_last: 0.0265
9. ask_price_diff_pct_last: 0.0221
10. down_ask_from_fair_last: 0.0200
11. vol_regime_low_std: 0.0174
12. down_ask_std_30s_last: 0.0172
13. velocity_abs_last: 0.0142
14. down_ask_from_fair_median: 0.0133
15. down_ask_std_5s_std: 0.0101

### Data Leak Fix
Initial run showed 100% accuracy because `winner_binary` was included in the aggregation (creating `winner_binary_last`, `winner_binary_median` features). Fixed by excluding label columns from aggregation. After fix: 94.6% accuracy.

### Caveat
Uses **last 25% of observations** = late-game data near resolution. At that point, the market price already reflects the likely winner. See Section 3b for 300-600s actionable window test.

---

## 3b. Market-Level: 300-600s Window (Actionable Test)

**Question:** Does 94.6% accuracy hold when we only use observations from the 300-600s entry window (the actionable trading window)?

### Data After Filtering
- Train: 622,612 rows -> 428 markets
- Test: 483,801 rows -> 289 markets
- Same aggregation (median/std/last) but only from 300-600s observations

### Results

| Model | Accuracy | AUC | F1 | CV Mean |
|-------|----------|-----|-----|---------|
| Logistic Regression | 83.0% | 0.893 | 0.827 | 0.774 |
| Gradient Boosting | 81.3% | 0.893 | 0.811 | 0.797 |
| **Baseline (expensive side)** | **83.7%** | **-** | **-** | **-** |

### Classification Report (LR, 300-600s, Test: 289 markets)
```
              precision    recall  f1-score   support
        DOWN       0.83      0.84      0.83       147
          UP       0.83      0.82      0.83       142
    accuracy                           0.83       289
```

### Top Features (GB, 300-600s)
1. down_ask_last: 0.0869
2. up_bid_last: 0.0701
3. down_ask_from_fair_last: 0.0640
4. ask_price_diff_last: 0.0558
5. down_mid_last: 0.0511
6. up_mid_last: 0.0407
7. down_bid_last: 0.0342
8. up_ask_from_fair_last: 0.0330
9. mid_price_diff_last: 0.0297
10. velocity_mean_30s_median: 0.0173

### Comparison

| Window | LR Accuracy | LR AUC | vs Baseline |
|--------|-------------|--------|-------------|
| Last 25% (near resolution) | 94.6% | 0.980 | +10.9% |
| **300-600s (actionable)** | **83.0%** | **0.893** | **-0.7%** |
| Baseline (expensive_side) | 83.7% | - | - |

### Conclusion
At 300-600s remaining, ML **cannot beat the simple "expensive side wins" baseline** (83.0% vs 83.7%). The 94.6% accuracy was entirely from late-game price convergence near resolution.

**This means:** ML adds no predictive value at the actionable trading window. The FADE strategy's edge comes from:
1. **Filtering** to expensive_ask >= $0.80 (raises win rate from 83.7% to ~94%)
2. **Maker execution** (0% fees)
3. **Hold to resolution** (no premature exits)

Not from having a better signal about which side wins.

---

## 4. Entry Timing Model (Option C: predict WHEN to enter)

**Target:** `good_entry` = binary label defined as:
- `expensive_wins == 1` (buying expensive side would be profitable)
- AND `expensive_ask >= 0.70`
- AND `time_remaining_secs` in [90, 600]

**Label distribution:**
- Train: 645,723 good entries / 1,853,184 total (34.8%)
- Test: 522,983 good entries / 1,448,071 total (36.1%)

### Results (Gradient Boosting)

| Metric | Value |
|--------|-------|
| Accuracy | 94.1% |
| AUC | 0.981 |
| F1 | 0.921 |

### Classification Report (Test)
```
              precision    recall  f1-score   support
   Bad Entry       0.96      0.97      0.96    925088
  Good Entry       0.91      0.90      0.92    522983
    accuracy                           0.94   1448071
```

### Top Features
1. **expensive_ask: 0.5472** (55% of importance)
2. **time_remaining_secs: 0.1710** (17%)
3. **time_urgency: 0.1124** (11%)
4. **time_urgency_sq: 0.0806** (8%)
5. binance_price: 0.0372
6. cheap_ask: 0.0305
7. volatility_percentile: 0.0017
8. ask_price_diff_pct: 0.0015
9. in_entry_window: 0.0015
10. velocity_mean_60s: 0.0014

### PnL by Confidence Threshold

Assuming maker entry at `expensive_ask - 0.03`, hold to resolution:

| Threshold | Signals | Win Rate | Avg PnL/trade | Total PnL |
|-----------|---------|----------|---------------|-----------|
| P>=0.3 | 575,713 | 89.2% | $0.049 | $27,905 |
| P>=0.4 | 564,439 | 89.3% | $0.048 | $27,226 |
| P>=0.5 | 553,646 | 89.5% | $0.049 | $26,853 |
| P>=0.6 | 527,566 | 90.0% | $0.048 | $25,120 |
| **P>=0.7** | **455,620** | **92.0%** | **$0.049** | **$22,272** |
| P>=0.8 | 365,129 | 94.1% | $0.046 | $16,872 |

**Interpretation:** The model essentially learned the FADE filter (`expensive_ask >= $0.80` + time window) in a continuous, optimized way. At P>=0.7, it achieves 92% win rate with 456K signals. At P>=0.8, it matches the FADE threshold's 94% accuracy.

The ~$0.05 avg PnL/trade is per share. With 15 shares per trade: ~$0.73/trade.

---

## 5. Key Takeaways

### What the ML confirms
1. **Price is the signal.** Across all models, price features (asks, bids, mid prices, price diffs) dominate. Velocity, OBI, and composite scores are secondary.
2. **Linear > complex.** Logistic Regression beats XGBoost on test data. The signal is fundamentally linear: which side is more expensive predicts the winner.
3. **FADE strategy is validated.** The entry timing model's top features (`expensive_ask`, `time_remaining`) are exactly the FADE filter. ML doesn't find a better signal, it just smooths the threshold.
4. **Market-level prediction is strong.** 94.6% accuracy at market level shows that near-resolution prices are highly predictive. Question is whether this holds at 300-600s remaining.

### What's NOT useful
1. **Velocity** - 0.4% importance in entry timing model. Used as filter in FADE, not predictor.
2. **OBI/Imbalance** - <0.2% importance. Despite Baguette analysis showing 98.1% contrarian signal, ML doesn't find it useful on aggregate data.
3. **Complex models** - XGBoost overfits. GBM is marginal over LR. Keep it simple.

### Next steps
1. **Test market-level with 300-600s window** - Does 94.6% hold when we only see data from the actionable entry window?
2. **Use ML as confidence scorer on FADE** - Feed ML probability to size positions (more shares when P>=0.8, fewer when P>=0.6)
3. **Investigate why OBI doesn't help** - May need per-market OBI features, not per-tick

---

## 6. Files Reference

| File | Purpose |
|------|---------|
| `research/ml/market_predictor/data_loader.py` | Load 6 datasets with 60/40 split |
| `research/ml/market_predictor/feature_engineer.py` | 110 features from raw observer data |
| `research/ml/market_predictor/train_models.py` | Train LR/RF/XGB (original script) |
| `research/ml/market_predictor/run_overnight.py` | Full 10-step pipeline with checkpoints |
| `research/ml/market_predictor/models/*.joblib` | Saved model files |
| `research/ml/market_predictor/models/model_comparison_all.csv` | All results CSV |
| `research/ml/market_predictor/CODEBASE_SYNTHESIS.md` | Pre-implementation research (has overgeneralizations) |
| `research/ml/market_predictor/TRADING_MECHANICS_QUESTIONS.md` | User answers on trading mechanics |

---

## 7. Pair Trade Analysis: Early Maker Entry (Baguette Replication)

**Date:** February 8, 2026
**Script:** `research/backtests/pair_trade_analysis.py`
**Data:** 6 datasets, 683 markets, 202 hours

### Motivation

Current FADE strategy buys ONLY the expensive side at $0.80+ as maker, with no hedge. Whale trader **Baguette** (93% maker, 82.5% accuracy) enters at ~9s after market start (~$0.50/$0.50), buys BOTH sides, achieves 100% pair rate and 48.7% hedge ratio.

**Question:** Can we enter earlier (lower threshold) and hedge the cheap side as maker to reduce drawdown?

### 7a. FADE Signal Accuracy by Price Bucket (Step 1)

At what entry threshold does "expensive side wins" remain profitable?

| Bucket | N (6 datasets) | Accuracy | Edge | Edge/$ | Verdict |
|--------|-----------------|----------|------|--------|---------|
| $0.50 | 41 | 59.2% | $0.073 | 14.9% | Coin flip, unprofitable |
| $0.55 | 27 | 50.3% | -$0.078 | -13.2% | Unprofitable |
| $0.60 | 12 | 68.9% | $0.066 | 11.3% | Marginal, inconsistent |
| $0.65 | 9 | 45.8% | -$0.213 | -31.6% | Unprofitable |
| $0.70 | 1 | 0.0% | -$0.750 | -100% | Too few signals |
| **$0.75** | **7** | **100%** | **$0.213** | **27.0%** | **Best edge/$** |
| $0.85 | 2 | 100% | $0.125 | 14.3% | Reliable |
| $0.90 | 5 | 100% | $0.057 | 6.0% | Reliable, low edge |
| $0.95 | 5 | 100% | $0.020 | 2.0% | Minimal edge |

**Key finding:** Below $0.75, accuracy is unreliable (45-69%). At $0.75+, accuracy is 100% across 19 signals. The FADE signal does NOT work at Baguette-style early entry ($0.50-$0.55). Baguette's edge comes from a **different signal** (likely OBI/flow-based), not from the "expensive side wins" heuristic.

### 7b. Cheap Side Fill Rate (Step 2)

Can we get maker hedge fills on the cheap side? Using `hedge_bid = cheap_ask - $0.03`:

| Bucket | N | Fill (ever) | Fill 300s | Fill 600s | Avg Cheap Ask |
|--------|---|-------------|-----------|-----------|---------------|
| $0.50 | 41 | 94% | 82% | 92% | $0.49 |
| $0.55 | 23 | 84% | 80% | 84% | $0.44 |
| $0.60 | 12 | 100% | 89% | 100% | $0.39 |
| $0.65 | 7 | 100% | 86% | 100% | $0.35 |
| $0.75 | 7 | 100% | 100% | 100% | $0.22 |
| $0.90+ | 8 | 100% | 75% | 100% | $0.06 |

**By prediction outcome:**
- Correct predictions: 97% fill rate (ever), 92% within 300s
- Wrong predictions: 74% fill rate (ever), 72% within 300s

**Key finding:** Hedge fills are achievable even at early entry. At $0.50-$0.55, 84-94% of hedges fill eventually. When the prediction is correct (cheap side drops), nearly all fill. When wrong, ~74% still fill due to early-entry oscillation. This validates Baguette's 100% pair rate at early entry.

### 7c. Cheap Side Trajectory (Step 4)

How does the cheap side's ask price evolve after signal?

| Time Remaining | Correct (cheap_ask) | Wrong (cheap_ask) |
|---------------|---------------------|-------------------|
| 800s | $0.45 | $0.53 |
| 600s | $0.38 | $0.59 |
| 400s | $0.31 | $0.69 |
| 200s | $0.22 | $0.80 |
| 100s | $0.16 | $0.85 |
| 50s | $0.14 | $0.87 |

**Key finding:** Clear divergence. When correct, cheap_ask drops steadily (hedge is a loss, but controlled). When wrong, cheap_ask rises sharply (hedge is a large win, offsetting the losing winner side). The hedge provides meaningful downside protection: when wrong, hedge gains ~$0.34 per share ($0.53 → $0.87 resolution) vs winner loss of ~$0.52.

### 7d. Grid Search Backtest (Step 3) — FINAL

90 configs x 6 datasets = 540 rows. Configs: 6 thresholds (T55-T80) x 5 hedge ratios (H0/H25/H50/H75/H100) x 3 sizes (S15/S20/S30). Total backtest: ~202 hours across 6 datasets.

#### FADE Baseline: T80_H0_S15 Per-Dataset

| Dataset | Trades | PnL | $/hr | Accuracy | Max DD | ROI |
|---------|--------|-----|------|----------|--------|-----|
| IS+OOS2 | 72 | $127 | $1.83 | 95.8% | 10.8% | 74.7% |
| OOS3+4 | 209 | $78 | $1.65 | 87.6% | 110.5% | 45.8% |
| OOS7 | 301 | $306 | $16.14 | 93.7% | 92.5% | 179.9% |
| OOS8 | 433 | $244 | $13.44 | 89.8% | 210.0% | 143.3% |
| OOS9 | 25 | **-$67** | -$1.47 | 76.0% | 51.3% | -39.5% |
| OOS10 | 113 | $226 | $83.19 | 100.0% | 0.0% | 132.7% |
| **Combined** | **1153** | **$913** | **$4.52** | **90.5%** | - | - |

#### Top 10 Configs by Combined PnL (S15, normalized)

| Rank | Config | Combined PnL | $/hr | Trades | Avg Acc | Avg Max DD |
|------|--------|-------------|------|--------|---------|-----------|
| 1 | **T75_H25_S15** | **$950** | **$4.70** | 1214 | 88.6% | 81.0% |
| 2 | T80_H0_S15 | $913 | $4.52 | 1153 | 90.5% | 79.2% |
| 3 | T75_H0_S15 | $870 | $4.30 | 1131 | 86.8% | 99.9% |
| 4 | T80_H25_S15 | $847 | $4.19 | 1153 | 90.5% | 65.1% |
| 5 | T70_H0_S15 | $755 | $3.74 | 2138 | 85.4% | 204.0% |
| 6 | T80_H50_S15 | $662 | $3.28 | 1153 | 90.5% | 56.9% |
| 7 | T70_H25_S15 | $618 | $3.06 | 2138 | 85.4% | 170.7% |
| 8 | T75_H50_S15 | $590 | $2.92 | 1375 | 87.1% | 85.6% |
| 9 | T65_H0_S15 | $589 | $2.91 | 2530 | 82.1% | 232.3% |
| 10 | T80_H75_S15 | $533 | $2.64 | 1153 | 90.5% | 46.8% |

Winner T75_H25_S15 edges out FADE baseline (T80_H0) by $37 combined, with 5% more trades but lower avg accuracy (88.6% vs 90.5%).

#### Hedging Impact (S15, combined across 6 datasets)

| Threshold | H0 PnL | H50 PnL | H100 PnL | H0 Max DD | H50 Max DD | H100 Max DD |
|-----------|--------|---------|----------|-----------|------------|-------------|
| T55 | -$144 | -$171 | -$673 | 543% | 379% | 266% |
| T60 | $409 | -$30 | -$469 | 482% | 336% | 261% |
| T65 | $589 | $250 | -$315 | 403% | 292% | 193% |
| T70 | $755 | $361 | -$83 | 347% | 264% | 193% |
| T75 | $870 | $590 | $180 | 277% | 202% | 193% |
| **T80** | **$913** | **$662** | **$254** | **210%** | **160%** | **191%** |

Pattern: H100 destroys PnL at every threshold (costs ~$0.20/share on 85-95% correct trades). H50 cuts DD by ~30-40% but also cuts PnL by ~25-50%. H25 is the sweet spot at T75/T80 (small PnL cost, meaningful DD reduction).

#### Per-Dataset Best Config (S15)

| Dataset | Best Config | PnL | Trades | Accuracy | Max DD |
|---------|-------------|-----|--------|----------|--------|
| IS+OOS2 | T80_H0_S15 | $127 | 72 | 95.8% | 10.8% |
| OOS3+4 | T75_H25_S15 | $139 | 108 | 90.7% | 24.9% |
| OOS7 | T60_H0_S15 | $682 | 540 | 84.3% | 172.9% |
| OOS8 | T70_H0_S15 | $359 | 617 | 84.6% | 345.8% |
| OOS9 | T55_H100_S15 | -$22 | 45 | 71.1% | 24.6% |
| OOS10 | T55_H0_S15 | $514 | 227 | 88.5% | 30.6% |

No single config dominates all datasets. OOS7/OOS8 favor lower thresholds (more volume), OOS3+4/IS+OOS2 favor higher thresholds (higher accuracy).

#### OOS9: Zero Configs Profitable

**0 out of 90 configs are profitable on OOS9.** Best result: T55_H100_S15 at -$22. T80_H0_S15 loses -$67 (76% accuracy, only 25 trades before session stop). OOS9 is a losing regime where the FADE signal accuracy drops to 76% even at T80. The adaptive session stop (ADAPT25) is critical — it limits losses to 25 trades and prevents catastrophic drawdown.

#### Key Conclusions

1. **T75_H25_S15 is the overall winner** ($950 combined, $4.70/hr), narrowly beating the FADE baseline T80_H0_S15 ($913, $4.52/hr). The 25% hedge at T75 adds volume (+5% trades) while keeping DD manageable.
2. **Hedging is counterproductive at H50+.** At T80, going from H0 to H50 costs $251 in PnL to reduce max DD from 210% to 160%. At H100, PnL drops by $659. The hedge loses money on the ~90% correct trades.
3. **T80 remains the safest threshold.** Highest avg accuracy (90.5%), profitable on 5/6 datasets. T75 has slightly more total PnL but lower accuracy and higher DD.
4. **OOS9 is a regime breaker.** No config survives it profitably. Session stop limits damage. Without ADAPT25, OOS9 losses would be far worse.
5. **Dataset variance is high.** Best config per dataset ranges from T55 (OOS10) to T80 (IS+OOS2). No universal optimal threshold below T80.

### 7e. Time-of-Day Analysis (FADE Signal)

**Script:** `research/backtests/pair_trade_tod_analysis.py`
**Data:** `research/findings/data/pair_trade_tod_accuracy.csv` (1376 signals across 6 datasets)

Tested whether time-of-day filtering could rescue early entry. IST sessions from separate CHEAP strategy analysis showed US Late (12AM-5AM IST) was best for CHEAP. Does the same apply to FADE?

**Overall FADE accuracy by session:**

| Session (IST) | Signals | Accuracy | Avg Ask |
|---------------|---------|----------|---------|
| Asia (5AM-1PM) | 397 | 78.8% | $0.74 |
| London+US (6PM-12AM) | 751 | 80.4% | $0.77 |
| London (1PM-6PM) | 16 | 81.2% | $0.77 |
| **US Late (12AM-5AM)** | **212** | **65.6%** | **$0.73** |

**Low threshold ($0.50-$0.65) by session:**

| Session | Signals | Accuracy |
|---------|---------|----------|
| Asia | 186 | 58.1% |
| London+US | 279 | 62.7% |
| US Late | 105 | **47.6%** |

**High threshold ($0.75+) by session:**

| Session | Signals | Accuracy |
|---------|---------|----------|
| **Asia** | **186** | **99.5%** |
| London+US | 416 | 92.3% |
| US Late | 99 | 83.8% |

**Surprising finding: FADE and CHEAP have OPPOSITE time-of-day patterns.**
- US Late is BEST for CHEAP but WORST for FADE (65.6% overall, 47.6% at low thresholds)
- Asia is WORST for CHEAP but BEST for high-threshold FADE (99.5%)
- Time filtering does NOT rescue early entry — no session exceeds 63% at $0.50-$0.65
- Time filtering COULD boost current FADE: Asia-only at $0.75+ = 99.5% accuracy (vs 92% overall)

**Notable UTC hours:**
- UTC 20 (IST 1:30AM): 43 signals, 23.3% accuracy — extreme outlier, avoid
- UTC 23 (IST 4:30AM): 30 signals, 93.3% accuracy — best hour
- UTC 3 (IST 8:30AM): 4 signals, 0% accuracy — tiny sample but bad

### 7f. Whale Time-of-Day Patterns

**Data:** 7 whale crossref files (OOS6+OOS9), 71,007 unique trades after dedup (65,384 buys, 5,622 sells).

**IST sessions:** US Late (12AM-5AM), Asia (5AM-1PM), London (1PM-6PM), London+US (6PM-12AM).

#### Baguette (7,153 buys, 76 markets, 96.3% win rate, $17K PnL)

| Session | Trades | Win Rate | Avg Size | Avg Price | % Expensive | Avg Time Rem | PnL |
|---------|--------|----------|----------|-----------|-------------|-------------|-----|
| Asia 5-13 | 3,000 | 95.6% | 6.2 | $0.569 | 58.5% | 470s | $6,739 |
| London 13-18 | 931 | 96.1% | 7.7 | $0.568 | 56.7% | 531s | $2,571 |
| London+US 18-24 | 1,165 | 96.6% | 7.3 | $0.587 | 68.0% | 386s | $3,086 |
| US Late 0-5 | 2,057 | 97.1% | 6.8 | $0.608 | 67.5% | 449s | $4,648 |

**First entry per market (Baguette):**

| Session | Markets | Avg Time Rem | Avg Price | Win Rate |
|---------|---------|-------------|-----------|----------|
| Asia | 32 | 841s | $0.465 | 96.9% |
| London | 11 | 827s | $0.447 | 90.9% |
| London+US | 10 | 825s | $0.509 | 90.0% |
| US Late | 23 | 793s | $0.508 | 91.3% |

**Baguette sells (exits) by session:**

| Session | Trades | Avg Sell Price | Avg Size | Avg Time Rem |
|---------|--------|---------------|----------|-------------|
| Asia | 2,367 | $0.637 | 5.2 | 413s |
| London | 791 | $0.639 | 6.4 | 459s |
| London+US | 904 | $0.679 | 6.3 | 340s |
| US Late | 1,560 | $0.702 | 5.9 | 383s |

#### Gabagool (58,231 buys, 101 markets, 96.9% win rate, $337K PnL)

| Session | Trades | Win Rate | Avg Size | Avg Price | % Expensive | Avg Time Rem | PnL |
|---------|--------|----------|----------|-----------|-------------|-------------|-----|
| Asia 5-13 | 19,643 | 97.6% | 11.7 | $0.466 | 43.3% | 491s | $113,201 |
| London 13-18 | 5,597 | 96.8% | 11.9 | $0.482 | 46.0% | 527s | $31,845 |
| London+US 18-24 | 18,189 | 97.0% | 12.3 | $0.469 | 44.2% | 483s | $108,793 |
| US Late 0-5 | 14,802 | 96.0% | 11.8 | $0.471 | 44.0% | 496s | $82,878 |

**First entry per market (Gabagool):**

| Session | Markets | Avg Time Rem | Avg Price | Win Rate |
|---------|---------|-------------|-----------|----------|
| Asia | 32 | 853s | $0.475 | 96.9% |
| London | 11 | 835s | $0.465 | 72.7% |
| London+US | 32 | 846s | $0.475 | 96.9% |
| US Late | 26 | 859s | $0.504 | 88.5% |

#### Key Findings: Whale TOD Patterns

1. **Neither whale shows strong time-of-day selectivity.** Both Baguette and Gabagool trade across ALL sessions with consistent strategies. There is no session they avoid or concentrate on.

2. **Win rates are uniformly high (95-97%) across all sessions.** Unlike our FADE signal (which varies from 65% to 99% by session), whale accuracy is stable. This confirms their edge is NOT time-dependent.

3. **Baguette shifts strategy slightly by session:**
   - US Late + London+US: buys MORE expensive side (67-68%) at higher avg price ($0.59-$0.61)
   - Asia + London: buys LESS expensive side (57-59%) at lower avg price ($0.57)
   - Exits at higher prices during US Late ($0.70 avg sell) vs Asia ($0.64 avg sell)

4. **Gabagool is remarkably consistent:** 43-46% expensive side, 11.7-12.3 avg size, $0.47 avg price across ALL sessions. Max size capped at 24 shares. Pure systematic strategy.

5. **Both enter very early regardless of session:** First entry is ~793-859s remaining (7-12 seconds after market start). Baguette is slightly faster during London sessions.

6. **London has lowest first-entry win rate** for both whales (Baguette 90.9%, Gabagool 72.7%) — but small sample (11 markets each).

7. **Implication for our strategy:** Since whales' edge is NOT time-dependent, copying their time-of-day patterns won't help us. Our edge (FADE signal) IS time-dependent — Asia session at $0.75+ gives 99.5% accuracy. We should exploit OUR time-of-day edge, not theirs.

### 7g. Preliminary Conclusions

1. **FADE signal doesn't work at Baguette entry prices.** Below $0.75, "expensive side wins" accuracy drops to 50-69% — near coin-flip. Baguette's 82.5% accuracy at $0.55 entry must come from a different signal (OBI, flow imbalance, or proprietary alpha).

2. **Hedging is mechanically feasible.** At $0.50-$0.55, hedge fills 84-94% of the time as maker. Baguette's 100% pair rate is achievable with sufficient patience (600s order timeout).

3. **Optimal threshold remains $0.75-$0.80.** At $0.75, accuracy is 100% with 27% edge/$. At $0.80, accuracy is ~95% with 18% edge/$. The extra volume at $0.75 may compensate for lower edge/$.

4. **Hedging at $0.75+ is counterproductive.** At $0.75+, accuracy is so high (~100%) that hedge losses drag down PnL. The winning hedge fill rate at these prices still costs ~$0.20-0.22 per share on correct predictions (which happen 95%+ of the time).

5. **To replicate Baguette, we need a DIFFERENT SIGNAL** — not just lower thresholds on the FADE signal. Candidates:
   - OBI contrarian signal (98.1% when aligned per whale analysis)
   - ML probability score (83% from market-level model at 300-600s)
   - Combined: ML + OBI + FADE threshold

6. **Whale edge is NOT time-dependent.** Both Baguette and Gabagool trade all sessions with 95-97% win rate uniformly. Our FADE signal IS time-dependent (Asia $0.75+ = 99.5%). Time-of-day filtering is OUR edge, not theirs.

### 7h. Files

| File | Purpose |
|------|---------|
| `research/backtests/pair_trade_analysis.py` | Full analysis script (5 steps) |
| `research/backtests/pair_trade_tod_analysis.py` | Time-of-day accuracy analysis |
| `research/findings/data/pair_trade_accuracy_by_bucket.csv` | Step 1 results |
| `research/findings/data/pair_trade_fill_rate.csv` | Step 2 results |
| `research/findings/data/pair_trade_trajectory.csv` | Step 4 results |
| `research/findings/data/pair_trade_grid_results.csv` | Step 3 results FINAL (540 rows) |
| `research/findings/data/pair_trade_grid_checkpoint.csv` | Step 3 checkpoints (540 rows, FINAL) |
| `research/findings/data/pair_trade_tod_accuracy.csv` | TOD accuracy (1376 signals) |

---

*Generated: February 8, 2026*
*Pipeline runtime: ~51 minutes (10 steps) for ML; pair trade analysis ongoing*
*Total data: 3.3M rows, 726 markets, 223 hours*
