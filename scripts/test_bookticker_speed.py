#!/usr/bin/env python3
"""
Test Binance @bookTicker vs @trade Stream Speed

Measures:
1. Update frequency (updates/sec) for both streams
2. Latency and any rate limiting
3. Price accuracy comparison

Expected:
- @trade: ~5 updates/sec (200ms between ticks)
- @bookTicker: ~20-50 updates/sec (20-50ms between ticks)

Usage:
    python scripts/test_bookticker_speed.py
    python scripts/test_bookticker_speed.py --duration 30
"""

import asyncio
import sys
import os
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient


async def test_stream_speed(use_book_ticker: bool, duration_secs: int = 10) -> dict:
    """Test a single stream's speed and collect metrics."""
    stream_name = "@bookTicker" if use_book_ticker else "@trade"
    print(f"\nTesting {stream_name} stream for {duration_secs} seconds...")

    client = BinanceClient(window_seconds=60, use_book_ticker=use_book_ticker)
    await client.connect()

    # Wait for connection
    for _ in range(20):
        if client.current_price > 0:
            break
        await asyncio.sleep(0.1)

    if client.current_price <= 0:
        print(f"  ERROR: Could not connect to {stream_name}")
        await client.disconnect()
        return {"error": "Connection failed"}

    print(f"  Connected! Initial price: ${client.current_price:,.2f}")

    # Collect metrics
    start_time = time.time()
    start_count = len(client._price_history)
    prices = []
    timestamps = []

    while time.time() - start_time < duration_secs:
        current_count = len(client._price_history)
        if current_count > len(prices):
            prices.append(client.current_price)
            timestamps.append(time.time())
        await asyncio.sleep(0.01)  # 10ms polling

    end_time = time.time()
    end_count = len(client._price_history)
    actual_duration = end_time - start_time

    await client.disconnect()

    # Calculate metrics
    total_updates = end_count - start_count
    updates_per_sec = total_updates / actual_duration

    # Calculate inter-update intervals
    intervals = []
    if len(timestamps) > 1:
        for i in range(1, len(timestamps)):
            intervals.append((timestamps[i] - timestamps[i-1]) * 1000)  # Convert to ms

    avg_interval = sum(intervals) / len(intervals) if intervals else 0
    min_interval = min(intervals) if intervals else 0
    max_interval = max(intervals) if intervals else 0

    # Price range
    price_range = max(prices) - min(prices) if prices else 0

    results = {
        "stream": stream_name,
        "duration_secs": actual_duration,
        "total_updates": total_updates,
        "updates_per_sec": updates_per_sec,
        "avg_interval_ms": avg_interval,
        "min_interval_ms": min_interval,
        "max_interval_ms": max_interval,
        "price_samples": len(prices),
        "price_range": price_range,
    }

    print(f"  Total updates: {total_updates}")
    print(f"  Updates/sec: {updates_per_sec:.1f}")
    print(f"  Avg interval: {avg_interval:.1f}ms")
    print(f"  Min interval: {min_interval:.1f}ms")
    print(f"  Max interval: {max_interval:.1f}ms")

    return results


async def test_parallel_streams(duration_secs: int = 10):
    """Test both streams in parallel to check for rate limiting."""
    print(f"\nTesting PARALLEL streams for {duration_secs} seconds...")
    print("This checks if running both streams causes rate limiting...")

    # Create both clients
    client_trade = BinanceClient(window_seconds=60, use_book_ticker=False)
    client_book = BinanceClient(window_seconds=60, use_book_ticker=True)

    # Connect both
    await client_trade.connect()
    await client_book.connect()

    # Wait for both to connect
    for _ in range(20):
        if client_trade.current_price > 0 and client_book.current_price > 0:
            break
        await asyncio.sleep(0.1)

    if client_trade.current_price <= 0 or client_book.current_price <= 0:
        print("  ERROR: Could not connect both streams")
        await client_trade.disconnect()
        await client_book.disconnect()
        return None

    print(f"  Both connected!")
    print(f"  @trade price: ${client_trade.current_price:,.2f}")
    print(f"  @bookTicker price: ${client_book.current_price:,.2f}")

    # Collect metrics
    start_time = time.time()
    trade_start = len(client_trade._price_history)
    book_start = len(client_book._price_history)

    await asyncio.sleep(duration_secs)

    trade_end = len(client_trade._price_history)
    book_end = len(client_book._price_history)
    end_time = time.time()
    actual_duration = end_time - start_time

    await client_trade.disconnect()
    await client_book.disconnect()

    trade_updates = trade_end - trade_start
    book_updates = book_end - book_start

    trade_rate = trade_updates / actual_duration
    book_rate = book_updates / actual_duration

    print(f"\n  Parallel Results:")
    print(f"    @trade: {trade_updates} updates ({trade_rate:.1f}/sec)")
    print(f"    @bookTicker: {book_updates} updates ({book_rate:.1f}/sec)")
    print(f"    Speed ratio: {book_rate/trade_rate:.1f}x faster" if trade_rate > 0 else "")

    # Check for rate limiting
    if trade_rate < 3:
        print(f"  WARNING: @trade rate seems low ({trade_rate:.1f}/sec), possible rate limiting")
    if book_rate < 10:
        print(f"  WARNING: @bookTicker rate seems low ({book_rate:.1f}/sec), possible rate limiting")

    return {
        "trade_updates": trade_updates,
        "trade_rate": trade_rate,
        "book_updates": book_updates,
        "book_rate": book_rate,
        "speed_ratio": book_rate / trade_rate if trade_rate > 0 else 0,
    }


async def main():
    parser = argparse.ArgumentParser(description="Test Binance stream speeds")
    parser.add_argument('--duration', type=int, default=10, help='Test duration in seconds')
    parser.add_argument('--parallel', action='store_true', help='Test parallel streams')
    args = parser.parse_args()

    print("=" * 70)
    print("BINANCE WEBSOCKET SPEED TEST")
    print("=" * 70)
    print(f"Test duration: {args.duration} seconds per stream")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Test @trade stream
    trade_results = await test_stream_speed(use_book_ticker=False, duration_secs=args.duration)

    # Test @bookTicker stream
    book_results = await test_stream_speed(use_book_ticker=True, duration_secs=args.duration)

    # Compare
    print("\n" + "=" * 70)
    print("COMPARISON")
    print("=" * 70)

    if "error" not in trade_results and "error" not in book_results:
        trade_rate = trade_results["updates_per_sec"]
        book_rate = book_results["updates_per_sec"]
        speed_ratio = book_rate / trade_rate if trade_rate > 0 else 0

        print(f"\n  @trade:      {trade_rate:.1f} updates/sec (avg {trade_results['avg_interval_ms']:.0f}ms interval)")
        print(f"  @bookTicker: {book_rate:.1f} updates/sec (avg {book_results['avg_interval_ms']:.0f}ms interval)")
        print(f"\n  Speed improvement: {speed_ratio:.1f}x faster with @bookTicker")

        # Calculate spike detection improvement
        trade_spike_time = 3 * (1000 / trade_rate) if trade_rate > 0 else 0  # 3 ticks in ms
        book_spike_time = 3 * (1000 / book_rate) if book_rate > 0 else 0

        print(f"\n  Spike Detection (3-tick):")
        print(f"    @trade:      {trade_spike_time:.0f}ms")
        print(f"    @bookTicker: {book_spike_time:.0f}ms")
        print(f"    Improvement: {trade_spike_time - book_spike_time:.0f}ms faster")

    # Test parallel if requested
    if args.parallel:
        parallel_results = await test_parallel_streams(args.duration)

        if parallel_results:
            print("\n" + "=" * 70)
            print("PARALLEL STREAM TEST (Rate Limiting Check)")
            print("=" * 70)

            # Compare parallel vs sequential rates
            if "error" not in trade_results:
                seq_trade = trade_results["updates_per_sec"]
                par_trade = parallel_results["trade_rate"]
                trade_drop = (seq_trade - par_trade) / seq_trade * 100 if seq_trade > 0 else 0

                seq_book = book_results["updates_per_sec"]
                par_book = parallel_results["book_rate"]
                book_drop = (seq_book - par_book) / seq_book * 100 if seq_book > 0 else 0

                print(f"\n  Rate comparison (sequential vs parallel):")
                print(f"    @trade:      {seq_trade:.1f}/sec -> {par_trade:.1f}/sec ({trade_drop:+.1f}% change)")
                print(f"    @bookTicker: {seq_book:.1f}/sec -> {par_book:.1f}/sec ({book_drop:+.1f}% change)")

                if abs(trade_drop) > 20 or abs(book_drop) > 20:
                    print(f"\n  WARNING: Significant rate drop in parallel mode - possible rate limiting!")
                else:
                    print(f"\n  OK: No significant rate limiting detected in parallel mode")

    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
