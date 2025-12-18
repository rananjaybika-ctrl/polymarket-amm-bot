#!/usr/bin/env python3
"""
Test script for Trade Logging.

Demonstrates the TradeLogger service for recording trades,
exporting to CSV, and calculating statistics.

Usage:
    python scripts/test_trade_logging.py
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
from src.services.trade_logger import TradeLogger


console = Console()


async def test_single_trades(logger: TradeLogger, finder: MarketFinder):
    """Test logging single trades."""
    console.print("\n[bold]1. Testing Single Trade Logging[/]")

    markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

    if not markets:
        console.print("  [red]No markets found![/]")
        return

    market = markets[0]

    # Log some trades
    trade1 = logger.log_trade(
        market=market,
        side="UP",
        action="BUY",
        price=0.51,
        size=10,
        order_id="ord-001",
    )

    trade2 = logger.log_trade(
        market=market,
        side="DOWN",
        action="BUY",
        price=0.51,
        size=10,
        order_id="ord-002",
    )

    console.print(f"  Trade 1: {trade1}")
    console.print(f"  Trade 2: {trade2}")
    console.print(f"  Total trades: {len(logger._trades)}")


async def test_pair_trades(logger: TradeLogger, finder: MarketFinder):
    """Test logging pair trades."""
    console.print("\n[bold]2. Testing Pair Trade Logging[/]")

    markets = await finder.find_btc_15min_markets(active_only=True, limit=2)

    if not markets:
        return

    # Log several pair trades with different costs
    test_cases = [
        {"up_price": 0.49, "down_price": 0.49, "size": 10},  # Profitable
        {"up_price": 0.48, "down_price": 0.48, "size": 15},  # Very profitable
        {"up_price": 0.51, "down_price": 0.51, "size": 5},   # Unprofitable
        {"up_price": 0.50, "down_price": 0.50, "size": 20},  # Break-even
    ]

    for i, case in enumerate(test_cases):
        market = markets[i % len(markets)]
        pair = logger.log_pair_trade(
            market=market,
            up_price=case["up_price"],
            up_size=case["size"],
            down_price=case["down_price"],
            down_size=case["size"],
            up_order_id=f"up-{i:03d}",
            down_order_id=f"down-{i:03d}",
            notes=f"Test trade {i+1}",
        )
        console.print(f"  {pair}")

    console.print(f"\n  Total pair trades: {len(logger._pair_trades)}")


async def test_csv_export(logger: TradeLogger):
    """Test CSV export functionality."""
    console.print("\n[bold]3. Testing CSV Export[/]")

    # Export trades
    trades_path = logger.export_csv()
    console.print(f"  Exported trades to: {trades_path}")

    # Export pairs
    pairs_path = logger.export_pairs_csv()
    console.print(f"  Exported pairs to: {pairs_path}")

    # Show file contents preview
    console.print("\n  [dim]Trades CSV preview:[/]")
    with open(trades_path) as f:
        lines = f.readlines()[:5]
        for line in lines:
            console.print(f"    {line.strip()[:80]}...")

    console.print("\n  [dim]Pairs CSV preview:[/]")
    with open(pairs_path) as f:
        lines = f.readlines()[:5]
        for line in lines:
            console.print(f"    {line.strip()[:80]}...")


async def test_statistics(logger: TradeLogger):
    """Test statistics calculation."""
    console.print("\n[bold]4. Testing Statistics[/]")

    # Session stats
    session_stats = logger.get_session_stats()

    console.print(Panel.fit(
        f"[bold]Total Trades:[/] {session_stats.total_trades}\n"
        f"[bold]Total Pairs:[/] {session_stats.total_pairs:.1f}\n"
        f"[bold]Total Cost:[/] ${session_stats.total_cost:.2f}\n"
        f"[bold]Total Profit:[/] ${session_stats.total_profit:.4f}\n"
        f"[bold]Winning:[/] {session_stats.winning_trades}\n"
        f"[bold]Losing:[/] {session_stats.losing_trades}\n"
        f"[bold]Win Rate:[/] {session_stats.win_rate:.1f}%\n"
        f"[bold]Avg Profit/Trade:[/] ${session_stats.avg_profit_per_trade:.4f}\n"
        f"[bold]ROI:[/] {session_stats.roi:.2f}%",
        title="Session Statistics",
        border_style="cyan"
    ))

    # Daily stats
    daily_stats = logger.get_daily_stats()

    console.print(Panel.fit(
        f"[bold]Trades Today:[/] {daily_stats.total_trades}\n"
        f"[bold]Pairs Today:[/] {daily_stats.total_pairs:.1f}\n"
        f"[bold]Profit Today:[/] ${daily_stats.total_profit:.4f}\n"
        f"[bold]Win Rate:[/] {daily_stats.win_rate:.1f}%",
        title="Daily Statistics",
        border_style="green"
    ))


async def test_filtering(logger: TradeLogger, finder: MarketFinder):
    """Test trade filtering."""
    console.print("\n[bold]5. Testing Trade Filtering[/]")

    markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

    if not markets:
        return

    market = markets[0]

    # Filter by market
    market_trades = logger.get_trades(market_slug=market.slug)
    console.print(f"  Trades for {market.slug[:20]}...: {len(market_trades)}")

    # Filter by side
    up_trades = logger.get_trades(side="UP")
    down_trades = logger.get_trades(side="DOWN")
    console.print(f"  UP trades: {len(up_trades)}")
    console.print(f"  DOWN trades: {len(down_trades)}")

    # Filter pair trades by profitability
    profitable_pairs = logger.get_pair_trades(profitable_only=True)
    all_pairs = logger.get_pair_trades()
    console.print(f"  Profitable pairs: {len(profitable_pairs)}/{len(all_pairs)}")


async def test_trade_table(logger: TradeLogger):
    """Display trades in a table."""
    console.print("\n[bold]6. Pair Trades Summary Table[/]")

    pairs = logger.get_pair_trades()

    table = Table(title="All Pair Trades")
    table.add_column("Time", style="dim")
    table.add_column("Market")
    table.add_column("Up", justify="right")
    table.add_column("Down", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Pairs", justify="right")
    table.add_column("Profit", justify="right")
    table.add_column("Status")

    for pair in pairs:
        profit_style = "green" if pair.is_profitable else "red"
        status = "[green]WIN[/]" if pair.is_profitable else "[red]LOSS[/]"

        table.add_row(
            pair.timestamp.strftime("%H:%M:%S"),
            pair.market_slug.replace("btc-updown-15m-", "")[:10],
            f"${pair.up_price:.2f}",
            f"${pair.down_price:.2f}",
            f"${pair.pair_cost:.4f}",
            f"{pair.pair_count:.0f}",
            f"[{profit_style}]${pair.total_profit:.4f}[/]",
            status,
        )

    console.print(table)


async def main():
    """Run trade logging tests."""
    console.print(Panel.fit(
        "[bold cyan]Trade Logging Test[/]",
        subtitle="Phase 3: Trading Core"
    ))

    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()

    # Create logger with test directory
    log_dir = Path("./logs/test")
    logger = TradeLogger(log_dir=log_dir)

    try:
        # Connect (needed for market discovery)
        console.print("\n[yellow]Connecting to Polymarket...[/]")
        await client.connect()
        console.print("[green]Connected![/]")

        # Start session
        session_id = logger.start_session()
        console.print(f"Session: {session_id}")

        # Run tests
        await test_single_trades(logger, finder)
        await test_pair_trades(logger, finder)
        await test_csv_export(logger)
        await test_statistics(logger)
        await test_filtering(logger, finder)
        await test_trade_table(logger)

        # Summary
        console.print(Panel.fit(
            "[bold green]All tests completed![/]\n\n"
            "Trade logging is ready for:\n"
            "- Recording individual trades\n"
            "- Recording pair trades\n"
            "- CSV export (trades and pairs)\n"
            "- Statistics calculation\n"
            "- Trade filtering\n"
            f"\nLog files saved to: {log_dir}",
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
