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

### Core Idea
Quote both sides continuously, but SKEW quotes based on directional signal (BTC velocity/spike).

### Quote Formula (GLFT)

```
reservation_price = mid_price - q * gamma * sigma^2 * (T - t)
bid = reservation_price - spread/2 - skew * q
ask = reservation_price + spread/2 - skew * q

Where:
  q = inventory position
  gamma = risk aversion parameter
  sigma = volatility estimate
  T - t = time remaining
  skew = alpha adjustment based on directional signal
```

### Polymarket Implementation

```python
# When BTC velocity > 0 (expect UP to win):
alpha_adjustment = k * velocity_bps * time_remaining_factor

up_bid = mid - spread/2 - alpha_adjustment   # Less aggressive buying UP
up_ask = mid + spread/2 - alpha_adjustment   # More aggressive selling UP
down_bid = mid - spread/2 + alpha_adjustment # More aggressive buying DOWN
down_ask = mid + spread/2 + alpha_adjustment # Less aggressive selling DOWN
```

### Pros
- Earn spread both sides
- Inventory balances naturally
- No crossing spread (no taker fees)

### Cons
- Continuous quote management complexity
- Adverse selection risk
- Parameter calibration needed (gamma, sigma estimates)

### Data Requirements
- `velocity_bps` - BTC velocity signal
- `time_remaining` - Seconds until market resolution
- Orderbook state (mid price, spread)

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

*Updated: January 28, 2026*
