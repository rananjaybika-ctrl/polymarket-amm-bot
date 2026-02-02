# Mathematical Pattern Detection: Frank-Wolfe and Bregman Projection

**Date:** February 2, 2026
**Type:** Research Analysis - Pattern Recognition
**Purpose:** Identify observable trading signatures from sophisticated optimization algorithms

---

## Executive Summary

This document analyzes the mathematical fingerprints that Frank-Wolfe algorithm and Bregman divergence optimization would leave in market data. These patterns enable detection of sophisticated arbitrageurs using optimization-based strategies, allowing us to either:
1. Avoid competing during their active periods
2. Identify profitable opportunities they've discovered
3. Understand market microstructure during arbitrage events

---

## Part I: Frank-Wolfe Algorithm Patterns

### 1.1 Mathematical Background

The Frank-Wolfe (Conditional Gradient) algorithm solves constrained optimization problems:

```
min F(x) subject to x in C (convex constraint set)
```

For prediction market arbitrage, this becomes:
```
Find optimal allocation x* that minimizes distance to arbitrage-free prices
```

Key properties:
- **Sparse solutions**: Iterates stay on vertices/faces of polytope
- **Linear convergence**: Gap decreases as O(1/k)
- **Vertex-seeking**: Each step finds extreme point via linear optimization

### 1.2 Observable Patterns in Trade Data

#### Pattern A: Sparse Allocations (Concentrated Bets)

**Mathematical Signature:**
```
Frank-Wolfe produces k-sparse solutions where k << n (number of conditions)
```

**What to look for:**
| Metric | Frank-Wolfe Signature | Normal Trading |
|--------|----------------------|----------------|
| Active positions | 3-5 conditions | Diversified |
| Position concentration | >60% in top 2 conditions | <30% in any condition |
| Herfindahl Index | >0.25 | <0.10 |
| Zero allocations | >70% of conditions | <30% of conditions |

**Detection Code:**
```python
def detect_sparse_allocation(positions: dict) -> tuple[bool, dict]:
    """
    Detect if allocation pattern matches Frank-Wolfe sparsity.

    Args:
        positions: {condition_id: position_size}

    Returns:
        (is_sparse, metrics)
    """
    if not positions:
        return False, {}

    total = sum(abs(v) for v in positions.values())
    if total == 0:
        return False, {}

    # Sort by absolute position size
    sorted_pos = sorted(positions.items(), key=lambda x: abs(x[1]), reverse=True)

    # Concentration metrics
    weights = [abs(v) / total for _, v in sorted_pos]
    top2_concentration = sum(weights[:2])

    # Herfindahl-Hirschman Index
    hhi = sum(w**2 for w in weights)

    # Sparsity: fraction with zero allocation
    n_zero = sum(1 for v in positions.values() if abs(v) < 0.01 * total)
    sparsity = n_zero / len(positions)

    # Active conditions
    n_active = sum(1 for v in positions.values() if abs(v) >= 0.01 * total)

    is_fw_like = (
        top2_concentration > 0.60 and
        hhi > 0.25 and
        n_active <= 5
    )

    return is_fw_like, {
        'top2_concentration': top2_concentration,
        'hhi': hhi,
        'sparsity': sparsity,
        'n_active': n_active,
        'n_total': len(positions)
    }
```

**Thresholds:**
- **Strong signal**: HHI > 0.35, top2 > 0.70, active <= 3
- **Moderate signal**: HHI > 0.25, top2 > 0.60, active <= 5
- **Weak signal**: HHI > 0.15, top2 > 0.50, active <= 7

#### Pattern B: Iterative Position Building

**Mathematical Signature:**
```
Frank-Wolfe iteration: x_{k+1} = x_k + gamma_k * (s_k - x_k)
where s_k is a vertex and gamma_k = 2/(k+2) (diminishing step size)
```

**What to look for:**
| Metric | Frank-Wolfe Signature | Normal Trading |
|--------|----------------------|----------------|
| Trade sequence | Alternating conditions | Random/uniform |
| Trade sizes | Decreasing sequence | Random |
| Time between trades | Regular intervals (solver iterations) | Irregular |
| Convergence pattern | Position sizes stabilize | No pattern |

**Detection Algorithm:**
```python
def detect_iterative_building(trades: list[dict], window_minutes: float = 5.0) -> tuple[bool, dict]:
    """
    Detect Frank-Wolfe iterative building pattern.

    Args:
        trades: List of trades with {'timestamp', 'condition_id', 'size', 'side'}
        window_minutes: Time window to analyze

    Returns:
        (is_iterative, metrics)
    """
    if len(trades) < 5:
        return False, {}

    # Sort by timestamp
    trades_sorted = sorted(trades, key=lambda x: x['timestamp'])

    # Check for regular intervals
    intervals = []
    for i in range(1, len(trades_sorted)):
        dt = (trades_sorted[i]['timestamp'] - trades_sorted[i-1]['timestamp'])
        intervals.append(dt)

    avg_interval = sum(intervals) / len(intervals)
    interval_std = (sum((i - avg_interval)**2 for i in intervals) / len(intervals))**0.5
    cv_interval = interval_std / avg_interval if avg_interval > 0 else float('inf')

    # Check for decreasing trade sizes (Frank-Wolfe step sizes decrease)
    sizes = [abs(t['size']) for t in trades_sorted]
    decreasing_count = sum(1 for i in range(1, len(sizes)) if sizes[i] <= sizes[i-1] * 1.1)
    decreasing_ratio = decreasing_count / (len(sizes) - 1)

    # Check for condition cycling (vertex seeking)
    conditions = [t['condition_id'] for t in trades_sorted]
    unique_ratio = len(set(conditions)) / len(conditions)

    is_iterative = (
        cv_interval < 0.5 and  # Regular intervals
        decreasing_ratio > 0.6 and  # Decreasing sizes
        0.3 < unique_ratio < 0.7  # Some but not too much cycling
    )

    return is_iterative, {
        'cv_interval': cv_interval,
        'avg_interval_sec': avg_interval,
        'decreasing_ratio': decreasing_ratio,
        'condition_cycling_ratio': unique_ratio,
        'n_trades': len(trades_sorted)
    }
```

**Thresholds:**
- **Regular intervals**: CV(interval) < 0.5 (low variance)
- **Decreasing sizes**: >60% of trades smaller than previous
- **Convergence**: Final 3 trades within 5% of each other

#### Pattern C: Entry/Exit at Specific Thresholds

**Mathematical Signature:**
```
Frank-Wolfe exits when duality gap g(x_k) < epsilon
g(x_k) = max_{s in vertices} <grad F(x_k), x_k - s>
```

**What to look for:**
| Metric | Frank-Wolfe Signature | Normal Trading |
|--------|----------------------|----------------|
| Entry mispricing | Precise threshold (e.g., 2.0%) | Variable |
| Exit timing | When gap closes to epsilon | Hold to resolution |
| Re-entry | After new gap opens | Random |

**Detection Metrics:**
```python
ENTRY_THRESHOLDS = {
    'fw_typical': 0.020,  # 2% mispricing triggers entry
    'fw_aggressive': 0.015,  # 1.5% for larger arbitrageurs
    'fw_conservative': 0.030,  # 3% for smaller/cautious
}

def detect_threshold_entries(trader_history: list[dict],
                             market_prices: dict) -> tuple[bool, dict]:
    """
    Detect if entries cluster around specific mispricing thresholds.

    Args:
        trader_history: Trader's entry/exit records
        market_prices: Price data at each entry point
    """
    if len(trader_history) < 10:
        return False, {}

    mispricings = []
    for entry in trader_history:
        if entry['action'] == 'entry':
            # Calculate mispricing at entry
            prices = market_prices.get(entry['timestamp'], {})
            sum_prices = sum(prices.values())
            mispricing = abs(1.0 - sum_prices)
            mispricings.append(mispricing)

    if not mispricings:
        return False, {}

    avg_mispricing = sum(mispricings) / len(mispricings)
    std_mispricing = (sum((m - avg_mispricing)**2 for m in mispricings) / len(mispricings))**0.5
    cv_mispricing = std_mispricing / avg_mispricing if avg_mispricing > 0 else float('inf')

    # Frank-Wolfe entries should cluster tightly around threshold
    is_threshold = cv_mispricing < 0.30  # Low variance in entry conditions

    return is_threshold, {
        'avg_mispricing_at_entry': avg_mispricing,
        'std_mispricing': std_mispricing,
        'cv_mispricing': cv_mispricing,
        'n_entries': len(mispricings)
    }
```

---

## Part II: Bregman Divergence Optimization Patterns

### 2.1 Mathematical Background

Bregman divergence generalizes squared distance using convex functions:

```
D_phi(x||y) = phi(x) - phi(y) - <grad phi(y), x - y>
```

For LMSR prediction markets with negative entropy:
```
D(mu||theta) = sum_i [mu_i * log(mu_i/theta_i)]  (KL divergence)
```

**Optimal arbitrage trade = Bregman projection of prices onto valid probability polytope**

### 2.2 Observable Patterns in Trade Data

#### Pattern D: Trades Cluster Around Price Deviations

**Mathematical Signature:**
```
Bregman projection moves prices toward nearest point in valid set M
Trade profit ~ D(projection || current_prices)
```

**What to look for:**
| Metric | Bregman Signature | Normal Trading |
|--------|-------------------|----------------|
| Trade timing | When KL divergence exceeds threshold | Random |
| Trade direction | Always toward valid probabilities | Mixed |
| Position sizing | Proportional to divergence magnitude | Fixed |
| Multi-leg coordination | Simultaneous rebalancing | Sequential |

**Detection Code:**
```python
import math

def compute_kl_divergence(prices: dict, valid_prob: dict = None) -> float:
    """
    Compute KL divergence from market prices to nearest valid distribution.

    For binary market: valid is {condition: p} where sum(p) = 1.0
    """
    if valid_prob is None:
        # Normalize current prices to valid probability
        total = sum(prices.values())
        valid_prob = {k: v/total for k, v in prices.items()}

    kl = 0.0
    for cond, p_valid in valid_prob.items():
        p_market = prices.get(cond, 1e-10)
        p_market = max(p_market, 1e-10)  # Avoid log(0)
        p_valid = max(p_valid, 1e-10)
        kl += p_valid * math.log(p_valid / p_market)

    return kl


def detect_divergence_trading(trades: list[dict],
                              price_history: dict) -> tuple[bool, dict]:
    """
    Detect if trades occur when divergence exceeds threshold.

    Args:
        trades: List of trader's trades
        price_history: {timestamp: {condition: price}}
    """
    if len(trades) < 5:
        return False, {}

    divergences_at_trade = []
    divergences_background = []

    # Sample background divergences
    all_timestamps = sorted(price_history.keys())
    for ts in all_timestamps[::10]:  # Sample every 10th timestamp
        prices = price_history[ts]
        div = compute_kl_divergence(prices)
        divergences_background.append(div)

    # Measure divergence at each trade
    for trade in trades:
        ts = trade['timestamp']
        # Find nearest price snapshot
        nearest_ts = min(price_history.keys(), key=lambda x: abs(x - ts))
        prices = price_history[nearest_ts]
        div = compute_kl_divergence(prices)
        divergences_at_trade.append(div)

    avg_at_trade = sum(divergences_at_trade) / len(divergences_at_trade)
    avg_background = sum(divergences_background) / len(divergences_background)

    # Bregman traders enter when divergence is high
    ratio = avg_at_trade / avg_background if avg_background > 0 else 1.0
    is_divergence_based = ratio > 1.5  # Trade when divergence 50%+ above average

    return is_divergence_based, {
        'avg_divergence_at_trade': avg_at_trade,
        'avg_divergence_background': avg_background,
        'divergence_ratio': ratio,
        'n_trades': len(trades)
    }
```

**Typical Bregman Entry Thresholds:**
| Market Type | KL Divergence Threshold | Approx % Mispricing |
|-------------|------------------------|---------------------|
| Single condition | >0.0002 | ~2% |
| Multi-condition | >0.0010 | ~3% |
| Tournament bracket | >0.0050 | ~5% |

#### Pattern E: Typical Profit Margins

**Mathematical Signature:**
```
Expected profit per trade = b * D(mu*||theta)
where b = LMSR liquidity parameter
```

**What to look for:**
| Metric | Bregman Signature | Normal Trading |
|--------|-------------------|----------------|
| Profit per trade | $50-$500 (proportional to divergence) | Variable |
| Win rate | >90% (arbitrage, not speculation) | 50-60% |
| Holding period | Days to weeks | Minutes to hours |
| Risk exposure | Near-zero (hedged) | Significant |

**Observed from Article Analysis:**
```
Top Bregman arbitrageur: $2,009,632 from 4,049 trades
Average profit per trade: $496
Implied average divergence: ~5% mispricing
```

**Detection Metrics:**
```python
def profile_profit_pattern(trader_pnl: list[dict]) -> dict:
    """
    Profile profit patterns to identify Bregman optimization.
    """
    if not trader_pnl:
        return {}

    profits = [t['pnl'] for t in trader_pnl if t['pnl'] > 0]
    losses = [abs(t['pnl']) for t in trader_pnl if t['pnl'] < 0]

    win_rate = len(profits) / len(trader_pnl) if trader_pnl else 0
    avg_profit = sum(profits) / len(profits) if profits else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # Bregman arbitrage has high win rate, small losses
    is_arbitrage_like = (
        win_rate > 0.85 and
        avg_profit > 50 and
        (avg_loss < avg_profit * 0.3 if losses else True)
    )

    return {
        'win_rate': win_rate,
        'avg_profit': avg_profit,
        'avg_loss': avg_loss,
        'profit_loss_ratio': avg_profit / avg_loss if avg_loss > 0 else float('inf'),
        'total_trades': len(trader_pnl),
        'is_arbitrage_pattern': is_arbitrage_like
    }
```

---

## Part III: Volatility Filtering Patterns

### 3.1 Z-Score Based Entries

Our existing volatility tracker uses OU process parameters to compute z-scores:

```python
z = (log_vol - mu) / sigma_stat
```

**Validated Thresholds (from grid search):**
| Zone | Z-Score Range | Interpretation | Recommendation |
|------|---------------|----------------|----------------|
| LOW | z < 0 | Below average volatility | SKIP - insufficient movement |
| MEDIUM | 0 < z < 1.5 | Normal volatility | TRADE - optimal zone |
| HIGH | 1.5 < z < 2.5 | Elevated volatility | CAUTION - increased risk |
| EXTREME | z > 2.5 | Extreme volatility | SKIP - direction uncertain |

**Best configuration (from Jan 22, 2026 analysis):**
```python
# Optimal z-score filter
Z_LO = 0.0   # Skip low volatility
Z_HI = 1.5   # Skip high volatility
METHOD = "ewma"  # Adaptive is best for hourly rate
```

**Improvement:** +52% over no filter

### 3.2 Standard Deviation Bands

For BTC price monitoring, we track volatility bands:

```python
def compute_volatility_bands(prices: list[float],
                            window: int = 60,
                            n_std: float = 2.0) -> dict:
    """
    Compute Bollinger-style bands for volatility filtering.
    """
    if len(prices) < window:
        return {'upper': None, 'lower': None, 'mid': None}

    recent = prices[-window:]
    mid = sum(recent) / len(recent)
    std = (sum((p - mid)**2 for p in recent) / len(recent))**0.5

    return {
        'upper': mid + n_std * std,
        'lower': mid - n_std * std,
        'mid': mid,
        'std': std,
        'pct_band': (n_std * std / mid) * 100  # Band width as % of mid
    }
```

**Entry Signals:**
| Signal Type | Condition | Interpretation |
|-------------|-----------|----------------|
| Breakout UP | price > upper_band | Strong upward momentum |
| Breakout DOWN | price < lower_band | Strong downward momentum |
| Mean reversion | price crosses mid | Potential reversal |
| Band squeeze | pct_band < 0.5% | Low vol, breakout imminent |

### 3.3 Time-of-Day Patterns

From observed whale trading (gabagool analysis):

**High-Activity Periods (EST):**
| Time | Activity Level | Notes |
|------|----------------|-------|
| 9:00-9:30 AM | High | US market open overlap |
| 12:00-1:00 PM | Medium | Lunch consolidation |
| 4:00-4:30 PM | High | US close, positioning |
| 9:00-10:00 PM | Very High | BTC 15m market active trading |

**Detection:**
```python
def analyze_time_of_day(trades: list[dict]) -> dict:
    """
    Analyze trading activity by hour.
    """
    from collections import defaultdict
    import datetime

    hourly_counts = defaultdict(int)
    hourly_volume = defaultdict(float)

    for trade in trades:
        ts = datetime.datetime.fromtimestamp(trade['timestamp'])
        hour = ts.hour
        hourly_counts[hour] += 1
        hourly_volume[hour] += abs(trade.get('size', 0))

    peak_hour = max(hourly_counts.keys(), key=lambda h: hourly_counts[h])

    return {
        'peak_hour': peak_hour,
        'peak_count': hourly_counts[peak_hour],
        'hourly_distribution': dict(hourly_counts),
        'hourly_volume': dict(hourly_volume)
    }
```

---

## Part IV: Detection in Our Market Observation Data

### 4.1 Integrated Detection System

```python
class SophisticatedTraderDetector:
    """
    Detect Frank-Wolfe and Bregman optimization patterns in trade data.
    """

    def __init__(self):
        self.thresholds = {
            'fw_sparse_hhi': 0.25,
            'fw_sparse_top2': 0.60,
            'fw_iterative_cv': 0.50,
            'bregman_divergence_ratio': 1.5,
            'bregman_win_rate': 0.85,
        }

    def analyze_trader(self,
                       trades: list[dict],
                       positions: dict,
                       price_history: dict) -> dict:
        """
        Comprehensive analysis of trader behavior.

        Returns detection scores and classification.
        """
        results = {
            'trader_id': trades[0].get('trader_id') if trades else None,
            'n_trades': len(trades),
        }

        # Frank-Wolfe sparsity check
        is_sparse, sparse_metrics = detect_sparse_allocation(positions)
        results['fw_sparse'] = is_sparse
        results['sparse_metrics'] = sparse_metrics

        # Frank-Wolfe iterative building
        is_iterative, iter_metrics = detect_iterative_building(trades)
        results['fw_iterative'] = is_iterative
        results['iterative_metrics'] = iter_metrics

        # Bregman divergence trading
        is_divergence, div_metrics = detect_divergence_trading(trades, price_history)
        results['bregman_divergence'] = is_divergence
        results['divergence_metrics'] = div_metrics

        # Profit pattern
        pnl_data = [{'pnl': t.get('pnl', 0)} for t in trades if 'pnl' in t]
        profit_metrics = profile_profit_pattern(pnl_data)
        results['profit_metrics'] = profit_metrics

        # Classification
        fw_score = int(is_sparse) + int(is_iterative)
        bregman_score = int(is_divergence) + int(profit_metrics.get('is_arbitrage_pattern', False))

        if fw_score >= 2 and bregman_score >= 2:
            results['classification'] = 'SOPHISTICATED_ARBITRAGEUR'
        elif fw_score >= 2:
            results['classification'] = 'FRANK_WOLFE_USER'
        elif bregman_score >= 2:
            results['classification'] = 'BREGMAN_OPTIMIZER'
        elif fw_score >= 1 or bregman_score >= 1:
            results['classification'] = 'POTENTIAL_SYSTEMATIC'
        else:
            results['classification'] = 'RETAIL_TRADER'

        return results
```

### 4.2 Specific Metrics to Track

For our BTC 15-minute markets, track these metrics per trader:

| Metric | Calculation | Threshold |
|--------|-------------|-----------|
| Trade interval CV | std(intervals) / mean(intervals) | < 0.5 = systematic |
| Position HHI | sum(weight_i^2) | > 0.25 = concentrated |
| Entry mispricing CV | std(mispricing) / mean(mispricing) | < 0.3 = threshold-based |
| Win rate | wins / total | > 85% = arbitrage |
| Avg profit/trade | sum(pnl) / n_trades | > $50 = sophisticated |
| Time-to-hedge | avg(entry_ts - hedge_ts) | < 30s = atomic execution |

### 4.3 Application to Gabagool/Baguette Analysis

From our whale analysis files:

**Gabagool Profile:**
```
- Hedge ratio: 92.3% (high hedging = arbitrage-like)
- Pair cost median: $1.006 (near break-even)
- Entry delay median: 313s (late entry = NOT early information)
- Trade interval median: 2s (very fast = systematic)
- Classification: MIXED (maker/taker)
```

**Baguette Profile:**
```
- Hedge ratio: 63.3% (moderate hedging)
- Pair cost median: $1.162 (losing on pairs)
- Entry delay median: 9s (early entry = information advantage?)
- Trade interval median: 4s (fast = systematic)
- Classification: MAKER (92.9% single-fills)
```

**Assessment:**
- Neither shows clear Frank-Wolfe pattern (not sparse enough)
- Gabagool shows some Bregman characteristics (high hedge ratio, fast execution)
- Both appear to be market-making strategies, not cross-market arbitrage

---

## Part V: Implementation Recommendations

### 5.1 Detection Pipeline

```
1. Load trade data stream
   ↓
2. Group by trader_id
   ↓
3. For each trader with >10 trades:
   a. Compute sparse allocation metrics
   b. Check iterative building pattern
   c. Measure divergence correlation
   d. Profile profit pattern
   ↓
4. Classify trader
   ↓
5. If SOPHISTICATED_ARBITRAGEUR detected:
   a. Alert for monitoring
   b. Track their entry conditions
   c. Potentially follow (if atomic execution not required)
```

### 5.2 What We Cannot Copy

From the article, sophisticated arbitrage requires:
- **Gurobi IP solver** (~$10K/year license)
- **Parallel atomic execution** (all legs in same block, <30ms)
- **Multi-market position tracking** (days of capital lockup)
- **LLM for dependency parsing** (market relationship extraction)

### 5.3 What We Can Learn

From detecting these traders:
1. **Entry timing signals** - When they enter, mispricing exists
2. **Market relationships** - Which conditions they trade together
3. **Exit conditions** - When arbitrage closes
4. **Avoid competition** - Don't compete on their edges

---

## Appendix: Summary Detection Table

| Pattern | Metric | Threshold | Confidence |
|---------|--------|-----------|------------|
| **Frank-Wolfe Sparsity** | HHI | > 0.25 | Medium |
| | Top-2 concentration | > 60% | Medium |
| | Active positions | <= 5 | Low |
| **Frank-Wolfe Iteration** | Interval CV | < 0.50 | High |
| | Size decreasing | > 60% | Medium |
| | Condition cycling | 30-70% | Low |
| **Bregman Divergence** | Entry/background ratio | > 1.50 | High |
| | KL at entry | > 0.0002 | Medium |
| **Profit Pattern** | Win rate | > 85% | High |
| | Avg profit | > $50 | Medium |
| | Profit/loss ratio | > 3.0 | Medium |
| **Volatility Filter** | Z-score range | [0, 1.5] | High |
| | Band breakout | > 2 std | Medium |
| **Time-of-Day** | Peak hours (EST) | 9-10 PM | Medium |

---

*"Detect patterns. Understand competition. Adapt strategy."*
