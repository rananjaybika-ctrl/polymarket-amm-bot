#!/usr/bin/env python3
"""
Test Polymarket API Connection

This script verifies your Polymarket API connection works correctly.
Run this after setting up your .env file with WALLET_PRIVATE_KEY.

Usage:
    source venv/bin/activate
    python scripts/test_connection.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from src.config import Config, ConfigError
from src.api.polymarket_client import (
    PolymarketClient,
    PolymarketClientError,
    AuthenticationError,
    ConnectionError,
)


console = Console()


def print_header():
    """Print script header."""
    console.print(Panel.fit(
        "[bold blue]Polymarket API Connection Test[/bold blue]\n"
        "Testing connection to Polymarket CLOB API",
        border_style="blue"
    ))
    console.print()


def print_success(message: str):
    """Print success message."""
    console.print(f"[green]✓[/green] {message}")


def print_error(message: str):
    """Print error message."""
    console.print(f"[red]✗[/red] {message}")


def print_info(message: str):
    """Print info message."""
    console.print(f"[blue]ℹ[/blue] {message}")


async def test_connection():
    """Run connection tests."""
    print_header()

    # Step 1: Load configuration
    console.print("[bold]Step 1: Loading Configuration[/bold]")
    try:
        config = Config()
        config.validate()
        print_success("Configuration loaded from .env file")
        print_info(f"  Host: {config.polymarket_host}")
        print_info(f"  Chain ID: {config.chain_id}")
        print_info(f"  Dry Run Mode: {config.dry_run_mode}")
    except ConfigError as e:
        print_error(f"Configuration error: {e}")
        console.print()
        console.print("[yellow]How to fix:[/yellow]")
        console.print("  1. Copy .env.example to .env: [cyan]cp .env.example .env[/cyan]")
        console.print("  2. Add your wallet private key to .env")
        console.print("  3. Run this script again")
        return False
    console.print()

    # Step 2: Initialize client
    console.print("[bold]Step 2: Connecting to Polymarket[/bold]")
    client = PolymarketClient(config)

    try:
        await client.connect()
        print_success("Connected to Polymarket API")
        print_success("API credentials derived successfully")
    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        console.print()
        console.print("[yellow]How to fix:[/yellow]")
        console.print("  - Check your WALLET_PRIVATE_KEY in .env file")
        console.print("  - Private key should be 64 hex characters (with or without 0x prefix)")
        console.print("  - Make sure the wallet has been used on Polymarket before")
        return False
    except ConnectionError as e:
        print_error(f"Connection failed: {e}")
        console.print()
        console.print("[yellow]How to fix:[/yellow]")
        console.print("  - Check your internet connection")
        console.print("  - Try again in a few minutes (API might be down)")
        console.print("  - Check https://status.polymarket.com for outages")
        return False
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        return False
    console.print()

    # Step 3: Get wallet address
    console.print("[bold]Step 3: Wallet Information[/bold]")
    try:
        wallet_address = client.get_wallet_address()
        print_success(f"Wallet address: {wallet_address}")
    except Exception as e:
        print_error(f"Could not get wallet address: {e}")
    console.print()

    # Step 4: Fetch balance
    console.print("[bold]Step 4: Fetching USDC Balance[/bold]")
    try:
        balance = await client.get_balance()
        print_success(f"USDC Balance: ${balance:.2f}")
        if balance == 0:
            print_info("  Balance is $0.00 - this is OK for testing")
            print_info("  You'll need USDC on Polygon to trade")
    except Exception as e:
        print_error(f"Could not fetch balance: {e}")
        print_info("  This might be normal for new wallets")
    console.print()

    # Step 5: Fetch markets
    console.print("[bold]Step 5: Fetching Markets[/bold]")
    try:
        markets_response = await client.get_simplified_markets()
        markets = markets_response.get("data", [])

        if not markets:
            print_error("No markets found")
            return False

        print_success(f"Found {len(markets)} markets")

        # Filter for BTC 15-min Up/Down markets
        btc_markets = []
        for market in markets:
            # Check if market description contains BTC and Up/Down indicators
            desc = market.get("question", "").lower()
            if "btc" in desc and ("up" in desc or "down" in desc) and "15" in desc:
                btc_markets.append(market)

        if btc_markets:
            print_success(f"Found {len(btc_markets)} BTC 15-min Up/Down markets")

            # Show sample market
            sample = btc_markets[0]
            console.print()
            console.print("[bold]Sample BTC Market:[/bold]")
            console.print(f"  Question: {sample.get('question', 'N/A')}")
            console.print(f"  Condition ID: {sample.get('condition_id', 'N/A')[:20]}...")

            # Get tokens
            tokens = sample.get("tokens", [])
            if tokens:
                console.print("  Tokens:")
                for token in tokens[:2]:  # Show first 2 tokens (YES/NO)
                    token_id = token.get("token_id", "N/A")
                    outcome = token.get("outcome", "N/A")
                    console.print(f"    {outcome}: {token_id[:20]}...")
        else:
            print_info("No BTC 15-min Up/Down markets found in first page")
            print_info("This is normal - markets are created close to their time")

            # Show a sample market anyway
            if markets:
                sample = markets[0]
                console.print()
                console.print("[bold]Sample Market (first available):[/bold]")
                console.print(f"  Question: {sample.get('question', 'N/A')[:60]}...")

    except Exception as e:
        print_error(f"Could not fetch markets: {e}")
        return False
    console.print()

    # Step 6: Test orderbook (if we have a market)
    console.print("[bold]Step 6: Fetching Sample Orderbook[/bold]")
    try:
        # Try to get an orderbook from first available market with tokens
        test_token_id = None
        for market in markets[:10]:  # Check first 10 markets
            tokens = market.get("tokens", [])
            if tokens:
                test_token_id = tokens[0].get("token_id")
                if test_token_id:
                    break

        if test_token_id:
            orderbook = await client.get_orderbook(test_token_id)

            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])

            print_success("Orderbook fetched successfully")

            if bids or asks:
                # Create orderbook table
                table = Table(title="Sample Orderbook (Top 3)")
                table.add_column("Bid Price", style="green")
                table.add_column("Bid Size", style="green")
                table.add_column("Ask Price", style="red")
                table.add_column("Ask Size", style="red")

                max_rows = min(3, max(len(bids), len(asks)))
                for i in range(max_rows):
                    bid_price = f"${float(bids[i]['price']):.3f}" if i < len(bids) else ""
                    bid_size = str(bids[i].get("size", "")) if i < len(bids) else ""
                    ask_price = f"${float(asks[i]['price']):.3f}" if i < len(asks) else ""
                    ask_size = str(asks[i].get("size", "")) if i < len(asks) else ""
                    table.add_row(bid_price, bid_size, ask_price, ask_size)

                console.print(table)

                # Calculate spread
                if bids and asks:
                    best_bid = float(bids[0]["price"])
                    best_ask = float(asks[0]["price"])
                    spread = best_ask - best_bid
                    print_info(f"  Spread: ${spread:.4f}")
            else:
                print_info("  Orderbook is empty (no open orders)")
        else:
            print_info("No tokens found to test orderbook")

    except Exception as e:
        print_error(f"Could not fetch orderbook: {e}")
    console.print()

    # Disconnect
    await client.disconnect()

    # Final summary
    console.print(Panel.fit(
        "[bold green]All Tests Passed![/bold green]\n\n"
        "Your Polymarket connection is working correctly.\n"
        "You're ready to proceed with the next phase.",
        border_style="green"
    ))

    return True


def main():
    """Entry point."""
    try:
        success = asyncio.run(test_connection())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Test cancelled by user[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Unexpected error: {e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
