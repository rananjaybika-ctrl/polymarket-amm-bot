#!/usr/bin/env python3
"""
Verify Backtest with Actual Polymarket Resolutions

This script:
1. Queries Polymarket API for actual market resolutions
2. Compares with our orderbook-based guesses
3. Recalculates backtest PnL with verified data

This addresses the concern that 34% of markets have "unclear" resolution
at the end of our observation window.
"""

import pandas as pd
import numpy as np
import httpx
import json
import time
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass

# =============================================================================
# CONFIGURATION
# =============================================================================

SHARES = 15
MIN_TIME = 60
VELOCITY_THRESHOLD = 0.50
LOSER_OFFSET = 0.12
STOP_LOSS_PCT = 0.07


@dataclass
class TradeResult:
    slug: str
    hedge_type: str  # "passive", "stoploss", "unhedged"
    winner_side: str
    winner_fill: float
    loser_fill: float
    pair_cost: float
    orderbook_resolution: str  # Our guess from orderbook
    api_resolution: Optional[str]  # Actual from Polymarket API
    resolution_confidence: str  # "clear" or "unclear"


def get_resolution_from_api(slug: str) -> Optional[str]:
    """Query Polymarket API for actual resolution."""
    try:
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url)
            data = resp.json()

            if not data:
                return None

            event = data[0]
            market = event.get("markets", [{}])[0]

            outcomes = json.loads(market.get("outcomes", "[]"))
            prices = json.loads(market.get("outcomePrices", "[]"))

            if not prices:
                return None

            for i, price in enumerate(prices):
                if str(price) == "1":
                    if i < len(outcomes):
                        return outcomes[i].upper()

            return None
    except Exception as e:
        print(f"  API error for {slug}: {e}")
        return None


def get_orderbook_resolution(mdf: pd.DataFrame) -> tuple:
    """Get resolution guess from orderbook and confidence level."""
    final = mdf.iloc[-1]
    up_bid = final['up_bid']
    down_bid = final['down_bid']

    if up_bid >= 0.90:
        return 'UP', 'clear'
    elif down_bid >= 0.90:
        return 'DOWN', 'clear'
    else:
        return ('UP' if up_bid > down_bid else 'DOWN'), 'unclear'


def load_market_data() -> Dict[str, pd.DataFrame]:
    """Load observer CSV data."""
    observer_dir = Path('research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"Loading data from {len(csv_files)} files...")

    all_markets = {}
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            if df.empty or 'binance_price' not in df.columns:
                continue
            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                            all_markets[slug] = mdf.copy()
        except:
            continue

    return all_markets


def simulate_trades(all_markets: Dict[str, pd.DataFrame]) -> List[TradeResult]:
    """Simulate velocity strategy and track results."""
    results = []

    for slug, mdf in all_markets.items():
        mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
        ob_resolution, confidence = get_orderbook_resolution(mdf)

        i = 0
        while i < len(mdf):
            row = mdf.iloc[i]
            time_rem = row['time_remaining_secs']
            vel = row['velocity_bps']

            if time_rem < MIN_TIME:
                break

            if abs(vel) >= VELOCITY_THRESHOLD:
                winner_side = "UP" if vel > 0 else "DOWN"

                if winner_side == "UP":
                    winner_fill = row['up_ask']
                    loser_ask = row['down_ask']
                else:
                    winner_fill = row['down_ask']
                    loser_ask = row['up_ask']

                loser_target = loser_ask - LOSER_OFFSET
                loser_target = max(0.01, min(0.95, loser_target))

                # Scan for hedge
                hedge_type = "unhedged"
                loser_fill = 0.0

                for j in range(i + 1, len(mdf)):
                    check = mdf.iloc[j]
                    if check['time_remaining_secs'] < 10:
                        break

                    if winner_side == "UP":
                        loser_ask_now = check['down_ask']
                        winner_bid_now = check['up_bid']
                    else:
                        loser_ask_now = check['up_ask']
                        winner_bid_now = check['down_bid']

                    if loser_ask_now <= loser_target:
                        hedge_type = "passive"
                        loser_fill = loser_target
                        i = j + 5
                        break

                    drop = (winner_fill - winner_bid_now) / winner_fill if winner_fill > 0 else 0
                    if drop >= STOP_LOSS_PCT:
                        hedge_type = "stoploss"
                        loser_fill = loser_ask_now
                        i = j + 5
                        break

                pair_cost = winner_fill + loser_fill if hedge_type != "unhedged" else winner_fill

                results.append(TradeResult(
                    slug=slug,
                    hedge_type=hedge_type,
                    winner_side=winner_side,
                    winner_fill=winner_fill,
                    loser_fill=loser_fill,
                    pair_cost=pair_cost,
                    orderbook_resolution=ob_resolution,
                    api_resolution=None,  # Will be filled later
                    resolution_confidence=confidence,
                ))

            i += 1

    return results


def calculate_pnl(trade: TradeResult, resolution: str) -> float:
    """Calculate PnL for a trade given the actual resolution."""
    correct = (trade.winner_side == resolution)

    if trade.hedge_type != "unhedged":
        return (1.0 - trade.pair_cost) * SHARES
    else:
        if correct:
            return (1.0 - trade.winner_fill) * SHARES
        else:
            return (0.0 - trade.winner_fill) * SHARES


def main():
    print("=" * 80)
    print("BACKTEST VERIFICATION WITH POLYMARKET RESOLUTIONS")
    print("=" * 80)

    # Load data
    all_markets = load_market_data()
    print(f"Loaded {len(all_markets)} markets")

    # Simulate trades
    print("\nSimulating trades...")
    trades = simulate_trades(all_markets)
    print(f"Total trades: {len(trades)}")

    # Get unique slugs that need resolution lookup
    unclear_slugs = set(t.slug for t in trades if t.resolution_confidence == 'unclear')
    all_slugs = set(t.slug for t in trades)

    print(f"\nMarkets with unclear resolution: {len(unclear_slugs)}")
    print(f"Total unique markets with trades: {len(all_slugs)}")

    # Query API for actual resolutions
    print("\nQuerying Polymarket API for actual resolutions...")
    api_resolutions = {}

    for i, slug in enumerate(all_slugs):
        if i % 10 == 0:
            print(f"  Progress: {i}/{len(all_slugs)}")
        resolution = get_resolution_from_api(slug)
        if resolution:
            api_resolutions[slug] = resolution
        time.sleep(0.1)  # Rate limiting

    print(f"Got {len(api_resolutions)} resolutions from API")

    # Update trades with API resolutions
    for trade in trades:
        trade.api_resolution = api_resolutions.get(trade.slug)

    # Calculate PnL with different resolution sources
    print("\n" + "=" * 80)
    print("PNL COMPARISON")
    print("=" * 80)

    # 1. Hedged trades (always reliable)
    hedged = [t for t in trades if t.hedge_type != 'unhedged']
    hedged_pnl = sum((1.0 - t.pair_cost) * SHARES for t in hedged)

    # 2. Unhedged trades
    unhedged = [t for t in trades if t.hedge_type == 'unhedged']

    # Calculate with orderbook resolution (our original method)
    unhedged_pnl_orderbook = sum(
        calculate_pnl(t, t.orderbook_resolution) for t in unhedged
    )

    # Calculate with API resolution (verified)
    unhedged_with_api = [t for t in unhedged if t.api_resolution]
    unhedged_pnl_api = sum(
        calculate_pnl(t, t.api_resolution) for t in unhedged_with_api
    )
    unhedged_without_api = [t for t in unhedged if not t.api_resolution]

    print(f"\nHEDGED TRADES (reliable):")
    print(f"  Count: {len(hedged)}")
    print(f"  PnL: ${hedged_pnl:.2f}")

    print(f"\nUNHEDGED TRADES:")
    print(f"  Total: {len(unhedged)}")
    print(f"  With API resolution: {len(unhedged_with_api)}")
    print(f"  Without API resolution: {len(unhedged_without_api)}")

    print(f"\n  PnL with ORDERBOOK resolution (original): ${unhedged_pnl_orderbook:.2f}")
    print(f"  PnL with API resolution (verified): ${unhedged_pnl_api:.2f}")

    # Accuracy comparison
    print("\n" + "=" * 80)
    print("RESOLUTION ACCURACY")
    print("=" * 80)

    verified = [t for t in unhedged if t.api_resolution]
    if verified:
        correct_guesses = sum(1 for t in verified if t.orderbook_resolution == t.api_resolution)
        accuracy = correct_guesses / len(verified) * 100
        print(f"\n  Verified unhedged trades: {len(verified)}")
        print(f"  Orderbook guess correct: {correct_guesses}")
        print(f"  Accuracy: {accuracy:.1f}%")

        # Break down by confidence
        clear = [t for t in verified if t.resolution_confidence == 'clear']
        unclear = [t for t in verified if t.resolution_confidence == 'unclear']

        if clear:
            clear_correct = sum(1 for t in clear if t.orderbook_resolution == t.api_resolution)
            print(f"\n  Clear confidence: {len(clear)} trades, {clear_correct} correct ({clear_correct/len(clear)*100:.1f}%)")

        if unclear:
            unclear_correct = sum(1 for t in unclear if t.orderbook_resolution == t.api_resolution)
            print(f"  Unclear confidence: {len(unclear)} trades, {unclear_correct} correct ({unclear_correct/len(unclear)*100:.1f}%)")

    # Calculate direction accuracy
    print("\n" + "=" * 80)
    print("VELOCITY SIGNAL ACCURACY")
    print("=" * 80)

    trades_with_resolution = [t for t in trades if t.api_resolution]
    if trades_with_resolution:
        signal_correct = sum(1 for t in trades_with_resolution if t.winner_side == t.api_resolution)
        signal_accuracy = signal_correct / len(trades_with_resolution) * 100
        print(f"\n  Trades with verified resolution: {len(trades_with_resolution)}")
        print(f"  Velocity signal correct: {signal_correct}")
        print(f"  Signal accuracy: {signal_accuracy:.1f}%")

    # Final totals
    total_hours = len(all_markets) * 15 / 60

    print("\n" + "=" * 80)
    print("FINAL PNL SUMMARY")
    print("=" * 80)

    # Conservative: hedged only
    print(f"\n  CONSERVATIVE (hedged only):")
    print(f"    PnL: ${hedged_pnl:.2f}")
    print(f"    Hourly: ${hedged_pnl/total_hours:.2f}/hr")

    # With verified unhedged
    verified_total = hedged_pnl + unhedged_pnl_api
    print(f"\n  WITH VERIFIED UNHEDGED:")
    print(f"    PnL: ${verified_total:.2f}")
    print(f"    Hourly: ${verified_total/total_hours:.2f}/hr")

    # Original (for comparison)
    original_total = hedged_pnl + unhedged_pnl_orderbook
    print(f"\n  ORIGINAL (orderbook resolution):")
    print(f"    PnL: ${original_total:.2f}")
    print(f"    Hourly: ${original_total/total_hours:.2f}/hr")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
