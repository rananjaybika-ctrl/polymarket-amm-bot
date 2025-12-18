#!/usr/bin/env python3
"""
Test script for WebSocket Integration.

Demonstrates real-time orderbook streaming from Polymarket
WebSocket API with live price updates.

Usage:
    python scripts/test_websocket.py
    python scripts/test_websocket.py --duration 30  # Run for 30 seconds
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.api.websocket_client import WebSocketClient, BookUpdate, PriceChange, TradeUpdate
from src.services.market_finder import MarketFinder


console = Console()

# Global state for live display
latest_updates = {
    "up": {"bid": None, "ask": None, "timestamp": None},
    "down": {"bid": None, "ask": None, "timestamp": None},
    "pair_cost": None,
    "message_count": 0,
    "last_message": "",
}


def on_book_update(update: BookUpdate):
    """Handle book update message."""
    latest_updates["message_count"] += 1
    latest_updates["last_message"] = f"book update for {update.token_id[:8]}..."

    # We'll identify Up vs Down by tracking which token
    # For now just update generic display
    if update.best_bid:
        latest_updates["up"]["bid"] = update.best_bid
    if update.best_ask:
        latest_updates["up"]["ask"] = update.best_ask
    latest_updates["up"]["timestamp"] = update.timestamp


def on_price_change(update: PriceChange):
    """Handle price change message."""
    latest_updates["message_count"] += 1
    latest_updates["last_message"] = f"price change for {update.token_id[:8]}..."


def on_trade(update: TradeUpdate):
    """Handle trade message."""
    latest_updates["message_count"] += 1
    latest_updates["last_message"] = f"trade: {update.size:.1f} @ ${update.price:.4f}"
    console.print(f"  [yellow]TRADE:[/] {update.size:.4f} @ ${update.price:.4f} ({update.side})")


def generate_status_table() -> Table:
    """Generate status table for live display."""
    table = Table(title=f"WebSocket Status (Messages: {latest_updates['message_count']})")
    table.add_column("Token")
    table.add_column("Best Bid", justify="right")
    table.add_column("Best Ask", justify="right")
    table.add_column("Spread", justify="right")
    table.add_column("Updated")

    for side in ["up", "down"]:
        data = latest_updates[side]
        bid = f"${data['bid']:.4f}" if data['bid'] else "-"
        ask = f"${data['ask']:.4f}" if data['ask'] else "-"

        if data['bid'] and data['ask']:
            spread = f"${data['ask'] - data['bid']:.4f}"
        else:
            spread = "-"

        updated = data['timestamp'].strftime("%H:%M:%S") if data['timestamp'] else "-"

        table.add_row(side.upper(), bid, ask, spread, updated)

    return table


async def test_connection(ws_client: WebSocketClient):
    """Test WebSocket connection."""
    console.print("\n[bold]1. Testing WebSocket Connection[/]")

    success = await ws_client.connect()

    if success:
        console.print("  [green]Connected to Polymarket WebSocket![/]")
    else:
        console.print("  [red]Failed to connect[/]")
        return False

    return True


async def test_subscription(ws_client: WebSocketClient, finder: MarketFinder):
    """Test market subscription."""
    console.print("\n[bold]2. Testing Market Subscription[/]")

    # Find active markets
    markets = await finder.find_btc_15min_markets(active_only=True, limit=1)

    if not markets:
        console.print("  [red]No markets found![/]")
        return None

    market = markets[0]
    console.print(f"  Market: {market.question[:50]}...")
    console.print(f"  Up Token: {market.up_token_id[:20]}...")
    console.print(f"  Down Token: {market.down_token_id[:20]}...")

    # Subscribe
    success = await ws_client.subscribe([market.up_token_id, market.down_token_id])

    if success:
        console.print("  [green]Subscribed to market tokens![/]")
    else:
        console.print("  [red]Subscription failed[/]")
        return None

    return market


async def test_streaming(ws_client: WebSocketClient, duration: int = 10):
    """Test receiving streaming data."""
    console.print(f"\n[bold]3. Streaming Data for {duration} seconds...[/]")
    console.print("  [dim]Waiting for updates...[/]")

    # Run WebSocket for specified duration
    await ws_client.run_for_duration(duration)

    console.print(f"\n  [green]Received {latest_updates['message_count']} messages[/]")
    console.print(f"  Last message: {latest_updates['last_message']}")


async def test_reconnection(ws_client: WebSocketClient):
    """Test auto-reconnect behavior."""
    console.print("\n[bold]4. Testing Auto-Reconnect[/]")
    console.print("  [dim]Simulating disconnect...[/]")

    if ws_client._ws:
        await ws_client._ws.close()
        console.print("  Connection closed")

    # Auto-reconnect should handle this
    console.print("  [dim]Auto-reconnect will trigger on next run...[/]")


async def main():
    """Run WebSocket tests."""
    console.print(Panel.fit(
        "[bold cyan]WebSocket Integration Test[/]",
        subtitle="Phase 3: Trading Core"
    ))

    # Parse duration argument
    duration = 10
    if "--duration" in sys.argv:
        idx = sys.argv.index("--duration")
        if idx + 1 < len(sys.argv):
            duration = int(sys.argv[idx + 1])

    console.print(f"\n[dim]Test duration: {duration} seconds[/]")

    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()
    ws_client = WebSocketClient(auto_reconnect=True)

    # Register callbacks
    ws_client.on_book_update(on_book_update)
    ws_client.on_price_change(on_price_change)
    ws_client.on_trade(on_trade)

    try:
        # Connect REST API for market discovery
        console.print("\n[yellow]Connecting to REST API...[/]")
        await client.connect()
        console.print("[green]Connected![/]")

        # Run tests
        if not await test_connection(ws_client):
            return

        market = await test_subscription(ws_client, finder)
        if not market:
            return

        await test_streaming(ws_client, duration)

        # Summary
        console.print(Panel.fit(
            "[bold green]WebSocket test completed![/]\n\n"
            f"Messages received: {latest_updates['message_count']}\n"
            "WebSocket features:\n"
            "- Real-time orderbook updates\n"
            "- Price change notifications\n"
            "- Trade execution alerts\n"
            "- Auto-reconnect support",
            title="Summary",
            border_style="green"
        ))

    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        import traceback
        traceback.print_exc()

    finally:
        await ws_client.disconnect()
        await client.disconnect()
        await finder.close()


if __name__ == "__main__":
    asyncio.run(main())
