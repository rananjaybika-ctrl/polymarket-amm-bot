#!/usr/bin/env python3
"""
High-Frequency Binance Price Logger

Captures every @bookTicker update (~183/sec) for accurate spike detection backtesting.

Output: research/binance_hf/btc_prices_YYYYMMDD_HHMMSS.csv
Format: timestamp_ms, price, bid, ask

Usage:
    python scripts/binance_price_logger.py
    python scripts/binance_price_logger.py --duration 4  # Run for 4 hours
"""

import asyncio
import csv
import os
import sys
import time
import signal
from datetime import datetime
from pathlib import Path
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets
import json


class BinancePriceLogger:
    """High-frequency Binance BTC price logger."""

    WEBSOCKET_URL = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"

    def __init__(self, output_dir: str = "research/binance_hf"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.running = False
        self.csv_file = None
        self.csv_writer = None
        self.count = 0
        self.start_time = None
        self.last_status = 0

    def _init_csv(self):
        """Initialize CSV file for logging."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = self.output_dir / f"btc_prices_{timestamp}.csv"

        self.csv_file = open(filepath, 'w', newline='', buffering=1)  # Line buffered
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(['timestamp_ms', 'price', 'bid', 'ask'])

        print(f"Logging to: {filepath}")
        return filepath

    async def run(self, duration_hours: float = None):
        """Run the price logger with proper error handling and cleanup."""
        self.running = True
        self.start_time = time.time()
        self.last_status = self.start_time  # Reset status timer
        self.count = 0
        timeout_count = 0  # Track consecutive timeouts

        filepath = self._init_csv()

        try:
            end_time = None
            if duration_hours:
                end_time = self.start_time + (duration_hours * 3600)
                print(f"Will run for {duration_hours} hours")
            else:
                print("Running indefinitely (Ctrl+C to stop)")

            print(f"Connecting to Binance @bookTicker stream...")

            reconnect_delay = 1.0

            while self.running:
                try:
                    async with websockets.connect(
                        self.WEBSOCKET_URL,
                        ping_interval=20,
                        ping_timeout=10,
                        close_timeout=5,
                    ) as ws:
                        print(f"Connected! Logging prices...")
                        reconnect_delay = 1.0  # Reset on successful connect
                        timeout_count = 0  # Reset timeout counter

                        while self.running:
                            # Check duration
                            if end_time and time.time() >= end_time:
                                print("\nDuration reached.")
                                self.running = False
                                break

                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                                timeout_count = 0  # Reset on successful receive

                                # Parse JSON with error handling
                                try:
                                    data = json.loads(msg)
                                except json.JSONDecodeError as e:
                                    print(f"Invalid JSON: {msg[:50]}... - {e}")
                                    continue

                                # Extract prices with validation
                                try:
                                    bid = float(data.get('b', 0))
                                    ask = float(data.get('a', 0))
                                except (ValueError, TypeError) as e:
                                    print(f"Invalid price data: {e}")
                                    continue

                                if bid <= 0 or ask <= 0:
                                    continue  # Skip invalid prices

                                mid = (bid + ask) / 2

                                # Log with millisecond timestamp
                                ts_ms = int(time.time() * 1000)
                                self.csv_writer.writerow([ts_ms, f"{mid:.2f}", f"{bid:.2f}", f"{ask:.2f}"])
                                self.count += 1

                                # Status update every 10 seconds
                                now = time.time()
                                if now - self.last_status >= 10:
                                    elapsed = now - self.start_time
                                    rate = self.count / elapsed if elapsed > 0 else 0
                                    print(f"  [{elapsed/60:.1f}m] {self.count:,} prices logged ({rate:.1f}/sec)")
                                    self.last_status = now
                                    self.csv_file.flush()

                            except asyncio.TimeoutError:
                                timeout_count += 1
                                if timeout_count > 5:
                                    print(f"Multiple timeouts ({timeout_count}), reconnecting...")
                                    break  # Trigger reconnect
                                continue

                except websockets.ConnectionClosed as e:
                    if not self.running:
                        break
                    print(f"Connection closed: {e}. Reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 30.0)
                except Exception as e:
                    if not self.running:
                        break
                    print(f"Unexpected error: {e}. Reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 30.0)

        finally:
            # Ensure proper cleanup regardless of how we exit
            if self.csv_file and not self.csv_file.closed:
                self.csv_file.flush()
                self.csv_file.close()

            # Final stats
            elapsed = time.time() - self.start_time if self.start_time else 0
            rate = self.count / elapsed if elapsed > 0 else 0

            print(f"\n{'='*60}")
            print(f"LOGGING COMPLETE")
            print(f"{'='*60}")
            print(f"  Duration: {elapsed/60:.1f} minutes")
            print(f"  Total prices: {self.count:,}")
            print(f"  Average rate: {rate:.1f}/sec")
            print(f"  File: {filepath}")
            try:
                print(f"  Size: {filepath.stat().st_size / 1024 / 1024:.2f} MB")
            except (OSError, FileNotFoundError):
                print(f"  Size: (unable to stat file)")

    def stop(self):
        """Stop the logger gracefully with flush."""
        self.running = False
        # Flush is handled in run()'s finally block


async def main():
    parser = argparse.ArgumentParser(description="High-frequency Binance price logger")
    parser.add_argument('--duration', type=float, default=None,
                        help='Duration in hours (default: run indefinitely)')
    parser.add_argument('--output', type=str, default='research/binance_hf',
                        help='Output directory')
    args = parser.parse_args()

    logger = BinancePriceLogger(output_dir=args.output)

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\nStopping...")
        logger.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 60)
    print("BINANCE HIGH-FREQUENCY PRICE LOGGER")
    print("=" * 60)
    print(f"Stream: @bookTicker (~183 updates/sec)")
    print(f"Output: {args.output}/")
    print()

    await logger.run(duration_hours=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
