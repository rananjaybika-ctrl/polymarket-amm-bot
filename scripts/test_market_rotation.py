#!/usr/bin/env python3
"""
Test script for Market Rotation.

Demonstrates the MarketRotator service in two modes:
- **Continuous mode** (default): Runs 24/7, only looks at markets within a rolling window
- **Session mode**: Stops after max_markets or max_duration limits

The 60-minute window means "only consider markets ending within 60 mins" -
the bot keeps running, but filters which markets to trade.

Usage:
    python scripts/test_market_rotation.py
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.services.market_finder import MarketFinder
from src.services.pair_analyzer import PairAnalyzer
from src.services.market_rotator import (
    MarketRotator,
    RotationEvent,
    RotationReason,
    SessionEndReason,
)


console = Console()


def format_time_remaining(seconds: float) -> str:
    """Format seconds as human-readable time."""
    if seconds < 0:
        return "Expired"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    if mins > 60:
        hours = mins // 60
        mins = mins % 60
        return f"{hours}h {mins}m"
    return f"{mins}m {secs}s"


def on_rotation(event: RotationEvent):
    """Callback when rotation occurs."""
    from_slug = event.from_market.slug if event.from_market else "None"
    to_slug = event.to_market.slug if event.to_market else "None"
    console.print(f"\n[yellow]ROTATION:[/] {from_slug} → {to_slug}")
    console.print(f"  Reason: {event.reason.value}")


async def main():
    """Test the MarketRotator service with PairAnalyzer integration."""
    console.print(Panel.fit(
        "[bold cyan]Market Rotation Test[/]",
        subtitle="Phase 2: Market Intelligence"
    ))

    finder = MarketFinder()
    config = Config()
    client = PolymarketClient(config)

    try:
        # Connect to API for pair analysis
        console.print("\n[yellow]Connecting to Polymarket API...[/]")
        await client.connect()
        console.print("[green]Connected![/]")

        # Create rotator in CONTINUOUS mode (default) - runs 24/7
        rotator = MarketRotator(
            finder=finder,
            continuous=True,  # Runs 24/7 without stopping
            market_window_minutes=60,  # Only consider markets within 60 min
            on_rotation=on_rotation,
        )

        console.print("\n[yellow]Configuration (Continuous Mode):[/]")
        console.print(f"  Continuous: {rotator.continuous} (runs 24/7)")
        console.print(f"  Market Window: {rotator.market_window_minutes} minutes")
        console.print("  → Bot never stops automatically")
        console.print("  → Only trades markets ending within 60 min window")

        # Discover available markets first
        console.print("\n[yellow]Discovering markets...[/]")
        markets = await finder.find_btc_15min_markets(active_only=True, limit=10)

        if not markets:
            console.print("[red]No active markets found![/]")
            return

        # Show available markets
        table = Table(title="Available Markets for Session")
        table.add_column("#", style="dim")
        table.add_column("Market", style="cyan")
        table.add_column("Starts In", style="yellow")
        table.add_column("Ends In", style="green")
        table.add_column("Status")

        for i, market in enumerate(markets[:6], 1):
            time_until_start = market.time_until_start()
            time_remaining = market.time_remaining()

            start_str = format_time_remaining(time_until_start)
            end_str = format_time_remaining(time_remaining)

            if market.is_expired():
                status = "[red]Expired[/]"
            elif market.is_active():
                status = "[green]Active[/]"
            else:
                status = "[yellow]Pending[/]"

            table.add_row(
                str(i),
                market.question.replace("Bitcoin Up or Down - ", "")[:30],
                start_str,
                end_str,
                status,
            )

        console.print(table)

        # Show which markets are within the window
        console.print("\n[yellow]Checking markets in 60-min window...[/]")
        window_markets = await finder.get_markets_in_window(hours=1.0)
        console.print(f"  Markets ending within 60 min: {len(window_markets)}")

        if not window_markets:
            console.print("\n[yellow]No markets end within 60 minutes.[/]")
            console.print("This is expected - the window filters markets by END time.")
            console.print("In production, the bot would wait until a market enters the window.")

            # Show when the next market will enter the window
            if markets:
                next_market = markets[0]
                time_remaining = next_market.time_remaining()
                mins_until_window = (time_remaining - 3600) / 60
                console.print(f"\n[cyan]Next market enters window in: {mins_until_window:.0f} minutes[/]")
                console.print(f"  Market: {next_market.question}")

            # Demonstrate session mode as fallback for testing
            console.print("\n[yellow]Creating session-mode rotator for demonstration...[/]")
            rotator = MarketRotator(
                finder=finder,
                continuous=False,  # Session mode for demo
                max_markets=4,
                max_duration_minutes=60,
                on_rotation=on_rotation,
            )
            console.print("  Using session mode (finds ALL active markets, not just those in window)")

        # Start session
        console.print("\n[yellow]Starting trading session...[/]")
        success = await rotator.start_session()

        if not success:
            console.print("[red]Failed to start session![/]")
            return

        console.print("[green]Session started![/]")

        # Show current state
        console.print(Panel.fit(
            f"[bold]Current Market:[/] {rotator.current_market.question}\n"
            f"[bold]Markets Traded:[/] {rotator.session_stats.markets_traded}\n"
            f"[bold]Mode:[/] {'Continuous (24/7)' if rotator.continuous else 'Session'}\n"
            f"[bold]Window:[/] {rotator.market_window_minutes} minutes",
            title="Session Status",
            border_style="green"
        ))

        # Demonstrate rotation check
        console.print("\n[yellow]Checking rotation conditions...[/]")

        should_rotate = rotator.should_rotate()
        reason = rotator.get_rotation_reason()

        console.print(f"  Should Rotate: {should_rotate}")
        console.print(f"  Reason: {reason.value if reason else 'None'}")

        # Check session completion
        console.print("\n[yellow]Checking session completion...[/]")

        is_complete = rotator.is_session_complete()
        end_reason = rotator.get_session_end_reason()

        console.print(f"  Is Complete: {is_complete}")
        console.print(f"  End Reason: {end_reason.value if end_reason else 'None'}")
        if rotator.continuous:
            console.print("  [green]→ Continuous mode: never stops automatically![/]")

        # Show what rotation would look like
        if not is_complete:
            console.print("\n[yellow]Simulating manual rotation...[/]")

            # Find what next market would be
            next_markets = [m for m in markets if m.slug != rotator.current_market.slug]
            if next_markets:
                console.print(f"  Next market would be: {next_markets[0].question}")
            else:
                console.print("  No next market available")

        # Show markets in the rolling window
        console.print("\n[bold]Markets in Rolling Window (60 min):[/]")

        timeline_table = Table()
        timeline_table.add_column("Market #", style="cyan")
        timeline_table.add_column("Time Window")
        timeline_table.add_column("Ends In")
        timeline_table.add_column("Status")

        for i, market in enumerate(markets[:6], 1):
            time_window = market.question.replace("Bitcoin Up or Down - ", "")
            time_remaining = market.time_remaining()
            ends_in = format_time_remaining(time_remaining)

            # Check if within 60-min window
            in_window = 0 < time_remaining <= rotator.market_window_minutes * 60

            if i == 1:
                status = "[green]← Current[/]"
            elif in_window:
                status = "[cyan]In window[/]"
            else:
                status = "[dim]Outside window[/]"

            timeline_table.add_row(
                f"#{i}",
                time_window[:25],
                ends_in,
                status,
            )

        console.print(timeline_table)

        # Demonstrate PairAnalyzer integration
        console.print("\n[bold]═══ PairAnalyzer Integration ═══[/]")

        analyzer = PairAnalyzer(client)

        console.print("\n[yellow]Analyzing current market for opportunities...[/]")
        opportunity = await analyzer.analyze_market(rotator.current_market)

        console.print(Panel.fit(
            f"[bold]Market:[/] {opportunity.market.question}\n"
            f"[bold]Up Ask:[/] ${opportunity.up_ask:.4f}\n"
            f"[bold]Down Ask:[/] ${opportunity.down_ask:.4f}\n"
            f"[bold]Pair Cost:[/] ${opportunity.pair_cost:.4f}\n"
            f"[bold]Profit/Pair:[/] ${opportunity.profit_per_pair:.4f}\n"
            f"[bold]Executable:[/] {opportunity.executable_size:.0f} pairs\n"
            f"[bold]Status:[/] {'[green]PROFITABLE[/]' if opportunity.is_profitable else '[yellow]Not profitable[/]'}",
            title="Current Market Analysis",
            border_style="magenta"
        ))

        # Show how rotation + analysis would work in production
        console.print("\n[bold]Production Workflow (Continuous Mode):[/]")
        console.print("  1. MarketRotator.start_session() → Get first market in window")
        console.print("  2. Loop forever:")
        console.print("     a. PairAnalyzer.analyze_market() → Check pair cost")
        console.print("     b. If profitable: Execute trade")
        console.print("     c. If rotator.should_rotate(): rotator.rotate()")
        console.print("        → Finds next market within 60-min rolling window")
        console.print("     d. Sleep and repeat (bot never stops)")
        console.print("  3. Only stops on manual termination or fatal error")

        # End session
        console.print("\n[yellow]Ending session...[/]")
        stats = await rotator.end_session(SessionEndReason.MANUAL_STOP)

        console.print(Panel.fit(
            f"[bold]Duration:[/] {stats.duration_minutes:.1f} minutes\n"
            f"[bold]Markets Traded:[/] {stats.markets_traded}\n"
            f"[bold]Rotations:[/] {len(stats.rotations)}\n"
            f"[bold]End Reason:[/] {stats.end_reason.value}",
            title="Session Complete",
            border_style="cyan"
        ))

    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise
    finally:
        await client.disconnect()
        await finder.close()


if __name__ == "__main__":
    asyncio.run(main())
