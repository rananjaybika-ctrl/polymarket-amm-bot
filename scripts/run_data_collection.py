#!/usr/bin/env python3
"""
Data Collection Wrapper - Runs Observer + Price Logger Together

Automatically starts both:
1. Observer (Polymarket orderbook + velocity at 5Hz)
2. Binance Price Logger (BTC prices at 60Hz)

Both run for the same duration and stop together.

Usage:
    python scripts/run_data_collection.py --hours 6
    python scripts/run_data_collection.py --until 21:30  # Stop at 9:30 PM local
    python scripts/run_data_collection.py --continuous   # Run until Ctrl+C
"""

import asyncio
import argparse
import signal
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.observer import SpreadCaptureObserver
from scripts.binance_price_logger import BinancePriceLogger


class DataCollectionManager:
    """Manages concurrent observer and price logger."""

    def __init__(self, output_dir: str = "research"):
        self.output_dir = Path(output_dir)
        self.observer_dir = self.output_dir / "observer"
        self.binance_dir = self.output_dir / "binance_hf"

        # Create directories
        self.observer_dir.mkdir(parents=True, exist_ok=True)
        self.binance_dir.mkdir(parents=True, exist_ok=True)

        self.observer = None
        self.price_logger = None
        self.running = False

    async def run(self, duration_hours: float = None, until_time: str = None):
        """
        Run both data collectors.

        Args:
            duration_hours: Run for this many hours
            until_time: Run until this time (HH:MM format, local time)
        """
        self.running = True

        # Calculate end time
        end_time = None
        if until_time:
            today = datetime.now().date()
            hour, minute = map(int, until_time.split(':'))
            end_time = datetime.combine(today, datetime.min.time().replace(hour=hour, minute=minute))
            if end_time <= datetime.now():
                end_time += timedelta(days=1)  # Tomorrow if time already passed
            duration_secs = (end_time - datetime.now()).total_seconds()
            duration_hours = duration_secs / 3600
            print(f"Will run until {end_time.strftime('%Y-%m-%d %H:%M')} ({duration_hours:.1f} hours)")
        elif duration_hours:
            end_time = datetime.now() + timedelta(hours=duration_hours)
            print(f"Will run for {duration_hours} hours (until {end_time.strftime('%H:%M')})")
        else:
            print("Running continuously (Ctrl+C to stop)")

        # Initialize collectors
        # Observer takes duration/continuous in __init__, not run()
        self.observer = SpreadCaptureObserver(
            duration_hours=duration_hours or 12.0,
            continuous=duration_hours is None,
            starting_balance=170.0,
            trade_size=5.0,
            output_dir=str(self.observer_dir),
        )
        self.price_logger = BinancePriceLogger(output_dir=str(self.binance_dir))

        print("=" * 60)
        print("DATA COLLECTION MANAGER")
        print("=" * 60)
        print(f"Observer output:      {self.observer_dir}/")
        print(f"Price logger output:  {self.binance_dir}/")
        print()

        # Run both concurrently
        try:
            await asyncio.gather(
                self._run_observer(duration_hours),
                self._run_price_logger(duration_hours),
            )
        except asyncio.CancelledError:
            print("\nShutting down...")
        finally:
            self.stop()

    async def _run_observer(self, duration_hours: float = None):
        """Run observer with duration."""
        try:
            # Observer uses instance attributes set in __init__
            await self.observer.run()
        except Exception as e:
            print(f"Observer error: {e}")

    async def _run_price_logger(self, duration_hours: float = None):
        """Run price logger with duration."""
        try:
            await self.price_logger.run(duration_hours=duration_hours)
        except Exception as e:
            print(f"Price logger error: {e}")

    def stop(self):
        """Stop both collectors gracefully."""
        self.running = False
        if self.observer:
            self.observer.stop()
        if self.price_logger:
            self.price_logger.stop()
        print("\nBoth collectors stopped.")


async def main():
    parser = argparse.ArgumentParser(description="Run Observer + Price Logger together")
    parser.add_argument('--hours', type=float, default=None,
                        help='Duration in hours')
    parser.add_argument('--until', type=str, default=None,
                        help='Run until time (HH:MM format, e.g., 21:30)')
    parser.add_argument('--continuous', action='store_true',
                        help='Run until manually stopped')
    parser.add_argument('--output', type=str, default='research',
                        help='Output base directory')
    args = parser.parse_args()

    manager = DataCollectionManager(output_dir=args.output)

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\nStopping...")
        manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Determine duration
    duration = args.hours
    until = args.until

    if args.continuous:
        duration = None
        until = None
    elif not duration and not until:
        # Default: run for 1 hour
        duration = 1.0

    await manager.run(duration_hours=duration, until_time=until)


if __name__ == "__main__":
    asyncio.run(main())
