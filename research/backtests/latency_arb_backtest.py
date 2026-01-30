#!/usr/bin/env python3
"""
Latency Arbitrage Strategy Backtest

Exploits the measured 0.6-2.35 second lag between Binance price move
and Polymarket orderbook reaction.

Key Insight from Research:
- 78.8% of lags are <= 1.0 seconds
- Mean lag: 2.35s, Median: 0.81s
- Actionable window: ~800ms

Strategy:
1. Detect Binance BTC spike (3-tick, ~50ms at 60Hz)
2. Immediately buy predicted winner on Polymarket
3. Post loser hedge bid based on spike magnitude
4. Stop-loss hedge if winner drops 7%

Target: $15-25/hr

Usage:
    python research/latency_arb_backtest.py
    python research/latency_arb_backtest.py --analyze-lag
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from collections import defaultdict
import argparse


# =============================================================================
# CONFIGURATION
# =============================================================================

STARTING_BALANCE = 170.0
TARGET_SHARES = 15
MIN_TIME = 120  # Entry cutoff (more conservative for latency arb)

# Polymarket order restrictions
MIN_ORDER_QTY = 5      # Minimum 5 shares per order
MIN_ORDER_VALUE = 1.0  # Minimum $1 per order

# Market filtering
MIN_RUNTIME_SECS = 300
REQUIRE_STANDARD_START = True

# Spike detection
# Observer data is 5Hz (200ms intervals), not 60Hz
# 3 ticks at 5Hz = 600ms lookback
# Use 1-tick for faster detection on 5Hz data
SPIKE_LOOKBACK = 1  # 1 tick at 5Hz = 200ms (was 3 ticks for 60Hz)
SPIKE_THRESHOLD = 0.03  # Increase threshold for 5Hz data (was 0.02%)

# Magnitude-based loser bid
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01

# Stop-loss - test multiple levels
STOP_LOSS_PCT = 0.10  # Increased from 7% to 10% to reduce false stop-outs
STOP_LOSS_OPTIONS = [0.05, 0.07, 0.10, 0.12, 0.15]  # For testing

# Latency parameters
MAX_ACTIONABLE_LAG_MS = 800  # ~800ms window
MIN_CYCLE_GAP_SAMPLES = 5


# =============================================================================
# ORDER VALIDATION
# =============================================================================

def validate_order(shares: int, price: float) -> bool:
    """
    Validate order meets Polymarket restrictions.

    Polymarket requires:
    - Minimum 5 shares per order
    - Minimum $1 order value
    """
    if shares < MIN_ORDER_QTY:
        return False
    if shares * price < MIN_ORDER_VALUE:
        return False
    return True


def get_valid_order_size(price: float, target_shares: int = TARGET_SHARES) -> int:
    """
    Get valid order size that meets Polymarket restrictions.

    Returns target_shares if valid, otherwise returns minimum valid size,
    or 0 if no valid size exists.
    """
    if price <= 0:
        return 0

    # Check if target_shares meets requirements
    if target_shares >= MIN_ORDER_QTY and target_shares * price >= MIN_ORDER_VALUE:
        return target_shares

    # Calculate minimum shares needed for $1 minimum
    min_shares_for_value = int(np.ceil(MIN_ORDER_VALUE / price))
    min_valid_shares = max(MIN_ORDER_QTY, min_shares_for_value)

    return min_valid_shares


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class LatencyArbResult:
    """Result of a latency arbitrage trade."""
    strategy: str = "latency_arb"
    cycle_num: int = 0
    market_slug: str = ""
    entry_time_remaining: float = 0.0
    winner_side: str = ""
    winner_fill_price: float = 0.0
    loser_fill_price: float = 0.0
    hedge_type: str = ""  # "passive", "stoploss", "unhedged"
    pair_cost: float = 0.0
    pnl: float = 0.0
    resolution: str = ""
    prediction_correct: bool = False
    samples_to_hedge: int = 0

    # Spike specifics
    spike_magnitude: float = 0.0
    spike_to_fill_ms: float = 0.0  # Latency measurement

    # Lag bucket analysis
    lag_bucket: str = ""  # "<500ms", "500-1000ms", "1-2s", ">2s"


@dataclass
class LagAnalysis:
    """Analysis of Binance->Polymarket lag."""
    spike_ts_ms: int
    spike_direction: str
    spike_magnitude: float
    poly_reaction_ts_ms: int
    lag_ms: float


# =============================================================================
# RESOLUTION CACHE
# =============================================================================

_RESOLUTION_CACHE: Dict[str, str] = {}


def load_resolution_cache():
    """Load actual market resolutions."""
    global _RESOLUTION_CACHE
    resolution_file = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions.csv')

    if resolution_file.exists():
        try:
            df = pd.read_csv(resolution_file)
            for _, row in df.iterrows():
                slug = row['market']
                winner = row['winner']
                if winner in ('UP', 'DOWN'):
                    _RESOLUTION_CACHE[slug] = winner
            print(f"  Loaded {len(_RESOLUTION_CACHE)} resolutions")
        except Exception as e:
            print(f"  Warning: Could not load resolutions: {e}")


def get_resolution(mdf: pd.DataFrame, slug: str = "") -> str:
    """Get market resolution."""
    if slug and slug in _RESOLUTION_CACHE:
        return _RESOLUTION_CACHE[slug]

    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        return 'UP'
    elif final['down_bid'] >= 0.90:
        return 'DOWN'
    else:
        return 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'


# =============================================================================
# MARKET FILTERING
# =============================================================================

def is_valid_market(mdf: pd.DataFrame, slug: str) -> Tuple[bool, str]:
    """Validate market completeness."""
    if len(mdf) < 25:
        return False, "too_few_samples"

    first = mdf.iloc[0]['time_remaining_secs']
    last = mdf.iloc[-1]['time_remaining_secs']

    runtime = first - last
    if runtime < MIN_RUNTIME_SECS:
        return False, "runtime_under_5min"

    if REQUIRE_STANDARD_START:
        try:
            timestamp = int(slug.split('-')[-1])
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if dt.minute % 15 != 0:
                return False, "irregular_start_time"
        except:
            pass

    if first < 800 or last > 60:
        return False, "incomplete_observation"

    return True, "valid"


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def add_spike_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add spike detection columns to dataframe."""
    df = df.copy()

    if 'binance_price' not in df.columns:
        df['spike_detected'] = False
        df['spike_direction'] = None
        df['spike_magnitude'] = 0.0
        return df

    df['price_change_3tick'] = df['binance_price'].pct_change(periods=SPIKE_LOOKBACK) * 100
    df['spike_magnitude'] = df['price_change_3tick'].abs()
    df['spike_detected'] = df['spike_magnitude'] >= SPIKE_THRESHOLD
    df['spike_direction'] = df['price_change_3tick'].apply(
        lambda x: 'UP' if x >= SPIKE_THRESHOLD else ('DOWN' if x <= -SPIKE_THRESHOLD else None)
    )

    return df


def get_lag_bucket(lag_ms: float) -> str:
    """Categorize lag into buckets."""
    if lag_ms < 500:
        return "<500ms"
    elif lag_ms < 1000:
        return "500-1000ms"
    elif lag_ms < 2000:
        return "1-2s"
    else:
        return ">2s"


# =============================================================================
# LATENCY ARBITRAGE SIMULATION
# =============================================================================

def simulate_latency_arb_market(
    mdf: pd.DataFrame,
    slug: str,
) -> Optional[List[LatencyArbResult]]:
    """
    Simulate latency arbitrage strategy.

    Detects Binance spikes and exploits lag to Polymarket orderbook.
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
    mdf = add_spike_columns(mdf)

    resolution = get_resolution(mdf, slug)
    trades = []
    cycle_num = 0

    i = 0
    in_trade = False

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']

        if time_rem < MIN_TIME:
            break

        if not in_trade:
            # Look for spike signal
            if not row.get('spike_detected', False):
                i += 1
                continue

            spike_dir = row.get('spike_direction')
            if spike_dir is None:
                i += 1
                continue

            spike_mag = row['spike_magnitude']
            # Handle timestamp - use index-based timing if timestamp_ms not available
            entry_ts = row.get('timestamp_ms', 0)
            if entry_ts == 0 and 'timestamp' in row:
                # Try to parse timestamp string
                try:
                    import datetime as dt
                    entry_ts = int(pd.to_datetime(row['timestamp']).timestamp() * 1000)
                except:
                    pass

            # Determine winner/loser
            winner_side = spike_dir
            if winner_side == "UP":
                winner_fill_price = row['up_ask']
                loser_ask = row['down_ask']
                loser_bid = row['down_bid']
            else:
                winner_fill_price = row['down_ask']
                loser_ask = row['up_ask']
                loser_bid = row['up_bid']

            # Validate winner entry order meets Polymarket restrictions
            order_size = get_valid_order_size(winner_fill_price, TARGET_SHARES)
            if not validate_order(order_size, winner_fill_price):
                i += 1
                continue

            in_trade = True
            cycle_num += 1
            entry_time = time_rem

            # Magnitude-based loser bid
            expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT
            loser_target_bid = loser_ask - expected_drop
            loser_target_bid = max(0.01, min(0.95, loser_target_bid))

            # Scan forward for hedge
            loser_filled = False
            loser_fill_price = 0.0
            hedge_type = "unhedged"
            samples_to_hedge = 0
            spike_to_fill_ms = 0.0

            for j in range(i + 1, len(mdf)):
                check_row = mdf.iloc[j]
                check_time = check_row['time_remaining_secs']

                if check_time < 10:
                    break

                if winner_side == "UP":
                    loser_ask_now = check_row['down_ask']
                    winner_bid_now = check_row['up_bid']
                else:
                    loser_ask_now = check_row['up_ask']
                    winner_bid_now = check_row['down_bid']

                # Check passive fill (loser ask dropped to our bid)
                if loser_ask_now <= loser_target_bid:
                    loser_filled = True
                    loser_fill_price = loser_target_bid
                    hedge_type = "passive"
                    samples_to_hedge = j - i

                    # Measure latency using sample count if timestamps unavailable
                    fill_ts = check_row.get('timestamp_ms', 0)
                    if fill_ts == 0 and 'timestamp' in check_row:
                        try:
                            fill_ts = int(pd.to_datetime(check_row['timestamp']).timestamp() * 1000)
                        except:
                            pass

                    if entry_ts > 0 and fill_ts > 0:
                        spike_to_fill_ms = fill_ts - entry_ts
                    else:
                        # Estimate based on sample count (5Hz = 200ms per sample)
                        spike_to_fill_ms = samples_to_hedge * 200

                    i = j + MIN_CYCLE_GAP_SAMPLES
                    break

                # Check stop-loss
                if winner_fill_price > 0:
                    drop_pct = (winner_fill_price - winner_bid_now) / winner_fill_price
                    if drop_pct >= STOP_LOSS_PCT:
                        loser_filled = True
                        loser_fill_price = loser_ask_now
                        hedge_type = "stoploss"
                        samples_to_hedge = j - i

                        fill_ts = check_row.get('timestamp_ms', 0)
                        if fill_ts == 0 and 'timestamp' in check_row:
                            try:
                                fill_ts = int(pd.to_datetime(check_row['timestamp']).timestamp() * 1000)
                            except:
                                pass

                        if entry_ts > 0 and fill_ts > 0:
                            spike_to_fill_ms = fill_ts - entry_ts
                        else:
                            spike_to_fill_ms = samples_to_hedge * 200

                        i = j + MIN_CYCLE_GAP_SAMPLES
                        break

            # Calculate PnL using validated order size
            prediction_correct = (winner_side == resolution)

            if loser_filled:
                # Validate loser hedge order meets Polymarket restrictions
                loser_order_size = get_valid_order_size(loser_fill_price, order_size)
                if not validate_order(loser_order_size, loser_fill_price):
                    # Can't hedge with valid order - treat as unhedged
                    loser_filled = False
                    hedge_type = "unhedged"

            if loser_filled:
                pair_cost = winner_fill_price + loser_fill_price
                pnl = (1.0 - pair_cost) * order_size
            else:
                i = j + MIN_CYCLE_GAP_SAMPLES if 'j' in dir() else i + 1
                if prediction_correct:
                    pnl = (1.0 - winner_fill_price) * order_size
                else:
                    pnl = (0.0 - winner_fill_price) * order_size
                pair_cost = winner_fill_price

            lag_bucket = get_lag_bucket(spike_to_fill_ms) if spike_to_fill_ms > 0 else "unknown"

            trades.append(LatencyArbResult(
                strategy="latency_arb",
                cycle_num=cycle_num,
                market_slug=slug,
                entry_time_remaining=entry_time,
                winner_side=winner_side,
                winner_fill_price=winner_fill_price,
                loser_fill_price=loser_fill_price,
                hedge_type=hedge_type,
                pair_cost=pair_cost,
                pnl=pnl,
                resolution=resolution,
                prediction_correct=prediction_correct,
                samples_to_hedge=samples_to_hedge,
                spike_magnitude=spike_mag,
                spike_to_fill_ms=spike_to_fill_ms,
                lag_bucket=lag_bucket,
            ))

            in_trade = False

        i += 1

    return trades if trades else None


# =============================================================================
# LAG ANALYSIS
# =============================================================================

def analyze_latency(
    observer_df: pd.DataFrame,
    binance_df: pd.DataFrame,
) -> List[LagAnalysis]:
    """
    Analyze latency between Binance spikes and Polymarket reactions.

    Uses high-frequency Binance data to detect spikes, then measures
    how long until Polymarket orderbook reflects the move.
    """
    if binance_df.empty:
        print("No Binance HF data available for lag analysis")
        return []

    # Add spike detection to Binance data
    binance_df = binance_df.copy()
    binance_df['price_change_3tick'] = binance_df['price'].pct_change(3) * 100
    binance_df['spike_magnitude'] = binance_df['price_change_3tick'].abs()
    binance_df['is_spike'] = binance_df['spike_magnitude'] >= SPIKE_THRESHOLD
    binance_df['spike_direction'] = binance_df['price_change_3tick'].apply(
        lambda x: 'UP' if x >= SPIKE_THRESHOLD else ('DOWN' if x <= -SPIKE_THRESHOLD else None)
    )

    spikes = binance_df[binance_df['is_spike']].copy()
    print(f"Detected {len(spikes):,} spikes in Binance HF data")

    lag_results = []

    for _, spike in spikes.iterrows():
        spike_ts = spike['timestamp_ms']
        spike_dir = spike['spike_direction']
        spike_mag = spike['spike_magnitude']

        # Find observer rows within 5 seconds after spike
        mask = (observer_df['timestamp_ms'] >= spike_ts) & \
               (observer_df['timestamp_ms'] <= spike_ts + 5000)
        nearby = observer_df[mask]

        if len(nearby) < 2:
            continue

        first_row = nearby.iloc[0]
        baseline_up_bid = first_row['up_bid']
        baseline_down_bid = first_row['down_bid']

        # Look for Polymarket reaction
        for _, row in nearby.iterrows():
            reacted = False

            if spike_dir == "UP" and row['up_bid'] > baseline_up_bid + 0.005:
                reacted = True
            elif spike_dir == "DOWN" and row['down_bid'] > baseline_down_bid + 0.005:
                reacted = True

            if reacted:
                lag_ms = row['timestamp_ms'] - spike_ts
                lag_results.append(LagAnalysis(
                    spike_ts_ms=spike_ts,
                    spike_direction=spike_dir,
                    spike_magnitude=spike_mag,
                    poly_reaction_ts_ms=row['timestamp_ms'],
                    lag_ms=lag_ms,
                ))
                break

    return lag_results


# =============================================================================
# DATA LOADING
# =============================================================================

def load_market_data() -> Tuple[Dict[str, pd.DataFrame], Dict]:
    """Load and filter market data."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('grid_obs_*.csv'))
    csv_files.extend(sorted(observer_dir.glob('spread_capture_obs_*.csv')))

    print(f"Loading data from {len(csv_files)} files...")

    all_markets = {}
    filter_stats = defaultdict(int)

    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            if df.empty:
                continue

            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                is_valid, reason = is_valid_market(mdf, slug)

                if is_valid:
                    if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                        all_markets[slug] = mdf.copy()
                    filter_stats["valid"] += 1
                else:
                    filter_stats[reason] += 1

        except Exception as e:
            continue

    filter_stats["valid"] = len(all_markets)
    print(f"Unique valid markets: {len(all_markets)}")
    return all_markets, dict(filter_stats)


def load_binance_hf_data() -> pd.DataFrame:
    """Load high-frequency Binance data."""
    binance_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/binance_hf')
    csv_files = sorted(binance_dir.glob('btc_prices_*.csv'))

    if not csv_files:
        return pd.DataFrame()

    dfs = []
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath)
            dfs.append(df)
            print(f"  Loaded {len(df):,} rows from {filepath.name}")
        except Exception as e:
            print(f"  Error loading {filepath.name}: {e}")

    if not dfs:
        return pd.DataFrame()

    merged = pd.concat(dfs, ignore_index=True)
    merged = merged.sort_values('timestamp_ms').reset_index(drop=True)
    return merged


# =============================================================================
# ANALYSIS
# =============================================================================

def analyze_results(trades: List[LatencyArbResult], total_hours: float) -> Dict:
    """Analyze latency arbitrage results."""
    if not trades:
        return {"error": "No trades"}

    # Overall metrics
    total_pnl = sum(t.pnl for t in trades)
    hourly_rate = total_pnl / total_hours if total_hours > 0 else 0

    # By hedge type
    passive = [t for t in trades if t.hedge_type == "passive"]
    stoploss = [t for t in trades if t.hedge_type == "stoploss"]
    unhedged = [t for t in trades if t.hedge_type == "unhedged"]

    # By lag bucket
    lag_buckets = defaultdict(list)
    for t in trades:
        lag_buckets[t.lag_bucket].append(t)

    # Accuracy
    correct = [t for t in trades if t.prediction_correct]
    accuracy = len(correct) / len(trades) * 100

    return {
        "total_trades": len(trades),
        "total_pnl": total_pnl,
        "hourly_rate": hourly_rate,
        "accuracy": accuracy,
        # By hedge type
        "passive_count": len(passive),
        "passive_pnl": sum(t.pnl for t in passive),
        "stoploss_count": len(stoploss),
        "stoploss_pnl": sum(t.pnl for t in stoploss),
        "unhedged_count": len(unhedged),
        "unhedged_pnl": sum(t.pnl for t in unhedged),
        # By lag bucket
        "lag_buckets": {
            bucket: {
                "count": len(trades_in_bucket),
                "pnl": sum(t.pnl for t in trades_in_bucket),
            }
            for bucket, trades_in_bucket in lag_buckets.items()
        },
        # Spike stats
        "avg_spike_magnitude": np.mean([t.spike_magnitude for t in trades]),
        "avg_spike_to_fill_ms": np.mean([t.spike_to_fill_ms for t in trades if t.spike_to_fill_ms > 0]),
    }


# =============================================================================
# MAIN REPORT
# =============================================================================

def print_report(all_markets: Dict, results: Dict, lag_analysis: List[LagAnalysis]):
    """Print comprehensive report."""
    total_hours = len(all_markets) * 15 / 60

    print("\n" + "=" * 80)
    print("LATENCY ARBITRAGE BACKTEST RESULTS")
    print("=" * 80)

    print(f"\nMarkets: {len(all_markets)}")
    print(f"Total hours: {total_hours:.1f}")
    print(f"Stop-loss: {STOP_LOSS_PCT:.0%}")

    if "error" in results:
        print(f"\nNo trades found!")
        return

    print(f"\n{'=' * 40}")
    print("PERFORMANCE SUMMARY")
    print(f"{'=' * 40}")
    print(f"Total trades: {results['total_trades']}")
    print(f"Total PnL: ${results['total_pnl']:.2f}")
    print(f"Hourly rate: ${results['hourly_rate']:.2f}/hr")
    print(f"Accuracy: {results['accuracy']:.1f}%")

    print(f"\n{'=' * 40}")
    print("HEDGE TYPE BREAKDOWN")
    print(f"{'=' * 40}")
    print(f"Passive: {results['passive_count']} trades, ${results['passive_pnl']:.2f}")
    print(f"Stop-loss: {results['stoploss_count']} trades, ${results['stoploss_pnl']:.2f}")
    print(f"Unhedged: {results['unhedged_count']} trades, ${results['unhedged_pnl']:.2f}")

    print(f"\n{'=' * 40}")
    print("LAG BUCKET BREAKDOWN")
    print(f"{'=' * 40}")
    for bucket, data in sorted(results['lag_buckets'].items()):
        print(f"{bucket:>12}: {data['count']:>4} trades, ${data['pnl']:>8.2f}")

    print(f"\n{'=' * 40}")
    print("SPIKE STATISTICS")
    print(f"{'=' * 40}")
    print(f"Avg spike magnitude: {results['avg_spike_magnitude']:.4f}%")
    if results['avg_spike_to_fill_ms'] > 0:
        print(f"Avg spike-to-fill: {results['avg_spike_to_fill_ms']:.0f}ms")

    # Lag analysis summary
    if lag_analysis:
        print(f"\n{'=' * 40}")
        print("DETAILED LAG ANALYSIS")
        print(f"{'=' * 40}")
        lags = [la.lag_ms for la in lag_analysis]
        print(f"Spikes analyzed: {len(lag_analysis)}")
        print(f"Mean lag: {np.mean(lags):.0f}ms")
        print(f"Median lag: {np.median(lags):.0f}ms")
        print(f"Min/Max: {np.min(lags):.0f}ms / {np.max(lags):.0f}ms")

        # Percentiles
        print(f"\nPercentiles:")
        for p in [25, 50, 75, 90, 95]:
            print(f"  {p}th: {np.percentile(lags, p):.0f}ms")

        # Bucket distribution
        print(f"\nBucket distribution:")
        buckets = defaultdict(int)
        for la in lag_analysis:
            buckets[get_lag_bucket(la.lag_ms)] += 1
        for bucket in ["<500ms", "500-1000ms", "1-2s", ">2s"]:
            pct = buckets[bucket] / len(lag_analysis) * 100
            print(f"  {bucket:>12}: {buckets[bucket]:>5} ({pct:>5.1f}%)")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Latency Arbitrage Backtest")
    parser.add_argument('--analyze-lag', action='store_true',
                        help='Perform detailed lag analysis with HF Binance data')
    args = parser.parse_args()

    print("=" * 80)
    print("LATENCY ARBITRAGE BACKTEST")
    print("=" * 80)

    # Load resolution data
    print("\nLoading resolution data...")
    load_resolution_cache()

    # Load market data
    all_markets, filter_stats = load_market_data()

    if not all_markets:
        print("No valid markets found!")
        return

    total_hours = len(all_markets) * 15 / 60
    print(f"Total hours: {total_hours:.1f}")

    # Run simulation
    print("\nRunning latency arbitrage simulation...")
    all_trades = []
    for slug, mdf in all_markets.items():
        trades = simulate_latency_arb_market(mdf, slug)
        if trades:
            all_trades.extend(trades)

    results = analyze_results(all_trades, total_hours)

    # Optional lag analysis with HF data
    lag_analysis = []
    if args.analyze_lag:
        print("\nLoading Binance HF data for lag analysis...")
        binance_df = load_binance_hf_data()

        if not binance_df.empty:
            # Combine all observer data
            observer_dfs = []
            for mdf in all_markets.values():
                observer_dfs.append(mdf)
            observer_combined = pd.concat(observer_dfs, ignore_index=True)
            observer_combined = observer_combined.sort_values('timestamp_ms').reset_index(drop=True)

            print("\nAnalyzing Binance->Polymarket latency...")
            lag_analysis = analyze_latency(observer_combined, binance_df)

    # Print report
    print_report(all_markets, results, lag_analysis)


if __name__ == "__main__":
    main()
