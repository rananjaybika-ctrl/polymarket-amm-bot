#!/usr/bin/env python3
"""
AGGRESSIVE_M V2 Stop Loss Analysis - Losing Trades Only

Purpose: Analyze which stop loss method minimizes losses on the ~10% of trades
where FADE is wrong (fade_correct == False).

Tests:
1. TIME-BASED STOPS: Exit after N seconds regardless of price
   - 30s, 60s, 120s, 300s

2. PRICE-BASED STOPS: Exit when price moves X% against us
   - 10%, 15%, 20%, 25% adverse move from entry

For each losing trade, we track the expensive_side price over time and measure:
- What would the loss be if we stopped at time T?
- What would the loss be if we stopped at price drop X%?
- Compare to hold-to-resolution loss ($0.68/share on average)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from tqdm import tqdm
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# CONFIGURATION
# =============================================================================
BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")

# Time-based stop windows (seconds)
TIME_STOPS = [30, 60, 120, 300]

# Price-based stop thresholds (% drop from entry)
PRICE_STOPS = [0.10, 0.15, 0.20, 0.25]

# Datasets to analyze
DATASETS = {
    "IS+OOS2": {
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
    },
    "OOS7": {
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
    },
}


def load_losing_trades() -> pd.DataFrame:
    """Load AGGRESSIVE_M V2 study results, filter for losing trades."""
    results_path = BASE_DIR / "research/findings/data/aggressive_m_v2_ewma_study_results.csv"

    if not results_path.exists():
        print(f"ERROR: Results file not found: {results_path}")
        return None

    df = pd.read_csv(results_path)
    print(f"Loaded {len(df):,} total signals")

    # Filter for losing trades (FADE was wrong)
    losing = df[df['fade_correct'] == False].copy()
    print(f"Losing trades (FADE wrong): {len(losing):,} ({len(losing)/len(df)*100:.1f}%)")

    # Column name compatibility (old: loser_ask, new: expensive_ask)
    if 'loser_ask' in losing.columns and 'expensive_ask' not in losing.columns:
        losing['expensive_ask'] = losing['loser_ask']
    if 'winner_ask' in losing.columns and 'spike_ask' not in losing.columns:
        losing['spike_ask'] = losing['winner_ask']

    # Filter for expensive_ask >= 0.65 (the actual V2 filter)
    losing = losing[losing['expensive_ask'] >= 0.65].copy()
    print(f"After expensive_ask >= $0.65 filter: {len(losing):,}")

    return losing


def load_observer_data(dataset_key: str) -> pd.DataFrame:
    """Load observer data for a dataset."""
    config = DATASETS[dataset_key]

    obs_dfs = []
    for fname in config['obs_files']:
        fpath = BASE_DIR / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)

    if not obs_dfs:
        return None

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.sort_values('timestamp_ms').reset_index(drop=True)
    return obs_df


def analyze_stop_loss_for_trade(
    trade_row: pd.Series,
    obs_df: pd.DataFrame,
    time_stops: list,
    price_stops: list,
) -> dict:
    """
    For a single losing trade, analyze what the loss would be with different stops.

    Args:
        trade_row: Row from losing trades DataFrame
        obs_df: Observer data for the market
        time_stops: List of time-based stop windows (seconds)
        price_stops: List of price-based stop thresholds (% drop)

    Returns:
        Dict with loss at each stop type
    """
    spike_ts = trade_row['spike_ts']
    spike_dir = trade_row['spike_direction']
    # Our entry (FADE buys expensive_side) - handle both column names
    entry_price = trade_row.get('expensive_ask', trade_row.get('loser_ask'))
    market_slug = trade_row['market_slug']

    # Get market data after spike
    mdf = obs_df[
        (obs_df['market_slug'] == market_slug) &
        (obs_df['timestamp_ms'] >= spike_ts)
    ].copy()

    if len(mdf) == 0:
        return None

    # Determine which price column to track (we bought expensive_side)
    if spike_dir == "UP":
        # Spike UP → expensive_side is DOWN
        price_col = 'down_ask'  # Track ask to see what we could sell at
        bid_col = 'down_bid'
    else:
        # Spike DOWN → expensive_side is UP
        price_col = 'up_ask'
        bid_col = 'up_bid'

    mdf = mdf.dropna(subset=[price_col])
    if len(mdf) == 0:
        return None

    # Initialize results
    results = {
        'market_slug': market_slug,
        'spike_ts': spike_ts,
        'entry_price': entry_price,
        'resolution_loss': entry_price,  # Lose full entry on wrong FADE
    }

    # TIME-BASED STOPS
    for t in time_stops:
        window_end = spike_ts + (t * 1000)  # Convert to ms
        window_df = mdf[mdf['timestamp_ms'] <= window_end]

        if len(window_df) > 0:
            # Get the bid at end of window (what we could sell at)
            exit_row = window_df.iloc[-1]
            exit_bid = exit_row.get(bid_col, exit_row[price_col] - 0.02)
            if pd.isna(exit_bid):
                exit_bid = exit_row[price_col] - 0.02

            # Loss = entry - exit (we bought at entry, sell at exit_bid)
            loss = entry_price - max(0, exit_bid)
            results[f'time_stop_{t}s'] = loss
        else:
            results[f'time_stop_{t}s'] = entry_price  # No data = assume full loss

    # PRICE-BASED STOPS
    for pct in price_stops:
        stop_price = entry_price * (1 - pct)  # Stop if price drops to this

        # Find first row where bid drops below stop_price
        stopped = False
        for _, row in mdf.iterrows():
            bid = row.get(bid_col, row[price_col] - 0.02)
            if pd.isna(bid):
                bid = row[price_col] - 0.02

            if bid <= stop_price:
                # Stop triggered
                loss = entry_price - stop_price
                results[f'price_stop_{int(pct*100)}pct'] = loss
                stopped = True
                break

        if not stopped:
            # Stop never triggered - still lose at resolution
            results[f'price_stop_{int(pct*100)}pct'] = entry_price

    # Track price trajectory for analysis
    # Get price at various time points
    for t in [10, 30, 60, 120, 300]:
        window_end = spike_ts + (t * 1000)
        window_df = mdf[mdf['timestamp_ms'] <= window_end]
        if len(window_df) > 0:
            results[f'price_at_{t}s'] = window_df.iloc[-1][price_col]
        else:
            results[f'price_at_{t}s'] = np.nan

    return results


def main():
    """Run stop loss analysis on all losing trades."""
    print("=" * 70)
    print("AGGRESSIVE_M V2 STOP LOSS ANALYSIS - LOSING TRADES")
    print("=" * 70)

    # Load losing trades
    losing_trades = load_losing_trades()
    if losing_trades is None or len(losing_trades) == 0:
        print("No losing trades found!")
        return

    all_results = []

    for dataset_key in DATASETS.keys():
        print(f"\n--- Processing {dataset_key} ---")

        # Filter losing trades for this dataset
        dataset_trades = losing_trades[losing_trades['dataset'] == dataset_key]
        print(f"  Losing trades in {dataset_key}: {len(dataset_trades):,}")

        if len(dataset_trades) == 0:
            continue

        # Load observer data
        obs_df = load_observer_data(dataset_key)
        if obs_df is None:
            print(f"  Could not load observer data for {dataset_key}")
            continue

        print(f"  Observer rows: {len(obs_df):,}")

        # Analyze each losing trade
        for _, trade in tqdm(dataset_trades.iterrows(),
                            total=len(dataset_trades),
                            desc=f"Analyzing {dataset_key}"):
            result = analyze_stop_loss_for_trade(
                trade, obs_df, TIME_STOPS, PRICE_STOPS
            )
            if result:
                result['dataset'] = dataset_key
                all_results.append(result)

    if not all_results:
        print("\nNo results collected!")
        return

    # Convert to DataFrame
    results_df = pd.DataFrame(all_results)
    print(f"\n{'=' * 70}")
    print(f"RESULTS: {len(results_df):,} losing trades analyzed")
    print(f"{'=' * 70}")

    # Calculate mean loss for each stop type
    print(f"\n--- TIME-BASED STOPS ---")
    print(f"{'Stop Type':<20} | {'Mean Loss':>12} | {'Median Loss':>12} | {'Savings vs Res':>15}")
    print("-" * 65)

    resolution_loss = results_df['resolution_loss'].mean()
    print(f"{'Hold to Resolution':<20} | ${resolution_loss:>11.3f} | ${results_df['resolution_loss'].median():>11.3f} | {'(baseline)':>15}")

    for t in TIME_STOPS:
        col = f'time_stop_{t}s'
        if col in results_df.columns:
            mean_loss = results_df[col].mean()
            median_loss = results_df[col].median()
            savings = resolution_loss - mean_loss
            print(f"{'Time stop ' + str(t) + 's':<20} | ${mean_loss:>11.3f} | ${median_loss:>11.3f} | ${savings:>14.3f}")

    print(f"\n--- PRICE-BASED STOPS ---")
    print(f"{'Stop Type':<20} | {'Mean Loss':>12} | {'Median Loss':>12} | {'Savings vs Res':>15} | {'Triggered %':>12}")
    print("-" * 80)

    for pct in PRICE_STOPS:
        col = f'price_stop_{int(pct*100)}pct'
        if col in results_df.columns:
            mean_loss = results_df[col].mean()
            median_loss = results_df[col].median()
            savings = resolution_loss - mean_loss
            # Count how often stop was triggered (loss < resolution_loss)
            triggered = (results_df[col] < results_df['resolution_loss']).mean() * 100
            print(f"{'Price stop ' + str(int(pct*100)) + '%':<20} | ${mean_loss:>11.3f} | ${median_loss:>11.3f} | ${savings:>14.3f} | {triggered:>11.1f}%")

    # Price trajectory analysis
    print(f"\n--- PRICE TRAJECTORY (Mean expensive_side ask) ---")
    print(f"Entry price (mean): ${results_df['entry_price'].mean():.3f}")
    for t in [10, 30, 60, 120, 300]:
        col = f'price_at_{t}s'
        if col in results_df.columns:
            mean_price = results_df[col].mean()
            pct_drop = (results_df['entry_price'].mean() - mean_price) / results_df['entry_price'].mean() * 100
            print(f"  At {t}s: ${mean_price:.3f} ({pct_drop:+.1f}% from entry)")

    # By dataset breakdown
    print(f"\n--- BY DATASET ---")
    for dataset in results_df['dataset'].unique():
        ds_df = results_df[results_df['dataset'] == dataset]
        print(f"\n{dataset} ({len(ds_df):,} trades):")
        print(f"  Resolution loss: ${ds_df['resolution_loss'].mean():.3f}")
        for t in TIME_STOPS:
            col = f'time_stop_{t}s'
            if col in ds_df.columns:
                print(f"  Time stop {t}s: ${ds_df[col].mean():.3f} (saves ${ds_df['resolution_loss'].mean() - ds_df[col].mean():.3f})")

    # Save results
    output_path = BASE_DIR / "research/findings/data/aggressive_m_v2_stop_loss_analysis.csv"
    results_df.to_csv(output_path, index=False)
    print(f"\nSaved: {output_path}")

    # Summary recommendations
    print(f"\n{'=' * 70}")
    print("RECOMMENDATIONS")
    print(f"{'=' * 70}")

    best_time_stop = None
    best_time_savings = 0
    for t in TIME_STOPS:
        col = f'time_stop_{t}s'
        if col in results_df.columns:
            savings = resolution_loss - results_df[col].mean()
            if savings > best_time_savings:
                best_time_savings = savings
                best_time_stop = t

    best_price_stop = None
    best_price_savings = 0
    for pct in PRICE_STOPS:
        col = f'price_stop_{int(pct*100)}pct'
        if col in results_df.columns:
            savings = resolution_loss - results_df[col].mean()
            if savings > best_price_savings:
                best_price_savings = savings
                best_price_stop = pct

    print(f"\nBest TIME stop: {best_time_stop}s (saves ${best_time_savings:.3f}/share)")
    print(f"Best PRICE stop: {int(best_price_stop*100)}% (saves ${best_price_savings:.3f}/share)")

    if best_time_savings > best_price_savings:
        print(f"\n→ Recommend TIME-BASED stop at {best_time_stop}s")
    else:
        print(f"\n→ Recommend PRICE-BASED stop at {int(best_price_stop*100)}%")


if __name__ == "__main__":
    main()
