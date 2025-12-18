#!/usr/bin/env python3
"""
Test script for Position Tracking.

Demonstrates the PositionTracker service that manages
trading positions and calculates P&L.

Usage:
    python scripts/test_position_tracking.py
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
from src.services.position_tracker import PositionTracker


console = Console()


async def test_balance_fetch(client: PolymarketClient):
    """Test fetching USDC balance."""
    console.print("\n[bold]1. Fetching USDC Balance[/]")

    balance = await client.get_balance()
    console.print(f"  USDC Balance: ${balance:.2f}")

    return balance


async def test_position_sync(tracker: PositionTracker, finder: MarketFinder):
    """Test syncing positions from chain."""
    console.print("\n[bold]2. Syncing Position Balances from Chain[/]")

    # Get first few markets
    markets = await finder.find_btc_15min_markets(active_only=True, limit=3)

    if not markets:
        console.print("  [red]No markets found![/]")
        return []

    positions = []
    for market in markets:
        console.print(f"  Syncing: {market.question[:40]}...")
        position = await tracker.sync_position(market)
        positions.append(position)

        console.print(f"    Up Balance: {position.up_balance:.4f}")
        console.print(f"    Down Balance: {position.down_balance:.4f}")

    return positions


async def test_simulated_fills(tracker: PositionTracker, finder: MarketFinder):
    """Test adding simulated fills."""
    console.print("\n[bold]3. Testing Simulated Fills[/]")

    markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

    if not markets:
        console.print("  [red]No markets found![/]")
        return

    market = markets[0]
    console.print(f"  Market: {market.question[:40]}...")

    # Simulate a pair purchase
    console.print("\n  Simulating pair purchase: 10 pairs @ $0.51 each side")

    tracker.add_pair_fill(
        market=market,
        up_price=0.51,
        down_price=0.51,
        size=10,
    )

    position = tracker.get_position(market)

    console.print(Panel.fit(
        f"[bold]Up Balance:[/] {position.up_balance:.1f} @ ${position.up_avg_price:.4f}\n"
        f"[bold]Down Balance:[/] {position.down_balance:.1f} @ ${position.down_avg_price:.4f}\n"
        f"[bold]Pair Count:[/] {position.pair_count:.1f}\n"
        f"[bold]Pair Cost:[/] ${position.pair_cost:.4f}\n"
        f"[bold]Total Cost:[/] ${position.total_cost:.2f}\n"
        f"[bold]Unrealized PnL:[/] ${position.unrealized_pnl:.4f} ({position.unrealized_pnl_percent:.2f}%)\n"
        f"[bold]Is Balanced:[/] {position.is_balanced}",
        title="Position After Fill",
        border_style="cyan"
    ))

    # Add another fill to test averaging
    console.print("\n  Adding another fill: 5 pairs @ $0.49 each side")

    tracker.add_pair_fill(
        market=market,
        up_price=0.49,
        down_price=0.49,
        size=5,
    )

    position = tracker.get_position(market)

    console.print(Panel.fit(
        f"[bold]Up Balance:[/] {position.up_balance:.1f} @ ${position.up_avg_price:.4f}\n"
        f"[bold]Down Balance:[/] {position.down_balance:.1f} @ ${position.down_avg_price:.4f}\n"
        f"[bold]Pair Count:[/] {position.pair_count:.1f}\n"
        f"[bold]Pair Cost:[/] ${position.pair_cost:.4f}\n"
        f"[bold]Total Cost:[/] ${position.total_cost:.2f}\n"
        f"[bold]Unrealized PnL:[/] ${position.unrealized_pnl:.4f} ({position.unrealized_pnl_percent:.2f}%)\n"
        f"[bold]Fills:[/] {len(position.fills)}",
        title="Position After Second Fill",
        border_style="green"
    ))


async def test_portfolio_summary(tracker: PositionTracker):
    """Test portfolio summary."""
    console.print("\n[bold]4. Portfolio Summary[/]")

    summary = await tracker.get_portfolio_summary()

    console.print(Panel.fit(
        f"[bold]Active Positions:[/] {summary.total_positions}\n"
        f"[bold]Total Pairs:[/] {summary.total_pairs:.1f}\n"
        f"[bold]Total Cost:[/] ${summary.total_cost:.2f}\n"
        f"[bold]Unrealized PnL:[/] ${summary.total_unrealized_pnl:.4f} ({summary.total_pnl_percent:.2f}%)\n"
        f"[bold]USDC Balance:[/] ${summary.usdc_balance:.2f}\n"
        f"[bold]Total Value:[/] ${summary.total_value:.2f}\n"
        f"[bold]Exposure Up:[/] {summary.total_exposure_up:.1f}\n"
        f"[bold]Exposure Down:[/] {summary.total_exposure_down:.1f}\n"
        f"[bold]Is Balanced:[/] {summary.is_balanced}",
        title="Portfolio Summary",
        border_style="magenta"
    ))


async def test_imbalance_detection(tracker: PositionTracker, finder: MarketFinder):
    """Test imbalance detection."""
    console.print("\n[bold]5. Testing Imbalance Detection[/]")

    markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

    if not markets:
        return

    market = markets[0]

    # Create an imbalanced position (more Up than Down)
    console.print("  Creating imbalanced position: 5 Up only")

    tracker.add_fill(market, "UP", 0.50, 5)

    position = tracker.get_position(market)

    console.print(f"  Up Balance: {position.up_balance:.1f}")
    console.print(f"  Down Balance: {position.down_balance:.1f}")
    console.print(f"  Unmatched Up: {position.unmatched_up:.1f}")
    console.print(f"  Unmatched Down: {position.unmatched_down:.1f}")
    console.print(f"  Is Balanced: {position.is_balanced}")
    console.print(f"  Has Exposure: {position.has_exposure}")

    # Check needs rebalance
    needs_rebalance = tracker.needs_rebalance(threshold=1.0)
    console.print(f"\n  Needs Rebalance (threshold=1.0): {needs_rebalance}")

    # List imbalanced positions
    imbalanced = tracker.get_imbalanced_positions()
    console.print(f"  Imbalanced Positions: {len(imbalanced)}")


async def test_position_table(tracker: PositionTracker):
    """Display positions in a table."""
    console.print("\n[bold]6. Position Summary Table[/]")

    positions = tracker.get_all_positions()

    if not positions:
        console.print("  No positions tracked")
        return

    table = Table(title="All Positions")
    table.add_column("Market", style="cyan")
    table.add_column("Up", justify="right")
    table.add_column("Down", justify="right")
    table.add_column("Pairs", justify="right")
    table.add_column("Cost/Pair", justify="right")
    table.add_column("PnL", justify="right")
    table.add_column("Balanced")

    for pos in positions:
        pnl_style = "green" if pos.unrealized_pnl > 0 else "red" if pos.unrealized_pnl < 0 else ""
        balanced_style = "green" if pos.is_balanced else "yellow"

        table.add_row(
            pos.market.slug.replace("btc-updown-15m-", "")[:15],
            f"{pos.up_balance:.1f}",
            f"{pos.down_balance:.1f}",
            f"{pos.pair_count:.1f}",
            f"${pos.pair_cost:.4f}",
            f"[{pnl_style}]${pos.unrealized_pnl:.4f}[/]",
            f"[{balanced_style}]{pos.is_balanced}[/]",
        )

    console.print(table)


async def main():
    """Run position tracking tests."""
    console.print(Panel.fit(
        "[bold cyan]Position Tracking Test[/]",
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

        # Create tracker
        tracker = PositionTracker(client)

        # Run tests
        await test_balance_fetch(client)
        await test_position_sync(tracker, finder)

        # Clear and run simulated tests
        tracker.clear_all_positions()
        await test_simulated_fills(tracker, finder)
        await test_portfolio_summary(tracker)
        await test_imbalance_detection(tracker, finder)
        await test_position_table(tracker)

        # Summary
        console.print(Panel.fit(
            "[bold green]All tests completed![/]\n\n"
            "Position tracking is ready for:\n"
            "- Syncing balances from chain\n"
            "- Recording fills with averaging\n"
            "- P&L calculation\n"
            "- Portfolio summary\n"
            "- Imbalance detection",
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
