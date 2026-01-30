#!/usr/bin/env python3
"""Fetch gabagool trades for new OOS7 markets."""

import asyncio
import json
import httpx
import requests
from pathlib import Path

GABAGOOL_ADDRESS = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
TRADES_URL = "https://data-api.polymarket.com/trades"

NEW_MARKETS = [
    "btc-updown-15m-1769766300",
    "btc-updown-15m-1769767200",
    "btc-updown-15m-1769768100",
    "btc-updown-15m-1769769000",
    "btc-updown-15m-1769769900",
    "btc-updown-15m-1769770800",
]

async def get_condition_id(client, slug):
    try:
        url = f"{GAMMA_API_URL}/events?slug={slug}"
        response = await client.get(url, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        if data:
            return data[0].get("markets", [{}])[0].get("conditionId")
    except Exception as e:
        print(f"  Error: {e}")
    return None

def fetch_trades_sync(condition_id, user_address):
    all_trades = []
    offset = 0
    while True:
        params = {"limit": 5000, "offset": offset, "takerOnly": "false",
                  "market": condition_id, "user": user_address}
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("trades", []) if isinstance(data, dict) else data if isinstance(data, list) else []
            all_trades.extend(batch)
            if len(batch) < 5000:
                break
            offset += 5000
        except Exception as e:
            print(f"    Error: {e}")
            break
    return all_trades

async def main():
    print("Fetching trades for new markets...")

    # Load existing
    existing_file = Path("research/findings/data/gabagool_trades_oos7.json")
    with open(existing_file) as f:
        existing = json.load(f)

    new_trades = []
    async with httpx.AsyncClient() as client:
        for slug in NEW_MARKETS:
            print(f"\n{slug}")
            condition_id = await get_condition_id(client, slug)
            if not condition_id:
                print("  No condition ID")
                continue

            trades = fetch_trades_sync(condition_id, GABAGOOL_ADDRESS)
            if trades:
                for t in trades:
                    t['market_slug'] = slug
                    t['condition_id'] = condition_id
                new_trades.extend(trades)
                existing['market_trade_counts'][slug] = len(trades)
                print(f"  Found {len(trades)} trades")
            else:
                print("  No trades")
            await asyncio.sleep(0.3)

    # Update existing data
    existing['trades'].extend(new_trades)
    existing['total_trades'] = len(existing['trades'])
    existing['markets_traded'] = len(existing['market_trade_counts'])

    with open(existing_file, 'w') as f:
        json.dump(existing, f, indent=2)

    print(f"\nAdded {len(new_trades)} new trades")
    print(f"Total: {existing['total_trades']} trades, {existing['markets_traded']} markets")

if __name__ == "__main__":
    asyncio.run(main())
