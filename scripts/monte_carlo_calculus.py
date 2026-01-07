#!/usr/bin/env python3
"""
Monte Carlo Simulation for Calculus MAKER Strategy

Uses historical trade data and orderbook snapshots to simulate strategy performance
across many sessions with varying market conditions.

Outputs:
- P&L distribution (mean, std dev, win rate, max drawdown)
- Fill analysis (fill rates, chase costs, time to fill)
- Parameter sensitivity (optimal M_MIN, M_MAX, LAMBDA)

Usage:
    python scripts/monte_carlo_calculus.py --sessions 1000 --verbose
"""

import argparse
import csv
import json
import math
import random
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import numpy as np


# =============================================================================
# DATA EXTRACTION
# =============================================================================

@dataclass
class OrderbookSnapshot:
    """Single orderbook snapshot from logs."""
    timestamp: datetime
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    pair_cost: float
    time_remaining: float = 900.0  # Default 15 min


@dataclass
class TradeRecord:
    """Trade record from CSV."""
    timestamp: datetime
    market_slug: str
    side: str
    size: float
    price: float
    pair_cost: float
    locked_profit: float


def extract_orderbook_from_logs(log_dir: Path) -> List[OrderbookSnapshot]:
    """Extract orderbook snapshots from server logs."""
    snapshots = []
    pattern = re.compile(
        r"\[ACCUM\].*UP=\$?([\d.]+)\s*\(ask=\$?([\d.]+).*DOWN=\$?([\d.]+)\s*\(ask=\$?([\d.]+).*PairCost=\$?([\d.]+)"
    )

    for log_file in sorted(log_dir.glob("server_*.log")):
        try:
            with open(log_file, 'r', errors='ignore') as f:
                for line in f:
                    match = pattern.search(line)
                    if match:
                        up_bid = float(match.group(1))
                        up_ask = float(match.group(2))
                        down_bid = float(match.group(3))
                        down_ask = float(match.group(4))
                        pair_cost = float(match.group(5))

                        # Extract timestamp from log line
                        ts_match = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", line)
                        if ts_match:
                            try:
                                ts = datetime.fromisoformat(ts_match.group(1).replace(' ', 'T'))
                            except:
                                ts = datetime.now()
                        else:
                            ts = datetime.now()

                        snapshots.append(OrderbookSnapshot(
                            timestamp=ts,
                            up_bid=up_bid,
                            up_ask=up_ask,
                            down_bid=down_bid,
                            down_ask=down_ask,
                            pair_cost=pair_cost,
                        ))
        except Exception as e:
            print(f"Warning: Could not read {log_file}: {e}")

    return snapshots


def extract_trades_from_csv(csv_dir: Path) -> List[TradeRecord]:
    """Extract trade records from CSV files."""
    trades = []

    for csv_file in sorted(csv_dir.glob("paper_trades_calculus_maker_*.csv")):
        try:
            with open(csv_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        trades.append(TradeRecord(
                            timestamp=datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')),
                            market_slug=row['market_slug'],
                            side=row['trade_side'],
                            size=float(row['size_filled']),
                            price=float(row['price']),
                            pair_cost=float(row.get('pos_pair_cost', 0)),
                            locked_profit=float(row.get('pos_locked_profit', 0)),
                        ))
                    except (ValueError, KeyError) as e:
                        continue
        except Exception as e:
            print(f"Warning: Could not read {csv_file}: {e}")

    return trades


# =============================================================================
# STATISTICAL MODELS
# =============================================================================

@dataclass
class MarketModel:
    """Statistical model of market behavior derived from historical data."""

    # Pair cost distribution
    pair_cost_mean: float = 0.96
    pair_cost_std: float = 0.02
    pair_cost_min: float = 0.92
    pair_cost_max: float = 1.02

    # Spread distribution
    spread_mean: float = 0.04
    spread_std: float = 0.01

    # Fill probability (function of distance from ask)
    fill_base_rate: float = 0.7  # Base fill rate at ask price
    fill_decay_per_cent: float = 0.15  # Fill rate drops 15% per 1c below ask

    # Price movement (volatility)
    price_drift_per_min: float = 0.0  # No directional bias
    price_vol_per_min: float = 0.02  # ~2% volatility per minute

    # Chase cost (how much we pay when chasing)
    chase_cost_mean: float = 0.03
    chase_cost_std: float = 0.02

    @classmethod
    def from_historical_data(
        cls,
        snapshots: List[OrderbookSnapshot],
        trades: List[TradeRecord]
    ) -> "MarketModel":
        """Build model from historical data."""
        model = cls()

        if snapshots:
            pair_costs = [s.pair_cost for s in snapshots if 0.9 < s.pair_cost < 1.1]
            if pair_costs:
                model.pair_cost_mean = statistics.mean(pair_costs)
                model.pair_cost_std = statistics.stdev(pair_costs) if len(pair_costs) > 1 else 0.02
                model.pair_cost_min = min(pair_costs)
                model.pair_cost_max = max(pair_costs)

            spreads = [s.up_ask - s.up_bid for s in snapshots if s.up_ask > s.up_bid]
            if spreads:
                model.spread_mean = statistics.mean(spreads)
                model.spread_std = statistics.stdev(spreads) if len(spreads) > 1 else 0.01

        if trades:
            # Analyze chase costs from trade data
            # Group trades by market to find pairs
            by_market: Dict[str, List[TradeRecord]] = {}
            for t in trades:
                if t.market_slug not in by_market:
                    by_market[t.market_slug] = []
                by_market[t.market_slug].append(t)

            # Calculate realized pair costs
            realized_pair_costs = []
            for market, market_trades in by_market.items():
                up_trades = [t for t in market_trades if t.side == "UP"]
                down_trades = [t for t in market_trades if t.side == "DOWN"]
                if up_trades and down_trades:
                    avg_up = sum(t.price * t.size for t in up_trades) / sum(t.size for t in up_trades)
                    avg_down = sum(t.price * t.size for t in down_trades) / sum(t.size for t in down_trades)
                    realized_pair_costs.append(avg_up + avg_down)

            if realized_pair_costs:
                # Chase cost = realized - theoretical (pair_cost_mean)
                chase_costs = [pc - model.pair_cost_mean for pc in realized_pair_costs if pc > model.pair_cost_mean]
                if chase_costs:
                    model.chase_cost_mean = statistics.mean(chase_costs)
                    model.chase_cost_std = statistics.stdev(chase_costs) if len(chase_costs) > 1 else 0.02

        return model


# =============================================================================
# CALCULUS STRATEGY SIMULATOR
# =============================================================================

@dataclass
class StrategyParams:
    """Calculus strategy parameters."""
    m_min: float = 0.005
    m_max: float = 0.025
    lambda_decay: float = 0.004
    max_shares: int = 50
    min_shares: int = 5
    target_shares: int = 15
    max_pair_cost: float = 0.995

    # Gradual chase parameters
    gradual_chase_enabled: bool = True
    chase_wait_early: float = 120.0  # seconds before first chase (>10 min)
    chase_step_early: float = 0.02
    chase_wait_mid: float = 60.0
    chase_step_mid: float = 0.04
    chase_wait_late: float = 30.0
    chase_step_late: float = 0.06
    chase_wait_urgent: float = 15.0
    chase_step_urgent: float = 0.10


def get_mispricing_threshold(time_remaining: float, params: StrategyParams) -> float:
    """Calculate mispricing threshold at given time."""
    return params.m_min + (params.m_max - params.m_min) * math.exp(
        -params.lambda_decay * (900 - time_remaining)
    )


def get_dynamic_size(time_remaining: float, params: StrategyParams) -> int:
    """Calculate dynamic order size based on time."""
    urgency = (1 - time_remaining / 900) ** 2
    raw_size = params.min_shares + (params.max_shares - params.min_shares) * urgency
    return max(params.min_shares, 5 * round(raw_size / 5))


@dataclass
class SimulatedOrder:
    """Simulated order."""
    side: str
    price: float
    size: int
    placed_at: float  # time_remaining when placed
    filled: bool = False
    fill_price: float = 0.0
    chase_count: int = 0


@dataclass
class SimulationResult:
    """Result of a single market simulation."""
    up_shares: int = 0
    up_avg_price: float = 0.0
    down_shares: int = 0
    down_avg_price: float = 0.0
    pair_cost: float = 0.0
    locked_pnl: float = 0.0
    chase_cost: float = 0.0
    fill_rate: float = 0.0
    trades_attempted: int = 0
    trades_filled: int = 0
    final_imbalance: int = 0


def simulate_market(
    model: MarketModel,
    params: StrategyParams,
    market_duration: float = 900.0,
    time_step: float = 5.0,
    verbose: bool = False,
) -> SimulationResult:
    """
    Simulate a single 15-minute market with the calculus strategy.

    Returns SimulationResult with P&L and fill statistics.
    """
    result = SimulationResult()

    # Initialize market state
    # Start with random pair cost from distribution
    current_pair_cost = max(
        model.pair_cost_min,
        min(model.pair_cost_max, random.gauss(model.pair_cost_mean, model.pair_cost_std))
    )

    # Split into UP and DOWN prices (roughly equal)
    up_ask = 0.5 + random.uniform(-0.1, 0.1)
    down_ask = current_pair_cost - up_ask
    up_bid = up_ask - max(0.01, random.gauss(model.spread_mean, model.spread_std))
    down_bid = down_ask - max(0.01, random.gauss(model.spread_mean, model.spread_std))

    # Position tracking
    up_shares = 0
    up_total_cost = 0.0
    down_shares = 0
    down_total_cost = 0.0

    # Pending orders
    pending_up: Optional[SimulatedOrder] = None
    pending_down: Optional[SimulatedOrder] = None

    # Simulation loop
    time_remaining = market_duration

    while time_remaining > 0:
        # Update market prices (random walk)
        drift = model.price_drift_per_min * (time_step / 60)
        vol = model.price_vol_per_min * (time_step / 60) ** 0.5

        up_change = random.gauss(drift, vol)
        down_change = random.gauss(-drift, vol)  # Inverse correlation

        up_ask = max(0.05, min(0.95, up_ask + up_change))
        down_ask = max(0.05, min(0.95, down_ask + down_change))
        up_bid = max(0.01, up_ask - max(0.01, random.gauss(model.spread_mean, model.spread_std)))
        down_bid = max(0.01, down_ask - max(0.01, random.gauss(model.spread_mean, model.spread_std)))

        current_pair_cost = up_ask + down_ask

        # Check if pending orders filled
        for pending, side in [(pending_up, "UP"), (pending_down, "DOWN")]:
            if pending and not pending.filled:
                ask = up_ask if side == "UP" else down_ask

                # Fill probability based on distance from ask
                distance = ask - pending.price
                if distance <= 0:
                    # At or above ask - guaranteed fill
                    fill_prob = 1.0
                else:
                    # Below ask - probability decreases
                    fill_prob = model.fill_base_rate * math.exp(-model.fill_decay_per_cent * distance * 100)

                if random.random() < fill_prob:
                    pending.filled = True
                    pending.fill_price = pending.price

                    if side == "UP":
                        up_shares += pending.size
                        up_total_cost += pending.size * pending.fill_price
                        result.trades_filled += 1
                    else:
                        down_shares += pending.size
                        down_total_cost += pending.size * pending.fill_price
                        result.trades_filled += 1

        # Clear filled orders
        if pending_up and pending_up.filled:
            pending_up = None
        if pending_down and pending_down.filled:
            pending_down = None

        # Gradual chase for pending orders
        if params.gradual_chase_enabled:
            for pending, side in [(pending_up, "UP"), (pending_down, "DOWN")]:
                if pending and not pending.filled:
                    order_age = pending.placed_at - time_remaining
                    ask = up_ask if side == "UP" else down_ask

                    # Determine chase parameters based on time
                    if time_remaining >= 600:
                        wait_time, step_size = params.chase_wait_early, params.chase_step_early
                    elif time_remaining >= 300:
                        wait_time, step_size = params.chase_wait_mid, params.chase_step_mid
                    elif time_remaining >= 120:
                        wait_time, step_size = params.chase_wait_late, params.chase_step_late
                    else:
                        wait_time, step_size = params.chase_wait_urgent, params.chase_step_urgent

                    if order_age >= wait_time:
                        new_price = min(pending.price + step_size, ask, 0.98)
                        if new_price > pending.price:
                            pending.price = new_price
                            pending.chase_count += 1
                            result.chase_cost += step_size * pending.size

        # Check if we should place new orders
        threshold = get_mispricing_threshold(time_remaining, params)
        mispricing = 1.0 - current_pair_cost

        if mispricing >= threshold and current_pair_cost <= params.max_pair_cost:
            size = get_dynamic_size(time_remaining, params)
            size = min(size, params.target_shares - max(up_shares, down_shares))

            if size >= params.min_shares:
                # Place orders on sides that need shares
                if up_shares < params.target_shares and pending_up is None:
                    patient_price = up_bid - threshold
                    pending_up = SimulatedOrder(
                        side="UP",
                        price=max(0.01, patient_price),
                        size=min(size, params.target_shares - up_shares),
                        placed_at=time_remaining,
                    )
                    result.trades_attempted += 1

                if down_shares < params.target_shares and pending_down is None:
                    patient_price = down_bid - threshold
                    pending_down = SimulatedOrder(
                        side="DOWN",
                        price=max(0.01, patient_price),
                        size=min(size, params.target_shares - down_shares),
                        placed_at=time_remaining,
                    )
                    result.trades_attempted += 1

        time_remaining -= time_step

    # Calculate final results
    result.up_shares = up_shares
    result.down_shares = down_shares
    result.up_avg_price = up_total_cost / up_shares if up_shares > 0 else 0
    result.down_avg_price = down_total_cost / down_shares if down_shares > 0 else 0

    pairs = min(up_shares, down_shares)
    if pairs > 0:
        result.pair_cost = result.up_avg_price + result.down_avg_price
        result.locked_pnl = pairs * (1.0 - result.pair_cost)

    result.final_imbalance = abs(up_shares - down_shares)
    result.fill_rate = result.trades_filled / result.trades_attempted if result.trades_attempted > 0 else 0

    return result


# =============================================================================
# MONTE CARLO RUNNER
# =============================================================================

@dataclass
class MonteCarloResults:
    """Aggregated Monte Carlo results."""
    sessions: int = 0

    # P&L metrics
    mean_pnl: float = 0.0
    std_pnl: float = 0.0
    min_pnl: float = 0.0
    max_pnl: float = 0.0
    win_rate: float = 0.0

    # Fill metrics
    mean_fill_rate: float = 0.0
    mean_chase_cost: float = 0.0
    mean_imbalance: float = 0.0

    # Position metrics
    mean_pairs: float = 0.0
    mean_pair_cost: float = 0.0

    # Distribution
    pnl_percentiles: Dict[int, float] = field(default_factory=dict)

    # Raw results for analysis
    all_results: List[SimulationResult] = field(default_factory=list)


def run_monte_carlo(
    model: MarketModel,
    params: StrategyParams,
    num_sessions: int = 1000,
    verbose: bool = False,
) -> MonteCarloResults:
    """Run Monte Carlo simulation across many sessions."""
    results = MonteCarloResults(sessions=num_sessions)

    pnls = []
    fill_rates = []
    chase_costs = []
    imbalances = []
    pairs_list = []
    pair_costs = []

    for i in range(num_sessions):
        if verbose and (i + 1) % 100 == 0:
            print(f"  Simulating session {i + 1}/{num_sessions}...")

        sim = simulate_market(model, params, verbose=verbose)
        results.all_results.append(sim)

        pnls.append(sim.locked_pnl)
        fill_rates.append(sim.fill_rate)
        chase_costs.append(sim.chase_cost)
        imbalances.append(sim.final_imbalance)
        pairs_list.append(min(sim.up_shares, sim.down_shares))
        if sim.pair_cost > 0:
            pair_costs.append(sim.pair_cost)

    # Calculate statistics
    results.mean_pnl = statistics.mean(pnls)
    results.std_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 0
    results.min_pnl = min(pnls)
    results.max_pnl = max(pnls)
    results.win_rate = sum(1 for p in pnls if p > 0) / len(pnls)

    results.mean_fill_rate = statistics.mean(fill_rates)
    results.mean_chase_cost = statistics.mean(chase_costs)
    results.mean_imbalance = statistics.mean(imbalances)

    results.mean_pairs = statistics.mean(pairs_list)
    results.mean_pair_cost = statistics.mean(pair_costs) if pair_costs else 0

    # Percentiles
    sorted_pnls = sorted(pnls)
    for p in [5, 10, 25, 50, 75, 90, 95]:
        idx = int(len(sorted_pnls) * p / 100)
        results.pnl_percentiles[p] = sorted_pnls[min(idx, len(sorted_pnls) - 1)]

    return results


def run_parameter_sensitivity(
    model: MarketModel,
    base_params: StrategyParams,
    num_sessions_per_config: int = 200,
    verbose: bool = False,
) -> Dict[str, List[Tuple[float, MonteCarloResults]]]:
    """Test different parameter values."""
    sensitivity = {}

    # Test M_MIN variations
    print("\nTesting M_MIN sensitivity...")
    m_min_results = []
    for m_min in [0.003, 0.005, 0.007, 0.010, 0.015]:
        params = StrategyParams(**{**base_params.__dict__, "m_min": m_min})
        results = run_monte_carlo(model, params, num_sessions_per_config, verbose=False)
        m_min_results.append((m_min, results))
        print(f"  M_MIN={m_min:.3f}: mean_pnl=${results.mean_pnl:.3f}, win_rate={results.win_rate:.1%}")
    sensitivity["m_min"] = m_min_results

    # Test M_MAX variations
    print("\nTesting M_MAX sensitivity...")
    m_max_results = []
    for m_max in [0.015, 0.020, 0.025, 0.030, 0.040]:
        params = StrategyParams(**{**base_params.__dict__, "m_max": m_max})
        results = run_monte_carlo(model, params, num_sessions_per_config, verbose=False)
        m_max_results.append((m_max, results))
        print(f"  M_MAX={m_max:.3f}: mean_pnl=${results.mean_pnl:.3f}, win_rate={results.win_rate:.1%}")
    sensitivity["m_max"] = m_max_results

    # Test LAMBDA variations
    print("\nTesting LAMBDA sensitivity...")
    lambda_results = []
    for lam in [0.002, 0.004, 0.006, 0.008, 0.010]:
        params = StrategyParams(**{**base_params.__dict__, "lambda_decay": lam})
        results = run_monte_carlo(model, params, num_sessions_per_config, verbose=False)
        lambda_results.append((lam, results))
        print(f"  LAMBDA={lam:.3f}: mean_pnl=${results.mean_pnl:.3f}, win_rate={results.win_rate:.1%}")
    sensitivity["lambda"] = lambda_results

    # Test gradual chase ON vs OFF
    print("\nTesting gradual chase...")
    chase_results = []
    for enabled in [True, False]:
        params = StrategyParams(**{**base_params.__dict__, "gradual_chase_enabled": enabled})
        results = run_monte_carlo(model, params, num_sessions_per_config, verbose=False)
        chase_results.append((enabled, results))
        label = "ON" if enabled else "OFF"
        print(f"  Gradual Chase {label}: mean_pnl=${results.mean_pnl:.3f}, win_rate={results.win_rate:.1%}, chase_cost=${results.mean_chase_cost:.3f}")
    sensitivity["gradual_chase"] = chase_results

    return sensitivity


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Monte Carlo simulation for Calculus strategy")
    parser.add_argument("--sessions", type=int, default=1000, help="Number of sessions to simulate")
    parser.add_argument("--sensitivity", action="store_true", help="Run parameter sensitivity analysis")
    parser.add_argument("--sensitivity-sessions", type=int, default=200, help="Sessions per config in sensitivity")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--no-historical", action="store_true", help="Skip historical data extraction")
    args = parser.parse_args()

    print("=" * 60)
    print("MONTE CARLO SIMULATION - CALCULUS MAKER STRATEGY")
    print("=" * 60)

    # Find data directories
    script_dir = Path(__file__).parent
    project_dir = script_dir.parent
    log_dir = project_dir / "logs"
    web_dir = project_dir / "web"

    # Extract historical data
    model = MarketModel()

    if not args.no_historical:
        print("\n1. Extracting historical data...")

        snapshots = extract_orderbook_from_logs(log_dir)
        print(f"   Found {len(snapshots)} orderbook snapshots")

        trades = extract_trades_from_csv(web_dir)
        print(f"   Found {len(trades)} trade records")

        if snapshots or trades:
            model = MarketModel.from_historical_data(snapshots, trades)
            print(f"\n   Market Model (from historical data):")
            print(f"   - Pair cost: μ={model.pair_cost_mean:.4f}, σ={model.pair_cost_std:.4f}")
            print(f"   - Spread: μ={model.spread_mean:.4f}, σ={model.spread_std:.4f}")
            print(f"   - Chase cost: μ={model.chase_cost_mean:.4f}, σ={model.chase_cost_std:.4f}")
    else:
        print("\n1. Using default market model (no historical data)")

    # Run main simulation
    print(f"\n2. Running Monte Carlo simulation ({args.sessions} sessions)...")
    params = StrategyParams()
    results = run_monte_carlo(model, params, args.sessions, args.verbose)

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    print(f"\nP&L Distribution ({args.sessions} sessions):")
    print(f"  Mean P&L:     ${results.mean_pnl:+.4f}")
    print(f"  Std Dev:      ${results.std_pnl:.4f}")
    print(f"  Min P&L:      ${results.min_pnl:+.4f}")
    print(f"  Max P&L:      ${results.max_pnl:+.4f}")
    print(f"  Win Rate:     {results.win_rate:.1%}")

    print(f"\nPercentiles:")
    for p, val in sorted(results.pnl_percentiles.items()):
        print(f"  {p}th: ${val:+.4f}")

    print(f"\nFill Analysis:")
    print(f"  Mean Fill Rate:  {results.mean_fill_rate:.1%}")
    print(f"  Mean Chase Cost: ${results.mean_chase_cost:.4f}")
    print(f"  Mean Imbalance:  {results.mean_imbalance:.1f} shares")

    print(f"\nPosition Analysis:")
    print(f"  Mean Pairs:     {results.mean_pairs:.1f}")
    print(f"  Mean Pair Cost: ${results.mean_pair_cost:.4f}")

    # Parameter sensitivity
    if args.sensitivity:
        print("\n" + "=" * 60)
        print("PARAMETER SENSITIVITY ANALYSIS")
        print("=" * 60)

        sensitivity = run_parameter_sensitivity(
            model, params, args.sensitivity_sessions, args.verbose
        )

        # Find optimal parameters
        print("\n" + "-" * 40)
        print("OPTIMAL PARAMETERS:")
        print("-" * 40)

        for param_name, param_results in sensitivity.items():
            if param_name == "gradual_chase":
                best = max(param_results, key=lambda x: x[1].mean_pnl)
                print(f"  {param_name}: {'ON' if best[0] else 'OFF'} (mean_pnl=${best[1].mean_pnl:.4f})")
            else:
                best = max(param_results, key=lambda x: x[1].mean_pnl)
                print(f"  {param_name}: {best[0]:.4f} (mean_pnl=${best[1].mean_pnl:.4f})")

    print("\n" + "=" * 60)
    print("SIMULATION COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
