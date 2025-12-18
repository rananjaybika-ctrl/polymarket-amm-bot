#!/usr/bin/env python3
"""
Test script for Balance Management.

Demonstrates the BalanceManager service for fund management,
risk controls, and imbalance recovery recommendations.

Usage:
    python scripts/test_balance_management.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.services.market_finder import MarketFinder
from src.services.pair_analyzer import PairAnalyzer
from src.services.position_tracker import PositionTracker
from src.services.balance_manager import BalanceManager, RecoveryAction


console = Console()


async def test_available_capital(manager: BalanceManager):
    """Test available capital calculation."""
    console.print("\n[bold]1. Testing Available Capital[/]")

    available = await manager.get_available_capital()
    balance = await manager.client.get_balance()

    console.print(f"  USDC Balance: ${balance:.2f}")
    console.print(f"  Min Reserve: ${manager.min_balance_reserve:.2f}")
    console.print(f"  Max Exposure: {manager.max_exposure_percent * 100:.0f}%")
    console.print(f"  Available Capital: ${available:.2f}")


async def test_trade_validation(
    manager: BalanceManager,
    analyzer: PairAnalyzer,
    finder: MarketFinder,
):
    """Test trade validation."""
    console.print("\n[bold]2. Testing Trade Validation[/]")

    markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

    if not markets:
        console.print("  [red]No markets found![/]")
        return None

    market = markets[0]
    opportunity = await analyzer.analyze_market(market)

    console.print(f"  Market: {market.question[:40]}...")
    console.print(f"  Pair Cost: ${opportunity.pair_cost:.4f}")
    console.print(f"  Profitable: {opportunity.is_profitable}")

    # Validate different sizes
    test_sizes = [1, 10, 50, 100, 500]

    table = Table(title="Trade Validation Results")
    table.add_column("Size", justify="right")
    table.add_column("Valid")
    table.add_column("Max Size", justify="right")
    table.add_column("Reason")

    for size in test_sizes:
        validation = await manager.validate_trade(opportunity, size=size)
        valid_style = "green" if validation.valid else "red"

        table.add_row(
            str(size),
            f"[{valid_style}]{validation.valid}[/]",
            str(validation.max_size),
            validation.reason[:40],
        )

    console.print(table)

    return opportunity


async def test_recovery_recommendations(manager: BalanceManager, tracker: PositionTracker, finder: MarketFinder):
    """Test recovery recommendations for imbalanced positions."""
    console.print("\n[bold]3. Testing Recovery Recommendations[/]")

    markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

    if not markets:
        return

    market = markets[0]

    # Create test positions with different imbalances
    console.print("  Creating test positions...")

    # Scenario 1: Balanced position
    tracker.clear_all_positions()
    tracker.add_pair_fill(market, up_price=0.50, down_price=0.50, size=10)

    pos = tracker.get_position(market)
    rec = manager.get_recovery_recommendation(pos)

    console.print(Panel.fit(
        f"[bold]Scenario 1: Balanced Position[/]\n"
        f"Up: {pos.up_balance:.1f}, Down: {pos.down_balance:.1f}\n"
        f"Action: {rec.action.value}\n"
        f"Reason: {rec.reason}",
        border_style="green"
    ))

    # Scenario 2: Excess Up tokens
    tracker.add_fill(market, "UP", price=0.48, size=5)

    pos = tracker.get_position(market)
    rec = manager.get_recovery_recommendation(
        pos,
        up_bid=0.47,  # Current bid for selling
        down_ask=0.51,  # Current ask for buying
    )

    console.print(Panel.fit(
        f"[bold]Scenario 2: Excess Up Tokens[/]\n"
        f"Up: {pos.up_balance:.1f}, Down: {pos.down_balance:.1f}\n"
        f"Unmatched Up: {pos.unmatched_up:.1f}\n"
        f"Action: {rec.action.value}\n"
        f"Size: {rec.size:.1f}\n"
        f"Reason: {rec.reason}\n"
        f"Profitable: {rec.profitable}",
        border_style="yellow"
    ))

    # Scenario 3: Excess Down tokens
    tracker.clear_all_positions()
    tracker.add_pair_fill(market, up_price=0.50, down_price=0.50, size=10)
    tracker.add_fill(market, "DOWN", price=0.48, size=5)

    pos = tracker.get_position(market)
    rec = manager.get_recovery_recommendation(
        pos,
        up_ask=0.51,  # Current ask for buying
        down_bid=0.47,  # Current bid for selling
    )

    console.print(Panel.fit(
        f"[bold]Scenario 3: Excess Down Tokens[/]\n"
        f"Up: {pos.up_balance:.1f}, Down: {pos.down_balance:.1f}\n"
        f"Unmatched Down: {pos.unmatched_down:.1f}\n"
        f"Action: {rec.action.value}\n"
        f"Size: {rec.size:.1f}\n"
        f"Reason: {rec.reason}\n"
        f"Profitable: {rec.profitable}",
        border_style="yellow"
    ))


async def test_daily_loss_limit(manager: BalanceManager):
    """Test daily loss limit tracking."""
    console.print("\n[bold]4. Testing Daily Loss Limit[/]")

    # Reset counter
    manager.reset_daily_loss()

    console.print(f"  Max Daily Loss: ${manager.max_daily_loss:.2f}")
    console.print(f"  Current Loss: ${manager._daily_realized_loss:.2f}")
    console.print(f"  Within Limit: {manager.is_within_daily_limit()}")

    # Simulate losses
    losses = [10.0, 15.0, 20.0, 10.0]

    for loss in losses:
        manager.record_realized_loss(loss)
        within = manager.is_within_daily_limit()
        remaining = manager.get_remaining_daily_budget()

        status = "[green]OK[/]" if within else "[red]STOPPED[/]"
        console.print(f"  After ${loss:.2f} loss: ${manager._daily_realized_loss:.2f} total, remaining: ${remaining:.2f} {status}")


async def test_portfolio_health(manager: BalanceManager, tracker: PositionTracker, finder: MarketFinder):
    """Test portfolio health metrics."""
    console.print("\n[bold]5. Portfolio Health Check[/]")

    # Set up a sample portfolio
    markets = await finder.find_btc_15min_markets(active_only=True, limit=2)

    if len(markets) >= 2:
        tracker.clear_all_positions()
        tracker.add_pair_fill(markets[0], up_price=0.49, down_price=0.49, size=20)
        tracker.add_pair_fill(markets[1], up_price=0.50, down_price=0.50, size=15)

    # Reset loss counter for clean test
    manager.reset_daily_loss()
    manager.record_realized_loss(5.0)

    health = await manager.get_portfolio_health()

    console.print(Panel.fit(
        f"[bold]USDC Balance:[/] ${health['usdc_balance']:.2f}\n"
        f"[bold]Available Capital:[/] ${health['available_capital']:.2f}\n"
        f"[bold]Total Positions:[/] {health['total_positions']}\n"
        f"[bold]Total Pairs:[/] {health['total_pairs']:.1f}\n"
        f"[bold]Unrealized PnL:[/] ${health['total_pnl']:.4f}\n"
        f"[bold]Exposure Up:[/] {health['exposure_up']:.1f}\n"
        f"[bold]Exposure Down:[/] {health['exposure_down']:.1f}\n"
        f"[bold]Is Balanced:[/] {health['is_balanced']}\n"
        f"[bold]Daily Loss:[/] ${health['daily_loss']:.2f}\n"
        f"[bold]Within Limit:[/] {health['within_daily_limit']}\n"
        f"[bold]Remaining Budget:[/] ${health['remaining_daily_budget']:.2f}",
        title="Portfolio Health",
        border_style="magenta"
    ))


async def main():
    """Run balance management tests."""
    console.print(Panel.fit(
        "[bold cyan]Balance Management Test[/]",
        subtitle="Phase 3: Trading Core"
    ))

    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()

    try:
        # Connect
        console.print("\n[yellow]Connecting to Polymarket...[/]")
        await client.connect()
        console.print("[green]Connected![/]")

        # Create services
        tracker = PositionTracker(client)
        analyzer = PairAnalyzer(client)
        manager = BalanceManager(
            client,
            tracker,
            max_position_size=100,
            max_daily_loss=50.0,
            min_balance_reserve=10.0,
            max_exposure_percent=0.8,
        )

        console.print(f"\n[dim]Risk Limits:[/]")
        console.print(f"  Max Position: {manager.max_position_size} pairs/market")
        console.print(f"  Max Daily Loss: ${manager.max_daily_loss:.2f}")
        console.print(f"  Min Reserve: ${manager.min_balance_reserve:.2f}")
        console.print(f"  Max Exposure: {manager.max_exposure_percent * 100:.0f}%")

        # Run tests
        await test_available_capital(manager)
        await test_trade_validation(manager, analyzer, finder)
        await test_recovery_recommendations(manager, tracker, finder)
        await test_daily_loss_limit(manager)
        await test_portfolio_health(manager, tracker, finder)

        # Summary
        console.print(Panel.fit(
            "[bold green]All tests completed![/]\n\n"
            "Balance management is ready for:\n"
            "- Available capital calculation\n"
            "- Pre-trade validation\n"
            "- Imbalance recovery recommendations\n"
            "- Daily loss limit tracking\n"
            "- Portfolio health monitoring",
            title="Summary",
            border_style="green"
        ))

    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise

    finally:
        await client.disconnect()
        await finder.close()


if __name__ == "__main__":
    asyncio.run(main())
