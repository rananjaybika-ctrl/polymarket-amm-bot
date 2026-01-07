# Utility modules for Polymarket AMM Bot
"""
Contains:
- network_monitor: WiFi failover and connectivity monitoring
- telegram_notifier: Telegram notifications and remote commands (recommended)
- discord_notifier: Discord webhook notifications (deprecated)
- market_detector: Market type detection for adaptive parameters
"""

from src.utils.network_monitor import NetworkMonitor, NetworkStatus, NetworkMonitorError
from src.utils.telegram_notifier import TelegramNotifier, TelegramNotifierError
from src.utils.discord_notifier import DiscordNotifier, DiscordNotifierError
from src.utils.market_detector import MarketTypeDetector, DetectionResult

__all__ = [
    "NetworkMonitor",
    "NetworkStatus",
    "NetworkMonitorError",
    "TelegramNotifier",
    "TelegramNotifierError",
    "DiscordNotifier",
    "DiscordNotifierError",
    "MarketTypeDetector",
    "DetectionResult",
]
