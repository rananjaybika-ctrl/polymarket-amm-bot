# Directional Market Making Strategies

**Status:** REFERENCE DOCUMENT
**Last Updated:** January 28, 2026

---

## Overview

This document catalogs 7 directional market making strategies for Polymarket BTC prediction markets. Each strategy combines different alpha sources with market making mechanics.

---

## Strategy Summary

| # | Strategy | Alpha Source | Status | Data Required |
|---|----------|--------------|--------|---------------|
| 1 | Avellaneda-Stoikov | Spread + Signal Skew | Have data | velocity, time, orderbook |
| 2 | Inventory-Skewed | Mean Reversion | Have data | inventory, velocity, orderbook |
| 3 | Regime-Adaptive | Vol Timing | Have data | z_score, flow imbalance, time |
| 4 | **OBI Alpha** | Microstructure | **Collecting** | Full orderbook depth (5 levels) |
| 5 | Theta Harvesting | Time Decay | Have data | time_remaining, price paths |
| 6 | Cross-Market | Latency (= AGGRESSIVE) | Have data | Binance feed, Poly orderbook |
| 7 | VPIN Avoidance | Anti-Adverse Selection | Partial | Trade tape with timestamps |

---

## Validated Strategies (Separate Docs)

| Strategy | $/hr | Win Rate | Config | Doc |
|----------|------|----------|--------|-----|
| AGGRESSIVE | $7.76-17.59 | 68-72% dir acc | TIME120s_SKIP | [AGGRESSIVE.md](AGGRESSIVE.md) |
| CONTRARIAN | $9-12 | 43% WR (+8pp edge) | Hold-to-resolution | [CONTRARIAN.md](CONTRARIAN.md) |

---

## Strategy 1: Avellaneda-Stoikov with Alpha Signal

### Academic Foundation
- [Avellaneda & Stoikov (2008)](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf) - Original optimal MM framework
- [Cartea & Wang (2019)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3439440) - Alpha signal extension
- [Guéant-Lehalle-Fernandez-Tapia (2013)](https://arxiv.org/abs/1105.3115) - Closed-form approximation

### Polymarket Reality: Unified Orderbook

**CRITICAL:** Polymarket uses a unified orderbook where:
- UP_price + DOWN_price ≈ $1.00 (always)
- No shorting - you can only be LONG UP or LONG DOWN tokens
- Exit via MERGE: 1 UP + 1 DOWN = $1.00 back (FREE via Builder Relayer)

**Inventory Model:**
```
up_tokens:   tokens you hold (>= 0)
down_tokens: tokens you hold (>= 0)
pairs = min(up_tokens, down_tokens)  # Can merge for $1.00 each
net_exposure = up_tokens - down_tokens  # +ve = bullish, -ve = bearish
```

### Core Idea (Adapted for Polymarket)

**Buy-Only Mode:** Only place BIDS, exit via MERGE (not selling).

Goal: Accumulate pairs where `(up_cost + down_cost) < $1.00`, then merge for profit.

Adjust bid aggressiveness based on:
1. **Inventory balance** - bid more on whichever side you need
2. **Directional signal** - bid more on predicted winner (using AGGRESSIVE's spike detection)

### Recommended Signal: AGGRESSIVE's Spike Detection

**FINDING:** Continuous signals (velocity, EWMA level) are weak (2.5% autocorrelation).
Event-based spike detection from AGGRESSIVE achieves 70% directional accuracy.

Use AGGRESSIVE's proven signal:
```python
# AGGRESSIVE spike detection (70% accuracy)
z_score = (btc_price - ewma_slow) / ewma_std  # lookback: 72 ticks (1.2s)
threshold = ou_sigmoid(z_score)  # adaptive threshold

if z_score > threshold and obi_confirms:
    spike_direction = "UP"
elif z_score < -threshold and obi_confirms:
    spike_direction = "DOWN"
else:
    spike_direction = None  # No signal
```

### Buy-Only Quote Formula

```python
# Inventory state
net_exposure = up_tokens - down_tokens

# Time factor: More time = more risk = bigger adjustments
time_factor = time_remaining / 900  # 900s = 15 min market

# Inventory adjustment (gamma = risk aversion, 0.1-0.2)
# Pushes bids toward completing pairs
inventory_adjust = gamma * net_exposure * time_factor

# Signal adjustment (k = signal multiplier, 1.0)
# Only applied when spike detected
if spike_direction == "UP":
    signal_adjust = k * (z_score - threshold) * time_factor
elif spike_direction == "DOWN":
    signal_adjust = -k * (abs(z_score) - threshold) * time_factor
else:
    signal_adjust = 0

# Base bid (aim for profitable pair cost)
target_pair_cost = 0.97  # $0.03 profit per pair
base_bid = target_pair_cost / 2  # $0.485

# Final bids
up_bid = base_bid - inventory_adjust + signal_adjust
down_bid = base_bid + inventory_adjust - signal_adjust

# Safety: clamp to not exceed market ask - min_edge
up_bid = min(up_bid, market_up_ask - 0.01)
down_bid = min(down_bid, market_down_ask - 0.01)
```

### Parameter Explanation

| Parameter | Value | Meaning |
|-----------|-------|---------|
| gamma | 0.1-0.2 | Risk aversion. Higher = faster rebalancing toward neutral |
| k | 1.0 | Signal strength multiplier. Higher = more aggressive on predicted winner |
| time_factor | remaining/total | More time = more uncertainty = bigger adjustments |
| z_threshold | 1.5 | From AGGRESSIVE. Only trade signals in z-zone [0, 1.5] |

### Why time_remaining (not time_elapsed)?

`time_factor = time_remaining / 900` because:
- **More time remaining = more can go wrong** = need bigger safety margin
- **Less time remaining = closer to resolution** = prices more certain
- As resolution approaches, inventory risk decreases (outcome becomes clear)

### Profit Mechanisms

1. **Pair Completion:** Buy UP @ $0.48 + DOWN @ $0.44 = $0.92 -> Merge for $1.00 = $0.08 profit
2. **Directional Carry:** Buy predicted winner, hold to resolution (70% accuracy)
3. **Merge Arbitrage:** When pair_cost < $1.00, guaranteed profit via merge

### Backtest Results

Best AS config (pure continuous): **$2.84/hr**
AGGRESSIVE (event-based): **$7.76-17.59/hr**

**CRITICAL UPDATE (Jan 29, 2026):** With TIME STOP + ORDER PULLING:
- **$18.04/hr** with 220-500s time window + 5000ms pulling + z>1.5 + vel_aligned
- See `research/findings/AS_TIME_STOP_CRITICAL_FINDING.md` for full analysis

**Key Discovery:** The profit comes from **DIRECTIONAL CARRY** (65.5% fill accuracy, 80.4% unhedged on winners), NOT from pair merges (pair cost $1.031 > $1.00).

**Recommendation:** Use AGGRESSIVE's spike detection as the signal source, with AS-style inventory management for pair completion.

### Pros
- Guaranteed exit via merge (no selling required)
- Inventory naturally balances toward pairs
- Combines proven 70% accurate signal with MM mechanics
- No taker fees (limit orders only)

### Cons
- Lower signal frequency than pure AGGRESSIVE (more selective)
- Requires inventory tracking
- Parameter tuning (gamma, k) affects performance

### Data Requirements
- BTC price feed (60Hz) for spike detection
- `time_remaining` - Seconds until market resolution
- Orderbook state (up_bid, up_ask, down_bid, down_ask)
- OBI (orderbook imbalance) for signal confirmation
- Inventory state (up_tokens, down_tokens)

---

## Strategy 2: Inventory-Skewed Quote Maker

### Academic Foundation
- [Guéant, Lehalle, Fernandez-Tapia (2013)](https://arxiv.org/abs/1105.3115)

### Core Idea
Profit from mean-reversion of inventory. Skew quotes to reduce inventory risk while biasing toward directional target.

### Implementation

```python
# Target inventory based on directional signal
if velocity_bps > 0.1:
    target_inventory = +10  # Want to be long UP
elif velocity_bps < -0.1:
    target_inventory = -10  # Want to be short UP (long DOWN)
else:
    target_inventory = 0    # Neutral

# Skew quotes to reach target
inventory_gap = current_inventory - target_inventory
skew = inventory_gap * skew_factor

up_bid = mid - base_spread/2 - skew
up_ask = mid + base_spread/2 - skew
```

### Pros
- Natural inventory management
- Captures alpha without crossing spread
- Works well in oscillating markets

### Cons
- May accumulate inventory in trending market
- Requires good inventory tracking

### Data Requirements
- Current inventory position
- `velocity_bps` signal
- Orderbook state

---

## Strategy 3: Regime-Adaptive Spread Maker

### Core Idea
Vary spread width by volatility regime. Wide spreads in volatile periods, tight in calm. Protects against adverse selection while capturing more spread in volatile markets.

### Implementation

```python
base_spread = 0.02  # $0.02 minimum

# Volatility multiplier (1.0 to 2.0x)
vol_multiplier = 1 + z_score * 0.5

# Flow toxicity multiplier
flow_imbalance = (buys - sells) / total_volume
toxic_multiplier = 1 + abs(flow_imbalance) * 0.3

# Time decay factor (0.5 to 1.0x)
time_factor = max(0.5, time_remaining / 900)

# Final spread
spread = base_spread * vol_multiplier * toxic_multiplier * time_factor
```

### Regime Classification

| Z-Score | Regime | Spread Multiplier |
|---------|--------|-------------------|
| < 0.5 | Calm | 1.0x (tight) |
| 0.5 - 1.0 | Normal | 1.25x |
| 1.0 - 1.5 | Active | 1.5x |
| > 1.5 | Volatile | 2.0x (wide) |

### Pros
- Protects against adverse selection
- Captures more spread in volatile markets
- Self-adapting to regime

### Cons
- Requires good volatility estimation
- May miss opportunities when spreads too wide

### Data Requirements
- `z_score` - Volatility measure
- Flow imbalance (buy/sell volume ratio)
- `time_remaining`

---

## Strategy 4: Order Book Imbalance Alpha

### Academic Foundation
- [HFTBacktest Tutorial - OBI](https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html)

### Core Idea
Orderbook imbalance predicts short-term price. When bids >> asks, price rises. Use this signal to skew quotes.

### Imbalance Calculation

```python
# Sum depth at top N levels
bid_depth = sum(bid_sizes at top 3-5 levels)
ask_depth = sum(ask_sizes at top 3-5 levels)

# Imbalance: ranges from -1 (all asks) to +1 (all bids)
imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
```

### Trading Logic

```python
if imbalance > 0.3:  # Strong bid pressure -> lean long UP
    up_ask = mid + tight_spread    # Aggressive selling UP (expect fill + price rise)
    up_bid = mid - wide_spread     # Passive buying UP
elif imbalance < -0.3:  # Strong ask pressure -> lean long DOWN
    up_ask = mid + wide_spread     # Passive selling UP
    up_bid = mid - tight_spread    # Aggressive buying UP (to sell later)
else:  # Neutral
    up_ask = mid + base_spread
    up_bid = mid - base_spread
```

### Pros
- Very short-term signal (high turnover)
- Captures microstructure alpha
- Combines well with other signals

### Cons
- Thin orderbooks on Polymarket
- Fast signal decay
- Requires full depth data

### Data Requirements
- **Full orderbook depth (5 levels)** - NOW COLLECTING
- Observer columns: `up_bid_1-5`, `up_bid_size_1-5`, `up_ask_1-5`, `up_ask_size_1-5`
- Computed: `up_imbalance`, `down_imbalance`

### Current Data Collection

Observer running on AWS (until 5pm IST Jan 28):
- 42 new depth columns added Jan 27
- Sampling: 200ms (5 samples/sec)
- Output: `research/observer/grid_obs_20260127.csv`

---

## Strategy 5: Time-Decay Theta Harvesting

### Core Idea
Binary options have time decay. Prices converge to $0 or $1 at resolution. Exploit the known time structure.

### Time Phases

| Phase | Time Remaining | Strategy |
|-------|----------------|----------|
| Early | > 10 min | Wide spreads, small positions, focus on spread capture |
| Mid | 3-10 min | Medium spreads, directional positions based on signals |
| Late | < 3 min | Very wide spreads OR exit, high gamma = high risk |

### Key Insight
As `time_remaining -> 0`:
- Prices become extreme (near $0 or $1)
- Spreads widen naturally
- Volatility spikes
- Gamma risk highest

### Implementation

```python
if time_remaining > 600:  # > 10 min
    position_size = base_size * 0.5
    spread = base_spread * 1.5
elif time_remaining > 180:  # 3-10 min
    position_size = base_size * 1.0
    spread = base_spread * 1.0
else:  # < 3 min
    position_size = 0  # Exit or hold existing only
    spread = base_spread * 2.0
```

### Pros
- Exploits known time structure
- Natural position sizing
- Works with any other strategy

### Cons
- Most alpha in final minutes (highest risk)
- Thin liquidity near resolution

### Data Requirements
- `time_remaining` - Seconds until resolution
- Historical price paths (for calibration)

---

## Strategy 6: Cross-Market Information Flow

### Core Idea
Polymarket prices lag Binance by 0.6-2.35s. Use information asymmetry as market maker.

### Implementation

```python
# When spike detected on Binance:
if spike_direction == "UP":
    up_bid = up_ask - 0.01    # Near top of book (expect fill)
    down_ask = down_bid + 0.03  # Far from top (hedge if needed)
elif spike_direction == "DOWN":
    down_bid = down_ask - 0.01
    up_ask = up_bid + 0.03
```

### Relationship to AGGRESSIVE

This is essentially the AGGRESSIVE strategy as MAKER instead of TAKER:
- Same signal (BTC spike via OU threshold)
- Same direction prediction
- Different execution (limit orders vs market orders)

### Pros
- Information advantage
- Maker fees (avoid taker fees)
- Natural hedge structure

### Cons
- Competition for queue priority
- Fill uncertainty
- Latency matters

### Data Requirements
- Binance price feed (60Hz)
- Polymarket orderbook state
- Spike detection (OU threshold)

### Reference
See [AGGRESSIVE.md](AGGRESSIVE.md) for spike detection details.

---

## Strategy 7: VPIN-Based Toxic Flow Avoidance

### Academic Foundation
- [Easley et al. - VPIN](https://www.quantresearch.org/VPIN.pdf)

### Core Idea
Monitor order flow toxicity. When flow is toxic (informed traders active), widen spreads to avoid adverse selection.

### VPIN Calculation

```python
bucket_size = 100  # shares per bucket
buys = trades where price >= mid
sells = trades where price < mid

# Over last 50 buckets
vpin = abs(sum(buys) - sum(sells)) / sum(total)
```

### Trading Logic

```python
if vpin > 0.7:    spread_multiplier = 2.0  # High toxicity - wide spreads
elif vpin > 0.5:  spread_multiplier = 1.5  # Moderate toxicity
else:             spread_multiplier = 1.0  # Normal spreads
```

### Pros
- Protects against informed traders
- Only provides liquidity when profitable
- Evidence-based (academic research)

### Cons
- Requires trade-level data
- Complex calculation
- May reduce trading volume

### Data Requirements
- Trade tape with timestamps and sizes
- Classification of buys vs sells (tick rule)

### Current Status
Partial data - need trade tape with timestamps for full implementation.

---

## Strategy Comparison Matrix

| Strategy | Alpha Source | Complexity | Data Needs | Capital Eff |
|----------|--------------|------------|------------|-------------|
| 1. Avellaneda-Stoikov | Spread + Signal | High | Medium | High |
| 2. Inventory-Skewed | Mean Reversion | Medium | Low | Medium |
| 3. Regime-Adaptive | Vol Timing | Medium | Medium | Medium |
| 4. OBI Alpha | Microstructure | High | High | High |
| 5. Theta Harvesting | Time Decay | Low | Low | Low |
| 6. Cross-Market | Latency | Medium | Medium | High |
| 7. VPIN Avoidance | Anti-Adverse | High | High | Medium |

---

## Combining Strategies

### Recommended Combinations

1. **Base + Overlay:**
   - Base: Avellaneda-Stoikov (continuous quoting)
   - Overlay: OBI Alpha (short-term skew adjustment)

2. **Signal + Protection:**
   - Signal: Cross-Market (AGGRESSIVE signal)
   - Protection: VPIN Avoidance (widen on toxic flow)

3. **Time-Aware:**
   - Any strategy + Theta Harvesting time multipliers
   - Wide spreads near resolution

### Signal Hierarchy

When signals conflict:
1. VPIN > 0.7: Override all - go wide or exit
2. OBI strong (|imbalance| > 0.5): Short-term skew
3. Velocity signal: Medium-term direction
4. Inventory skew: Background mean-reversion

---

## Academic Sources

### Market Making Theory
- [Avellaneda & Stoikov (2008)](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf) - High-frequency trading in limit order books
- [Guéant, Lehalle, Fernandez-Tapia (2013)](https://arxiv.org/abs/1105.3115) - Optimal MM strategies
- [Cartea & Wang (2019)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3439440) - MM with alpha signals

### Microstructure
- [GLFT Approximation Tutorial](https://hftbacktest.readthedocs.io/en/py-v2.1.0/tutorials/GLFT%20Market%20Making%20Model%20and%20Grid%20Trading.html)
- [OBI Tutorial](https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html)
- [VPIN Paper](https://www.quantresearch.org/VPIN.pdf)

### Practical Guides
- [Hummingbot AS Guide](https://hummingbot.org/blog/guide-to-the-avellaneda--stoikov-strategy/)
- [Polymarket Liquidity Rewards](https://docs.polymarket.com/developers/market-makers/liquidity-rewards)

---

## Related Files

| File | Purpose |
|------|---------|
| `research/strategies/AGGRESSIVE.md` | Cross-market strategy (validated) |
| `research/strategies/CONTRARIAN.md` | Mean-reversion strategy (validated) |
| `research/findings/SIGNAL_ACCURACY_FINDINGS.md` | Spike x velocity interaction |
| `research/findings/VOLATILITY_FILTER.md` | EWMA z-score optimal |
| `src/strategies/enhanced_spike.py` | AGGRESSIVE implementation |
| `research/observer/grid_obs_*.csv` | Depth data (collecting) |

---

---

## CRITICAL FINDING: Time Stop + Order Pulling (Jan 29, 2026)

**TIME STOP IS THE KEY TO AS PROFITABILITY**

### Without Time Stop (Profile 15 Results)
| Entry Window | Hourly Rate | Pair Cost |
|--------------|-------------|-----------|
| 300-900s | -$9.92/hr | $1.73 |
| 400-900s | -$7.35/hr | $1.68 |
| 500-900s | -$1.65/hr | $1.65 |

### With Time Stop (220-500s) + Optimal Signals
| Config | Win% | Pair Cost | Hourly Rate |
|--------|------|-----------|-------------|
| z>1.5 + vel_aligned | 65.5% | $1.031 | **+$18.04/hr** |
| vel_aligned only | 65.0% | $1.038 | +$15.54/hr |
| Baseline | 59.7% | $1.069 | +$2.25/hr |

### Why Time Stop Works
1. Avoids late-market adverse selection (high info, expensive prices)
2. Catches early-mid market (cheaper prices, less informed flow)
3. Filters out worst 16pp of adverse selection

### Critical Insight: Profit Source
| Source | PnL | Hourly |
|--------|-----|--------|
| Merge (pairs) | -$35.40 | -$2.86/hr |
| Unrealized (carry) | +$258.83 | +$20.90/hr |
| **Total** | **+$223.43** | **+$18.04/hr** |

**The AS strategy makes money from DIRECTIONAL CARRY, not merges.**
- 65.5% fills on winning side
- 80.4% unhedged positions on correct side at resolution

### Winning Config
```python
ASConfig(
    mode=StrategyMode.ASYMMETRIC_EWMA,
    z_threshold=1.5,
    require_velocity_aligned=True,
    entry_window_min_secs=220,  # TIME STOP
    entry_window_max_secs=500,  # TIME STOP
    max_order_age_ms=5000,      # SLOW PULLING
)
```

### Next Steps
1. Test multi-phase prototype (accumulate cheap → signal skew → time stop)
2. Test fixed grid levels vs dynamic AS pricing
3. See `research/findings/AS_TIME_STOP_CRITICAL_FINDING.md` for full analysis

---

*Updated: January 29, 2026*
