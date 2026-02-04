#!/usr/bin/env python3
"""
Latency Test Script for Polymarket AMM Bot

Run this on your AWS instance to measure actual network latencies.
Results should be used to configure paper trading's network_latency_ms.

Usage:
    python scripts/test_latency.py

Measures:
    1. Binance WebSocket latency (price feed)
    2. Polymarket CLOB REST API latency (order placement)
    3. Polymarket WebSocket latency (orderbook updates)
"""

import asyncio
import time
import statistics
import json
import aiohttp
import websockets
from typing import List, Tuple


async def test_binance_ws_latency(num_samples: int = 20) -> Tuple[float, float, float]:
    """
    Measure Binance WebSocket latency by comparing server timestamp to local time.

    Returns:
        Tuple of (avg_ms, min_ms, max_ms)
    """
    print("\n" + "=" * 60)
    print("BINANCE WEBSOCKET LATENCY TEST")
    print("=" * 60)

    latencies = []
    uri = "wss://stream.binance.com:9443/ws/btcusdt@trade"

    try:
        async with websockets.connect(uri) as ws:
            print(f"Connected to Binance WS, collecting {num_samples} samples...")

            # Drain initial buffered messages (wait for fresh data)
            print("  Draining buffer...")
            for _ in range(5):
                await asyncio.wait_for(ws.recv(), timeout=5.0)

            # Now measure fresh messages as they arrive
            # NO SLEEP between reads - we want the freshest message each time
            for i in range(num_samples):
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                local_time_ms = time.time() * 1000
                data = json.loads(msg)

                # Binance trade message has 'T' (trade time) and 'E' (event time)
                # 'E' = event time (when Binance processed), 'T' = trade time
                server_time_ms = data.get('E') or data.get('T')
                if server_time_ms:
                    latency_ms = local_time_ms - server_time_ms
                    latencies.append(latency_ms)
                    print(f"  Sample {i+1}/{num_samples}: {latency_ms:.1f}ms")

                # NO SLEEP - read messages as fast as they arrive
                # Sleeping causes buffer buildup and stale timestamps

    except Exception as e:
        print(f"Error: {e}")
        return (0, 0, 0)

    if latencies:
        avg = statistics.mean(latencies)
        min_lat = min(latencies)
        max_lat = max(latencies)
        std = statistics.stdev(latencies) if len(latencies) > 1 else 0

        print(f"\nBinance WS Results:")
        print(f"  Average: {avg:.1f}ms")
        print(f"  Min: {min_lat:.1f}ms")
        print(f"  Max: {max_lat:.1f}ms")
        print(f"  Std Dev: {std:.1f}ms")

        return (avg, min_lat, max_lat)

    return (0, 0, 0)


async def test_polymarket_rest_latency(num_samples: int = 10) -> Tuple[float, float, float]:
    """
    Measure Polymarket CLOB REST API latency.

    Returns:
        Tuple of (avg_ms, min_ms, max_ms)
    """
    print("\n" + "=" * 60)
    print("POLYMARKET CLOB REST API LATENCY TEST")
    print("=" * 60)

    latencies = []

    # Test endpoints
    endpoints = [
        ("Health", "https://clob.polymarket.com/health"),
        ("Markets", "https://clob.polymarket.com/markets"),
    ]

    async with aiohttp.ClientSession() as session:
        for name, url in endpoints:
            print(f"\nTesting {name} endpoint: {url}")

            for i in range(num_samples):
                start = time.time()
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        await resp.read()
                        latency_ms = (time.time() - start) * 1000
                        latencies.append(latency_ms)
                        print(f"  Sample {i+1}/{num_samples}: {latency_ms:.1f}ms (status={resp.status})")
                except Exception as e:
                    print(f"  Sample {i+1}/{num_samples}: ERROR - {e}")

                await asyncio.sleep(0.2)  # Respect rate limits

    if latencies:
        avg = statistics.mean(latencies)
        min_lat = min(latencies)
        max_lat = max(latencies)
        std = statistics.stdev(latencies) if len(latencies) > 1 else 0

        print(f"\nPolymarket REST Results:")
        print(f"  Average: {avg:.1f}ms")
        print(f"  Min: {min_lat:.1f}ms")
        print(f"  Max: {max_lat:.1f}ms")
        print(f"  Std Dev: {std:.1f}ms")

        return (avg, min_lat, max_lat)

    return (0, 0, 0)


async def test_polymarket_ws_latency(num_samples: int = 10) -> Tuple[float, float, float]:
    """
    Measure Polymarket WebSocket connection + first message latency.

    Returns:
        Tuple of (avg_ms, min_ms, max_ms)
    """
    print("\n" + "=" * 60)
    print("POLYMARKET WEBSOCKET LATENCY TEST")
    print("=" * 60)

    latencies = []
    uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    for i in range(num_samples):
        start = time.time()
        try:
            async with websockets.connect(uri, ping_interval=None) as ws:
                # Send a subscription message
                sub_msg = {
                    "type": "market",
                    "assets_ids": []  # Empty just to test connection
                }
                await ws.send(json.dumps(sub_msg))

                # Wait for response
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    latency_ms = (time.time() - start) * 1000
                    latencies.append(latency_ms)
                    print(f"  Sample {i+1}/{num_samples}: {latency_ms:.1f}ms (connect + first msg)")
                except asyncio.TimeoutError:
                    latency_ms = (time.time() - start) * 1000
                    latencies.append(latency_ms)
                    print(f"  Sample {i+1}/{num_samples}: {latency_ms:.1f}ms (connect only, no response)")

        except Exception as e:
            print(f"  Sample {i+1}/{num_samples}: ERROR - {e}")

        await asyncio.sleep(0.5)  # Don't spam

    if latencies:
        avg = statistics.mean(latencies)
        min_lat = min(latencies)
        max_lat = max(latencies)
        std = statistics.stdev(latencies) if len(latencies) > 1 else 0

        print(f"\nPolymarket WS Results:")
        print(f"  Average: {avg:.1f}ms")
        print(f"  Min: {min_lat:.1f}ms")
        print(f"  Max: {max_lat:.1f}ms")
        print(f"  Std Dev: {std:.1f}ms")

        return (avg, min_lat, max_lat)

    return (0, 0, 0)


async def main():
    print("\n" + "#" * 60)
    print("# POLYMARKET AMM BOT - LATENCY TEST")
    print("# Run this on your AWS instance for accurate measurements")
    print("#" * 60)

    # Run all tests
    binance_latency = await test_binance_ws_latency(num_samples=20)
    polymarket_rest_latency = await test_polymarket_rest_latency(num_samples=10)
    polymarket_ws_latency = await test_polymarket_ws_latency(num_samples=5)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"\nBinance WebSocket (price feed):")
    print(f"  {binance_latency[0]:.1f}ms avg ({binance_latency[1]:.1f}-{binance_latency[2]:.1f}ms range)")

    print(f"\nPolymarket REST API (order placement):")
    print(f"  {polymarket_rest_latency[0]:.1f}ms avg ({polymarket_rest_latency[1]:.1f}-{polymarket_rest_latency[2]:.1f}ms range)")

    print(f"\nPolymarket WebSocket (orderbook):")
    print(f"  {polymarket_ws_latency[0]:.1f}ms avg ({polymarket_ws_latency[1]:.1f}-{polymarket_ws_latency[2]:.1f}ms range)")

    # Recommendation
    print("\n" + "=" * 60)
    print("RECOMMENDATION FOR PAPER TRADING")
    print("=" * 60)

    # Use Polymarket REST latency as the basis (order placement latency)
    recommended_latency = polymarket_rest_latency[0] if polymarket_rest_latency[0] > 0 else 100

    print(f"""
Based on measurements, set network_latency_ms in paper trading:

    # In src/services/paper_trading.py or passed via config:
    network_latency_ms = {recommended_latency:.0f}  # ms

This represents your AWS→Polymarket round-trip for order placement.
The 500ms Polymarket taker delay is ADDED to this automatically.

Total taker delay = 500ms (exchange) + {recommended_latency:.0f}ms (network) = {500 + recommended_latency:.0f}ms
""")

    print("\nNote: Binance latency affects spike detection freshness.")
    print(f"Your Binance feed has ~{binance_latency[0]:.0f}ms latency from exchange.")


if __name__ == "__main__":
    asyncio.run(main())
