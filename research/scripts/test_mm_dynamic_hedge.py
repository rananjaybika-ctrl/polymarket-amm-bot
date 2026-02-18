#!/usr/bin/env python3
"""
Market Making with Dynamic Hedging Strategy Backtest

Based on aggressive_m_v2_grid_search.py structure with MAKER execution.

Strategy:
1. HALF SIZE: On spike + exp_ask >= $0.80, accumulate both sides with 40% bias to expensive
2. Cancel orders on high velocity BTC moves (adverse selection protection)
3. If wrong (trend reverses for 30s): FULL SIZE with 60% bias to new expensive
4. Near expiry: reduce imbalance to 40% OR fully hedge

Best signal from research: expensive_ask >= $0.80 (100% trend holding at 10s)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# PARAMETERS
# =============================================================================
# Position sizing
HALF_SIZE = 5
FULL_SIZE = 10

# Imbalance bias
BIAS_NORMAL = 0.40  # 40% more on expensive side
BIAS_HEAVY = 0.60   # 60% bias after being wrong

# Signal thresholds (from research: 100% trend holding at $0.80)
MIN_EXPENSIVE_ASK = 0.80
MIN_TIME_REMAINING = 90  # seconds

# Risk management
HIGH_VEL_THRESHOLD = 5.0      # bps/s - cancel orders
TREND_SHIFT_SECONDS = 30      # how long trend must persist
ENTRY_OFFSET = 0.01           # bid 1c below best_ask for maker
EXPIRY_WINDOW = 120           # seconds before expiry to hedge

# OU params (1s combined calibration)
OU_MU = -6.3978
OU_SIGMA = 1.7051
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
EWMA_HALFLIFE_MS = 1000


@dataclass
class TradeResult:
    market_slug: str
    up_shares: int
    down_shares: int
    up_cost: float
    down_cost: float
    resolution: str
    pnl: float
    imbalance: float
    trend_shifted: bool
    phase: str
    dataset: str


# =============================================================================
# SPIKE DETECTION (same as aggressive_m_v2)
# =============================================================================
def precompute_spikes_ewma(btc_df: pd.DataFrame, halflife_ms: int = EWMA_HALFLIFE_MS) -> pd.DataFrame:
    """EWMA spike detection with velocity."""
    halflife_ticks = halflife_ms / 16.67
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first')

    prices = df['price'].values
    timestamps = df['timestamp_ms'].values
    n = len(prices)

    # EWMA price
    ewma_prices = np.zeros(n)
    ewma_prices[0] = prices[0]
    gap_threshold_ms = 30 * 60 * 1000

    for i in range(1, n):
        if timestamps[i] - timestamps[i-1] > gap_threshold_ms:
            ewma_prices[i] = prices[i]
        else:
            ewma_prices[i] = alpha * prices[i] + (1 - alpha) * ewma_prices[i-1]

    df['ewma_price'] = ewma_prices
    df['deviation_pct'] = (df['price'] - df['ewma_price']) / df['ewma_price'] * 100
    df['spike_magnitude'] = df['deviation_pct'].abs()

    # Velocity (5s lookback)
    lookback = 300  # ~5s at 60Hz
    velocity = np.zeros(n)
    for i in range(lookback, n):
        dt_s = (timestamps[i] - timestamps[i-lookback]) / 1000
        if dt_s > 0:
            velocity[i] = (prices[i] - prices[i-lookback]) / prices[i-lookback] * 10000 / dt_s
    df['velocity_bps'] = velocity
    df['abs_velocity'] = np.abs(velocity)

    # OU adaptive threshold
    returns = df['price'].pct_change() * 100
    var_alpha = 1 - 0.5 ** (1.0 / 300)
    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01

    thresholds = []
    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            continue
        variance = var_alpha * (r ** 2) + (1 - var_alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        log_vol = np.log(vol)
        z = (log_vol - OU_MU) / OU_SIGMA
        z_clamped = max(-10, min(10, z * 1.5))
        sigmoid = 1.0 / (1.0 + np.exp(-z_clamped))
        mult = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
        thresholds.append(max(0.015, min(0.10, OU_BASE_THRESHOLD * mult)))

    df['threshold'] = thresholds
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['deviation_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['deviation_pct'] < 0), 'spike_direction'] = 'DOWN'

    print(f"  Spikes: {df['spike_detected'].sum():,}")
    return df


# =============================================================================
# MARKET SIMULATION
# =============================================================================
def simulate_market(
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    dataset_name: str,
    full_hedge: bool = False,
) -> Optional[TradeResult]:
    """Simulate MM + dynamic hedge on a single market.

    obs_df must have 'abs_velocity' column pre-merged from BTC data.
    """

    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) < 50:
        return None

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # State
    up_shares = 0
    down_shares = 0
    up_cost = 0.0
    down_cost = 0.0

    phase = "waiting"
    entry_expensive_side = None
    trend_shift_start = None
    trend_shifted = False
    orders_paused = False
    last_action_ts = 0
    cooldown_ms = 10000  # 10s between buys

    # Pending maker orders
    pending_up = None  # (bid_price, shares, order_ts)
    pending_down = None

    # Iterate through observer rows
    for _, obs_row in mdf.iterrows():
        ts = obs_row['timestamp_ms']
        time_rem = obs_row.get('time_remaining_secs', 450)

        up_bid = obs_row['up_bid']
        up_ask = obs_row['up_ask']
        down_bid = obs_row['down_bid']
        down_ask = obs_row['down_ask']

        if pd.isna(up_ask) or pd.isna(down_ask):
            continue

        # Get BTC velocity (pre-merged)
        abs_vel = obs_row.get('abs_velocity', 0)
        if pd.isna(abs_vel):
            abs_vel = 0

        # Determine expensive side
        if up_ask > down_ask:
            expensive_side = 'UP'
            expensive_ask = up_ask
            cheap_ask = down_ask
        else:
            expensive_side = 'DOWN'
            expensive_ask = down_ask
            cheap_ask = up_ask

        # ===== CHECK PENDING ORDERS FOR FILLS =====
        # Maker fill: our bid gets hit when ask drops to our level
        if pending_up and up_ask <= pending_up[0]:
            up_shares += pending_up[1]
            up_cost += pending_up[0] * pending_up[1]
            pending_up = None

        if pending_down and down_ask <= pending_down[0]:
            down_shares += pending_down[1]
            down_cost += pending_down[0] * pending_down[1]
            pending_down = None

        # ===== HIGH VELOCITY - CANCEL ORDERS =====
        if abs_vel > HIGH_VEL_THRESHOLD:
            pending_up = None
            pending_down = None
            orders_paused = True
            continue
        else:
            orders_paused = False

        # ===== PHASE LOGIC =====
        if phase == "waiting":
            # Check for entry signal
            if expensive_ask >= MIN_EXPENSIVE_ASK and time_rem >= MIN_TIME_REMAINING:
                phase = "half_size"
                entry_expensive_side = expensive_side

        elif phase == "half_size":
            # Check for trend shift
            if expensive_side != entry_expensive_side:
                if trend_shift_start is None:
                    trend_shift_start = ts
                elif (ts - trend_shift_start) >= TREND_SHIFT_SECONDS * 1000:
                    trend_shifted = True
                    phase = "full_size"
                    entry_expensive_side = expensive_side  # Update to new expensive
                    trend_shift_start = None
            else:
                trend_shift_start = None

            # Place maker orders with bias
            if not orders_paused and (ts - last_action_ts) >= cooldown_ms:
                target_expensive = int(HALF_SIZE * (1 + BIAS_NORMAL))
                target_cheap = int(HALF_SIZE * (1 - BIAS_NORMAL))

                if expensive_side == 'UP':
                    if up_shares < target_expensive and pending_up is None:
                        bid = up_ask - ENTRY_OFFSET
                        pending_up = (bid, 1, ts)
                        last_action_ts = ts
                    if down_shares < target_cheap and pending_down is None:
                        bid = down_ask - ENTRY_OFFSET
                        pending_down = (bid, 1, ts)
                        last_action_ts = ts
                else:
                    if down_shares < target_expensive and pending_down is None:
                        bid = down_ask - ENTRY_OFFSET
                        pending_down = (bid, 1, ts)
                        last_action_ts = ts
                    if up_shares < target_cheap and pending_up is None:
                        bid = up_ask - ENTRY_OFFSET
                        pending_up = (bid, 1, ts)
                        last_action_ts = ts

        elif phase == "full_size":
            # Heavier bias to NEW expensive side
            if not orders_paused and (ts - last_action_ts) >= cooldown_ms:
                target_expensive = int(FULL_SIZE * (1 + BIAS_HEAVY))

                if expensive_side == 'UP':
                    if up_shares < target_expensive and pending_up is None:
                        bid = up_ask - ENTRY_OFFSET
                        pending_up = (bid, 1, ts)
                        last_action_ts = ts
                else:
                    if down_shares < target_expensive and pending_down is None:
                        bid = down_ask - ENTRY_OFFSET
                        pending_down = (bid, 1, ts)
                        last_action_ts = ts

        # ===== NEAR EXPIRY - HEDGE =====
        if time_rem <= EXPIRY_WINDOW and phase in ["half_size", "full_size"]:
            phase = "hedging"

        if phase == "hedging":
            if not orders_paused and (ts - last_action_ts) >= cooldown_ms:
                imbalance = (up_shares - down_shares) / max(up_shares + down_shares, 1)

                if full_hedge:
                    # Try to fully balance
                    if up_shares > down_shares and pending_down is None:
                        bid = down_ask - ENTRY_OFFSET
                        pending_down = (bid, 1, ts)
                        last_action_ts = ts
                    elif down_shares > up_shares and pending_up is None:
                        bid = up_ask - ENTRY_OFFSET
                        pending_up = (bid, 1, ts)
                        last_action_ts = ts
                else:
                    # Reduce to 40% imbalance
                    if abs(imbalance) > BIAS_NORMAL:
                        if imbalance > 0 and pending_down is None:
                            bid = down_ask - ENTRY_OFFSET
                            pending_down = (bid, 1, ts)
                            last_action_ts = ts
                        elif imbalance < 0 and pending_up is None:
                            bid = up_ask - ENTRY_OFFSET
                            pending_up = (bid, 1, ts)
                            last_action_ts = ts

    # ===== RESOLUTION =====
    if up_shares == 0 and down_shares == 0:
        return None

    total_cost = up_cost + down_cost
    if resolution == 'UP':
        pnl = up_shares * 1.0 - total_cost
    else:
        pnl = down_shares * 1.0 - total_cost

    imbalance = (up_shares - down_shares) / max(up_shares + down_shares, 1)

    return TradeResult(
        market_slug=slug,
        up_shares=up_shares,
        down_shares=down_shares,
        up_cost=up_cost,
        down_cost=down_cost,
        resolution=resolution,
        pnl=pnl,
        imbalance=imbalance,
        trend_shifted=trend_shifted,
        phase=phase,
        dataset=dataset_name,
    )


# =============================================================================
# BACKTEST RUNNER
# =============================================================================
def run_backtest(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    dataset_name: str,
    full_hedge: bool = False,
) -> List[TradeResult]:
    """Run backtest on all markets."""

    # Precompute spikes
    print(f"\nComputing spikes...")
    btc_spikes = precompute_spikes_ewma(btc_df)

    # PRE-MERGE BTC velocity with observer data (avoid O(n²) lookup)
    print(f"  Merging BTC velocity with observer...")
    btc_for_merge = btc_spikes[['timestamp_ms', 'abs_velocity']].copy()
    btc_for_merge = btc_for_merge.sort_values('timestamp_ms')
    obs_sorted = obs_df.sort_values('timestamp_ms')
    obs_merged = pd.merge_asof(
        obs_sorted,
        btc_for_merge,
        on='timestamp_ms',
        direction='nearest',
        tolerance=1000  # 1s tolerance
    )
    obs_merged['abs_velocity'] = obs_merged['abs_velocity'].fillna(0)
    print(f"  Merged: {len(obs_merged):,} rows")

    # Get resolutions
    markets = obs_merged['market_slug'].unique()
    print(f"  Markets: {len(markets)}")

    resolutions = {}
    for slug in markets:
        mdf = obs_merged[obs_merged['market_slug'] == slug]
        last = mdf.iloc[-1]
        if last['up_bid'] > 0.9:
            resolutions[slug] = 'UP'
        elif last['down_bid'] > 0.9:
            resolutions[slug] = 'DOWN'
        elif last['up_bid'] > last['down_bid']:
            resolutions[slug] = 'UP'
        else:
            resolutions[slug] = 'DOWN'

    # Simulate each market (use obs_merged which has velocity pre-merged)
    trades = []
    print(f"  Simulating {len(markets)} markets...")
    for i, slug in enumerate(markets):
        if i > 0 and i % 50 == 0:
            print(f"    {i}/{len(markets)} markets processed...")
        res = resolutions.get(slug)
        if not res:
            continue
        result = simulate_market(obs_merged, slug, res, dataset_name, full_hedge)
        if result:
            trades.append(result)
    print(f"  Done: {len(trades)} trades")

    return trades


def print_results(trades: List[TradeResult], label: str):
    """Print results."""
    print(f"\n{'='*60}")
    print(f"RESULTS: {label}")
    print(f"{'='*60}")

    if not trades:
        print("No trades")
        return

    print(f"Markets traded: {len(trades)}")

    pnls = [t.pnl for t in trades]
    print(f"\n💰 PnL:")
    print(f"  Total: ${sum(pnls):.2f}")
    print(f"  Mean: ${np.mean(pnls):.3f}")
    print(f"  Win rate: {np.mean([p > 0 for p in pnls])*100:.1f}%")
    print(f"  Winners: {sum(1 for p in pnls if p > 0)}/{len(pnls)}")

    # Positions
    up_shares = [t.up_shares for t in trades]
    down_shares = [t.down_shares for t in trades]
    imbalances = [t.imbalance for t in trades]
    print(f"\n📊 Positions:")
    print(f"  Avg UP: {np.mean(up_shares):.1f}, Avg DOWN: {np.mean(down_shares):.1f}")
    print(f"  Avg imbalance: {np.mean(imbalances):.2f}")

    # Trend shifts
    shifted = [t for t in trades if t.trend_shifted]
    print(f"\n🔄 Trend shifts: {len(shifted)}/{len(trades)}")
    if shifted:
        s_pnl = sum(t.pnl for t in shifted)
        print(f"  Shifted PnL: ${s_pnl:.2f}")

    not_shifted = [t for t in trades if not t.trend_shifted]
    if not_shifted:
        ns_pnl = sum(t.pnl for t in not_shifted)
        print(f"  Not shifted PnL: ${ns_pnl:.2f}")

    # Duration estimate
    duration_h = 69.4  # IS+OOS2
    print(f"\n⏱️ PnL/hour: ${sum(pnls)/duration_h:.2f}")


def main():
    print("="*60)
    print("MM + DYNAMIC HEDGE BACKTEST")
    print("="*60)
    print(f"\nParameters:")
    print(f"  HALF={HALF_SIZE}, FULL={FULL_SIZE}")
    print(f"  BIAS_NORMAL={BIAS_NORMAL}, BIAS_HEAVY={BIAS_HEAVY}")
    print(f"  MIN_EXP_ASK=${MIN_EXPENSIVE_ASK}")
    print(f"  HIGH_VEL={HIGH_VEL_THRESHOLD} bps/s")

    # Load data
    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    btc_path = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")

    print(f"\nLoading...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    btc_df = pd.read_csv(btc_path)
    print(f"  Obs: {len(obs_df):,}, BTC: {len(btc_df):,}")

    # Scenario 1: 40% imbalance
    print(f"\n{'='*60}")
    print("SCENARIO 1: End with 40% imbalance")
    trades_s1 = run_backtest(btc_df, obs_df, "IS+OOS2", full_hedge=False)
    print_results(trades_s1, "40% Imbalance")

    # Scenario 2: Full hedge
    print(f"\n{'='*60}")
    print("SCENARIO 2: Full hedge")
    trades_s2 = run_backtest(btc_df, obs_df, "IS+OOS2", full_hedge=True)
    print_results(trades_s2, "Full Hedge")

    # Compare
    print(f"\n{'='*60}")
    print("COMPARISON")
    print("="*60)
    s1_pnl = sum(t.pnl for t in trades_s1)
    s2_pnl = sum(t.pnl for t in trades_s2)
    print(f"  40% Imbalance: ${s1_pnl:.2f}")
    print(f"  Full Hedge:    ${s2_pnl:.2f}")


if __name__ == "__main__":
    main()
