# Analysis: Is Velocity Useless? Why Is AGGRESSIVE Profitable?

**Context:** Phase 2 A-S backtest found velocity has 2.5% autocorrelation (useless). User asks: if velocity is useless, how does AGGRESSIVE make $9-17/hr?

---

## Short Answer

**AGGRESSIVE does NOT use velocity.** It uses a completely different signal: **OU threshold spike detection on EWMA z-score**. The two strategies use fundamentally different alpha sources.

The Phase 2 claim "velocity is useless" is narrowly correct: velocity as a **standalone, independent signal for A-S quote skewing** has no predictive power. But this says nothing about AGGRESSIVE, which never used velocity in the first place.

---

## What Each Strategy Actually Uses

| | A-S Market Making | AGGRESSIVE |
|--|-------------------|------------|
| **Alpha signal** | EWMA z-score direction | OU threshold spike detection |
| **How it enters** | Passive bid on predicted winner | Aggressive taker on winner side |
| **What velocity does** | Tried as independent skew signal | Not used at all |
| **Exit** | Hold to resolution | Time-stop (120s) + passive hedge fill |
| **Hedge** | None (directional carry) | Full hedge on loser side |
| **Result** | $2.84/hr best | $9-17/hr |

### AGGRESSIVE config (from `AGGRESSIVE.md`):
```python
threshold_method = "ou"      # OU adaptive sigmoid on z-score
zscore_method = "ewma"       # EWMA (adaptive, no drift)
lookback_ticks = 72          # 1200ms at 60Hz
time_stop_seconds = 120.0    # Force exit after 120s
z_lo = 0.0, z_hi = 1.5      # Z-zone filter
```

No velocity parameter exists in the AGGRESSIVE config. The signal is entirely z-score based.

---

## Why AGGRESSIVE Is Profitable (Not Just Probability/Risk Management)

AGGRESSIVE has a **real alpha source**: detecting BTC price spikes on Binance before Polymarket prices adjust. This is NOT just probability and risk management. Here's the evidence:

### 1. Direction Accuracy = Real Alpha (68-72% across 157 hours)

| Period | Hours | Dir Accuracy | $/hr @50sh |
|--------|-------|-------------|------------|
| IS (Jan 16-19) | 81.7 | **68.9%** | $7.76 |
| OOS3 (Jan 22-23) | 26.4 | **70.2%** | $17.59 |
| OOS4 (Jan 23-24) | 24.2 | **72.4%** | $16.72 |

68-72% direction accuracy **across 3 independent time periods** is not luck. If it were random, you'd expect 50%. The consistency (68.9% → 70.2% → 72.4%) across out-of-sample data shows a genuine information edge.

### 2. The Information Edge: Binance → Polymarket Lag

The lag between Binance and Polymarket is **real and documented**:
- Polymarket prices lag Binance by **0.6-2.35 seconds** (from `VELOCITY_EDGE_ANALYSIS_JAN11.md`)
- When BTC spikes on Binance, the OU threshold detects it before Polymarket orderbook adjusts
- AGGRESSIVE takes the winner side at the stale Polymarket ask price

This is a **structural microstructure edge**, not a probabilistic trick.

### 3. Risk Management PRESERVES Alpha (Doesn't Create It)

The risk management layers protect the edge but are not the source:

| Component | Role | Effect |
|-----------|------|--------|
| **OU threshold** | **Alpha source** | Detects spikes with 68-72% accuracy |
| Z-zone (0 < z < 1.5) | Filter | Removes LOW regime (42.5% accuracy < coin flip) |
| Time-stop (120s) | Risk mgmt | Cuts losers, +24% hourly rate vs 180s |
| Full hedge | Risk mgmt | Limits downside to spread cost |
| Cycling | Throughput | Re-enters after exit, +28% trade count |
| Skip >= $0.90 | Protection | Avoids unhedgeable "turkey" losses |

Without the OU threshold alpha, the filters and risk management alone would produce ~50% accuracy = breakeven or loss.

---

## What "Velocity Is Useless" Actually Means

From `SIGNAL_ACCURACY_FINDINGS.md`, the regression analysis shows:

### Velocity as standalone predictor:
```
velocity_bps coefficient: -0.0335, P>|t| = 0.5744 (NOT SIGNIFICANT)
```
Velocity alone explains essentially **zero** variance in direction outcome.

### BUT spike × velocity interaction:
```
spike × velocity interaction: p=0.001 *** (HIGHLY SIGNIFICANT)
R² jumps from 0.017 to 0.086 (5x improvement)
```

### What this means:
- **Velocity alone** → useless (p=0.574, random noise)
- **Spike alone** → also useless on its own (p=0.526)
- **Spike × Velocity together** → highly significant (p=0.001)

### Why AGGRESSIVE doesn't need velocity explicitly:
The OU threshold already captures the **spike magnitude** implicitly. When BTC spikes sharply, the z-score crosses the OU threshold. This IS the spike signal. The velocity component of the interaction is redundant because:
1. A large spike inherently implies high velocity over the lookback window
2. The EWMA z-score already encodes recent price momentum
3. The 120s time-stop exits before any velocity regime change matters

### Why A-S velocity modes failed:
The A-S backtest tried using velocity as an **independent, additive** signal to decide which side to quote:
```python
# A-S approach (FAILED):
if velocity_bps >= 0.10:
    quote UP only  # Velocity predicts UP winning
```

This treats velocity as an independent predictor (p=0.574 = no better than random). The correct usage would require the multiplicative interaction with spike magnitude, but A-S market making doesn't have discrete "spikes" - it quotes continuously.

---

## The Two Different "Velocity" Concepts

There are actually **three** different things called "velocity" in the codebase:

| Concept | Definition | Where Used | Useful? |
|---------|-----------|------------|---------|
| **BTC velocity (bps)** | `(price_now - price_3s_ago) / price_3s_ago × 10000` | A-S backtest | **NO** as standalone signal |
| **Velocity timing edge** | Binance→Polymarket lag (1-5s) | Jan 11 analysis | **YES** but $0.23/hr (throughput limited) |
| **OU threshold spike** | Z-score crosses adaptive sigmoid | AGGRESSIVE | **YES** - primary alpha ($9-17/hr) |

The Phase 2 claim "velocity is useless" refers specifically to #1 (BTC velocity in bps as a standalone directional signal for A-S). AGGRESSIVE uses #3, which is a fundamentally different signal.

---

## Why A-S at $2.84/hr vs AGGRESSIVE at $9-17/hr

| Factor | A-S ($2.84/hr) | AGGRESSIVE ($9-17/hr) |
|--------|----------------|----------------------|
| **Entry method** | Passive bid (cheaper, more fills) | Aggressive taker (instant, fewer fills) |
| **Signal** | EWMA z-score direction | OU threshold spike |
| **Accuracy** | ~62% | ~70% |
| **Exit** | Hold to resolution | Time-stop + hedge |
| **Key advantage** | Cheap entries ($0.53 avg) | Fast cycling (120s) + hedge protection |
| **Limiting factor** | Entry cost × accuracy | Spike frequency |

AGGRESSIVE wins because:
1. **Higher accuracy** (70% vs 62%)
2. **Full hedge** limits losses to spread cost when wrong
3. **120s cycling** allows ~6 trades/hr vs A-S holding positions to resolution
4. **Time-stop** cuts losers that would otherwise lose the full entry cost

---

## Summary

| Claim | Verdict |
|-------|---------|
| "Velocity is useless" | **Correct** for A-S standalone signal (p=0.574) |
| "AGGRESSIVE uses velocity" | **False** - uses OU threshold spike detection |
| "AGGRESSIVE is profitable just from probability/risk mgmt" | **False** - real structural alpha (Binance lag, 68-72% accuracy) |
| "Risk management helps" | **True** - time-stops, hedging, z-zone PRESERVE the alpha |
| "You need both spike AND velocity for alpha" | **True** for interaction term (p=0.001), but OU threshold implicitly captures both |

**Bottom line:** AGGRESSIVE's profitability comes from a genuine microstructure edge (Binance→Polymarket information lag detected via OU threshold). Risk management preserves this edge but doesn't create it. Velocity as an independent signal is indeed useless, but that's irrelevant to AGGRESSIVE which never used it.
