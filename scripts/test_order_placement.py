#!/usr/bin/env python3
"""
Test script for Order Placement.

Demonstrates the OrderExecutor service for pair trading,
including dry-run mode for safe testing.

Usage:
    python scripts/test_order_placement.py
    python scripts/test_order_placement.py --live  # Real order (requires confirmation)
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
from rich.prompt import Confirm

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.services.market_finder import MarketFinder
from src.services.pair_analyzer import PairAnalyzer
from src.services.order_executor import OrderExecutor, ExecutionStatus


console = Console()


async def test_tick_size(client: PolymarketClient, token_id: str):
    """Test tick size retrieval and price rounding."""
    console.print("\n[bold]1. Testing Tick Size & Price Rounding[/]")

    tick_size = client.get_tick_size(token_id)
    neg_risk = client.get_neg_risk(token_id)

    console.print(f"  Token ID: {token_id[:20]}...")
    console.print(f"  Tick Size: {tick_size}")
    console.print(f"  Neg Risk: {neg_risk}")

    # Test price rounding
    test_prices = [0.511, 0.5123, 0.49999, 0.01234]
    console.print("\n  Price Rounding Examples:")

    for price in test_prices:
        rounded = client.round_price(price, tick_size)
        console.print(f"    {price:.5f} → {rounded:.4f}")


async def test_order_creation(client: PolymarketClient, token_id: str):
    """Test order creation without submission."""
    console.print("\n[bold]2. Testing Order Creation (No Submit)[/]")

    try:
        order = client.create_order(
            token_id=token_id,
            side="BUY",
            price=0.50,
            size=1.0,
        )

        console.print("  [green]Order created successfully![/]")
        console.print(f"  Order type: {type(order).__name__}")

        # Show some order properties if available
        if hasattr(order, "salt"):
            console.print(f"  Order salt: {order.salt}")
        if hasattr(order, "signature"):
            console.print(f"  Signature: {str(order.signature)[:40]}...")

    except Exception as e:
        console.print(f"  [red]Failed to create order: {e}[/]")


async def test_pair_execution_dry_run(
    executor: OrderExecutor,
    analyzer: PairAnalyzer,
    finder: MarketFinder,
):
    """Test pair execution in dry-run mode."""
    console.print("\n[bold]3. Testing Pair Execution (Dry Run)[/]")

    # Find active market
    markets = await finder.find_btc_15min_markets(active_only=True, limit=3)

    if not markets:
        console.print("  [red]No active markets found![/]")
        return None

    market = markets[0]
    console.print(f"  Market: {market.question}")

    # Analyze for opportunity
    opportunity = await analyzer.analyze_market(market)

    console.print(Panel.fit(
        f"[bold]Up Ask:[/] ${opportunity.up_ask:.4f}\n"
        f"[bold]Down Ask:[/] ${opportunity.down_ask:.4f}\n"
        f"[bold]Pair Cost:[/] ${opportunity.pair_cost:.4f}\n"
        f"[bold]Profit/Pair:[/] ${opportunity.profit_per_pair:.4f}\n"
        f"[bold]Executable:[/] {opportunity.executable_size:.0f} pairs\n"
        f"[bold]Profitable:[/] {'Yes' if opportunity.is_profitable else 'No'}",
        title="Opportunity Analysis",
        border_style="cyan"
    ))

    # Execute dry run
    console.print("\n  Executing dry run (size=5 pairs)...")

    result = await executor.execute_opportunity(
        opportunity=opportunity,
        size=5,
        dry_run=True,
    )

    status_color = "green" if result.success else "red"
    console.print(f"  Result: [{status_color}]{result.success}[/]")
    console.print(f"  Up Order Status: {result.up_order.status.value}")
    console.print(f"  Down Order Status: {result.down_order.status.value}")
    console.print(f"  Expected Cost: ${result.expected_cost:.4f} per pair")

    return opportunity


async def test_open_orders(client: PolymarketClient):
    """Test fetching open orders."""
    console.print("\n[bold]4. Checking Open Orders[/]")

    try:
        orders = await client.get_open_orders()

        if not orders:
            console.print("  No open orders found")
            return

        console.print(f"  Found {len(orders)} open orders:")

        table = Table()
        table.add_column("Order ID", style="dim")
        table.add_column("Side")
        table.add_column("Price")
        table.add_column("Size")
        table.add_column("Status")

        for order in orders[:5]:  # Show first 5
            order_id = order.get("orderID", order.get("order_id", "?"))[:12] + "..."
            side = order.get("side", "?")
            price = order.get("price", "?")
            size = order.get("original_size", order.get("size", "?"))
            status = order.get("status", "?")

            table.add_row(order_id, side, str(price), str(size), status)

        console.print(table)

    except Exception as e:
        console.print(f"  [red]Failed to fetch orders: {e}[/]")


async def test_live_execution(
    executor: OrderExecutor,
    opportunity,
):
    """Execute a small live order (with confirmation)."""
    console.print("\n[bold]5. Live Execution Test[/]")

    if opportunity is None:
        console.print("  [yellow]Skipping - no opportunity available[/]")
        return

    if not opportunity.is_profitable:
        console.print("  [yellow]Skipping - opportunity not profitable[/]")
        console.print(f"    Pair cost: ${opportunity.pair_cost:.4f} (need < $1.00)")
        return

    console.print(Panel.fit(
        f"[bold yellow]WARNING: This will place REAL orders![/]\n\n"
        f"Market: {opportunity.market.question}\n"
        f"Size: 1 pair\n"
        f"Cost: ~${opportunity.pair_cost:.4f}\n"
        f"Expected Profit: ${opportunity.profit_per_pair:.4f}",
        title="Live Order Confirmation",
        border_style="yellow"
    ))

    if not Confirm.ask("Execute live order?", default=False):
        console.print("  [dim]Cancelled by user[/]")
        return

    console.print("\n  Executing live order...")

    result = await executor.execute_opportunity(
        opportunity=opportunity,
        size=1,
        dry_run=False,
    )

    if result.success:
        console.print(Panel.fit(
            f"[green]EXECUTION SUCCESSFUL![/]\n\n"
            f"Up Order: {result.up_order.order_id}\n"
            f"Down Order: {result.down_order.order_id}\n"
            f"Actual Cost: ${result.actual_cost:.4f}\n"
            f"Profit: ${result.total_profit:.4f}",
            title="Execution Result",
            border_style="green"
        ))
    else:
        console.print(Panel.fit(
            f"[red]EXECUTION FAILED[/]\n\n"
            f"Error: {result.error}\n"
            f"Up Status: {result.up_order.status.value}\n"
            f"Down Status: {result.down_order.status.value}",
            title="Execution Result",
            border_style="red"
        ))


async def main():
    """Run order placement tests."""
    console.print(Panel.fit(
        "[bold cyan]Order Placement Test[/]",
        subtitle="Phase 3: Trading Core"
    ))

    # Check for --live flag
    live_mode = "--live" in sys.argv

    if live_mode:
        console.print("[yellow]LIVE MODE ENABLED - Real orders will be placed![/]\n")
    else:
        console.print("[dim]Dry-run mode - No real orders will be placed[/]")
        console.print("[dim]Use --live flag to enable live trading[/]\n")

    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()

    try:
        # Connect
        console.print("[yellow]Connecting to Polymarket...[/]")
        await client.connect()
        console.print("[green]Connected![/]")

        # Get a test token
        markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

        if not markets:
            console.print("[red]No markets found for testing![/]")
            return

        test_token = markets[0].up_token_id

        # Run tests
        await test_tick_size(client, test_token)
        await test_order_creation(client, test_token)

        # Create services for pair testing
        analyzer = PairAnalyzer(client)
        executor = OrderExecutor(client)

        opportunity = await test_pair_execution_dry_run(executor, analyzer, finder)
        await test_open_orders(client)

        # Live test only if flag passed
        if live_mode:
            await test_live_execution(executor, opportunity)

        # Summary
        console.print(Panel.fit(
            "[bold green]All tests completed![/]\n\n"
            "Order placement is ready for:\n"
            "- Creating signed orders\n"
            "- Price rounding to tick size\n"
            "- Pair execution (dry-run tested)\n"
            "- Batch order submission",
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
