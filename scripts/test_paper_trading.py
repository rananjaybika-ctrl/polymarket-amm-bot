#!/usr/bin/env python3
"""
Test script for Paper Trading Engine.

Demonstrates the dry run simulation that executes
paper trades without risking real capital.

Usage:
    python scripts/test_paper_trading.py
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
from src.services.paper_trading import (
    PaperTradingEngine,
    SimulationConfig,
)


console = Console()


async def main():
    """Test the Paper Trading Engine."""
    console.print(Panel.fit(
        "[bold cyan]Paper Trading Engine Test[/]",
        subtitle="Phase 4: Dry Run"
    ))

    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()

    try:
        # Connect to get real market data
        console.print("\n[yellow]Connecting to Polymarket API...[/]")
        await client.connect()
        console.print("[green]Connected![/]")

        # Create paper trading engine
        sim_config = SimulationConfig(
            fill_probability=0.90,    # 90% fills
            partial_fill_rate=0.10,   # 10% partials
            slippage_bps=5.0,         # 5 bps slippage
            random_seed=42,           # Reproducible results
        )

        engine = PaperTradingEngine(
            config=sim_config,
            initial_balance=100.0,  # Start with $100
        )

        console.print("\n[yellow]Simulation Configuration:[/]")
        console.print(f"  Fill Probability: {sim_config.fill_probability:.0%}")
        console.print(f"  Partial Fill Rate: {sim_config.partial_fill_rate:.0%}")
        console.print(f"  Max Slippage: {sim_config.slippage_bps} bps")
        console.print(f"  Initial Balance: ${engine.initial_balance:.2f}")

        # Find markets to trade
        console.print("\n[yellow]Finding markets...[/]")
        markets = await finder.find_btc_15min_markets(active_only=True, limit=5)

        if not markets:
            console.print("[red]No markets found![/]")
            return

        console.print(f"  Found {len(markets)} markets")

        # Analyze markets for opportunities
        console.print("\n[bold]═══ Analyzing Markets ═══[/]")

        analyzer = PairAnalyzer(client)
        opportunities = []

        for market in markets[:3]:  # Analyze first 3
            opp = await analyzer.analyze_market(market)
            opportunities.append(opp)

            status = "[green]PROFITABLE[/]" if opp.is_profitable else "[yellow]Not profitable[/]"
            console.print(f"  {market.question[:40]}...")
            console.print(f"    Pair Cost: ${opp.pair_cost:.4f} | {status}")

        # Execute paper trades
        console.print("\n[bold]═══ Executing Paper Trades ═══[/]")

        for opp in opportunities:
            if not opp.is_profitable:
                console.print(f"\n[dim]Skipping {opp.market.slug} (not profitable)[/]")
                continue

            # Calculate trade size (max 10 pairs or what's available)
            size = min(10, int(opp.executable_size), int(engine.balance / opp.pair_cost))

            if size <= 0:
                console.print(f"\n[dim]Skipping {opp.market.slug} (insufficient balance)[/]")
                continue

            console.print(f"\n[yellow]Trading {size} pairs on {opp.market.slug}[/]")
            console.print(f"  Expected cost: ${size * opp.pair_cost:.4f}")

            result = await engine.execute_paper_trade(opp, size)

            if result.success:
                console.print(f"  [green]SUCCESS[/] - Both sides filled")
            elif result.up_order.filled_size > 0 or result.down_order.filled_size > 0:
                console.print(f"  [yellow]PARTIAL[/] - Some fills")
            else:
                console.print(f"  [red]FAILED[/] - No fills")

            console.print(f"  Up: {result.up_order.filled_size}/{result.up_order.size} @ ${result.up_order.filled_price:.4f}")
            console.print(f"  Down: {result.down_order.filled_size}/{result.down_order.size} @ ${result.down_order.filled_price:.4f}")
            console.print(f"  Actual cost: ${result.actual_cost:.4f}")

        # Show positions
        console.print("\n[bold]═══ Paper Positions ═══[/]")

        if engine.positions:
            pos_table = Table()
            pos_table.add_column("Market", style="cyan")
            pos_table.add_column("Up", justify="right")
            pos_table.add_column("Down", justify="right")
            pos_table.add_column("Pairs", justify="right")
            pos_table.add_column("Cost", justify="right")
            pos_table.add_column("Expected Profit", justify="right")

            for pos in engine.positions:
                pos_table.add_row(
                    pos.market_slug[:30],
                    f"{pos.up_size:.0f}",
                    f"{pos.down_size:.0f}",
                    f"{pos.pair_count}",
                    f"${pos.total_cost:.4f}",
                    f"${pos.expected_profit:.4f}",
                )

            console.print(pos_table)
        else:
            console.print("  [dim]No positions[/]")

        # Show statistics
        console.print("\n[bold]═══ Simulation Statistics ═══[/]")

        stats = engine.stats
        stats_table = Table(show_header=False)
        stats_table.add_column("Metric", style="cyan")
        stats_table.add_column("Value", justify="right")

        stats_table.add_row("Total Trades", str(stats.total_trades))
        stats_table.add_row("Successful Pairs", str(stats.successful_pairs))
        stats_table.add_row("Partial Fills", str(stats.partial_fills))
        stats_table.add_row("Failed Fills", str(stats.failed_fills))
        stats_table.add_row("Win Rate", f"{stats.win_rate:.1%}")
        stats_table.add_row("Total Cost", f"${stats.total_cost:.4f}")
        stats_table.add_row("Expected Profit", f"${stats.total_profit:.4f}")

        console.print(stats_table)

        # Show balance
        console.print("\n[bold]═══ Balance Summary ═══[/]")
        console.print(Panel.fit(
            f"[bold]Initial Balance:[/] ${engine.initial_balance:.2f}\n"
            f"[bold]Current Balance:[/] ${engine.balance:.2f}\n"
            f"[bold]Unrealized P&L:[/] ${engine.get_total_pnl():.4f}\n"
            f"[bold]Net Change:[/] ${engine.balance - engine.initial_balance + engine.get_total_pnl():.4f}",
            title="Paper Trading Summary",
            border_style="green" if engine.get_total_pnl() >= 0 else "red"
        ))

        # Simulate market resolution
        console.print("\n[bold]═══ Simulating Market Resolution ═══[/]")

        for pos in list(engine.positions):
            # Randomly pick winner (in real bot, this comes from market data)
            import random
            winner = random.choice(["UP", "DOWN"])
            console.print(f"\n  Resolving {pos.market_slug}: {winner} wins")

            pnl = engine.resolve_market(pos.market_slug, winner)
            console.print(f"  P&L: ${pnl:.4f}")

        # Final summary
        console.print("\n[bold]═══ Final Results ═══[/]")
        console.print(Panel.fit(
            f"[bold]Final Balance:[/] ${engine.balance:.2f}\n"
            f"[bold]Realized P&L:[/] ${engine.get_realized_pnl():.4f}\n"
            f"[bold]Total Return:[/] {((engine.balance / engine.initial_balance) - 1) * 100:.2f}%",
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
