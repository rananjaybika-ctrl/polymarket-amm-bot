#!/usr/bin/env python3
"""
Data Collection Wrapper - Runs Observer + Price Logger Together

Automatically starts both:
1. Observer (Polymarket orderbook + velocity at 5Hz)
2. Binance Price Logger (BTC prices at 60Hz)

Both run for the same duration and stop together.

Features:
- Health monitoring: Detects if observer stops writing data
- Error handling: Logs errors without silent failures
- Optional auto-restart: Disabled by default, enable with --auto-restart
- Graceful shutdown: Handles SIGINT/SIGTERM properly

Usage:
    python scripts/run_data_collection.py --hours 6
    python scripts/run_data_collection.py --until 21:30  # Stop at 9:30 PM local
    python scripts/run_data_collection.py --continuous   # Run until Ctrl+C
    python scripts/run_data_collection.py --hours 24 --auto-restart  # With auto-restart
"""

import asyncio
import argparse
import signal
import sys
import os
import time
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.observer import SpreadCaptureObserver
from scripts.binance_price_logger import BinancePriceLogger

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Health check constants
HEALTH_CHECK_INTERVAL_SECS = 30  # Check health every 30 seconds
HEALTH_WARNING_THRESHOLD_SECS = 60  # Warn if no writes for 60 seconds
HEALTH_ERROR_THRESHOLD_SECS = 120  # Error if no writes for 120 seconds
RESTART_BACKOFF_SECS = 5  # Wait 5 seconds before restart attempt
MAX_RESTART_ATTEMPTS = 3  # Maximum restart attempts before giving up


class DataCollectionManager:
    """Manages concurrent observer and price logger with health monitoring."""

    def __init__(self, output_dir: str = "research", auto_restart: bool = False):
        self.output_dir = Path(output_dir)
        self.observer_dir = self.output_dir / "observer"
        self.binance_dir = self.output_dir / "binance_hf"

        # Create directories
        self.observer_dir.mkdir(parents=True, exist_ok=True)
        self.binance_dir.mkdir(parents=True, exist_ok=True)

        self.observer = None
        self.price_logger = None
        self.running = False
        self.auto_restart = auto_restart

        # Health tracking
        self._observer_last_sample_count = 0
        self._observer_last_check_time = 0
        self._observer_restart_attempts = 0
        self._observer_task = None
        self._price_logger_task = None
        self._health_monitor_task = None

        # Duration tracking for restarts
        self._duration_hours = None
        self._end_time = None

    async def run(self, duration_hours: float = None, until_time: str = None):
        """
        Run both data collectors with health monitoring.

        Args:
            duration_hours: Run for this many hours
            until_time: Run until this time (HH:MM format, local time)
        """
        self.running = True
        self._duration_hours = duration_hours

        # Calculate end time
        if until_time:
            today = datetime.now().date()
            hour, minute = map(int, until_time.split(':'))
            self._end_time = datetime.combine(today, datetime.min.time().replace(hour=hour, minute=minute))
            if self._end_time <= datetime.now():
                self._end_time += timedelta(days=1)  # Tomorrow if time already passed
            duration_secs = (self._end_time - datetime.now()).total_seconds()
            duration_hours = duration_secs / 3600
            self._duration_hours = duration_hours
            logger.info(f"Will run until {self._end_time.strftime('%Y-%m-%d %H:%M')} ({duration_hours:.1f} hours)")
        elif duration_hours:
            self._end_time = datetime.now() + timedelta(hours=duration_hours)
            logger.info(f"Will run for {duration_hours} hours (until {self._end_time.strftime('%H:%M')})")
        else:
            logger.info("Running continuously (Ctrl+C to stop)")

        # Initialize collectors
        self._init_observer(duration_hours)
        self.price_logger = BinancePriceLogger(output_dir=str(self.binance_dir))

        print("=" * 60)
        print("DATA COLLECTION MANAGER")
        print("=" * 60)
        print(f"Observer output:      {self.observer_dir}/")
        print(f"Price logger output:  {self.binance_dir}/")
        print(f"Auto-restart:         {'ENABLED' if self.auto_restart else 'DISABLED'}")
        print(f"Health check interval: {HEALTH_CHECK_INTERVAL_SECS}s")
        print()

        # Run all tasks concurrently
        try:
            self._observer_task = asyncio.create_task(
                self._run_observer_with_error_handling()
            )
            self._price_logger_task = asyncio.create_task(
                self._run_price_logger_with_error_handling(duration_hours)
            )
            self._health_monitor_task = asyncio.create_task(
                self._health_monitor_loop()
            )

            # Wait for all tasks
            await asyncio.gather(
                self._observer_task,
                self._price_logger_task,
                self._health_monitor_task,
                return_exceptions=True
            )
        except asyncio.CancelledError:
            logger.info("Shutting down...")
        except Exception as e:
            logger.error(f"Unexpected error in data collection: {e}")
            traceback.print_exc()
        finally:
            self.stop()

    def _init_observer(self, duration_hours: float = None):
        """Initialize or reinitialize the observer."""
        self.observer = SpreadCaptureObserver(
            duration_hours=duration_hours or 12.0,
            continuous=duration_hours is None,
            starting_balance=170.0,
            trade_size=5.0,
            output_dir=str(self.observer_dir),
        )
        self._observer_last_sample_count = 0
        self._observer_last_check_time = time.time()

    async def _run_observer_with_error_handling(self):
        """Run observer with comprehensive error handling."""
        while self.running:
            try:
                logger.info("Starting observer...")
                await self.observer.run()
                # Normal exit (duration reached or stopped)
                if not self.running:
                    break
                logger.info("Observer finished normally")
                break
            except Exception as e:
                logger.error(f"OBSERVER CRASHED: {e}")
                traceback.print_exc()

                if not self.running:
                    break

                if self.auto_restart:
                    self._observer_restart_attempts += 1
                    if self._observer_restart_attempts > MAX_RESTART_ATTEMPTS:
                        logger.error(f"Observer exceeded max restart attempts ({MAX_RESTART_ATTEMPTS}). Giving up.")
                        self.running = False
                        break

                    logger.warning(f"Auto-restart enabled. Attempting restart {self._observer_restart_attempts}/{MAX_RESTART_ATTEMPTS} in {RESTART_BACKOFF_SECS}s...")
                    await asyncio.sleep(RESTART_BACKOFF_SECS)

                    # Reinitialize observer
                    remaining_hours = None
                    if self._end_time:
                        remaining_secs = (self._end_time - datetime.now()).total_seconds()
                        if remaining_secs <= 0:
                            logger.info("Duration expired during restart. Stopping.")
                            break
                        remaining_hours = remaining_secs / 3600

                    self._init_observer(remaining_hours or self._duration_hours)
                else:
                    logger.error("Auto-restart DISABLED. Observer stopped. Data collection incomplete.")
                    logger.error("To enable auto-restart, use --auto-restart flag")
                    # FIXED (Feb 5, 2026): Stop BOTH when observer crashes
                    # Previous behavior let price logger continue alone = incomplete data
                    logger.error("STOPPING PRICE LOGGER TOO - incomplete data is useless")
                    self.running = False
                    if self._price_logger_task and not self._price_logger_task.done():
                        self._price_logger_task.cancel()
                    break

    async def _run_price_logger_with_error_handling(self, duration_hours: float = None):
        """Run price logger with error handling."""
        try:
            logger.info("Starting price logger...")
            await self.price_logger.run(duration_hours=duration_hours)
            logger.info("Price logger finished normally")
        except Exception as e:
            logger.error(f"PRICE LOGGER CRASHED: {e}")
            traceback.print_exc()

    async def _health_monitor_loop(self):
        """Monitor observer health and log warnings/errors."""
        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECS)  # Initial delay

        while self.running:
            # Stop monitoring if observer task has completed its duration
            if self._observer_task and self._observer_task.done():
                logger.info("Observer task completed. Stopping health monitor and exiting.")
                self.running = False
                # FIXED (Feb 5, 2026): Also stop price logger when observer completes
                # Both must run together for complete data
                if self._price_logger_task and not self._price_logger_task.done():
                    logger.info("Stopping price logger since observer completed.")
                    self._price_logger_task.cancel()
                break

            try:
                await self._check_observer_health()
            except Exception as e:
                logger.error(f"Health check error: {e}")

            await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECS)

    async def _check_observer_health(self):
        """Check if observer is still writing data."""
        if not self.observer:
            return

        current_count = self.observer.sample_count
        current_time = time.time()
        time_since_last_check = current_time - self._observer_last_check_time

        # Check if sample count increased
        if current_count == self._observer_last_sample_count:
            # No new samples since last check
            time_without_writes = time_since_last_check

            if time_without_writes >= HEALTH_ERROR_THRESHOLD_SECS:
                logger.error(
                    f"OBSERVER HEALTH: No data written for {time_without_writes:.0f}s! "
                    f"Observer may have stopped. Samples: {current_count}"
                )
            elif time_without_writes >= HEALTH_WARNING_THRESHOLD_SECS:
                logger.warning(
                    f"OBSERVER HEALTH: No data written for {time_without_writes:.0f}s. "
                    f"Samples: {current_count}"
                )
        else:
            # Observer is healthy - samples are increasing
            new_samples = current_count - self._observer_last_sample_count
            rate = new_samples / time_since_last_check if time_since_last_check > 0 else 0
            logger.debug(f"Observer healthy: {new_samples} new samples ({rate:.1f}/s)")

            # Reset restart attempts on successful health check
            self._observer_restart_attempts = 0
            self._observer_last_sample_count = current_count
            self._observer_last_check_time = current_time

    def stop(self):
        """Stop both collectors gracefully."""
        if not self.running:
            return
        self.running = False
        logger.info("Stopping data collection...")

        # Cancel health monitor first
        if self._health_monitor_task and not self._health_monitor_task.done():
            self._health_monitor_task.cancel()

        # Stop observers
        if self.observer:
            try:
                self.observer.stop()
            except Exception as e:
                logger.error(f"Error stopping observer: {e}")

        if self.price_logger:
            try:
                self.price_logger.stop()
            except Exception as e:
                logger.error(f"Error stopping price logger: {e}")

        logger.info("Both collectors stopped.")


async def main():
    parser = argparse.ArgumentParser(
        description="Run Observer + Price Logger together with health monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python scripts/run_data_collection.py --hours 6
    python scripts/run_data_collection.py --hours 24 --auto-restart
    python scripts/run_data_collection.py --until 21:30
    python scripts/run_data_collection.py --continuous
        """
    )
    parser.add_argument('--hours', type=float, default=None,
                        help='Duration in hours')
    parser.add_argument('--until', type=str, default=None,
                        help='Run until time (HH:MM format, e.g., 21:30)')
    parser.add_argument('--continuous', action='store_true',
                        help='Run until manually stopped')
    parser.add_argument('--output', type=str, default='research',
                        help='Output base directory')
    parser.add_argument('--auto-restart', action='store_true',
                        help='Auto-restart observer on crash (OFF by default)')
    args = parser.parse_args()

    manager = DataCollectionManager(
        output_dir=args.output,
        auto_restart=args.auto_restart
    )

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal...")
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
