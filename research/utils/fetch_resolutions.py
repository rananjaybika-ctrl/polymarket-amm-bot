#!/usr/bin/env python3
"""
Fetch resolutions for all markets in observer data.

Queries the Polymarket Gamma API to get verified resolution data
for markets that have already ended.

Usage:
    python research/fetch_resolutions.py
"""

import asyncio
import pandas as pd
import httpx
from pathlib import Path
from datetime import datetime, timezone
import time


GAMMA_API_URL = "https://gamma-api.polymarket.com"
OBSERVER_DIR = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")
OUTPUT_FILE = OBSERVER_DIR / "market_resolutions_verified.csv"


async def get_market_resolution(client: httpx.AsyncClient, slug: str) -> dict:
    """
    Get the resolution for a market from Gamma API.

    Returns dict with: slug, winner, closed, resolved, up_price, down_price
    """
    import json

    try:
        url = f"{GAMMA_API_URL}/events?slug={slug}"
        response = await client.get(url, timeout=15.0)
        response.raise_for_status()
        data = response.json()

        if not data:
            return {"slug": slug, "winner": "NOT_FOUND", "closed": False, "resolved": False}

        event = data[0]
        market = event.get("markets", [{}])[0]

        closed = market.get("closed", False)
        # Check umaResolutionStatus instead of resolved field
        uma_status = market.get("umaResolutionStatus", "")
        resolved = uma_status == "resolved"

        # Get outcome prices - it's a JSON string, need to parse it
        outcome_prices_raw = market.get("outcomePrices", "[]")
        try:
            if isinstance(outcome_prices_raw, str):
                outcome_prices = json.loads(outcome_prices_raw)
            else:
                outcome_prices = outcome_prices_raw
        except json.JSONDecodeError:
            outcome_prices = []

        up_price = None
        down_price = None
        winner = None

        if outcome_prices and len(outcome_prices) >= 2:
            try:
                up_price = float(outcome_prices[0])
                down_price = float(outcome_prices[1])

                # Determine winner from prices
                if up_price >= 0.99:
                    winner = "UP"
                elif down_price >= 0.99:
                    winner = "DOWN"
                elif closed and resolved:
                    # Market is resolved but no clear winner at 0.99
                    winner = "UP" if up_price > down_price else "DOWN"
                else:
                    winner = "PENDING"
            except (ValueError, TypeError):
                pass

        return {
            "slug": slug,
            "winner": winner or "UNKNOWN",
            "closed": closed,
            "resolved": resolved,
            "uma_status": uma_status,
            "up_price": up_price,
            "down_price": down_price,
        }

    except httpx.TimeoutException:
        print(f"  Timeout: {slug}")
        return {"slug": slug, "winner": "TIMEOUT", "closed": False, "resolved": False}
    except Exception as e:
        print(f"  Error {slug}: {e}")
        return {"slug": slug, "winner": "ERROR", "closed": False, "resolved": False}


async def main():
    print("=" * 70)
    print("FETCH MARKET RESOLUTIONS FROM POLYMARKET API")
    print("=" * 70)

    # Get unique markets from observer data
    csv_files = sorted(OBSERVER_DIR.glob("grid_obs_*.csv"))
    csv_files.extend(sorted(OBSERVER_DIR.glob("spread_capture_obs_*.csv")))

    all_slugs = set()
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, usecols=['market_slug'])
            all_slugs.update(df['market_slug'].unique())
        except Exception as e:
            print(f"Error reading {filepath.name}: {e}")

    slugs = sorted(all_slugs)
    print(f"\nFound {len(slugs)} unique markets in observer data")

    # Query resolutions
    print(f"\nQuerying Polymarket API for resolutions...")

    results = []
    async with httpx.AsyncClient() as client:
        for i, slug in enumerate(slugs):
            result = await get_market_resolution(client, slug)
            results.append(result)

            status = result['winner']
            if status in ('UP', 'DOWN'):
                print(f"  [{i+1}/{len(slugs)}] {slug} -> {status}")
            else:
                print(f"  [{i+1}/{len(slugs)}] {slug} -> {status} (closed={result['closed']}, resolved={result['resolved']})")

            # Rate limiting
            await asyncio.sleep(0.2)

    # Convert to DataFrame
    df = pd.DataFrame(results)

    # Summary
    print(f"\n{'=' * 70}")
    print("RESOLUTION SUMMARY")
    print(f"{'=' * 70}")

    winner_counts = df['winner'].value_counts()
    for winner, count in winner_counts.items():
        pct = count / len(df) * 100
        print(f"  {winner:12}: {count:3} ({pct:5.1f}%)")

    verified = df[df['winner'].isin(['UP', 'DOWN'])]
    print(f"\n  VERIFIED resolutions: {len(verified)} / {len(df)} ({len(verified)/len(df)*100:.1f}%)")

    # Save results
    df.to_csv(OUTPUT_FILE, index=False)
    print(f"\n  Saved to: {OUTPUT_FILE}")

    # Also update the main resolution file with verified resolutions only
    if len(verified) > 0:
        # Format for backtest scripts
        backtest_format = verified[['slug', 'winner', 'up_price', 'down_price']].copy()
        backtest_format.columns = ['market', 'winner', 'up_final', 'down_final']

        # Append to existing or overwrite
        main_file = OBSERVER_DIR / "market_resolutions.csv"

        # Read existing
        existing = pd.DataFrame()
        if main_file.exists():
            try:
                existing = pd.read_csv(main_file)
            except:
                pass

        # Merge (keep new over old)
        if not existing.empty:
            existing = existing[~existing['market'].isin(backtest_format['market'])]
            combined = pd.concat([existing, backtest_format], ignore_index=True)
        else:
            combined = backtest_format

        combined.to_csv(main_file, index=False)
        print(f"  Updated: {main_file} ({len(combined)} total resolutions)")

    return df


if __name__ == "__main__":
    asyncio.run(main())
