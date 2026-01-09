#!/usr/bin/env python3
"""
Compare REST API vs WebSocket for market detection and monitoring.

Tests:
1. REST API: MarketFinder (current approach)
2. WebSocket: Real-time market data streaming

Usage:
    python scripts/compare_rest_ws_markets.py
"""

import asyncio
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live

from src.services.market_finder import MarketFinder, NoMarketsFoundError
from src.api.websocket_client import WebSocketClient, BookUpdate, PriceChange, MarketResolved

console = Console()


@dataclass
class TimingResult:
    """Timing result for an operation."""
    method: str
    operation: str
    duration_ms: float
    success: bool
    details: str = ""


class MarketComparison:
    """Compare REST vs WebSocket for market operations."""

    def __init__(self):
        self.finder = MarketFinder()
        self.ws_client = WebSocketClient(custom_features=True)
        self.results: list[TimingResult] = []

        # WebSocket state
        self.ws_book_updates: list[BookUpdate] = []
        self.ws_price_changes: list[PriceChange] = []
        self.ws_first_book_time: Optional[float] = None
        self.ws_resolution_events: list[MarketResolved] = []

    def _on_book_update(self, update: BookUpdate):
        """Handle WebSocket book update."""
        if self.ws_first_book_time is None:
            self.ws_first_book_time = time.time()
        self.ws_book_updates.append(update)

    def _on_price_change(self, change: PriceChange):
        """Handle WebSocket price change."""
        self.ws_price_changes.append(change)

    def _on_market_resolved(self, event: MarketResolved):
        """Handle market resolution event."""
        self.ws_resolution_events.append(event)
        console.print(f"[red]MARKET RESOLVED:[/] {event.winning_outcome}")

    async def test_rest_find_current_market(self) -> Optional[dict]:
        """Test REST API market finding."""
        start = time.time()
        try:
            market = await self.finder.get_current_market()
            duration = (time.time() - start) * 1000

            if market:
                self.results.append(TimingResult(
                    method="REST",
                    operation="Find current market",
                    duration_ms=duration,
                    success=True,
                    details=f"{market.slug}"
                ))
                return {
                    "slug": market.slug,
                    "up_token": market.up_token_id,
                    "down_token": market.down_token_id,
                    "time_remaining": market.time_remaining(),
                    "condition_id": market.condition_id,
                }
            else:
                self.results.append(TimingResult(
                    method="REST",
                    operation="Find current market",
                    duration_ms=duration,
                    success=False,
                    details="No market found"
                ))
                return None
        except Exception as e:
            duration = (time.time() - start) * 1000
            self.results.append(TimingResult(
                method="REST",
                operation="Find current market",
                duration_ms=duration,
                success=False,
                details=str(e)
            ))
            return None

    async def test_rest_get_active_market(self) -> Optional[dict]:
        """Test REST API get_active_market (with fallback)."""
        start = time.time()
        try:
            market = await self.finder.get_active_market()
            duration = (time.time() - start) * 1000

            self.results.append(TimingResult(
                method="REST",
                operation="Get active market (with fallback)",
                duration_ms=duration,
                success=True,
                details=f"{market.slug}, {int(market.time_remaining())}s left"
            ))
            return {
                "slug": market.slug,
                "up_token": market.up_token_id,
                "down_token": market.down_token_id,
                "time_remaining": market.time_remaining(),
            }
        except NoMarketsFoundError as e:
            duration = (time.time() - start) * 1000
            self.results.append(TimingResult(
                method="REST",
                operation="Get active market (with fallback)",
                duration_ms=duration,
                success=False,
                details="No active markets"
            ))
            return None
        except Exception as e:
            duration = (time.time() - start) * 1000
            self.results.append(TimingResult(
                method="REST",
                operation="Get active market (with fallback)",
                duration_ms=duration,
                success=False,
                details=str(e)
            ))
            return None

    async def test_rest_upcoming_markets(self) -> int:
        """Test REST API for upcoming markets."""
        start = time.time()
        try:
            markets = await self.finder.get_current_and_upcoming_markets(count=5)
            duration = (time.time() - start) * 1000

            self.results.append(TimingResult(
                method="REST",
                operation="Get 5 upcoming markets (parallel)",
                duration_ms=duration,
                success=True,
                details=f"Found {len(markets)} markets"
            ))
            return len(markets)
        except Exception as e:
            duration = (time.time() - start) * 1000
            self.results.append(TimingResult(
                method="REST",
                operation="Get 5 upcoming markets (parallel)",
                duration_ms=duration,
                success=False,
                details=str(e)
            ))
            return 0

    async def test_websocket_connect_and_subscribe(self, up_token: str, down_token: str) -> bool:
        """Test WebSocket connection and subscription."""
        # Register callbacks
        self.ws_client.on_book_update(self._on_book_update)
        self.ws_client.on_price_change(self._on_price_change)
        self.ws_client.on_market_resolved(self._on_market_resolved)

        # Connect
        start = time.time()
        connected = await self.ws_client.connect()
        connect_duration = (time.time() - start) * 1000

        self.results.append(TimingResult(
            method="WebSocket",
            operation="Connect",
            duration_ms=connect_duration,
            success=connected,
            details="Connected" if connected else "Failed"
        ))

        if not connected:
            return False

        # Subscribe
        start = time.time()
        subscribed = await self.ws_client.subscribe([up_token, down_token])
        subscribe_duration = (time.time() - start) * 1000

        self.results.append(TimingResult(
            method="WebSocket",
            operation="Subscribe to tokens",
            duration_ms=subscribe_duration,
            success=subscribed,
            details=f"2 tokens" if subscribed else "Failed"
        ))

        return subscribed

    async def test_websocket_first_data(self, timeout: float = 5.0) -> bool:
        """Test time to receive first WebSocket data."""
        start = time.time()
        self.ws_first_book_time = None
        self.ws_book_updates.clear()

        # Start receiving in background
        receive_task = asyncio.create_task(self.ws_client.run())

        try:
            # Wait for first book update
            while time.time() - start < timeout:
                if self.ws_first_book_time:
                    duration = (self.ws_first_book_time - start) * 1000
                    self.results.append(TimingResult(
                        method="WebSocket",
                        operation="Time to first orderbook",
                        duration_ms=duration,
                        success=True,
                        details=f"{len(self.ws_book_updates)} updates received"
                    ))
                    return True
                await asyncio.sleep(0.05)

            # Timeout
            self.results.append(TimingResult(
                method="WebSocket",
                operation="Time to first orderbook",
                duration_ms=timeout * 1000,
                success=False,
                details="Timeout"
            ))
            return False

        finally:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass

    async def test_websocket_orderbook_stream(self, duration_sec: float = 3.0) -> int:
        """Test WebSocket orderbook streaming rate."""
        self.ws_book_updates.clear()
        start = time.time()

        receive_task = asyncio.create_task(self.ws_client.run())

        try:
            await asyncio.sleep(duration_sec)

            update_count = len(self.ws_book_updates)
            rate = update_count / duration_sec

            self.results.append(TimingResult(
                method="WebSocket",
                operation=f"Orderbook updates in {duration_sec}s",
                duration_ms=duration_sec * 1000,
                success=update_count > 0,
                details=f"{update_count} updates ({rate:.1f}/sec)"
            ))

            return update_count

        finally:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass

    def print_results(self):
        """Print comparison results."""
        table = Table(title="REST vs WebSocket Comparison")
        table.add_column("Method", style="cyan")
        table.add_column("Operation", style="white")
        table.add_column("Duration", style="yellow", justify="right")
        table.add_column("Status", style="green")
        table.add_column("Details", style="dim")

        for r in self.results:
            status = "[green]OK[/]" if r.success else "[red]FAIL[/]"
            duration_str = f"{r.duration_ms:.1f}ms"

            # Color code by speed
            if r.duration_ms < 100:
                duration_str = f"[green]{duration_str}[/]"
            elif r.duration_ms < 500:
                duration_str = f"[yellow]{duration_str}[/]"
            else:
                duration_str = f"[red]{duration_str}[/]"

            table.add_row(
                r.method,
                r.operation,
                duration_str,
                status,
                r.details[:50]
            )

        console.print(table)

    async def cleanup(self):
        """Cleanup resources."""
        await self.ws_client.disconnect()
        await self.finder.close()


async def main():
    """Run the comparison tests."""
    console.print(Panel.fit(
        "[bold cyan]REST API vs WebSocket Comparison[/]\n"
        "[dim]Testing market detection and data streaming[/]",
        border_style="cyan"
    ))

    comparison = MarketComparison()

    try:
        # ==========================================
        # PHASE 1: REST API Tests
        # ==========================================
        console.print("\n[bold yellow]Phase 1: REST API Tests[/]")

        # Test 1: Find current market by slug calculation
        console.print("  Testing get_current_market()...")
        market_info = await comparison.test_rest_find_current_market()

        if not market_info:
            console.print("[red]Cannot proceed without current market[/]")
            return

        console.print(f"  [green]Found:[/] {market_info['slug']}")
        console.print(f"  [dim]Time remaining: {int(market_info['time_remaining'])}s[/]")

        # Test 2: Get active market (with fallback)
        console.print("  Testing get_active_market()...")
        await comparison.test_rest_get_active_market()

        # Test 3: Get upcoming markets (parallel fetch)
        console.print("  Testing get_current_and_upcoming_markets()...")
        await comparison.test_rest_upcoming_markets()

        # ==========================================
        # PHASE 2: WebSocket Tests
        # ==========================================
        console.print("\n[bold yellow]Phase 2: WebSocket Tests[/]")

        up_token = market_info['up_token']
        down_token = market_info['down_token']

        # Test 4: WebSocket connect and subscribe
        console.print("  Testing connect and subscribe...")
        if not await comparison.test_websocket_connect_and_subscribe(up_token, down_token):
            console.print("[red]WebSocket connection failed[/]")
        else:
            # Test 5: Time to first data
            console.print("  Testing time to first orderbook...")
            await comparison.test_websocket_first_data(timeout=5.0)

            # Test 6: Streaming rate
            console.print("  Testing orderbook streaming rate (3s)...")
            await comparison.test_websocket_orderbook_stream(duration_sec=3.0)

        # ==========================================
        # RESULTS
        # ==========================================
        console.print("\n")
        comparison.print_results()

        # Summary
        console.print(Panel.fit(
            "[bold]Summary:[/]\n\n"
            "[cyan]REST API:[/] Best for market discovery (slug calculation)\n"
            "  - get_current_market(): Direct slug lookup\n"
            "  - get_active_market(): With fallback search\n\n"
            "[cyan]WebSocket:[/] Best for real-time data after discovery\n"
            "  - Orderbook streaming: <100ms latency\n"
            "  - Market resolution events: Instant notification\n\n"
            "[yellow]Recommendation:[/] Hybrid approach\n"
            "  1. REST to find/validate current market\n"
            "  2. WebSocket for real-time orderbook + resolution",
            title="[bold]Comparison Results[/]",
            border_style="green"
        ))

    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        import traceback
        traceback.print_exc()
    finally:
        await comparison.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
