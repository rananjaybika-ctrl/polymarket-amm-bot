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

        # === DISCORD CONFIGURATION ===
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
