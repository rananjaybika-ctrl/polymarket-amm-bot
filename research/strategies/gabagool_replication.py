#!/usr/bin/env python3
"""
Gabagool Replication Strategy - Pure Pair Arbitrage

Based on deep analysis findings:
- Target ~5,000 shares per side (10,000 total per market)
- Use fixed sizes: 24 (primary), 5, 10, 20
- Execute both sides within 10 seconds
- Target pair cost < $1.00 for guaranteed profit
- Maintain strict 50/50 balance (no directional bias)
- Start early (>600s remaining)

This is a BACKTEST implementation to validate the strategy.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")


@dataclass
class GabagoolConfig:
    """Configuration matching observed Gabagool behavior."""

    # Position targets
    target_shares_per_side: float = 5000.0
    max_total_shares: float = 12000.0

    # Trade sizing (observed distribution)
    primary_size: float = 24.0  # 35% of trades
    secondary_sizes: List[float] = field(default_factory=lambda: [5.0, 10.0, 20.0, 15.0])

    # Timing
    min_time_remaining: float = 60.0  # Don't trade in last 60s
    start_time_remaining: float = 850.0  # Start early
    max_gap_seconds: float = 10.0  # Both sides within 10s

    # Pair cost target
    target_pair_cost: float = 0.998  # Under $1.00
    max_pair_cost: float = 1.02  # Stop if too expensive

    # Balance tolerance
    balance_tolerance: float = 0.05  # 5% imbalance max

    # Entry conditions
    min_spread: float = 0.01  # Don't trade in zero spread
    max_ask_price: float = 0.70  # Don't buy expensive side > $0.70


@dataclass
class Position:
    """Track position for a single market."""
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    trades: List[dict] = field(default_factory=list)
    first_trade_time: Optional[float] = None
    last_trade_time: Optional[float] = None

    @property
    def total_shares(self) -> float:
        return self.up_shares + self.down_shares

    @property
    def total_cost(self) -> float:
        return self.up_cost + self.down_cost

    @property
    def net_position(self) -> float:
        return self.up_shares - self.down_shares

    @property
    def pair_cost(self) -> float:
        min_shares = min(self.up_shares, self.down_shares)
        if min_shares <= 0:
            return float('inf')
        return self.total_cost / min_shares

    @property
    def balance_ratio(self) -> float:
        if max(self.up_shares, self.down_shares) == 0:
            return 1.0
        return min(self.up_shares, self.down_shares) / max(self.up_shares, self.down_shares)

    def add_trade(self, side: str, price: float, size: float, timestamp_ms: int, time_remaining: float):
        if side.upper() == 'UP':
            self.up_shares += size
            self.up_cost += price * size
        else:
            self.down_shares += size
            self.down_cost += price * size

        self.trades.append({
            'side': side.upper(),
            'price': price,
            'size': size,
            'timestamp_ms': timestamp_ms,
            'time_remaining': time_remaining,
        })

        if self.first_trade_time is None:
            self.first_trade_time = time_remaining
        self.last_trade_time = time_remaining


class GabagoolBacktest:
    """Backtest the Gabagool replication strategy."""

    def __init__(self, config: GabagoolConfig = None):
        self.config = config or GabagoolConfig()
        self.positions: dict[str, Position] = {}
        self.results: List[dict] = []

    def should_trade(self, row: pd.Series, position: Position) -> Tuple[bool, str, float]:
        """
        Determine if we should trade and which side.

        Returns: (should_trade, side, size)
        """
        time_remaining = row.get('time_remaining_secs', row.get('time_remaining', 0))

        # Don't trade too late
        if time_remaining < self.config.min_time_remaining:
            return False, '', 0.0

        # Don't trade too early (wait for market to establish)
        if time_remaining > self.config.start_time_remaining:
            return False, '', 0.0

        # Check if we've hit targets
        if position.total_shares >= self.config.max_total_shares:
            return False, '', 0.0

        # Get prices
        up_ask = row.get('up_ask', 1.0)
        down_ask = row.get('down_ask', 1.0)
        up_bid = row.get('up_bid', 0.0)
        down_bid = row.get('down_bid', 0.0)

        # Check spreads
        up_spread = up_ask - up_bid
        down_spread = down_ask - down_bid

        if up_spread < self.config.min_spread and down_spread < self.config.min_spread:
            return False, '', 0.0

        # Check pair cost (prospective)
        prospective_pair_cost = up_ask + down_ask
        if prospective_pair_cost > self.config.max_pair_cost:
            return False, '', 0.0

        # Determine which side needs more shares
        up_needed = self.config.target_shares_per_side - position.up_shares
        down_needed = self.config.target_shares_per_side - position.down_shares

        # Prioritize the side that's behind (maintain balance)
        if up_needed <= 0 and down_needed <= 0:
            return False, '', 0.0

        # Choose side based on:
        # 1. Which side is more behind
        # 2. Which side has better price
        # 3. Maintain balance

        if position.total_shares == 0:
            # First trade - pick cheaper side
            if up_ask <= down_ask and up_ask <= self.config.max_ask_price:
                return True, 'UP', self.config.primary_size
            elif down_ask <= self.config.max_ask_price:
                return True, 'DOWN', self.config.primary_size
            else:
                return False, '', 0.0

        # Subsequent trades - maintain balance
        imbalance = position.net_position / position.total_shares if position.total_shares > 0 else 0

        if imbalance > self.config.balance_tolerance:
            # Too many UP shares, buy DOWN
            if down_ask <= self.config.max_ask_price and down_needed > 0:
                return True, 'DOWN', min(self.config.primary_size, down_needed)
        elif imbalance < -self.config.balance_tolerance:
            # Too many DOWN shares, buy UP
            if up_ask <= self.config.max_ask_price and up_needed > 0:
                return True, 'UP', min(self.config.primary_size, up_needed)
        else:
            # Balanced - buy whichever is cheaper
            if up_ask <= down_ask and up_needed > 0 and up_ask <= self.config.max_ask_price:
                return True, 'UP', min(self.config.primary_size, up_needed)
            elif down_needed > 0 and down_ask <= self.config.max_ask_price:
                return True, 'DOWN', min(self.config.primary_size, down_needed)

        return False, '', 0.0

    def run_market(self, market_df: pd.DataFrame, market_slug: str, winner: str) -> dict:
        """Run backtest on a single market."""

        position = Position()
        market_df = market_df.sort_values('timestamp_ms').reset_index(drop=True)

        for _, row in market_df.iterrows():
            should, side, size = self.should_trade(row, position)

            if should:
                price = row['up_ask'] if side == 'UP' else row['down_ask']
                time_remaining = row.get('time_remaining_secs', row.get('time_remaining', 0))

                position.add_trade(
                    side=side,
                    price=price,
                    size=size,
                    timestamp_ms=row['timestamp_ms'],
                    time_remaining=time_remaining,
                )

        # Calculate PnL
        if winner.upper() == 'UP':
            revenue = position.up_shares * 1.0 + position.down_shares * 0.0
        else:
            revenue = position.up_shares * 0.0 + position.down_shares * 1.0

        pnl = revenue - position.total_cost

        # Guaranteed profit from pairs
        paired_shares = min(position.up_shares, position.down_shares)
        guaranteed_pnl = paired_shares * (1.0 - position.pair_cost) if position.pair_cost < float('inf') else 0

        return {
            'market_slug': market_slug,
            'winner': winner,
            'up_shares': position.up_shares,
            'down_shares': position.down_shares,
            'up_cost': position.up_cost,
            'down_cost': position.down_cost,
            'total_cost': position.total_cost,
            'pair_cost': position.pair_cost,
            'net_position': position.net_position,
            'balance_ratio': position.balance_ratio,
            'num_trades': len(position.trades),
            'revenue': revenue,
            'pnl': pnl,
            'guaranteed_pnl': guaranteed_pnl,
            'first_trade_time': position.first_trade_time,
            'last_trade_time': position.last_trade_time,
        }

    def run_backtest(self, observer_df: pd.DataFrame, resolutions_df: pd.DataFrame) -> pd.DataFrame:
        """Run backtest on all markets."""

        results = []

        # Get unique markets
        markets = observer_df['market_slug'].unique()

        print(f"Running Gabagool replication backtest on {len(markets)} markets...")

        for market_slug in markets:
            # Get winner
            res_row = resolutions_df[resolutions_df['slug'] == market_slug]
            if len(res_row) == 0:
                continue

            winner = res_row.iloc[0]['winner']
            if pd.isna(winner):
                continue

            # Get market data
            market_df = observer_df[observer_df['market_slug'] == market_slug]

            # Run backtest
            result = self.run_market(market_df, market_slug, winner)
            results.append(result)

        self.results = results
        return pd.DataFrame(results)


def load_observer_data(dataset: str = 'oos9') -> pd.DataFrame:
    """Load observer data for backtesting."""

    if dataset == 'oos9':
        path = BASE_DIR / "research/observer/grid_obs_oos9.csv"
    elif dataset == 'oos6':
        path = BASE_DIR / "research/observer/grid_obs_20260129.csv"
    else:
        raise ValueError(f"Unknown dataset: {dataset}")

    if not path.exists():
        # Try combined
        path = BASE_DIR / "research/observer/grid_obs_oos9_combined.csv"

    print(f"Loading observer data from {path}...")
    df = pd.read_csv(path, low_memory=False)
    print(f"  Loaded {len(df):,} rows")

    return df


def load_resolutions() -> pd.DataFrame:
    """Load market resolutions."""
    path = BASE_DIR / "research/observer/market_resolutions_verified.csv"
    return pd.read_csv(path)


def main():
    print("=" * 70)
    print("GABAGOOL REPLICATION BACKTEST")
    print("=" * 70)

    # Load data
    observer_df = load_observer_data('oos9')
    resolutions_df = load_resolutions()

    # Initialize backtest
    config = GabagoolConfig()
    backtest = GabagoolBacktest(config)

    # Run backtest
    results_df = backtest.run_backtest(observer_df, resolutions_df)

    # Print summary
    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)

    print(f"\nMarkets traded: {len(results_df)}")
    print(f"Total trades: {results_df['num_trades'].sum():,}")

    # Filter to markets with trades
    traded = results_df[results_df['num_trades'] > 0]
    print(f"Markets with trades: {len(traded)}")

    if len(traded) > 0:
        print(f"\n--- Position Stats ---")
        print(f"  Avg UP shares: {traded['up_shares'].mean():.1f}")
        print(f"  Avg DOWN shares: {traded['down_shares'].mean():.1f}")
        print(f"  Avg total shares: {(traded['up_shares'] + traded['down_shares']).mean():.1f}")
        print(f"  Avg balance ratio: {traded['balance_ratio'].mean():.3f}")

        print(f"\n--- Pair Cost Stats ---")
        valid_pc = traded[traded['pair_cost'] < 10]
        print(f"  Mean pair cost: ${valid_pc['pair_cost'].mean():.4f}")
        print(f"  Median pair cost: ${valid_pc['pair_cost'].median():.4f}")
        print(f"  Markets with pair cost < $1.00: {(valid_pc['pair_cost'] < 1.0).sum()}")

        print(f"\n--- PnL Stats ---")
        print(f"  Total PnL: ${traded['pnl'].sum():.2f}")
        print(f"  Mean PnL/market: ${traded['pnl'].mean():.2f}")
        print(f"  Median PnL/market: ${traded['pnl'].median():.2f}")
        print(f"  Win rate: {(traded['pnl'] > 0).mean() * 100:.1f}%")

        print(f"\n--- Guaranteed PnL (from pair cost < $1) ---")
        print(f"  Total guaranteed: ${traded['guaranteed_pnl'].sum():.2f}")
        print(f"  Mean guaranteed/market: ${traded['guaranteed_pnl'].mean():.2f}")

    # Save results
    output_path = BASE_DIR / "research/findings/data/gabagool_replication_backtest.csv"
    results_df.to_csv(output_path, index=False)
    print(f"\nResults saved to {output_path}")

    return results_df


if __name__ == "__main__":
    results = main()
