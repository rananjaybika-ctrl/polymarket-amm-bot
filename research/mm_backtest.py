#!/usr/bin/env python3
"""
Opportunistic Market Making Strategy Backtest

Two-sided passive quoting with dynamic offsets based on inventory imbalance.
Captures spread while staying balanced.

Key Formulas:
    imbalance = (up_shares - down_shares) / (up_shares + down_shares)
    adjustment = imbalance * 0.02
    up_offset = base_offset + adjustment   (wider if excess UP)
    down_offset = base_offset - adjustment (tighter if deficit UP)

Target: $10-15/hr

Usage:
    python research/mm_backtest.py
    python research/mm_backtest.py --rebalance-threshold 0.30
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timezone
from collections import defaultdict
import argparse


# =============================================================================
# CONFIGURATION
# =============================================================================

STARTING_BALANCE = 170.0
TARGET_SHARES = 15
MAX_POSITION_PER_SIDE = 200
MIN_TIME = 60  # Entry cutoff

# Polymarket order restrictions
MIN_ORDER_QTY = 5      # Minimum 5 shares per order
MIN_ORDER_VALUE = 1.0  # Minimum $1 per order

# Market filtering
MIN_RUNTIME_SECS = 300
REQUIRE_STANDARD_START = True

# MM parameters
BASE_OFFSET = 0.01  # Base offset from best_bid
MAX_OFFSET = 0.05   # Maximum offset
INVENTORY_ADJUSTMENT_FACTOR = 0.02  # Max 2 cents adjustment

# Rebalancing
DEFAULT_REBALANCE_THRESHOLD = 0.30  # 30% imbalance triggers rebalance

# Velocity zone offsets (from grid_mm_passive)
VELOCITY_ZONES = {
    'neutral':     {'vel_min': 0.00, 'vel_max': 0.10, 'base_offset': 0.01},
    'moderate':    {'vel_min': 0.10, 'vel_max': 0.30, 'base_offset': 0.01},
    'strong':      {'vel_min': 0.30, 'vel_max': 0.50, 'base_offset': 0.02},
    'very_strong': {'vel_min': 0.50, 'vel_max': 99.0, 'base_offset': 0.03},
}

# Cycling
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
class MMPosition:
    """Track market maker position."""
    up_shares: int = 0
    down_shares: int = 0
    up_cost: float = 0.0
    down_cost: float = 0.0

    @property
    def imbalance(self) -> float:
        """Signed imbalance: positive = UP heavy, negative = DOWN heavy."""
        total = self.up_shares + self.down_shares
        if total == 0:
            return 0.0
        return (self.up_shares - self.down_shares) / total

    @property
    def abs_imbalance(self) -> float:
        return abs(self.imbalance)

    @property
    def pairs(self) -> int:
        return min(self.up_shares, self.down_shares)

    @property
    def up_avg_price(self) -> float:
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0.0

    @property
    def down_avg_price(self) -> float:
        return self.down_cost / self.down_shares if self.down_shares > 0 else 0.0

    def add_fill(self, side: str, price: float, size: int):
        if side.upper() == "UP":
            self.up_cost += price * size
            self.up_shares += size
        else:
            self.down_cost += price * size
            self.down_shares += size

    def remove_fill(self, side: str, size: int):
        """Remove shares (for rebalancing)."""
        if side.upper() == "UP":
            if size >= self.up_shares:
                self.up_cost = 0.0
                self.up_shares = 0
            else:
                avg = self.up_avg_price
                self.up_shares -= size
                self.up_cost = avg * self.up_shares
        else:
            if size >= self.down_shares:
                self.down_cost = 0.0
                self.down_shares = 0
            else:
                avg = self.down_avg_price
                self.down_shares -= size
                self.down_cost = avg * self.down_shares

    def reset(self):
        self.up_shares = 0
        self.down_shares = 0
        self.up_cost = 0.0
        self.down_cost = 0.0


@dataclass
class MMTradeResult:
    """Result of a market making fill."""
    strategy: str = "opportunistic_mm"
    market_slug: str = ""
    time_remaining: float = 0.0
    side: str = ""
    fill_price: float = 0.0
    fill_size: int = 0
    is_rebalance: bool = False
    imbalance_before: float = 0.0
    imbalance_after: float = 0.0
    velocity_zone: str = ""
    offset_used: float = 0.0


@dataclass
class MMPairResult:
    """Result of a completed pair."""
    market_slug: str = ""
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0
    pair_cost: float = 0.0
    pnl: float = 0.0
    pairs: int = 0


@dataclass
class MMMarketResult:
    """Result from one market."""
    slug: str = ""
    total_fills: int = 0
    rebalance_fills: int = 0
    pairs_completed: int = 0
    total_pnl: float = 0.0
    max_imbalance: float = 0.0
    avg_pair_cost: float = 0.0


# =============================================================================
# RESOLUTION CACHE
# =============================================================================

_RESOLUTION_CACHE: Dict[str, str] = {}
_RESOLUTION_STATS: Dict[str, int] = {"known": 0, "guessed": 0, "skipped": 0}

# Set to False to allow guessed resolutions (with warning)
# WARNING: Guessed resolutions are based on prices ~60s before market end
REQUIRE_KNOWN_RESOLUTION = False


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
            print(f"  Loaded {len(_RESOLUTION_CACHE)} VERIFIED resolutions")
        except Exception as e:
            print(f"  Warning: Could not load resolutions: {e}")


def get_resolution(slug: str, mdf: pd.DataFrame = None) -> Optional[str]:
    """
    Get market resolution.

    Returns None if unknown and REQUIRE_KNOWN_RESOLUTION is True.
    Otherwise guesses from final prices (UNRELIABLE).
    """
    global _RESOLUTION_STATS

    if slug in _RESOLUTION_CACHE:
        _RESOLUTION_STATS["known"] += 1
        return _RESOLUTION_CACHE[slug]

    if REQUIRE_KNOWN_RESOLUTION:
        _RESOLUTION_STATS["skipped"] += 1
        return None

    # Guess from final prices (UNRELIABLE!)
    if mdf is not None and len(mdf) > 0:
        final = mdf.iloc[-1]
        _RESOLUTION_STATS["guessed"] += 1
        if final['up_bid'] >= 0.90:
            return 'UP'
        elif final['down_bid'] >= 0.90:
            return 'DOWN'
        else:
            return 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    _RESOLUTION_STATS["skipped"] += 1
    return None


def print_resolution_stats():
    """Print resolution statistics with warnings."""
    total = _RESOLUTION_STATS['known'] + _RESOLUTION_STATS['guessed']
    guessed_pct = _RESOLUTION_STATS['guessed'] / total * 100 if total > 0 else 0

    print(f"\n  Resolution stats:")
    print(f"    Known (verified): {_RESOLUTION_STATS['known']}")
    print(f"    Guessed (unreliable): {_RESOLUTION_STATS['guessed']}")
    print(f"    Skipped (no resolution): {_RESOLUTION_STATS['skipped']}")

    if guessed_pct > 50:
        print(f"\n  ⚠️  WARNING: {guessed_pct:.0f}% of resolutions are GUESSED!")
        print(f"      PnL calculations may be UNRELIABLE.")


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
# OFFSET CALCULATION
# =============================================================================

def get_velocity_zone(velocity_bps: float) -> str:
    """Get velocity zone name."""
    abs_vel = abs(velocity_bps)
    for zone_name, zone in VELOCITY_ZONES.items():
        if zone['vel_min'] <= abs_vel < zone['vel_max']:
            return zone_name
    return 'very_strong'


def get_base_offset(velocity_bps: float) -> float:
    """Get base offset for current velocity zone."""
    zone = get_velocity_zone(velocity_bps)
    return VELOCITY_ZONES[zone]['base_offset']


def calculate_dynamic_offsets(
    velocity_bps: float,
    imbalance: float,
) -> Tuple[float, float]:
    """
    Calculate dynamic offsets based on velocity and inventory imbalance.

    Args:
        velocity_bps: Current BTC velocity
        imbalance: Current position imbalance (-1 to 1)

    Returns:
        (up_offset, down_offset)
    """
    base = get_base_offset(velocity_bps)

    # Inventory adjustment
    # If UP heavy (imbalance > 0): widen UP offset, tighten DOWN offset
    # If DOWN heavy (imbalance < 0): widen DOWN offset, tighten UP offset
    adjustment = imbalance * INVENTORY_ADJUSTMENT_FACTOR

    up_offset = base + adjustment
    down_offset = base - adjustment

    # Clamp offsets
    up_offset = max(0.005, min(MAX_OFFSET, up_offset))
    down_offset = max(0.005, min(MAX_OFFSET, down_offset))

    return up_offset, down_offset


# =============================================================================
# MM SIMULATION
# =============================================================================

def simulate_mm_market(
    mdf: pd.DataFrame,
    slug: str,
    rebalance_threshold: float,
) -> Optional[MMMarketResult]:
    """
    Simulate opportunistic market making strategy with CORRECT resolution-based PnL.

    CRITICAL FIX: Pairs are now held to resolution and PnL is calculated correctly:
    - Completed pairs (UP + DOWN) = $1.00 at resolution (regardless of winner)
    - Unmatched shares = $1.00 if correct side, $0.00 if wrong side

    Previous bug: Counted pair profit immediately as (1 - pair_cost) without
    actually holding to resolution. This created fake profits.
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    # Get resolution (verified or guessed)
    resolution = get_resolution(slug, mdf)
    if resolution is None:
        return None  # Skip markets without resolution

    position = MMPosition()
    fills = []
    max_imbalance = 0.0

    # Simulated resting bids
    up_posted_bid = 0.0
    down_posted_bid = 0.0

    # Track previous prices for fill detection
    prev_up_bid = None
    prev_down_bid = None

    for i in range(len(mdf)):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        velocity_bps = row.get('velocity_bps', 0)

        if time_rem < MIN_TIME:
            continue

        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']

        # Track max imbalance
        if position.abs_imbalance > max_imbalance:
            max_imbalance = position.abs_imbalance

        # Calculate dynamic offsets
        up_offset, down_offset = calculate_dynamic_offsets(velocity_bps, position.imbalance)
        zone = get_velocity_zone(velocity_bps)

        # Skip posting on excess side if spike in that direction
        spike_up = row.get('spike_direction') == 'UP' if 'spike_direction' in row else False
        spike_down = row.get('spike_direction') == 'DOWN' if 'spike_direction' in row else False

        # Post bids if not at max position
        if position.up_shares < MAX_POSITION_PER_SIDE:
            if not (spike_up and position.imbalance > 0):
                up_posted_bid = up_bid - up_offset
                up_posted_bid = max(0.01, min(0.95, up_posted_bid))

        if position.down_shares < MAX_POSITION_PER_SIDE:
            if not (spike_down and position.imbalance < 0):
                down_posted_bid = down_bid - down_offset
                down_posted_bid = max(0.01, min(0.95, down_posted_bid))

        # Check fills
        up_filled = False
        if up_posted_bid > 0:
            if up_bid <= up_posted_bid or up_ask <= up_posted_bid:
                up_filled = True
            elif prev_up_bid is not None and prev_up_bid - up_bid >= 0.01:
                if up_bid <= up_posted_bid + 0.005:
                    up_filled = True

        if up_filled:
            # Validate order meets Polymarket restrictions
            order_size = get_valid_order_size(up_posted_bid, TARGET_SHARES)
            if validate_order(order_size, up_posted_bid):
                imbalance_before = position.imbalance
                position.add_fill("UP", up_posted_bid, order_size)
                fills.append(MMTradeResult(
                    market_slug=slug,
                    time_remaining=time_rem,
                    side="UP",
                    fill_price=up_posted_bid,
                    fill_size=order_size,
                    is_rebalance=False,
                    imbalance_before=imbalance_before,
                    imbalance_after=position.imbalance,
                    velocity_zone=zone,
                    offset_used=up_offset,
                ))
            up_posted_bid = 0.0

        down_filled = False
        if down_posted_bid > 0:
            if down_bid <= down_posted_bid or down_ask <= down_posted_bid:
                down_filled = True
            elif prev_down_bid is not None and prev_down_bid - down_bid >= 0.01:
                if down_bid <= down_posted_bid + 0.005:
                    down_filled = True

        if down_filled:
            # Validate order meets Polymarket restrictions
            order_size = get_valid_order_size(down_posted_bid, TARGET_SHARES)
            if validate_order(order_size, down_posted_bid):
                imbalance_before = position.imbalance
                position.add_fill("DOWN", down_posted_bid, order_size)
                fills.append(MMTradeResult(
                    market_slug=slug,
                    time_remaining=time_rem,
                    side="DOWN",
                    fill_price=down_posted_bid,
                    fill_size=order_size,
                    is_rebalance=False,
                    imbalance_before=imbalance_before,
                    imbalance_after=position.imbalance,
                    velocity_zone=zone,
                    offset_used=down_offset,
                ))
            down_posted_bid = 0.0

        prev_up_bid = up_bid
        prev_down_bid = down_bid

        # Rebalancing (sell excess side)
        total_position = position.up_shares + position.down_shares
        abs_diff = abs(position.up_shares - position.down_shares)

        should_rebalance = (
            abs_diff > 60 or
            (position.abs_imbalance > rebalance_threshold and total_position >= 30)
        )

        if should_rebalance:
            if position.imbalance > 0:
                rebalance_side = "UP"
                rebalance_price = up_bid
            else:
                rebalance_side = "DOWN"
                rebalance_price = down_bid

            rebalance_size = min(TARGET_SHARES, abs_diff // 2)
            # Validate rebalance order meets Polymarket restrictions
            if rebalance_size >= MIN_ORDER_QTY and validate_order(rebalance_size, rebalance_price):
                imbalance_before = position.imbalance
                position.remove_fill(rebalance_side, rebalance_size)
                fills.append(MMTradeResult(
                    market_slug=slug,
                    time_remaining=time_rem,
                    side=rebalance_side,
                    fill_price=rebalance_price,
                    fill_size=-rebalance_size,
                    is_rebalance=True,
                    imbalance_before=imbalance_before,
                    imbalance_after=position.imbalance,
                    velocity_zone=zone,
                    offset_used=0.0,
                ))

    if not fills:
        return None

    # =========================================================================
    # CRITICAL: Calculate PnL at RESOLUTION (not during market)
    # =========================================================================
    #
    # At resolution:
    # - Each completed PAIR (1 UP + 1 DOWN) pays out exactly $1.00
    # - Unmatched UP shares: worth $1.00 if resolution=UP, else $0.00
    # - Unmatched DOWN shares: worth $1.00 if resolution=DOWN, else $0.00
    #
    # This is the CORRECT calculation, not the fake (1 - pair_cost) approach.

    pairs_held = min(position.up_shares, position.down_shares)
    unmatched_up = position.up_shares - pairs_held
    unmatched_down = position.down_shares - pairs_held

    # Cost basis
    total_up_cost = position.up_cost
    total_down_cost = position.down_cost
    total_cost = total_up_cost + total_down_cost

    # Resolution payouts
    pair_payout = pairs_held * 1.0  # Each pair pays $1
    unmatched_up_payout = unmatched_up * (1.0 if resolution == "UP" else 0.0)
    unmatched_down_payout = unmatched_down * (1.0 if resolution == "DOWN" else 0.0)
    total_payout = pair_payout + unmatched_up_payout + unmatched_down_payout

    # PnL = payout - cost
    total_pnl = total_payout - total_cost

    # Calculate average pair cost for pairs actually held
    if pairs_held > 0:
        avg_pair_cost = (position.up_avg_price + position.down_avg_price)
    else:
        avg_pair_cost = 0.0

    rebalance_count = len([f for f in fills if f.is_rebalance])

    return MMMarketResult(
        slug=slug,
        total_fills=len(fills),
        rebalance_fills=rebalance_count,
        pairs_completed=pairs_held,
        total_pnl=total_pnl,
        max_imbalance=max_imbalance,
        avg_pair_cost=avg_pair_cost,
    )


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


# =============================================================================
# ANALYSIS
# =============================================================================

def analyze_results(results: List[MMMarketResult], total_hours: float) -> Dict:
    """Analyze MM results."""
    if not results:
        return {"error": "No results"}

    total_pnl = sum(r.total_pnl for r in results)
    total_pairs = sum(r.pairs_completed for r in results)
    total_fills = sum(r.total_fills for r in results)
    total_rebalances = sum(r.rebalance_fills for r in results)

    hourly_rate = total_pnl / total_hours if total_hours > 0 else 0
    pairs_per_hour = total_pairs / total_hours if total_hours > 0 else 0

    avg_pair_costs = [r.avg_pair_cost for r in results if r.avg_pair_cost > 0]
    avg_pair_cost = np.mean(avg_pair_costs) if avg_pair_costs else 0

    max_imbalances = [r.max_imbalance for r in results]
    avg_max_imbalance = np.mean(max_imbalances) if max_imbalances else 0

    return {
        "total_markets": len(results),
        "total_pairs": total_pairs,
        "total_fills": total_fills,
        "total_rebalances": total_rebalances,
        "total_pnl": total_pnl,
        "hourly_rate": hourly_rate,
        "pairs_per_hour": pairs_per_hour,
        "avg_pair_cost": avg_pair_cost,
        "avg_max_imbalance": avg_max_imbalance,
        "rebalance_ratio": total_rebalances / total_fills if total_fills > 0 else 0,
    }


# =============================================================================
# MAIN REPORT
# =============================================================================

def print_report(all_markets: Dict, results: Dict, rebalance_threshold: float):
    """Print comprehensive report."""
    total_hours = len(all_markets) * 15 / 60

    print("\n" + "=" * 80)
    print("OPPORTUNISTIC MARKET MAKING BACKTEST RESULTS")
    print("=" * 80)

    print(f"\nMarkets: {len(all_markets)}")
    print(f"Total hours: {total_hours:.1f}")
    print(f"Rebalance threshold: {rebalance_threshold:.0%}")
    print(f"Max position per side: {MAX_POSITION_PER_SIDE}")

    if "error" in results:
        print(f"\nNo results found!")
        return

    print(f"\n{'=' * 40}")
    print("PERFORMANCE SUMMARY")
    print(f"{'=' * 40}")
    print(f"Total pairs completed: {results['total_pairs']}")
    print(f"Pairs per hour: {results['pairs_per_hour']:.1f}")
    print(f"Total PnL: ${results['total_pnl']:.2f}")
    print(f"Hourly rate: ${results['hourly_rate']:.2f}/hr")
    print(f"Avg pair cost: ${results['avg_pair_cost']:.4f}")

    print(f"\n{'=' * 40}")
    print("INVENTORY MANAGEMENT")
    print(f"{'=' * 40}")
    print(f"Total fills: {results['total_fills']}")
    print(f"Rebalance trades: {results['total_rebalances']}")
    print(f"Rebalance ratio: {results['rebalance_ratio']:.1%}")
    print(f"Avg max imbalance: {results['avg_max_imbalance']:.1%}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Opportunistic MM Backtest")
    parser.add_argument('--rebalance-threshold', type=float, default=DEFAULT_REBALANCE_THRESHOLD,
                        help='Imbalance threshold to trigger rebalancing (default: 0.30)')
    args = parser.parse_args()

    print("=" * 80)
    print("OPPORTUNISTIC MARKET MAKING BACKTEST")
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
    print(f"\nRunning MM simulation (rebalance_threshold={args.rebalance_threshold:.0%})...")
    all_results = []
    for slug, mdf in all_markets.items():
        result = simulate_mm_market(mdf, slug, args.rebalance_threshold)
        if result:
            all_results.append(result)

    results = analyze_results(all_results, total_hours)

    # Print resolution stats
    print_resolution_stats()

    # Print report
    print_report(all_markets, results, args.rebalance_threshold)


if __name__ == "__main__":
    main()
