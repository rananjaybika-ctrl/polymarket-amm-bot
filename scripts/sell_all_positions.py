#!/usr/bin/env python3
"""
Emergency Sell All Positions

Use this script when UI/Telegram emergency sell doesn't work.
Sells ALL positions at emergency price (0.01) to ensure fill.

Usage:
    # Dry-run (default) - shows what would be sold
    python scripts/sell_all_positions.py

    # Actually sell everything
    python scripts/sell_all_positions.py --execute

    # Sell at specific price (default 0.01)
    python scripts/sell_all_positions.py --execute --price 0.50
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import aiohttp
from src.config import Config
from src.api.polymarket_client import PolymarketClient


async def fetch_all_positions(wallet: str) -> list:
    """Fetch all positions from Gamma API."""
    url = f"https://gamma-api.polymarket.com/positions?user={wallet}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                print(f"Error fetching positions: HTTP {response.status}")
                return []
            return await response.json()


async def sell_all(execute: bool = False, sell_price: float = 0.01):
    """
    Sell all positions.

    Args:
        execute: If True, actually sell. If False, dry-run.
        sell_price: Price to sell at (default 0.01 for emergency fill)
    """
    print("=" * 60)
    print("EMERGENCY SELL ALL POSITIONS")
    print("=" * 60)

    # Initialize
    config = Config()
    client = PolymarketClient(config)

    print("Connecting to Polymarket...")
    connected = await client.connect()

    if not connected:
        print("ERROR: Failed to connect to Polymarket")
        return

    wallet = client.get_wallet_address()
    print(f"Wallet: {wallet}")

    # Get current balance
    balance = await client.get_balance()
    print(f"Current USDC Balance: ${balance:.2f}")

    # Fetch all positions
    print("\nFetching positions...")
    positions = await fetch_all_positions(wallet)

    if not positions:
        print("No positions found.")
        await client.disconnect()
        return

    # Filter positions with balance > 0
    active_positions = []
    for pos in positions:
        size = float(pos.get("size", 0))
        if size > 0:
            active_positions.append({
                "token_id": pos.get("asset") or pos.get("tokenId") or pos.get("token_id"),
                "size": size,
                "outcome": pos.get("outcome", "Unknown"),
                "market": pos.get("title", pos.get("slug", "Unknown")),
                "condition_id": pos.get("conditionId") or pos.get("condition_id", ""),
            })

    if not active_positions:
        print("No active positions to sell.")
        await client.disconnect()
        return

    # Display positions
    total_shares = sum(p["size"] for p in active_positions)
    print(f"\nFound {len(active_positions)} positions ({total_shares:.2f} total shares):")
    print("-" * 60)

    for i, pos in enumerate(active_positions, 1):
        market_display = pos["market"][:40] if len(pos["market"]) > 40 else pos["market"]
        print(f"  {i}. {pos['outcome']}: {pos['size']:.2f} shares")
        print(f"     Market: {market_display}")
        print(f"     Token: {pos['token_id'][:20]}...")

    print("-" * 60)
    print(f"Sell price: ${sell_price:.2f}")
    print(f"Estimated proceeds: ${total_shares * sell_price:.2f}")

    if not execute:
        print("\n[DRY-RUN] No orders placed. Use --execute to sell.")
        await client.disconnect()
        return

    # Confirm before executing
    print("\n" + "!" * 60)
    print("WARNING: About to sell ALL positions at emergency price!")
    print("!" * 60)
    confirm = input("Type 'SELL' to confirm: ").strip()

    if confirm != "SELL":
        print("Aborted.")
        await client.disconnect()
        return

    # Execute sells
    print("\nExecuting sells...")
    results = {"success": 0, "failed": 0, "errors": []}

    for pos in active_positions:
        token_id = pos["token_id"]
        size = pos["size"]

        if not token_id:
            print(f"  SKIP: No token ID for {pos['outcome']}")
            results["failed"] += 1
            results["errors"].append(f"No token ID: {pos['outcome']}")
            continue

        try:
            print(f"  Selling {size:.2f} {pos['outcome']}...", end=" ")

            result = await client.place_order(
                token_id=token_id,
                side="SELL",
                price=sell_price,
                size=size,
            )

            status = result.get("status", "unknown")
            if status in ["MATCHED", "FILLED", "LIVE", "live", "matched"]:
                print(f"SUCCESS ({status})")
                results["success"] += 1
            else:
                error_msg = result.get("errorMsg", status)
                print(f"FAILED: {error_msg}")
                results["failed"] += 1
                results["errors"].append(f"{pos['outcome']}: {error_msg}")

        except Exception as e:
            print(f"ERROR: {e}")
            results["failed"] += 1
            results["errors"].append(f"{pos['outcome']}: {str(e)}")

    # Final summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Success: {results['success']}")
    print(f"Failed: {results['failed']}")

    if results["errors"]:
        print("\nErrors:")
        for err in results["errors"]:
            print(f"  - {err}")

    # Check new balance
    await asyncio.sleep(2)  # Wait for orders to settle
    new_balance = await client.get_balance()
    print(f"\nNew USDC Balance: ${new_balance:.2f}")
    print(f"Change: ${new_balance - balance:+.2f}")

    await client.disconnect()
    print("\nDone.")


async def main():
    parser = argparse.ArgumentParser(
        description="Emergency sell all positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Dry-run (show what would be sold)
    python scripts/sell_all_positions.py

    # Actually sell everything at $0.01 (emergency)
    python scripts/sell_all_positions.py --execute

    # Sell at $0.50 (less aggressive)
    python scripts/sell_all_positions.py --execute --price 0.50
        """
    )

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute the sells (default is dry-run)",
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0.01,
        help="Sell price (default: 0.01 for emergency fill)",
    )

    args = parser.parse_args()

    await sell_all(execute=args.execute, sell_price=args.price)


if __name__ == "__main__":
    asyncio.run(main())
