#!/usr/bin/env python3
"""
Live Trading Bot with Auto-Claim

Runs live trading with real market interactions.
Defaults to DRY-RUN mode (no real orders) for safety.

Features:
- Auto-claim: Automatically sells winning positions after market resolution
- Position sync: Syncs with on-chain balances periodically
- Dry-run mode: Simulates all actions without executing real trades (default)

Usage:
    # Dry-run mode (default - safe, no real orders)
    python scripts/run_live_bot.py

    # Check claimable positions only
    python scripts/run_live_bot.py --claim-only

    # Live mode (REAL ORDERS - use with caution)
    python scripts/run_live_bot.py --live

    # Run for specific duration
    python scripts/run_live_bot.py --duration 480 --live
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.config import Config
from src.api.polymarket_client import PolymarketClient, PolymarketClientError
from src.utils.telegram_notifier import TelegramNotifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


class LiveTradingBot:
    """
    Live trading bot with auto-claim functionality.

    Operates in two modes:
    - DRY-RUN (default): Simulates all actions, logs what would happen
    - LIVE: Executes real trades on Polymarket
    """

    def __init__(
        self,
        dry_run: bool = True,
        claim_interval_minutes: float = 15.0,
        sync_interval_minutes: float = 5.0,
    ):
        """
        Initialize the live trading bot.

        Args:
            dry_run: If True, simulate all actions (default: True for safety)
            claim_interval_minutes: How often to check for claimable positions
            sync_interval_minutes: How often to sync on-chain balances
        """
        self.dry_run = dry_run
        self.claim_interval = claim_interval_minutes * 60  # Convert to seconds
        self.sync_interval = sync_interval_minutes * 60

        self._config: Optional[Config] = None
        self._client: Optional[PolymarketClient] = None
        self._running: bool = False
        self._shutdown_event: asyncio.Event = asyncio.Event()

        # Telegram for notifications and remote control
        self._telegram: Optional[TelegramNotifier] = None

        # Track state
        self._last_claim_check: Optional[datetime] = None
        self._last_sync: Optional[datetime] = None
        self._total_claimed: float = 0.0
        self._claim_count: int = 0
        self._emergency_sell_requested: bool = False

    async def initialize(self) -> bool:
        """
        Initialize bot and connect to Polymarket.

        Returns:
            True if initialization successful
        """
        mode_str = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(f"Initializing Live Trading Bot in {mode_str} mode")

        if not self.dry_run:
            logger.warning("=" * 60)
            logger.warning("LIVE MODE ENABLED - REAL ORDERS WILL BE PLACED")
            logger.warning("=" * 60)

        try:
            # Load config
            self._config = Config()
            logger.info("Config loaded successfully")

            # Create and connect client
            self._client = PolymarketClient(self._config)
            connected = await self._client.connect()

            if not connected:
                logger.error("Failed to connect to Polymarket")
                return False

            wallet = self._client.get_wallet_address()
            logger.info(f"Connected to Polymarket API (wallet: {wallet[:10]}...)")

            # Initial balance check
            balance = await self._client.get_balance()
            logger.info(f"Current USDC balance: ${balance:.2f}")

            # Check for existing positions from previous sessions
            if not self.dry_run:
                existing = await self._check_existing_positions()
                if existing["total"] > 0:
                    logger.warning("=" * 60)
                    logger.warning(f"WARNING: {existing['total']} existing positions found!")
                    logger.warning(f"  Total UP shares: {existing['up']:.2f}")
                    logger.warning(f"  Total DOWN shares: {existing['down']:.2f}")
                    logger.warning(f"  Imbalance: {abs(existing['up'] - existing['down']):.2f}")
                    logger.warning("Use 'python scripts/sell_all_positions.py' to clear first.")
                    logger.warning("=" * 60)

            # Initialize Telegram for notifications and remote control
            self._telegram = TelegramNotifier(self._config)
            if self._telegram.enabled:
                # Register command handlers
                self._telegram.on_stop(self._handle_telegram_stop)
                self._telegram.on_sell_all(self._handle_telegram_sell_all)
                self._telegram.on_status(self._handle_telegram_status)
                self._telegram.on_balance(self._handle_telegram_balance)

                await self._telegram.start()
                await self._telegram.send_info(
                    f"Live Trading Bot started in {mode_str} mode\n"
                    f"Balance: ${balance:.2f} USDC"
                )
                await self._telegram.send_control_panel()
                logger.info("Telegram remote control enabled")
            else:
                logger.info("Telegram notifications disabled (no token configured)")

            return True

        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            return False

    # === Telegram Command Handlers ===

    async def _handle_telegram_stop(self) -> None:
        """Handle /stop command from Telegram."""
        logger.info("Stop command received from Telegram")
        self.signal_shutdown()

    async def _handle_telegram_sell_all(self) -> None:
        """Handle /sell_all command from Telegram - sets flag for main loop."""
        logger.info("Emergency sell command received from Telegram")
        self._emergency_sell_requested = True
        if self._telegram:
            await self._telegram.send_message("Emergency sell requested - will execute on next cycle")

    async def _handle_telegram_status(self) -> str:
        """Handle /status command from Telegram."""
        mode_str = "DRY-RUN" if self.dry_run else "LIVE"
        status_lines = [
            f"Mode: {mode_str}",
            f"Running: {self._running}",
            f"Claims: {self._claim_count} (${self._total_claimed:.2f})",
        ]
        if self._last_claim_check:
            status_lines.append(f"Last claim check: {self._last_claim_check.strftime('%H:%M:%S')}")
        if self._last_sync:
            status_lines.append(f"Last sync: {self._last_sync.strftime('%H:%M:%S')}")
        return "\n".join(status_lines)

    async def _handle_telegram_balance(self) -> str:
        """Handle /balance command from Telegram."""
        try:
            balance = await self._client.get_balance()
            return f"USDC Balance: ${balance:.2f}"
        except Exception as e:
            return f"Error fetching balance: {e}"

    async def _check_existing_positions(self) -> Dict[str, Any]:
        """Check for existing positions on startup."""
        import aiohttp

        result = {"up": 0.0, "down": 0.0, "total": 0}

        try:
            wallet = self._client.get_wallet_address()
            url = f"https://gamma-api.polymarket.com/positions?user={wallet}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        return result
                    positions = await response.json()

            for pos in positions:
                size = float(pos.get("size", 0))
                if size <= 0:
                    continue

                outcome = pos.get("outcome", "").upper()
                result["total"] += 1

                if outcome in ["YES", "UP"]:
                    result["up"] += size
                elif outcome in ["NO", "DOWN"]:
                    result["down"] += size

        except Exception as e:
            logger.debug(f"Could not check existing positions: {e}")

        return result

    async def check_and_claim_winnings(self) -> List[Dict[str, Any]]:
        """
        Check for and claim any winning positions from resolved markets.

        Returns:
            List of claim results
        """
        logger.info("Checking for claimable positions...")

        try:
            claimable = await self._client.get_claimable_positions()

            if not claimable:
                logger.info("No claimable positions found")
                return []

            # Filter to winning positions only
            winning = [p for p in claimable if p["is_winning"] and p["balance"] > 0]

            if not winning:
                logger.info(f"Found {len(claimable)} resolved positions, but none are winners")
                return []

            total_value = sum(p["estimated_value"] for p in winning)
            logger.info(f"Found {len(winning)} winning positions worth ~${total_value:.2f}")

            # Log each position
            for pos in winning:
                logger.info(
                    f"  - {pos['outcome']}: {pos['balance']:.2f} shares @ $0.99 = ${pos['estimated_value']:.2f}"
                )

            # Claim via sell
            results = await self._client.claim_all_winnings(dry_run=self.dry_run)

            for result in results:
                if result["dry_run"]:
                    logger.info(f"[DRY-RUN] Would claim: {result['message']}")
                elif result["status"] == "success":
                    logger.info(f"Claimed {result['balance']:.2f} shares")
                    self._total_claimed += result["estimated_value"]
                    self._claim_count += 1
                else:
                    logger.error(f"Claim failed: {result.get('error', 'Unknown error')}")

            self._last_claim_check = datetime.now(timezone.utc)
            return results

        except PolymarketClientError as e:
            logger.error(f"Error checking claimable positions: {e}")
            return []

    async def sync_balances(self) -> Dict[str, Any]:
        """
        Sync with on-chain balances.

        Returns:
            Current balance info
        """
        try:
            balance = await self._client.get_balance()

            # Get positions via Gamma API
            import aiohttp
            wallet = self._client.get_wallet_address()
            url = f"https://gamma-api.polymarket.com/positions?user={wallet}"

            total_position_value = 0.0
            position_count = 0

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                        if response.status == 200:
                            positions = await response.json()
                            for pos in positions:
                                size = float(pos.get("size", 0))
                                if size > 0:
                                    position_count += 1
                                    # Estimate value at 0.5 (neutral)
                                    total_position_value += size * 0.5
            except Exception as e:
                logger.debug(f"Could not fetch positions: {e}")

            result = {
                "usdc_balance": balance,
                "position_count": position_count,
                "estimated_position_value": total_position_value,
                "total_equity": balance + total_position_value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            logger.info(
                f"Balance sync: ${balance:.2f} USDC + "
                f"~${total_position_value:.2f} in {position_count} positions"
            )

            self._last_sync = datetime.now(timezone.utc)
            return result

        except PolymarketClientError as e:
            logger.error(f"Balance sync failed: {e}")
            return {}

    async def run_claim_only(self) -> None:
        """Run a single claim check and exit."""
        if not await self.initialize():
            return

        await self.check_and_claim_winnings()
        await self.sync_balances()

        logger.info("Claim check complete")

    async def run(self, duration_minutes: Optional[float] = None) -> None:
        """
        Run the live trading bot.

        Args:
            duration_minutes: How long to run (None = indefinitely)
        """
        if not await self.initialize():
            return

        self._running = True
        start_time = datetime.now(timezone.utc)
        end_time = None
        if duration_minutes:
            end_time = start_time + timedelta(minutes=duration_minutes)
            logger.info(f"Running until {end_time.isoformat()}")
        else:
            logger.info("Running indefinitely (Ctrl+C to stop)")

        # Initial sync
        await self.sync_balances()

        try:
            while self._running and not self._shutdown_event.is_set():
                now = datetime.now(timezone.utc)

                # Check duration
                if end_time and now >= end_time:
                    logger.info("Duration reached, shutting down")
                    break

                # Periodic claim check
                if (
                    self._last_claim_check is None or
                    (now - self._last_claim_check).total_seconds() >= self.claim_interval
                ):
                    await self.check_and_claim_winnings()

                # Periodic balance sync
                if (
                    self._last_sync is None or
                    (now - self._last_sync).total_seconds() >= self.sync_interval
                ):
                    await self.sync_balances()

                # Sleep briefly before next iteration
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=60.0,  # Check every minute
                    )
                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        """Clean up resources."""
        self._running = False
        logger.info(f"Session complete: Claimed ${self._total_claimed:.2f} from {self._claim_count} positions")

        # Send final Telegram notification and stop
        if self._telegram and self._telegram.enabled:
            try:
                await self._telegram.send_message(
                    f"Live Trading Bot stopped\n"
                    f"Session claims: ${self._total_claimed:.2f} from {self._claim_count} positions"
                )
                await self._telegram.stop()
            except Exception as e:
                logger.error(f"Error stopping Telegram: {e}")

    def signal_shutdown(self) -> None:
        """Signal the bot to shut down gracefully."""
        logger.info("Shutdown signal received")
        self._running = False
        self._shutdown_event.set()


async def main():
    parser = argparse.ArgumentParser(
        description="Live Trading Bot with Auto-Claim",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run mode (default - safe, no real orders)
  python scripts/run_live_bot.py

  # Check and claim winnings only (dry-run)
  python scripts/run_live_bot.py --claim-only

  # Live mode (REAL ORDERS)
  python scripts/run_live_bot.py --live

  # Run for 8 hours in live mode
  python scripts/run_live_bot.py --duration 480 --live
        """
    )

    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable LIVE mode (real orders). Default is dry-run.",
    )
    parser.add_argument(
        "--claim-only",
        action="store_true",
        help="Only check and claim winnings, then exit",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duration to run in minutes (default: indefinite)",
    )
    parser.add_argument(
        "--claim-interval",
        type=float,
        default=15.0,
        help="Minutes between claim checks (default: 15)",
    )
    parser.add_argument(
        "--sync-interval",
        type=float,
        default=5.0,
        help="Minutes between balance syncs (default: 5)",
    )

    args = parser.parse_args()

    # Determine mode
    dry_run = not args.live

    if args.live:
        print("\n" + "=" * 60)
        print("WARNING: LIVE MODE - REAL ORDERS WILL BE PLACED")
        print("=" * 60)
        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return
        print()

    # Create bot
    bot = LiveTradingBot(
        dry_run=dry_run,
        claim_interval_minutes=args.claim_interval,
        sync_interval_minutes=args.sync_interval,
    )

    # Setup signal handlers
    def signal_handler(sig, frame):
        bot.signal_shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run
    if args.claim_only:
        await bot.run_claim_only()
    else:
        await bot.run(duration_minutes=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
