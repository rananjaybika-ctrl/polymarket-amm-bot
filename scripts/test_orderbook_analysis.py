#!/usr/bin/env python3
"""
Test script for Orderbook Analysis (Pair Cost Detection).

Analyzes BTC 15-minute markets to find arbitrage opportunities
where pair_cost < $1.00.

Usage:
    python scripts/test_orderbook_analysis.py
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


console = Console()


async def main():
    """Test orderbook analysis and pair cost detection."""
    console.print(Panel.fit(
        "[bold cyan]Orderbook Analysis Test[/]",
        subtitle="Phase 2: Pair Cost Detection"
    ))

    # Initialize services
    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()

    try:
        # Connect to API
        console.print("\n[yellow]Connecting to Polymarket API...[/]")
        await client.connect()
        console.print("[green]Connected![/]")

        # Find markets
        console.print("\n[yellow]Finding BTC 15-minute markets...[/]")
        markets = await finder.find_btc_15min_markets(active_only=True, limit=10)

        if not markets:
            console.print("[red]No active markets found![/]")
            return

        console.print(f"[green]Found {len(markets)} markets[/]\n")

        # Create analyzer
        analyzer = PairAnalyzer(client)

        # Analyze all markets
        console.print("[yellow]Analyzing orderbooks for pair cost...[/]\n")

        # Create results table
        table = Table(title="Pair Cost Analysis")
        table.add_column("Market", style="cyan", max_width=35)
        table.add_column("Up Ask", style="yellow", justify="right")
        table.add_column("Down Ask", style="yellow", justify="right")
        table.add_column("Pair Cost", style="magenta", justify="right")
        table.add_column("Profit/Pair", justify="right")
        table.add_column("Max Pairs", style="blue", justify="right")
        table.add_column("Status", justify="center")

        opportunities = []

        for market in markets:
            try:
                opp = await analyzer.analyze_market(market)
                opportunities.append(opp)

                # Format values
                time_window = market.question.replace("Bitcoin Up or Down - ", "")[:35]

                up_ask = f"${opp.up_ask:.2f}" if opp.up_ask else "N/A"
                down_ask = f"${opp.down_ask:.2f}" if opp.down_ask else "N/A"

                if opp.pair_cost is not None:
                    pair_cost = f"${opp.pair_cost:.4f}"
                    profit = opp.profit_per_pair

                    if profit > 0:
                        profit_str = f"[green]+${profit:.4f}[/]"
                        status = "[green]PROFIT[/]"
                    elif profit > -0.05:
                        profit_str = f"[yellow]${profit:.4f}[/]"
                        status = "[yellow]CLOSE[/]"
                    else:
                        profit_str = f"[red]${profit:.4f}[/]"
                        status = "[red]LOSS[/]"

                    max_pairs = f"{opp.executable_size:.0f}"
                else:
                    pair_cost = "N/A"
                    profit_str = "N/A"
                    max_pairs = "0"
                    status = "[dim]NO LIQ[/]"

                table.add_row(
                    time_window,
                    up_ask,
                    down_ask,
                    pair_cost,
                    profit_str,
                    max_pairs,
                    status,
                )

            except Exception as e:
                console.print(f"[red]Error analyzing {market.slug}: {e}[/]")

        console.print(table)

        # Show detailed analysis of first market
        if opportunities:
            opp = opportunities[0]
            console.print(Panel.fit(
                str(opp),
                title="[bold]Detailed Analysis: First Market[/]",
                border_style="cyan"
            ))

        # Find best opportunity
        profitable = [o for o in opportunities if o.is_profitable]

        if profitable:
            best = max(profitable, key=lambda o: o.profit_per_pair)
            console.print(Panel.fit(
                f"[green]Best Opportunity Found![/]\n\n"
                f"Market: {best.market.question}\n"
                f"Pair Cost: ${best.pair_cost:.4f}\n"
                f"Profit/Pair: ${best.profit_per_pair:.4f}\n"
                f"Max Pairs: {best.executable_size:.0f}\n"
                f"Potential Profit: ${best.max_profit:.2f}",
                title="[bold green]ARBITRAGE OPPORTUNITY[/]",
                border_style="green"
            ))
        else:
            console.print(Panel.fit(
                "[yellow]No profitable opportunities at current prices.[/]\n\n"
                "This is normal - markets are efficient.\n"
                "Opportunities typically appear:\n"
                "  • Close to market start time\n"
                "  • During high volatility periods\n"
                "  • When liquidity is imbalanced",
                title="[bold]Market Status[/]",
                border_style="yellow"
            ))

        # Summary stats
        console.print("\n[bold]Summary:[/]")

        if opportunities:
            pair_costs = [o.pair_cost for o in opportunities if o.pair_cost]
            if pair_costs:
                avg_cost = sum(pair_costs) / len(pair_costs)
                min_cost = min(pair_costs)
                max_cost = max(pair_costs)

                console.print(f"  Average Pair Cost: ${avg_cost:.4f}")
                console.print(f"  Min Pair Cost: ${min_cost:.4f}")
                console.print(f"  Max Pair Cost: ${max_cost:.4f}")
                console.print(f"  Profitable Markets: {len(profitable)}/{len(opportunities)}")

    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise
    finally:
        await client.disconnect()
        await finder.close()


if __name__ == "__main__":
    asyncio.run(main())
