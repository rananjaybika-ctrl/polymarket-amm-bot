#!/usr/bin/env python3
"""
Test script for MarketFinder service.

Demonstrates finding BTC 15-minute Up/Down markets on Polymarket.

Usage:
    python scripts/test_market_finder.py
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

from src.services.market_finder import MarketFinder, NoMarketsFoundError


console = Console()


async def main():
    """Test the MarketFinder service."""
    console.print(Panel.fit(
        "[bold cyan]BTC 15-Minute Market Finder Test[/]",
        subtitle="Phase 2: Market Intelligence"
    ))

    finder = MarketFinder()

    try:
        # Find all BTC 15-min markets
        console.print("\n[yellow]Searching for BTC 15-minute markets...[/]")
        markets = await finder.find_btc_15min_markets(active_only=False, limit=20)

        if not markets:
            console.print("[red]No BTC 15-minute markets found![/]")
            console.print("This could mean:")
            console.print("  - Markets haven't started yet for the day")
            console.print("  - API endpoint changed")
            console.print("  - Network issues")
            return

        console.print(f"[green]Found {len(markets)} markets[/]\n")

        # Create summary table
        table = Table(title="BTC 15-Minute Markets")
        table.add_column("Time Window", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Time Left", style="green")
        table.add_column("Spread", style="magenta")
        table.add_column("Liquidity", style="blue")

        for market in markets[:10]:  # Show first 10
            # Extract time from question
            time_window = market.question.replace("Bitcoin Up or Down - ", "")

            # Status
            if market.is_expired():
                status = "[red]Expired[/]"
            elif market.is_active():
                status = "[green]Active[/]"
            else:
                status = "[yellow]Pending[/]"

            # Time remaining
            remaining = market.time_remaining()
            if remaining < 0:
                time_left = f"[red]Ended {int(-remaining)}s ago[/]"
            elif remaining < 60:
                time_left = f"[yellow]{int(remaining)}s[/]"
            else:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                time_left = f"{mins}m {secs}s"

            # Spread
            spread = market.spread
            if spread == 1.0:
                spread_str = "[dim]No orders[/]"
            elif spread <= 0.02:
                spread_str = f"[green]{spread:.1%}[/]"
            else:
                spread_str = f"[yellow]{spread:.1%}[/]"

            # Liquidity
            if market.liquidity > 0:
                liquidity_str = f"${market.liquidity:,.0f}"
            else:
                liquidity_str = "[dim]$0[/]"

            table.add_row(
                time_window[:40],
                status,
                time_left,
                spread_str,
                liquidity_str
            )

        console.print(table)

        # Get active market
        console.print("\n[yellow]Looking for currently active market...[/]")
        try:
            active = await finder.get_active_market()
            console.print(Panel.fit(
                f"[bold green]{active.question}[/]\n\n"
                f"[cyan]Slug:[/] {active.slug}\n"
                f"[cyan]Condition ID:[/] {active.condition_id[:20]}...\n"
                f"[cyan]Up Token:[/] {active.up_token_id[:30]}...\n"
                f"[cyan]Down Token:[/] {active.down_token_id[:30]}...\n"
                f"[cyan]Time Remaining:[/] {int(active.time_remaining())} seconds\n"
                f"[cyan]Spread:[/] {active.spread:.1%}\n"
                f"[cyan]Liquidity:[/] ${active.liquidity:,.2f}",
                title="[bold]Active Market[/]",
                border_style="green"
            ))
        except NoMarketsFoundError:
            console.print("[yellow]No active markets at the moment[/]")
            console.print("Markets may be between 15-minute windows")

        # Get next market
        console.print("\n[yellow]Looking for next upcoming market...[/]")
        next_market = await finder.get_next_market()
        if next_market:
            time_until = int(next_market.time_until_start())
            console.print(f"[cyan]Next:[/] {next_market.question}")
            console.print(f"[cyan]Starts in:[/] {time_until // 60}m {time_until % 60}s")
        else:
            console.print("[dim]No upcoming markets found[/]")

        # Show markets in 1-hour window
        console.print("\n[yellow]Markets in next 60 minutes:[/]")
        window_markets = await finder.get_markets_in_window(hours=1.0)
        console.print(f"Found [green]{len(window_markets)}[/] markets for trading session")

    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise
    finally:
        await finder.close()


if __name__ == "__main__":
    asyncio.run(main())
