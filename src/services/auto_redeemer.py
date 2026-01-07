"""
Auto-Redemption Service for Winning Positions.

Runs as a background task to automatically redeem resolved winning positions
using the Builder Relayer for gasless transactions.

Only works with Gnosis Safe wallets (signature_type=2) because the Python
Builder Relayer SDK only supports Safe transactions (not Proxy).

Usage:
    from src.services.auto_redeemer import AutoRedeemer

    redeemer = AutoRedeemer(config, notifier)
    await redeemer.start()  # Runs every 5 minutes in background
    await redeemer.stop()   # Clean shutdown
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class AutoRedeemer:
    """
    Background service for automatic redemption of winning positions.

    Features:
    - Runs every N minutes (configurable)
    - Fetches redeemable positions via Polymarket Data API
    - Redeems via Builder Relayer (gasless)
    - Sends Telegram notification on success
    - Rate limiting protection (1 second between API calls)
    - Only works with Gnosis Safe wallets
    """

    def __init__(
        self,
        config: 'Config',
        notifier: Optional['TelegramNotifier'] = None,
        interval_minutes: float = 5.0,
        on_redemption: Optional[Callable[[Dict], Awaitable[None]]] = None,
    ):
        """
        Initialize AutoRedeemer.

        Args:
            config: Bot configuration with wallet and Builder credentials
            notifier: Optional Telegram notifier for alerts
            interval_minutes: Minutes between redemption checks
            on_redemption: Optional callback when redemption succeeds
        """
        self.config = config
        self.notifier = notifier
        self.interval_seconds = interval_minutes * 60
        self.on_redemption = on_redemption

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_run: Optional[datetime] = None
        self._total_redeemed_usd: float = 0.0
        self._redemption_count: int = 0

        # Rate limiting - minimum delay between API calls
        self._min_delay_between_calls = 1.0  # seconds

    @property
    def wallet_address(self) -> str:
        """Get the wallet address to check for redeemable positions."""
        if self.config.wallet_type == "gnosis_safe":
            return self.config.safe_address
        return self.config.funder_address

    @property
    def stats(self) -> Dict[str, Any]:
        """Get current redemption statistics."""
        return {
            "total_redeemed_usd": self._total_redeemed_usd,
            "redemption_count": self._redemption_count,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "running": self._running,
            "interval_minutes": self.interval_seconds / 60,
        }

    async def start(self) -> None:
        """Start the auto-redemption background task."""
        if self._running:
            logger.warning("AutoRedeemer already running")
            return

        # Validate wallet type
        if self.config.wallet_type != "gnosis_safe":
            logger.warning(
                f"AutoRedeemer requires gnosis_safe wallet type, got {self.config.wallet_type}. "
                "Auto-redemption disabled."
            )
            return

        # Validate Builder credentials
        if not self._has_builder_credentials():
            logger.warning(
                "AutoRedeemer requires Builder credentials (BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE). "
                "Auto-redemption disabled."
            )
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"AutoRedeemer started: checking every {self.interval_seconds/60:.1f} minutes "
            f"for wallet {self.wallet_address[:10]}..."
        )

    async def stop(self) -> None:
        """Stop the auto-redemption background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"AutoRedeemer stopped. Total redeemed: ${self._total_redeemed_usd:.2f}")

    def _has_builder_credentials(self) -> bool:
        """Check if Builder credentials are configured."""
        api_key = getattr(self.config, 'builder_api_key', '') or os.getenv("BUILDER_API_KEY", "")
        secret = getattr(self.config, 'builder_secret', '') or os.getenv("BUILDER_SECRET", "")
        passphrase = getattr(self.config, 'builder_passphrase', '') or os.getenv("BUILDER_PASSPHRASE", "")
        return bool(api_key and secret and passphrase)

    async def _run_loop(self) -> None:
        """Main background loop."""
        # Initial delay to let the bot start up
        await asyncio.sleep(10)

        while self._running:
            try:
                await self._check_and_redeem()
                self._last_run = datetime.now(timezone.utc)
            except Exception as e:
                logger.error(f"AutoRedeemer error: {e}", exc_info=True)

            # Wait for next interval
            await asyncio.sleep(self.interval_seconds)

    async def _check_and_redeem(self) -> None:
        """Check for and redeem any winning positions."""
        import httpx

        wallet = self.wallet_address
        if not wallet:
            logger.debug("AutoRedeemer: No wallet address configured")
            return

        try:
            # 1. Fetch redeemable positions
            url = f"https://data-api.polymarket.com/positions?user={wallet}&redeemable=true&sizeThreshold=0"

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                positions = resp.json()

            if not positions:
                logger.debug("AutoRedeemer: No redeemable positions")
                return

            # 2. Group by condition_id (avoid double-redeeming)
            conditions_to_redeem = []
            total_value = 0.0

            seen = set()
            for pos in positions:
                cond_id = pos.get("conditionId")
                if cond_id and cond_id not in seen:
                    seen.add(cond_id)
                    size = float(pos.get("size", 0))
                    conditions_to_redeem.append({
                        "condition_id": cond_id,
                        "title": pos.get("title", "Unknown")[:50],
                        "size": size,
                        "outcome": pos.get("outcome", "?"),
                    })
                    total_value += size

            if not conditions_to_redeem:
                return

            logger.info(
                f"AutoRedeemer: Found {len(conditions_to_redeem)} redeemable position(s) "
                f"worth ~${total_value:.2f}"
            )

            # 3. Rate limit delay before Builder Relayer call
            await asyncio.sleep(self._min_delay_between_calls)

            # 4. Execute redemptions via Builder Relayer
            success = await self._execute_redemption(
                [c["condition_id"] for c in conditions_to_redeem]
            )

            if success:
                self._total_redeemed_usd += total_value
                self._redemption_count += len(conditions_to_redeem)

                # 5. Send notification
                if self.notifier:
                    try:
                        await self.notifier.send_message(
                            f"Auto-Redeemed {len(conditions_to_redeem)} position(s)\n"
                            f"Value: ${total_value:.2f}\n"
                            f"Total Redeemed: ${self._total_redeemed_usd:.2f}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send notification: {e}")

                # 6. Callback if registered
                if self.on_redemption:
                    try:
                        await self.on_redemption({
                            "count": len(conditions_to_redeem),
                            "value": total_value,
                            "conditions": conditions_to_redeem,
                        })
                    except Exception as e:
                        logger.warning(f"Redemption callback error: {e}")

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("AutoRedeemer: Rate limited by Data API, will retry next interval")
            else:
                logger.error(f"AutoRedeemer HTTP error: {e}")
        except Exception as e:
            logger.error(f"AutoRedeemer fetch error: {e}")

    async def _execute_redemption(self, condition_ids: List[str]) -> bool:
        """
        Execute redemption via Builder Relayer.

        Args:
            condition_ids: List of condition IDs to redeem

        Returns:
            True if redemption succeeded
        """
        try:
            # Import dependencies
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
            from py_builder_relayer_client.client import RelayClient
            from py_builder_relayer_client.models import SafeTransaction, OperationType
            from web3 import Web3

            # Get credentials
            api_key = getattr(self.config, 'builder_api_key', '') or os.getenv("BUILDER_API_KEY", "")
            secret = getattr(self.config, 'builder_secret', '') or os.getenv("BUILDER_SECRET", "")
            passphrase = getattr(self.config, 'builder_passphrase', '') or os.getenv("BUILDER_PASSPHRASE", "")

            if not all([api_key, secret, passphrase]):
                logger.error("AutoRedeemer: Missing Builder credentials")
                return False

            # Create RelayClient
            builder_creds = BuilderApiKeyCreds(
                key=api_key,
                secret=secret,
                passphrase=passphrase
            )
            builder_config = BuilderConfig(local_builder_creds=builder_creds)

            relay_client = RelayClient(
                relayer_url="https://relayer-v2.polymarket.com",
                chain_id=137,
                private_key=self.config.wallet_private_key,
                builder_config=builder_config
            )

            # Verify Safe is deployed
            safe_address = relay_client.get_expected_safe()
            is_deployed = relay_client.get_deployed(safe_address)

            if not is_deployed:
                logger.error(f"AutoRedeemer: Safe {safe_address} not deployed")
                return False

            # Build transactions
            CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
            USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

            transactions = []
            for cond_id in condition_ids:
                calldata = self._encode_redeem_tx(cond_id, CTF_ADDRESS, USDC_ADDRESS)
                tx = SafeTransaction(
                    to=Web3.to_checksum_address(CTF_ADDRESS),
                    operation=OperationType.Call,
                    data=calldata,
                    value="0"
                )
                transactions.append(tx)

            # Execute
            logger.info(f"AutoRedeemer: Submitting {len(transactions)} redemption(s) to Builder Relayer...")
            response = relay_client.execute(transactions, "Auto-Redeem Winning Positions")

            # Extract tx hash
            if isinstance(response, dict):
                tx_hash = response.get('txHash') or response.get('transaction_hash') or response.get('tx_hash')
            else:
                tx_hash = getattr(response, 'transaction_hash', None) or getattr(response, 'tx_hash', None)

            logger.info(f"AutoRedeemer: SUCCESS - TX: {tx_hash}")
            logger.info(f"AutoRedeemer: View at https://polygonscan.com/tx/{tx_hash}")
            return True

        except Exception as e:
            logger.error(f"AutoRedeemer execution error: {e}", exc_info=True)
            return False

    def _encode_redeem_tx(self, condition_id: str, ctf_address: str, usdc_address: str) -> str:
        """
        Encode redeemPositions calldata for the CTF contract.

        Args:
            condition_id: The condition ID to redeem
            ctf_address: Conditional Token Framework contract address
            usdc_address: USDC token address

        Returns:
            Hex-encoded calldata string
        """
        from web3 import Web3

        CTF_REDEEM_ABI = [{
            "name": "redeemPositions",
            "type": "function",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}
            ]
        }]

        w3 = Web3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(ctf_address),
            abi=CTF_REDEEM_ABI
        )

        # Convert condition_id to bytes32
        cond_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)

        calldata = contract.encode_abi(
            "redeemPositions",
            [
                Web3.to_checksum_address(usdc_address),
                bytes(32),  # parentCollectionId = 0
                cond_bytes,
                [1, 2]  # YES + NO index sets
            ]
        )

        return calldata if isinstance(calldata, str) else "0x" + calldata.hex()


async def run_once(config: 'Config') -> bool:
    """
    Run a single redemption check (useful for testing or manual triggers).

    Args:
        config: Bot configuration

    Returns:
        True if any positions were redeemed
    """
    redeemer = AutoRedeemer(config, interval_minutes=0)
    await redeemer._check_and_redeem()
    return redeemer._redemption_count > 0
