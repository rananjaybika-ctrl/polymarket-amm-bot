"""
Discord webhook notification system.

Sends notifications to Discord channels for:
- PNL updates (profits/trades)
- Loss alerts (with @mentions)
- Outage alerts (network/API issues with @mentions)

Usage:
    from src.config import Config
    from src.utils.discord_notifier import DiscordNotifier

    config = Config()
    notifier = DiscordNotifier(config)

    # Send a profit notification
    notifier.send_pnl("Trade Complete: +$1.50 locked profit", {
        "Market": "BTC 15-min Up",
        "Entry": "$0.48",
        "Exit": "$0.52"
    })

    # Send a loss alert (will @mention you)
    notifier.send_loss("LOSS ALERT: -$2.00 on BTC trade")

    # Send an outage alert (will @mention you)
    notifier.send_outage("API connection lost", outage_type="api")
"""

import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

try:
    from discord_webhook import DiscordWebhook, DiscordEmbed
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

from src.config import Config


logger = logging.getLogger(__name__)


# Embed colors (Discord uses decimal color codes)
COLOR_GREEN = 0x00FF00   # Profits/success
COLOR_RED = 0xFF0000     # Losses/errors
COLOR_ORANGE = 0xFF9900  # Warnings/outages
COLOR_BLUE = 0x0099FF    # Info


class DiscordNotifierError(Exception):
    """Base exception for Discord notifier errors."""
    pass


class DiscordNotifier:
    """
    Discord webhook notification sender.

    Sends formatted messages with embeds to Discord channels.
    Supports @mentions for urgent alerts (losses, outages).

    Attributes:
        config: Bot configuration with webhook URLs and user ID
        enabled: Whether notifications are enabled (webhooks configured)
    """

    def __init__(self, config: Config):
        """
        Initialize the Discord notifier.

        Args:
            config: Configuration object with Discord settings
        """
        if not DISCORD_AVAILABLE:
            logger.warning(
                "discord-webhook not installed. "
                "Run: pip install discord-webhook"
            )

        self.config = config

        # Check if webhooks are configured
        self.webhook_pnl = config.discord_webhook_pnl
        self.webhook_losses = config.discord_webhook_losses
        self.webhook_outages = config.discord_webhook_outages
        self.user_id = config.discord_user_id

        # Determine if notifications are enabled
        self.enabled = bool(
            DISCORD_AVAILABLE and
            (self.webhook_pnl or self.webhook_losses or self.webhook_outages) and
            not self._is_placeholder(self.webhook_pnl)
        )

        if not self.enabled:
            logger.info(
                "Discord notifications disabled - no valid webhooks configured"
            )

    def _is_placeholder(self, url: str) -> bool:
        """Check if a webhook URL is a placeholder."""
        if not url:
            return True
        placeholders = ["your_", "xxx", "placeholder", "example"]
        return any(p in url.lower() for p in placeholders)

    def _create_embed(
        self,
        title: str,
        description: str,
        color: int,
        fields: Optional[List[Dict[str, Any]]] = None,
        footer: Optional[str] = None
    ) -> 'DiscordEmbed':
        """
        Create a Discord embed with consistent styling.

        Args:
            title: Embed title
            description: Main message text
            color: Embed color (use COLOR_* constants)
            fields: Optional list of field dicts with 'name' and 'value'
            footer: Optional footer text

        Returns:
            DiscordEmbed object
        """
        embed = DiscordEmbed(
            title=title,
            description=description,
            color=color
        )

        # Add timestamp
        embed.set_timestamp()

        # Add fields if provided
        if fields:
            for field in fields:
                embed.add_embed_field(
                    name=field.get("name", ""),
                    value=str(field.get("value", "")),
                    inline=field.get("inline", True)
                )

        # Add footer
        if footer:
            embed.set_footer(text=footer)
        else:
            embed.set_footer(text="Polymarket AMM Bot")

        return embed

    def _send_webhook(
        self,
        webhook_url: str,
        content: Optional[str] = None,
        embed: Optional['DiscordEmbed'] = None
    ) -> bool:
        """
        Send a message to a Discord webhook.

        Args:
            webhook_url: The webhook URL to send to
            content: Optional text content (for @mentions)
            embed: Optional embed to include

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            logger.debug("Discord notifications disabled, skipping send")
            return False

        if not webhook_url or self._is_placeholder(webhook_url):
            logger.debug(f"Invalid webhook URL, skipping send")
            return False

        try:
            webhook = DiscordWebhook(
                url=webhook_url,
                content=content,
                username="AMM Bot"
            )

            if embed:
                webhook.add_embed(embed)

            response = webhook.execute()

            if response.status_code in (200, 204):
                logger.debug("Discord message sent successfully")
                return True
            else:
                logger.warning(
                    f"Discord webhook returned {response.status_code}: "
                    f"{response.text}"
                )
                return False

        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")
            return False

    def _get_mention(self) -> str:
        """Get the @mention string for the configured user."""
        if self.user_id and not self._is_placeholder(self.user_id):
            return f"<@{self.user_id}>"
        return ""

    def send_pnl(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a PNL/profit notification.

        Uses green embed color. Does NOT @mention (routine notification).

        Args:
            message: Main message (e.g., "Trade Complete: +$1.50")
            details: Optional dict of additional details to show

        Returns:
            True if sent successfully

        Example:
            notifier.send_pnl("Trade Complete: +$1.50 locked profit", {
                "Market": "BTC 15-min Up",
                "Entry Price": "$0.48",
                "Exit Price": "$0.52",
                "Shares": "10"
            })
        """
        if not self.enabled:
            return False

        # Create fields from details
        fields = None
        if details:
            fields = [
                {"name": k, "value": v, "inline": True}
                for k, v in details.items()
            ]

        embed = self._create_embed(
            title="Trade Update",
            description=message,
            color=COLOR_GREEN,
            fields=fields
        )

        return self._send_webhook(self.webhook_pnl, embed=embed)

    def send_loss(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a loss alert notification.

        Uses red embed color. INCLUDES @mention for urgency.

        Args:
            message: Main message (e.g., "LOSS ALERT: -$2.00")
            details: Optional dict of additional details

        Returns:
            True if sent successfully

        Example:
            notifier.send_loss("LOSS ALERT: -$2.00 on BTC trade", {
                "Market": "BTC 15-min Down",
                "Loss Amount": "-$2.00",
                "Position": "Closed"
            })
        """
        if not self.enabled:
            return False

        # Create fields from details
        fields = None
        if details:
            fields = [
                {"name": k, "value": v, "inline": True}
                for k, v in details.items()
            ]

        embed = self._create_embed(
            title="LOSS ALERT",
            description=message,
            color=COLOR_RED,
            fields=fields
        )

        # Include @mention for urgency
        mention = self._get_mention()

        return self._send_webhook(
            self.webhook_losses,
            content=mention if mention else None,
            embed=embed
        )

    def send_outage(
        self,
        message: str,
        outage_type: str = "network",
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send an outage/warning notification.

        Uses orange embed color. INCLUDES @mention for urgency.

        Args:
            message: Main message describing the outage
            outage_type: Type of outage - "network", "api", or "website"
            details: Optional dict of additional details

        Returns:
            True if sent successfully

        Example:
            notifier.send_outage("WiFi connection lost", outage_type="network")
            notifier.send_outage("API rate limit hit", outage_type="api", {
                "Retry After": "60 seconds"
            })
        """
        if not self.enabled:
            return False

        # Map outage type to title
        titles = {
            "network": "NETWORK OUTAGE",
            "api": "API OUTAGE",
            "website": "WEBSITE OUTAGE",
        }
        title = titles.get(outage_type.lower(), "OUTAGE")

        # Create fields from details
        fields = [{"name": "Type", "value": outage_type.upper(), "inline": True}]
        if details:
            fields.extend([
                {"name": k, "value": v, "inline": True}
                for k, v in details.items()
            ])

        embed = self._create_embed(
            title=title,
            description=message,
            color=COLOR_ORANGE,
            fields=fields
        )

        # Include @mention for urgency
        mention = self._get_mention()

        return self._send_webhook(
            self.webhook_outages,
            content=mention if mention else None,
            embed=embed
        )

    def send_info(
        self,
        title: str,
        message: str,
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a general info notification.

        Uses blue embed color. Does NOT @mention.

        Args:
            title: Notification title
            message: Main message
            details: Optional dict of additional details

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False

        # Create fields from details
        fields = None
        if details:
            fields = [
                {"name": k, "value": v, "inline": True}
                for k, v in details.items()
            ]

        embed = self._create_embed(
            title=title,
            description=message,
            color=COLOR_BLUE,
            fields=fields
        )

        # Send to PNL channel (general updates)
        return self._send_webhook(self.webhook_pnl, embed=embed)

    def send_startup(self) -> bool:
        """
        Send a bot startup notification.

        Returns:
            True if sent successfully
        """
        return self.send_info(
            title="Bot Started",
            message="Polymarket AMM Bot is now running",
            details={
                "Mode": "DRY RUN" if self.config.dry_run_mode else "LIVE",
                "Max Position": f"${self.config.max_total_cost}",
                "Target Profit": f"${self.config.target_locked_profit}"
            }
        )

    def send_shutdown(self, reason: str = "Manual shutdown") -> bool:
        """
        Send a bot shutdown notification.

        Args:
            reason: Reason for shutdown

        Returns:
            True if sent successfully
        """
        return self.send_info(
            title="Bot Stopped",
            message=f"Polymarket AMM Bot has stopped: {reason}"
        )

    def test_connection(self) -> Dict[str, bool]:
        """
        Test all configured webhooks.

        Returns:
            Dict mapping channel names to success status
        """
        results = {}

        if self.webhook_pnl and not self._is_placeholder(self.webhook_pnl):
            results["pnl"] = self.send_info(
                "Test Message",
                "This is a test message from the Polymarket AMM Bot."
            )
        else:
            results["pnl"] = False

        if self.webhook_losses and not self._is_placeholder(self.webhook_losses):
            embed = self._create_embed(
                "Test Loss Alert",
                "This is a test loss notification.",
                COLOR_RED
            )
            results["losses"] = self._send_webhook(self.webhook_losses, embed=embed)
        else:
            results["losses"] = False

        if self.webhook_outages and not self._is_placeholder(self.webhook_outages):
            embed = self._create_embed(
                "Test Outage Alert",
                "This is a test outage notification.",
                COLOR_ORANGE
            )
            results["outages"] = self._send_webhook(self.webhook_outages, embed=embed)
        else:
            results["outages"] = False

        return results

    def __repr__(self) -> str:
        """String representation showing enabled status."""
        status = "enabled" if self.enabled else "disabled"
        return f"DiscordNotifier({status})"
