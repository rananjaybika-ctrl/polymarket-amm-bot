"""
Configuration module for Polymarket AMM Bot.

Loads settings from environment variables (.env file).
Designed to be wallet-agnostic - switch between test and main wallet
by simply changing WALLET_PRIVATE_KEY in your .env file.

Usage:
    from src.config import Config
    config = Config()
    print(config.wallet_private_key)
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""
    pass


class Config:
    """
    Configuration class that loads settings from environment variables.

    All settings can be overridden via .env file in project root.
    See .env.example for documentation of all available settings.
    """

    def __init__(self, env_path: Optional[str] = None):
        """
        Initialize configuration from environment variables.

        Args:
            env_path: Optional path to .env file. If not provided,
                     looks for .env in project root.
        """
        # Load .env file
        if env_path:
            load_dotenv(env_path)
        else:
            # Find project root (where .env should be)
            project_root = Path(__file__).parent.parent
            load_dotenv(project_root / ".env")

        # === WALLET CONFIGURATION ===
        # Change this single value to switch between test and main wallet
        self.wallet_private_key: str = self._get_required("WALLET_PRIVATE_KEY")

        # Polygon RPC URL (default: public endpoint)
        self.polygon_rpc_url: str = os.getenv(
            "POLYGON_RPC_URL",
            "https://polygon-rpc.com"
        )

        # === POLYMARKET API ===
        self.polymarket_host: str = os.getenv(
            "POLYMARKET_HOST",
            "https://clob.polymarket.com"
        )
        self.chain_id: int = int(os.getenv("CHAIN_ID", "137"))  # Polygon mainnet

        # CLOB API credentials (for User WebSocket - real-time fill notifications)
        # These are the same as Builder Relayer credentials
        # Get from: https://polymarket.com/settings?tab=builder
        self.polymarket_api_key: str = os.getenv("POLYMARKET_API_KEY", "") or os.getenv("BUILDER_API_KEY", "")
        self.polymarket_secret: str = os.getenv("POLYMARKET_SECRET", "") or os.getenv("BUILDER_SECRET", "")
        self.polymarket_passphrase: str = os.getenv("POLYMARKET_PASSPHRASE", "") or os.getenv("BUILDER_PASSPHRASE", "")

        # === WALLET TYPE ===
        # "eoa" = standard MetaMask/hardware wallet (default)
        # "magic" = email login (Magic wallet) - requires FUNDER_ADDRESS
        # "gnosis_safe" = Gnosis Safe wallet - for Builder Relayer (gasless redemptions)
        self.wallet_type: str = os.getenv("WALLET_TYPE", "eoa").lower()

        # For Magic wallets: your actual Polymarket account address
        # Find this on Polymarket.com after logging in (top right corner)
        self.funder_address: str = os.getenv("FUNDER_ADDRESS", "")

        # For Gnosis Safe wallets: your deployed Safe address
        # Get this from relay_client.get_expected_safe() or your deployed Safe
        self.safe_address: str = os.getenv("SAFE_ADDRESS", "")

        # === TRADING PARAMETERS ===
        # Capital limits (in USD)
        self.max_total_cost: float = float(os.getenv("MAX_TOTAL_COST", "20.0"))
        self.max_trade_cost: float = float(os.getenv("MAX_TRADE_COST", "10.0"))
        self.min_trade_cost: float = float(os.getenv("MIN_TRADE_COST", "0.5"))

        # === RISK PARAMETERS ===
        # Safety pair cost - stop if pair_cost exceeds this (loss territory)
        self.safety_pair_cost: float = float(os.getenv("SAFETY_PAIR_COST", "1.02"))

        # Maximum allowed position imbalance (0.0 to 1.0)
        # 0.2 means max 20% difference between Up and Down shares
        self.max_imbalance: float = float(os.getenv("MAX_IMBALANCE", "0.20"))

        # Maximum shares per side (Up or Down)
        self.max_position_shares: int = int(os.getenv("MAX_POSITION_SHARES", "50"))

        # Daily loss limit (USD) - pause bot if exceeded
        self.daily_loss_limit: float = float(os.getenv("DAILY_LOSS_LIMIT", "20.0"))

        # Consecutive losses before auto-pause
        self.max_consecutive_losses: int = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))

        # === STRATEGY PARAMETERS ===
        # Grid levels (1 = simple single-level strategy)
        self.grid_levels: int = int(os.getenv("GRID_LEVELS", "1"))

        # Price spacing between grid levels (in cents)
        self.grid_spacing: float = float(os.getenv("GRID_SPACING", "0.01"))

        # Target locked profit before exiting position (USD)
        self.target_locked_profit: float = float(os.getenv("TARGET_LOCKED_PROFIT", "1.0"))

        # Order size per grid level (shares)
        self.order_size_per_level: int = int(os.getenv("ORDER_SIZE_PER_LEVEL", "10"))

        # Minimum spread required to enter market
        self.min_spread: float = float(os.getenv("MIN_SPREAD", "0.02"))

        # === TIMING PARAMETERS ===
        # How often to poll for order fills (seconds)
        self.poll_interval: float = float(os.getenv("POLL_INTERVAL", "1.0"))

        # How often to refresh grid orders (seconds)
        self.refresh_grid_seconds: float = float(os.getenv("REFRESH_GRID_SECONDS", "5.0"))

        # === NETWORK CONFIGURATION ===
        # WiFi network names for failover
        self.primary_wifi: str = os.getenv("PRIMARY_WIFI", "")
        self.backup_wifi_1: str = os.getenv("BACKUP_WIFI_1", "")
        self.backup_wifi_2: str = os.getenv("BACKUP_WIFI_2", "")

        # WiFi passwords (for automatic switching)
        self.backup_wifi_1_password: str = os.getenv("BACKUP_WIFI_1_PASSWORD", "")
        self.backup_wifi_2_password: str = os.getenv("BACKUP_WIFI_2_PASSWORD", "")

        # How often to check if primary network is available (seconds)
        self.network_poll_interval: int = int(os.getenv("NETWORK_POLL_INTERVAL", "15"))

        # === TELEGRAM CONFIGURATION ===
        # Bot token from @BotFather
        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        # Chat ID (get from /getUpdates after messaging your bot)
        self.telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

        # === BUILDER RELAYER (for gasless redemptions) ===
        # Get these from: https://polymarket.com/settings?tab=builder
        self.builder_api_key: str = os.getenv("BUILDER_API_KEY", "")
        self.builder_secret: str = os.getenv("BUILDER_SECRET", "")
        self.builder_passphrase: str = os.getenv("BUILDER_PASSPHRASE", "")

        # === AUTO-REDEMPTION SETTINGS ===
        # Automatically redeem winning positions in the background
        self.auto_redeem_enabled: bool = os.getenv("AUTO_REDEEM_ENABLED", "true").lower() == "true"
        # How often to check for redeemable positions (minutes)
        self.auto_redeem_interval_minutes: float = float(os.getenv("AUTO_REDEEM_INTERVAL_MINUTES", "5.0"))

        # === DISCORD CONFIGURATION (deprecated - use Telegram) ===
        # Webhook URLs for different notification channels
        self.discord_webhook_pnl: str = os.getenv("DISCORD_WEBHOOK_PNL", "")
        self.discord_webhook_losses: str = os.getenv("DISCORD_WEBHOOK_LOSSES", "")
        self.discord_webhook_outages: str = os.getenv("DISCORD_WEBHOOK_OUTAGES", "")

        # Your Discord user ID (for @mentions)
        self.discord_user_id: str = os.getenv("DISCORD_USER_ID", "")

        # === MODE FLAGS ===
        # Dry run mode - simulate trades without real money
        self.dry_run_mode: bool = os.getenv("DRY_RUN_MODE", "true").lower() == "true"

        # Enable real trading (requires explicit opt-in)
        self.enable_real_trading: bool = os.getenv("ENABLE_REAL_TRADING", "false").lower() == "true"

        # === LOGGING ===
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")
        self.log_to_file: bool = os.getenv("LOG_TO_FILE", "true").lower() == "true"

    def _get_required(self, key: str) -> str:
        """
        Get a required environment variable.

        Args:
            key: The environment variable name

        Returns:
            The value of the environment variable

        Raises:
            ConfigError: If the variable is not set
        """
        value = os.getenv(key)
        if not value:
            raise ConfigError(
                f"Required configuration '{key}' is not set. "
                f"Please add it to your .env file. "
                f"See .env.example for documentation."
            )
        return value

    def validate(self) -> bool:
        """
        Validate configuration values.

        Returns:
            True if all validations pass

        Raises:
            ConfigError: If any validation fails
        """
        # Check wallet key format (basic check)
        if not self.wallet_private_key.startswith("0x"):
            # Try to add 0x prefix if missing
            if len(self.wallet_private_key) == 64:
                self.wallet_private_key = "0x" + self.wallet_private_key
            else:
                raise ConfigError(
                    "WALLET_PRIVATE_KEY should be a 64-character hex string, "
                    "optionally prefixed with '0x'"
                )

        # Validate wallet type
        if self.wallet_type not in ("eoa", "magic", "gnosis_safe"):
            raise ConfigError(
                "WALLET_TYPE must be 'eoa' (MetaMask), 'magic' (email login), "
                "or 'gnosis_safe' (Safe wallet for Builder Relayer)"
            )

        if self.wallet_type == "magic" and not self.funder_address:
            raise ConfigError(
                "FUNDER_ADDRESS is required for Magic wallets. "
                "This is your Polymarket account address (shown on polymarket.com)"
            )

        if self.wallet_type == "gnosis_safe":
            if not self.safe_address:
                raise ConfigError(
                    "SAFE_ADDRESS is required for Gnosis Safe wallets. "
                    "Get this from relay_client.get_expected_safe() or your deployed Safe."
                )
            # For Safe wallets, funder_address should be the Safe address
            if not self.funder_address:
                self.funder_address = self.safe_address

        # Validate risk parameters
        if not 0 < self.max_imbalance <= 1:
            raise ConfigError("MAX_IMBALANCE must be between 0 and 1")

        if self.safety_pair_cost <= 1.0:
            raise ConfigError(
                "SAFETY_PAIR_COST should be > 1.0 (e.g., 1.02). "
                "This is the pair cost threshold that triggers emergency exit."
            )

        # Validate trading parameters
        if self.min_trade_cost >= self.max_trade_cost:
            raise ConfigError("MIN_TRADE_COST must be less than MAX_TRADE_COST")

        # Warn about real trading
        if self.enable_real_trading and not self.dry_run_mode:
            print("WARNING: Real trading is enabled! Trades will use real money.")

        return True

    def __repr__(self) -> str:
        """Return string representation (hides sensitive data)."""
        return (
            f"Config("
            f"dry_run={self.dry_run_mode}, "
            f"max_total_cost=${self.max_total_cost}, "
            f"max_imbalance={self.max_imbalance:.0%}, "
            f"grid_levels={self.grid_levels}, "
            f"target_profit=${self.target_locked_profit}"
            f")"
        )


class FeeConfig:
    """
    Fee and rebate configuration for Polymarket 15-minute crypto markets.

    From Polymarket docs (docs.polymarket.com/developers/market-makers/maker-rebates-program):
    - Taker fee: Up to 1.56% at 50% price, scales down at extremes
    - Maker rebate: Proportional share of taker fees collected

    NOTE: Rebate rates may change. Until Jan 9, 2026, 100% of fees → makers.
    After that date, percentage is at Polymarket's discretion.

    Usage:
        fee = FeeConfig.get_taker_fee(0.50)  # ~1.56%
        rebate = FeeConfig.get_maker_rebate(0.50)  # ~0.5%
    """

    # Taker fee configuration (from Polymarket docs)
    TAKER_FEE_BPS = 1000  # Base 10% (1000 basis points)
    MAX_TAKER_FEE_RATE = 0.0156  # 1.56% cap at 50% probability

    # Maker rebate configuration
    # NOTE: This is an ESTIMATE. Actual rebates depend on:
    # 1. Total market maker volume (proportional share)
    # 2. Polymarket's rebate percentage (100% until Jan 9, 2026)
    MAKER_REBATE_RATE = 0.01  # ~1% estimated rebate
    REBATE_SHARE = 1.0  # 100% of fees → rebates (may change)

    @classmethod
    def get_taker_fee(cls, price: float) -> float:
        """
        Calculate taker fee for a given price.

        Formula from Polymarket: fee = 1000 bps × 4 × price × (1 - price)
        Maximum effective rate is 1.56% at 50% probability.

        Args:
            price: The fill price (0.01 to 0.99)

        Returns:
            Fee rate as decimal (e.g., 0.0156 for 1.56%)
        """
        # fee = 1000 bps × 4 × price × (1 - price)
        raw_fee = (cls.TAKER_FEE_BPS / 10000) * 4 * price * (1 - price)
        return min(raw_fee, cls.MAX_TAKER_FEE_RATE)

    @classmethod
    def get_taker_fee_amount(cls, price: float, size: float) -> float:
        """
        Calculate taker fee amount in USD.

        Args:
            price: The fill price
            size: Number of shares

        Returns:
            Fee amount in USD
        """
        fee_rate = cls.get_taker_fee(price)
        return price * size * fee_rate

    @classmethod
    def get_maker_rebate(cls, price: float) -> float:
        """
        Estimate maker rebate rate for a given price.

        NOTE: This is approximate. Actual rebate depends on:
        - Your share of total maker volume in the market
        - Current rebate percentage (may be < 100% after Jan 9, 2026)

        Args:
            price: The fill price

        Returns:
            Estimated rebate rate as decimal (e.g., 0.01 for 1%)
        """
        return cls.MAKER_REBATE_RATE

    @classmethod
    def get_maker_rebate_amount(cls, price: float, size: float) -> float:
        """
        Estimate maker rebate amount in USD.

        Args:
            price: The fill price
            size: Number of shares

        Returns:
            Estimated rebate amount in USD
        """
        rebate_rate = cls.get_maker_rebate(price)
        return price * size * rebate_rate

    @classmethod
    def calculate_net_profit(
        cls,
        entry_price: float,
        hedge_price: float,
        size: float,
        entry_is_maker: bool = True,
        hedge_is_maker: bool = True,
    ) -> float:
        """
        Calculate net profit including fees and rebates.

        Args:
            entry_price: Price paid for entry side
            hedge_price: Price paid for hedge side
            size: Number of shares per side
            entry_is_maker: Whether entry order was maker
            hedge_is_maker: Whether hedge order was maker

        Returns:
            Net profit per share after fees/rebates
        """
        # Base profit from spread
        pair_cost = entry_price + hedge_price
        base_profit = 1.00 - pair_cost

        # Apply entry fee/rebate
        if entry_is_maker:
            base_profit += cls.get_maker_rebate(entry_price) * entry_price
        else:
            base_profit -= cls.get_taker_fee(entry_price) * entry_price

        # Apply hedge fee/rebate
        if hedge_is_maker:
            base_profit += cls.get_maker_rebate(hedge_price) * hedge_price
        else:
            base_profit -= cls.get_taker_fee(hedge_price) * hedge_price

        return base_profit

    @classmethod
    def get_max_taker_hedge_price(
        cls,
        entry_price: float,
        min_profit: float = 0.005,
    ) -> float:
        """
        Calculate maximum hedge price that's still profitable as taker.

        When entry is maker and hedge must be taker (emergency), account for:
        - Entry rebate (~1%)
        - Hedge taker fee (~1.56%)

        Args:
            entry_price: Price paid for entry (as maker)
            min_profit: Minimum required profit per share

        Returns:
            Maximum hedge price that maintains profitability
        """
        # Start with base max hedge
        base_max = 1.00 - entry_price - min_profit

        # Add entry rebate
        entry_rebate = cls.get_maker_rebate(entry_price) * entry_price

        # Subtract expected taker fee (at estimated hedge price)
        # Use iterative approach since fee depends on price
        estimated_hedge = base_max
        for _ in range(3):  # Converge in few iterations
            taker_fee = cls.get_taker_fee(estimated_hedge) * estimated_hedge
            estimated_hedge = base_max + entry_rebate - taker_fee

        return round(estimated_hedge, 4)


# Convenience function to load config
def load_config(env_path: Optional[str] = None) -> Config:
    """
    Load and validate configuration.

    Args:
        env_path: Optional path to .env file

    Returns:
        Validated Config instance
    """
    config = Config(env_path)
    config.validate()
    return config
