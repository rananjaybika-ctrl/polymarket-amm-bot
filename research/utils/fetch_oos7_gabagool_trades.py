#!/usr/bin/env python3
"""
Fetch gabagool's trades for OOS7 markets (Jan 29-30, 2026).

Uses the chart_generator.py fetch_trades() pattern to get trades from Polymarket API.
"""

import asyncio
import pandas as pd
import httpx
import json
from pathlib import Path
from datetime import datetime
import time

GABAGOOL_ADDRESS = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
TRADES_URL = "https://data-api.polymarket.com/trades"

OBSERVER_DIR = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")
OUTPUT_DIR = Path("/Users/rananjaybika/polymarket-amm-bot/research/findings/data")

# OOS7 dates
OOS7_FILES = [
    OBSERVER_DIR / "grid_obs_20260129.csv",
    OBSERVER_DIR / "grid_obs_20260130.csv",
]


def get_unique_markets(files):
    """Get unique market slugs from observer files."""
    all_slugs = set()
    for filepath in files:
        if filepath.exists():
            try:
                df = pd.read_csv(filepath, usecols=['market_slug'])
                all_slugs.update(df['market_slug'].unique())
            except Exception as e:
                print(f"Error reading {filepath.name}: {e}")
    return sorted(all_slugs)


async def get_condition_id(client: httpx.AsyncClient, slug: str) -> str:
    """Get condition ID for a market slug from Gamma API."""
    try:
        url = f"{GAMMA_API_URL}/events?slug={slug}"
        response = await client.get(url, timeout=15.0)
        response.raise_for_status()
        data = response.json()

        if not data:
            return None

        event = data[0]
        market = event.get("markets", [{}])[0]
        return market.get("conditionId")

    except Exception as e:
        print(f"  Error getting condition ID for {slug}: {e}")
        return None


def fetch_trades_sync(condition_id: str, user_address: str, page_limit: int = 5000):
    """Fetch all trades for a condition/user with pagination."""
    import requests

    all_trades = []
    offset = 0

    while True:
        params = {
            "limit": page_limit,
            "offset": offset,
            "takerOnly": "false",
            "market": condition_id,
            "user": user_address,
        }
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"    Error fetching trades: {e}")
            return []

        if isinstance(data, dict):
            batch = data.get("trades", [])
        elif isinstance(data, list):
            batch = data
        else:
            batch = []

        all_trades.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit

    return all_trades


async def main():
    print("=" * 70)
    print("FETCH GABAGOOL TRADES FOR OOS7 MARKETS")
    print("=" * 70)
    print(f"Gabagool address: {GABAGOOL_ADDRESS}")

    # Get unique markets from OOS7
    print("\nLoading OOS7 observer data...")
    slugs = get_unique_markets(OOS7_FILES)
    print(f"Found {len(slugs)} unique markets in OOS7 data")

    # Fetch condition IDs and trades
    all_trades = []
    market_trade_counts = {}

    async with httpx.AsyncClient() as client:
        for i, slug in enumerate(slugs):
            print(f"\n[{i+1}/{len(slugs)}] {slug}")

            # Get condition ID
            condition_id = await get_condition_id(client, slug)
            if not condition_id:
                print(f"  No condition ID found")
                continue

            print(f"  Condition ID: {condition_id[:20]}...")

            # Fetch gabagool's trades
            trades = fetch_trades_sync(condition_id, GABAGOOL_ADDRESS)

            if trades:
                # Add market slug to each trade
                for t in trades:
                    t['market_slug'] = slug
                    t['condition_id'] = condition_id

                all_trades.extend(trades)
                market_trade_counts[slug] = len(trades)
                print(f"  Found {len(trades)} trades")
            else:
                print(f"  No trades found")

            # Rate limiting
            await asyncio.sleep(0.3)

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total trades: {len(all_trades)}")
    print(f"Markets with trades: {len(market_trade_counts)}")

    if market_trade_counts:
        print("\nTrades per market:")
        for slug, count in sorted(market_trade_counts.items(), key=lambda x: -x[1])[:20]:
            print(f"  {slug}: {count}")

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / "gabagool_trades_oos7.json"

    result = {
        "gabagool_address": GABAGOOL_ADDRESS,
        "oos7_dates": ["20260129", "20260130"],
        "total_trades": len(all_trades),
        "markets_traded": len(market_trade_counts),
        "market_trade_counts": market_trade_counts,
        "trades": all_trades,
    }

    with open(output_file, 'w') as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved to: {output_file}")

    return result


if __name__ == "__main__":
    asyncio.run(main())
