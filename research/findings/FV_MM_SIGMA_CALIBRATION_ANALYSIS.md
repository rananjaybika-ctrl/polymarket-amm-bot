# FV MM Sigma Calibration Analysis — All 6 Datasets

**Date:** February 10, 2026
**Purpose:** Thorough volatility profiling of all 6 datasets to understand why the FV model's sigma is miscalibrated, and what an OU-style calibration needs to fix.
**Script:** `/tmp/vol_analysis_all_datasets.py`

---

## Executive Summary

The FV model fails because `d = ln(S/K) / (sigma * sqrt(T))` produces values that are too large in low-vol regimes, making the model overconfident. The EWMA sigma underestimates realized vol by up to 1.82x in calm periods but tracks it well in active periods. A flat multiplier won't fix this — the ratio is regime-dependent (0.86x to 1.82x). OU-style adaptive calibration could prevent sigma from decaying too far below the long-run mean during calm periods.

---

## 1. Cross-Dataset Summary

| Dataset | Type | Hours | BTC Range | Mean |15m Ret| | Realized σ/sec | EWMA σ/sec | Ratio (R/E) | d(10bps,600s) | Label |
|---------|------|-------|-----------|-----------------|---------------|------------|-------------|--------------|-------|
| **IS+OOS2** | Train | 23h | $91.9K-$95.5K | 0.098% | 0.0000657 | 0.0000362 | **1.82x** | **1.1** | LOW-MOD |
| **OOS3+4** | Train | 47h | $88.5K-$91.2K | 0.128% | 0.0000652 | 0.0000607 | 1.07x | 0.7 | LOW-MOD |
| **OOS9** | Train | 46h | $76.8K-$79.2K | 0.239% | 0.000101 | 0.000112 | 0.90x | 0.4 | MODERATE |
| **OOS7** | Holdout | 19h | $81.1K-$85.6K | 0.254% | 0.000127 | 0.000126 | 1.01x | 0.3 | HIGH |
| **OOS8** | Holdout | 24h | $75.7K-$84.1K | 0.259% | 0.000126 | 0.000146 | 0.86x | 0.3 | HIGH |
| **OOS10** | Holdout | ~8h | $71.6K-$74.1K | 0.458% | 0.000189 | (NaN) | — | 0.2 | EXTREME |

**Key:** Ratio = Realized / EWMA. >1.0 means EWMA underestimates. d = what the N(d2) model sees for a 10bps price move at T=600s.

---

## 2. Detailed Dataset Profiles

### IS+OOS2 (Jan 18-19) — LOW-MOD VOL, CRASH EVENT

- **Period:** 2026-01-18 06:03 to 2026-01-19 05:30 UTC
- **Price:** $95,175 → $92,658 (-2.64%)
- **Character:** 76% CALM, then EXTREME crash at 23:00 UTC Jan 18 ($95.5K → $91.9K)
- **Return stats:** Skew -3.9, Kurtosis 20.9 — one big crash event dominates
- **Autocorrelation:** +0.26 (TRENDING — cascade/waterfall)
- **EWMA underestimates by 1.82x** — worst dataset for FV. d=1.1 = model overconfident on every trade

**Regime distribution:**
- CALM (<0.10% 15m vol): 76.4%
- NORMAL (0.10-0.25%): 18.0%
- ACTIVE (0.25-0.50%): 3.4%
- HOT (>0.50%): 2.2%

**Hourly price trajectory:**
```
01-18 06:00-22:00  Quiet drift: $95,175 → $95,499 (range $94,910-$95,531)
                   Mostly LOW/MODERATE, hourly ranges 0.05-0.47%
01-18 23:00        CRASH: $95,499 → $93,673 (-1.91%, range 1.97%)
01-19 00:00        Continuation: $93,673 → $92,711 (-1.03%, range 1.88%)
01-19 01:00-05:00  Recovery chop: $92,711 → $92,658, MODERATE-HIGH vol
```

**Sigma calibration:**
- EWMA sigma median: 0.0000362/sec
- Realized sigma: 0.0000657/sec
- d with EWMA at 10bps/600s: **1.1** → N(1.1) = 0.86 → model says 86% confident (should be ~55-60%)

---

### OOS3+4 (Jan 22-24) — LOW-MOD VOL, US SESSION SPIKY

- **Period:** 2026-01-22 09:23 to 2026-01-24 08:32 UTC
- **Price:** $90,002 → $89,616 (-0.43%)
- **Character:** Asian hours CALM, US session (14-20 UTC) goes HIGH/EXTREME every day
- **Return stats:** Skew +0.05, Kurtosis 5.2 — symmetric fat tails
- **Autocorrelation:** -0.16 (MEAN-REVERTING — chop/range-bound)
- **EWMA tracks realized well (1.07x)** — best calibration case

**Regime distribution:**
- CALM: 45.4%
- NORMAL: 41.0%
- ACTIVE: 13.6%
- HOT: 0.0%

**Hourly price trajectory:**
```
01-22 09:00-12:00  Quiet: $90,002 → $90,021 (range ~0.15-0.28%)
01-22 13:00-17:00  US SESSION VOLATILITY: Range 0.66-1.38% per hour
                   Jan 22: $90,021 → $89,550 (sold off then recovered)
01-22 18:00-23:00  Evening: MODERATE-HIGH, range 0.18-1.02%
01-23 00:00-07:00  Asian: MODERATE, range 0.13-0.55%
01-23 08:00-12:00  London: Slow grind lower, $89,569 → $89,268
01-23 14:00-18:00  US SESSION AGAIN: 1.0-1.45% hourly ranges
                   Big pump: $89,018 → $90,600 then fade
01-23 19:00-24:00  Evening fade: $90,530 → $89,600
01-24 00:00-08:00  Quiet Asian: $89,600 → $89,616 (LOW-MODERATE)
```

**Sigma calibration:**
- EWMA sigma median: 0.0000607/sec
- Realized sigma: 0.0000652/sec
- d with EWMA at 10bps/600s: **0.7** → N(0.7) = 0.76 → still too confident but less extreme

---

### OOS9 (Feb 1-3) — MODERATE VOL, PERSISTENTLY ACTIVE

- **Period:** 2026-02-01 12:39 to 2026-02-03 10:13 UTC
- **Price:** $78,497 → $78,546 (+0.06%)
- **Character:** **Zero CALM periods.** Entire dataset is active. Persistent 0.2-0.5% 15m moves.
- **Return stats:** Skew -0.22, Kurtosis 0.12 — **near-perfect Gaussian** (cleanest dataset)
- **Autocorrelation:** +0.03 (RANDOM WALK)
- **EWMA slightly overestimates (0.90x)** — model would be less confident

**Regime distribution:**
- CALM: **0.0%**
- NORMAL: 65.3%
- ACTIVE: 30.8%
- HOT: 4.0%

**Hourly price trajectory:**
```
02-01 12:00-16:00  Selloff: $78,497 → $77,495 (crash to $76,762 at 15:00)
                   Hourly ranges 0.21-1.91% — EXTREME
02-01 17:00-20:00  Recovery attempt: $77,495 → $77,376 (volatile, 0.58-1.42%)
                   [GAP — no data Feb 1 21:00 to Feb 2 17:00]
02-02 17:00-23:00  HIGH-EXTREME: Range $77,889-$79,214, 0.41-0.99%/hr
02-03 00:00-06:00  Wild swings: $78,738→$79,065→$77,808→$78,753→$78,240→$78,594
                   Every hour 0.65-1.62% range
02-03 07:00-10:00  Settling: $78,594 → $78,546 (still 0.27-1.13% ranges)
```

**Sigma calibration:**
- EWMA sigma median: 0.000112/sec
- Realized sigma: 0.000101/sec
- d with EWMA at 10bps/600s: **0.4** → N(0.4) = 0.66 → more reasonable

---

### OOS7 (Jan 29-30) — HIGH VOL, CRASH + RECOVERY

- **Period:** 2026-01-29 16:05 to 2026-01-30 11:05 UTC
- **Price:** $84,786 → $82,828 (-2.31%)
- **Character:** Major crash Jan 30 01:00 ($84.2K → $81.1K in 1 hour = 3.6% range)
- **Return stats:** Skew -2.07, Kurtosis 9.6 — left-tail crash event
- **Autocorrelation:** -0.06 (RANDOM WALK)
- **EWMA matches realized perfectly (1.01x)**

**Regime distribution:**
- CALM: 6.7%
- NORMAL: 59.8%
- ACTIVE: 28.4%
- HOT: 5.0%

**Hourly price trajectory:**
```
01-29 16:00-18:00  Volatile session: $84,786 → $83,477
                   Range 1.33-1.91% per hour — EXTREME
01-29 19:00-23:00  Recovery + consolidation: $83,477 → $84,650
                   Ranges 0.35-1.13% — HIGH to EXTREME
01-30 00:00-02:00  CRASH: $84,650 → $82,165
                   01:00 hour: $84,192 → $82,468 (3.65% range!)
01-30 03:00-08:00  Recovery chop: $82,165 → $82,672
                   Still EXTREME ranges 0.55-1.04%
01-30 09:00-11:00  Settling: $82,672 → $82,828 (0.05-0.88% range)
```

**Sigma calibration:**
- EWMA sigma median: 0.000126/sec
- Realized sigma: 0.000127/sec
- d with EWMA at 10bps/600s: **0.3** → N(0.3) = 0.62 → reasonable

---

### OOS8 (Jan 31) — HIGH VOL, MASSIVE SELLOFF

- **Period:** 2026-01-31 05:52 to 2026-02-01 06:09 UTC
- **Price:** $84,017 → $78,624 (**-6.42%** — largest decline of any dataset)
- **Character:** US session (14-18 UTC) was catastrophic: 2.5-4.3% hourly ranges. Most volatile dataset overall.
- **Return stats:** Skew -1.21, Kurtosis 5.4 — heavy left tail
- **Autocorrelation:** -0.04 (RANDOM WALK)
- **EWMA slightly overestimates (0.86x)** — reacts to crash with elevated readings

**Regime distribution:**
- CALM: 6.0%
- NORMAL: 48.4%
- ACTIVE: 31.8%
- HOT: **13.8%** — most HOT time of any dataset

**Hourly price trajectory:**
```
01-31 05:00-07:00  Early decline: $84,017 → $83,547 (0.06-0.49%)
01-31 08:00-13:00  Accelerating: $83,547 → $82,768
                   08:00 crash starts (1.28% range)
01-31 14:00        CLIFF: $82,768 → $81,480 (-1.56%, 2.54% range)
01-31 15:00-16:00  Continuation: $81,480 → $80,365 (0.86-2.59% ranges)
01-31 17:00        CAPITULATION: $80,365 → $78,894 (-1.83%, 2.96% range)
01-31 18:00        BOTTOM: Low of $75,728 (4.33% hourly range!)
01-31 19:00-22:00  Bouncing: $78,038 → $78,155 (0.64-1.76% ranges)
01-31 23:00-02:00  Recovery: $78,155 → $78,900 (0.43-1.49% ranges)
02-01 03:00-06:00  Settling: $78,900 → $78,624 (0.09-0.61%)
```

**Sigma calibration:**
- EWMA sigma median: 0.000146/sec
- Realized sigma: 0.000126/sec
- d with EWMA at 10bps/600s: **0.3** → N(0.3) = 0.62 → reasonable

---

### OOS10 (Feb 4-5) — EXTREME VOL, ALL HOURS

- **Period:** 2026-02-04 19:00 to 2026-02-05 02:57 UTC (~8 hours)
- **Price:** $73,582 → $71,755 (-2.5%)
- **Character:** Every single hour EXTREME or HIGH. No calm periods whatsoever.
- **Return stats:** Skew +0.67, Kurtosis 0.82 — surprisingly Gaussian for extreme vol
- **Autocorrelation:** -0.32 (MEAN-REVERTING — violent whipsaws)
- **Data quality issue:** Some rows have epoch-0 timestamps → NaN in EWMA computation

**Regime distribution:**
- CALM: **0.0%**
- NORMAL: 8.6%
- ACTIVE: **64.6%**
- HOT: **26.7%**

**Hourly price trajectory:**
```
02-04 19:00  $73,582 → $73,923 (1.31% range)
02-04 20:00  $73,923 → $73,516 (0.97% range)
02-04 21:00  $73,516 → $72,800 (-0.97%, 1.96% range)
02-04 22:00  $72,800 → $72,543 (2.46% range — whipsaw)
02-04 23:00  $72,543 → $73,166 (+0.86%, 1.45% range)
02-05 00:00  $73,166 → $72,868 (0.78% range)
02-05 01:00  $72,868 → $72,421 (1.85% range)
02-05 02:00  $72,421 → $71,755 (-0.92%, 1.69% range)
```

**Sigma calibration:**
- EWMA sigma: NaN (bad data)
- Realized sigma: 0.000189/sec
- d with realized sigma at 10bps/600s: **0.22** → N(0.22) = 0.59 → model barely has an opinion

---

## 3. The Core Calibration Problem

### d-Value Determines Everything

The FV model computes `P(UP) = N(d)` where `d = ln(S/K) / (sigma * sqrt(T))`.

For a typical trade setup — BTC 10bps away from strike, 10 minutes remaining:

| Dataset | d with EWMA | N(d) = Model Confidence | Problem |
|---------|-------------|------------------------|---------|
| IS+OOS2 | **1.1** | **86%** | Way too confident — snaps to 0/1 |
| OOS3+4 | 0.7 | 76% | Still too confident |
| OOS9 | 0.4 | 66% | Getting reasonable |
| OOS7 | 0.3 | 62% | Reasonable |
| OOS8 | 0.3 | 62% | Reasonable |
| OOS10 | 0.2 | 59% | Model barely opinionated |

**Target:** d should be 0.3-0.5 for the model to produce reasonable probabilities. This means sigma needs to be ~0.00008-0.00013/sec.

### EWMA-to-Realized Ratio Is Regime-Dependent

| Dataset | Ratio (Realized/EWMA) | Vol Regime |
|---------|-----------------------|-----------|
| IS+OOS2 | **1.82x** | LOW-MOD (mostly CALM) |
| OOS3+4 | 1.07x | LOW-MOD (mixed CALM/ACTIVE) |
| OOS9 | 0.90x | MODERATE (persistent) |
| OOS7 | 1.01x | HIGH |
| OOS8 | 0.86x | HIGH |

**Pattern:** In CALM regimes, EWMA decays toward zero and massively underestimates. In ACTIVE regimes, EWMA tracks well or slightly overestimates (reacts to recent vol).

**A flat multiplier cannot fix this.** The ratio swings from 0.86x to 1.82x depending on regime.

### Why OU Calibration Could Fix This

The OU process for sigma would be:
```
d(sigma) = theta * (mu - sigma) * dt + xi * dW
```

Where:
- `mu` = long-run sigma level (floor that prevents EWMA from decaying to zero)
- `theta` = mean-reversion speed (how fast sigma returns to mu)
- `xi` = vol of vol (noise term)

**In calm regimes (IS+OOS2):** Raw EWMA sigma → 0.000036. With OU, sigma would be pulled up toward mu (~0.00008-0.00010), preventing the d-value blowup.

**In active regimes (OOS9, OOS7, OOS8):** EWMA sigma is already close to realized. OU would let it track freely since it's above mu.

This is exactly analogous to how `ou_calibration.py` prevents the BTC deviation z-score from being too sensitive in calm periods.

---

## 4. Hourly Volatility Profiles (All Datasets Combined)

Aggregated hour-of-day labels across all 6 datasets:

| Hour (UTC) | IS+OOS2 | OOS3+4 | OOS9 | OOS7 | OOS8 | OOS10 | Consensus |
|------------|---------|--------|------|------|------|-------|-----------|
| 0 | EXTREME | LOW | HIGH | MOD | EXTREME | EXTREME | HIGH |
| 1 | LOW | LOW | LOW | EXTREME | HIGH | EXTREME | HIGH |
| 2 | LOW | LOW | EXTREME | EXTREME | HIGH | EXTREME | HIGH |
| 3 | MOD | LOW | HIGH | MOD | LOW | — | MODERATE |
| 4 | DEAD | LOW | HIGH | MOD | LOW | — | LOW-MOD |
| 5 | DEAD | LOW | MOD | HIGH | MOD | — | LOW-MOD |
| 6 | DEAD | MOD | HIGH | EXTREME | MOD | — | MODERATE |
| 7 | DEAD | LOW | MOD | HIGH | MOD | — | LOW-MOD |
| 8 | DEAD | LOW | HIGH | MOD | HIGH | — | MODERATE |
| 9 | DEAD | LOW | EXTREME | MOD | MOD | — | MODERATE |
| 10 | LOW | DEAD | MOD | MOD | LOW | — | LOW |
| 11 | DEAD | LOW | — | DEAD | LOW | — | LOW |
| 12 | DEAD | DEAD | MOD | — | MOD | — | LOW |
| 13 | LOW | MOD | MOD | — | MOD | — | MODERATE |
| **14** | LOW | **HIGH** | **HIGH** | — | **EXTREME** | — | **HIGH** |
| **15** | DEAD | **EXTREME** | **EXTREME** | — | MOD | — | **HIGH** |
| **16** | LOW | MOD | MOD | **HIGH** | **EXTREME** | — | **HIGH** |
| **17** | LOW | **HIGH** | MOD | **HIGH** | **EXTREME** | — | **HIGH** |
| **18** | LOW | MOD | **HIGH** | **EXTREME** | **EXTREME** | — | **HIGH** |
| **19** | DEAD | MOD | MOD | **HIGH** | **EXTREME** | EXTREME | **HIGH** |
| 20 | LOW | HIGH | MOD | MOD | HIGH | HIGH | HIGH |
| 21 | LOW | MOD | HIGH | MOD | HIGH | EXTREME | HIGH |
| 22 | DEAD | MOD | MOD | MOD | MOD | EXTREME | MODERATE |
| 23 | EXTREME | LOW | MOD | LOW | HIGH | EXTREME | MODERATE |

**US market hours (14-20 UTC / 9am-3pm ET) are consistently the most volatile.** This is where the FV model would have the best sigma calibration (EWMA tracks realized) but also where 15m markets move the most.

---

## 5. Return Distribution Characteristics

| Dataset | Skew | Kurtosis | >1σ | >2σ | >3σ | Character |
|---------|------|----------|-----|-----|-----|-----------|
| IS+OOS2 | -3.89 | 20.9 | 10.5% | 4.2% | 2.1% | Crash-dominated, extreme left tail |
| OOS3+4 | +0.05 | 5.2 | 17.1% | 5.3% | 2.7% | Symmetric fat tails |
| OOS9 | -0.22 | 0.1 | 29.7% | 6.9% | 0.0% | Near-Gaussian |
| OOS7 | -2.07 | 9.6 | 19.5% | 3.9% | 1.3% | Crash left tail |
| OOS8 | -1.21 | 5.4 | 19.4% | 6.1% | 2.0% | Moderate left tail |
| OOS10 | +0.67 | 0.8 | 28.1% | 6.2% | 0.0% | Near-Gaussian, slight right tail |
| **Gaussian** | 0 | 0 | 31.7% | 4.6% | 0.3% | Reference |

**Key insight:** High kurtosis datasets (IS+OOS2, OOS7) have rare but extreme moves that the EWMA doesn't anticipate. The N(d2) model with EWMA sigma will be maximally wrong during these tail events — precisely when the model is most confident.

---

## 6. Next Steps: OU Calibration of Sigma

### What to calibrate:
1. Compute rolling EWMA sigma time series for each dataset
2. Fit OU process parameters (mu, theta, xi) to the sigma time series
3. Use OU-adjusted sigma in N(d2): `sigma_adj = max(sigma_ewma, mu_ou)`
4. Ensure d stays in 0.3-0.5 range across all regimes

### Existing OU params (from `ou_params_combined_1s.json`):
- mu: -6.40 (log-space)
- theta: 0.00358/sec (half-life 194s)
- xi: 1.70
- sigma_stat: 1.71

### Success criteria:
- d-value in [0.3, 0.8] for 10bps move at T=600s across ALL datasets
- Ratio (Realized/OU-adjusted) between 0.8x and 1.2x across all regimes
- FV model accuracy above 50% (currently 0-35%)

---

*Analysis date: February 10, 2026*
*Data: 6 datasets, 160+ hours, $71.6K-$95.5K BTC price range*
