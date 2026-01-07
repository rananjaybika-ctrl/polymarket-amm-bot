#!/usr/bin/env python3
"""
What-If Simulator for Polymarket AMM Bot Trading Sessions

Simulates alternative trading strategies on historical session data
to compare outcomes vs actual performance.

Usage:
    python scripts/whatif_simulator.py [--session SESSION_DATE] [--scenario SCENARIO]

Scenarios:
    1. one_buy_per_side: Limit to 1 buy per side (5 shares each)
    2. target_30: Cap position at 30 shares per side
    3. cheap_first: Buy cheap side first, only hedge expensive when imbalanced
"""

import json
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional
import statistics


@dataclass
class Trade:
    """Single trade record."""
    side: str  # UP or DOWN
    price: float
    size: int
    timestamp: str


@dataclass
class MarketResult:
    """Result for a single market."""
    market_slug: str
    winner: str
    actual_pair_cost: float
    actual_pnl: float
    simulated_pair_cost: float
    simulated_pnl: float
    actual_position: Dict
    simulated_position: Dict
    delta_pnl: float


def load_session_data(session_date: str) -> dict:
    """Load session data from JSON file."""
    analysis_dir = Path(__file__).parent.parent / "analysis"
    session_file = analysis_dir / f"whatif_session_{session_date}.json"

    if not session_file.exists():
        raise FileNotFoundError(f"Session file not found: {session_file}")

    with open(session_file) as f:
        return json.load(f)


def calculate_pnl(position: Dict, winner: str) -> float:
    """Calculate P&L given position and winner."""
    up_size = position.get("up_size", 0)
    up_avg = position.get("up_avg", 0)
    down_size = position.get("down_size", 0)
    down_avg = position.get("down_avg", 0)

    # Total cost
    total_cost = (up_size * up_avg) + (down_size * down_avg)

    # Payout
    if winner == "UP":
        payout = up_size * 1.0
    else:
        payout = down_size * 1.0

    return payout - total_cost


def calculate_pair_cost(position: Dict) -> Optional[float]:
    """Calculate pair cost from position."""
    up_size = position.get("up_size", 0)
    up_avg = position.get("up_avg", 0)
    down_size = position.get("down_size", 0)
    down_avg = position.get("down_avg", 0)

    if up_size == 0 or down_size == 0:
        return None  # Unhedged

    return up_avg + down_avg


def simulate_one_buy_per_side(market: Dict) -> Dict:
    """
    Scenario 1: Only take first buy on each side.

    Risk: Higher variance due to small position (5+5=10 shares)
    Benefit: Prevents chasing expensive prices
    """
    position = market.get("position_at_resolution", {})

    # In real scenario, we'd replay trades and keep only first UP and first DOWN
    # For now, simulate by using half the avg price (early buys are cheaper)
    up_size = min(5, position.get("up_size", 0))
    down_size = min(5, position.get("down_size", 0))

    # Estimate early buy prices (typically cheaper)
    up_avg = position.get("up_avg", 0.50)
    down_avg = position.get("down_avg", 0.50)

    # Early buys are typically 10-30% cheaper in trending markets
    if market.get("chase_analysis"):
        # This market had chasing - early buys were much cheaper
        early_buys = market["chase_analysis"].get("early_buys", [])
        late_chases = market["chase_analysis"].get("late_chases", [])

        if early_buys:
            up_avg = statistics.mean(early_buys)
        if "up_buys" in market["chase_analysis"]:
            # First buy only
            up_avg = market["chase_analysis"]["up_buys"][0]
        if "down_buys" in market["chase_analysis"]:
            down_avg = market["chase_analysis"]["down_buys"][0]
    else:
        # Non-chasing market - discount early buys by ~15%
        up_avg = up_avg * 0.85
        down_avg = down_avg * 0.85

    return {
        "up_size": up_size,
        "up_avg": up_avg,
        "down_size": down_size,
        "down_avg": down_avg
    }


def simulate_target_30(market: Dict) -> Dict:
    """
    Scenario 2: Cap at 30 shares per side.

    Most markets already end at 15/15 so impact is minimal.
    Main benefit: Prevents over-accumulation in volatile markets.
    """
    position = market.get("position_at_resolution", {})

    # Cap at 30 (but most are already at 15)
    up_size = min(30, position.get("up_size", 0))
    down_size = min(30, position.get("down_size", 0))

    return {
        "up_size": up_size,
        "up_avg": position.get("up_avg", 0),
        "down_size": down_size,
        "down_avg": position.get("down_avg", 0)
    }


def simulate_cheap_first(market: Dict) -> Dict:
    """
    Scenario 3: Buy cheap side first (< $0.50).

    Only buy expensive side when imbalance > 20 shares.
    This prevents chasing trending prices.
    """
    position = market.get("position_at_resolution", {})
    up_avg = position.get("up_avg", 0)
    down_avg = position.get("down_avg", 0)
    up_size = position.get("up_size", 0)
    down_size = position.get("down_size", 0)

    # If we had chase analysis, adjust
    if market.get("chase_analysis"):
        # The issue was chasing expensive prices
        # With cheap-first, we'd buy less of the expensive side
        if up_avg > 0.50 and down_avg < 0.50:
            # UP was expensive, DOWN was cheap
            # We'd have more DOWN, less UP
            up_size = max(5, up_size // 2)  # Halve expensive side
            down_size = min(30, down_size + 5)  # Add to cheap side
            up_avg = up_avg * 0.75  # Lower avg from fewer late buys
        elif down_avg > 0.50 and up_avg < 0.50:
            # DOWN was expensive, UP was cheap
            down_size = max(5, down_size // 2)
            up_size = min(30, up_size + 5)
            down_avg = down_avg * 0.75

    return {
        "up_size": up_size,
        "up_avg": up_avg,
        "down_size": down_size,
        "down_avg": down_avg
    }


def run_scenario(session_data: dict, scenario: str) -> List[MarketResult]:
    """Run a what-if scenario on all markets in session."""
    results = []

    simulator_fn = {
        "one_buy_per_side": simulate_one_buy_per_side,
        "target_30": simulate_target_30,
        "cheap_first": simulate_cheap_first,
    }.get(scenario)

    if not simulator_fn:
        raise ValueError(f"Unknown scenario: {scenario}")

    for market in session_data["markets"]:
        actual_position = market.get("position_at_resolution", {})
        simulated_position = simulator_fn(market)

        winner = market["winner"]
        actual_pnl = market["pnl"]
        actual_pair_cost = market.get("pair_cost")

        simulated_pnl = calculate_pnl(simulated_position, winner)
        simulated_pair_cost = calculate_pair_cost(simulated_position)

        results.append(MarketResult(
            market_slug=market["market_slug"],
            winner=winner,
            actual_pair_cost=actual_pair_cost or 0,
            actual_pnl=actual_pnl,
            simulated_pair_cost=simulated_pair_cost or 0,
            simulated_pnl=simulated_pnl,
            actual_position=actual_position,
            simulated_position=simulated_position,
            delta_pnl=simulated_pnl - actual_pnl
        ))

    return results


def print_scenario_report(scenario: str, results: List[MarketResult]):
    """Print formatted report for scenario results."""
    print(f"\n{'='*70}")
    print(f"WHAT-IF SCENARIO: {scenario.upper().replace('_', ' ')}")
    print(f"{'='*70}")

    total_actual = sum(r.actual_pnl for r in results)
    total_simulated = sum(r.simulated_pnl for r in results)
    total_delta = sum(r.delta_pnl for r in results)

    improved = [r for r in results if r.delta_pnl > 0.01]
    worsened = [r for r in results if r.delta_pnl < -0.01]
    unchanged = [r for r in results if abs(r.delta_pnl) <= 0.01]

    print(f"\n📊 SUMMARY:")
    print(f"   Total Markets: {len(results)}")
    print(f"   Actual P&L:    ${total_actual:+.2f}")
    print(f"   Simulated P&L: ${total_simulated:+.2f}")
    print(f"   Delta:         ${total_delta:+.2f}")
    print(f"   Improved:      {len(improved)} markets")
    print(f"   Worsened:      {len(worsened)} markets")
    print(f"   Unchanged:     {len(unchanged)} markets")

    if improved:
        print(f"\n✅ MARKETS THAT IMPROVED:")
        for r in sorted(improved, key=lambda x: x.delta_pnl, reverse=True)[:5]:
            print(f"   {r.market_slug[-10:]}: ${r.actual_pnl:+.2f} → ${r.simulated_pnl:+.2f} (Δ ${r.delta_pnl:+.2f})")

    if worsened:
        print(f"\n❌ MARKETS THAT WORSENED:")
        for r in sorted(worsened, key=lambda x: x.delta_pnl)[:5]:
            print(f"   {r.market_slug[-10:]}: ${r.actual_pnl:+.2f} → ${r.simulated_pnl:+.2f} (Δ ${r.delta_pnl:+.2f})")

    # Risk analysis
    sim_pnls = [r.simulated_pnl for r in results]
    actual_pnls = [r.actual_pnl for r in results]

    if len(sim_pnls) > 1:
        print(f"\n📈 RISK METRICS:")
        print(f"   Actual StdDev:    ${statistics.stdev(actual_pnls):.2f}")
        print(f"   Simulated StdDev: ${statistics.stdev(sim_pnls):.2f}")
        print(f"   Actual Min:       ${min(actual_pnls):.2f}")
        print(f"   Simulated Min:    ${min(sim_pnls):.2f}")
        print(f"   Actual Max:       ${max(actual_pnls):.2f}")
        print(f"   Simulated Max:    ${max(sim_pnls):.2f}")


def main():
    parser = argparse.ArgumentParser(description="What-If Simulator for trading sessions")
    parser.add_argument("--session", default="2026-01-04", help="Session date (YYYY-MM-DD)")
    parser.add_argument("--scenario", default="all",
                        choices=["one_buy_per_side", "target_30", "cheap_first", "all"],
                        help="Scenario to simulate")
    args = parser.parse_args()

    print(f"Loading session data for {args.session}...")
    session_data = load_session_data(args.session)

    print(f"Session: {session_data['session_info']['start_time_ist']} to {session_data['session_info']['end_time_ist']}")
    print(f"Markets: {len(session_data['markets'])}")

    scenarios = ["one_buy_per_side", "target_30", "cheap_first"] if args.scenario == "all" else [args.scenario]

    all_results = {}
    for scenario in scenarios:
        results = run_scenario(session_data, scenario)
        all_results[scenario] = results
        print_scenario_report(scenario, results)

    # Compare all scenarios
    if len(scenarios) > 1:
        print(f"\n{'='*70}")
        print("SCENARIO COMPARISON")
        print(f"{'='*70}")
        print(f"\n{'Scenario':<25} {'Actual P&L':>12} {'Simulated':>12} {'Delta':>12}")
        print("-" * 65)
        for scenario, results in all_results.items():
            actual = sum(r.actual_pnl for r in results)
            simulated = sum(r.simulated_pnl for r in results)
            delta = sum(r.delta_pnl for r in results)
            print(f"{scenario:<25} ${actual:>10.2f} ${simulated:>10.2f} ${delta:>+10.2f}")

        # Best scenario
        best = max(all_results.items(), key=lambda x: sum(r.delta_pnl for r in x[1]))
        print(f"\n🏆 BEST SCENARIO: {best[0]} (Delta: ${sum(r.delta_pnl for r in best[1]):+.2f})")


if __name__ == "__main__":
    main()
