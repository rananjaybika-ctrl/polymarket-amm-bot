"""
Telegram bot notification and command system.

Replaces Discord webhooks with Telegram for:
- Trade notifications (PNL updates, losses, outages)
- Remote commands (/stop, /sell_all, /status, /balance)

Setup:
1. Message @BotFather on Telegram, send /newbot
2. Copy the bot token to TELEGRAM_BOT_TOKEN in .env
3. Message your bot, then get chat_id via:
   curl https://api.telegram.org/bot<TOKEN>/getUpdates
4. Add chat_id to TELEGRAM_CHAT_ID in .env

Usage:
    from src.config import Config
    from src.utils.telegram_notifier import TelegramNotifier

    config = Config()
    notifier = TelegramNotifier(config)

    # Start listening for commands (non-blocking)
    await notifier.start()

    # Send notifications
    await notifier.send_pnl("Trade Complete: +$1.50", {"Market": "BTC 15-min"})

    # Register command handlers
    notifier.on_stop(my_stop_callback)
    notifier.on_sell_all(my_sell_callback)
"""

import asyncio
import logging
from typing import Optional, Dict, Any, Callable, Awaitable
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Check for telegram library
try:
    import aiohttp
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("aiohttp not installed for Telegram. Run: pip install aiohttp")


class TelegramNotifierError(Exception):
    """Base exception for Telegram notifier errors."""
    pass


class TelegramNotifier:
    """
    Telegram bot for notifications and remote commands.

    Provides:
    - Outbound notifications (trades, alerts, status)
    - Inbound command handling (/stop, /sell_all, /status, /balance)

    Attributes:
        config: Bot configuration with Telegram settings
        enabled: Whether Telegram is properly configured
    """

    # Class-level flag and lock to prevent multiple instances from polling
    _polling_active = False
    _polling_lock: asyncio.Lock = None  # Initialized lazily

    def __init__(self, config: 'Config', trading_mode: str = "paper"):
        """
        Initialize the Telegram notifier.

        Args:
            config: Configuration object with Telegram settings
            trading_mode: "paper" or "live" - displayed in all messages
        """
        self.config = config
        self.trading_mode = trading_mode.upper()  # "PAPER" or "LIVE"
        self.bot_token = getattr(config, 'telegram_bot_token', '')
        self.chat_id = getattr(config, 'telegram_chat_id', '')

        # Check if properly configured
        self.enabled = bool(
            TELEGRAM_AVAILABLE and
            self.bot_token and
            self.chat_id and
            not self._is_placeholder(self.bot_token)
        )

        if not self.enabled:
            logger.info("Telegram notifications disabled - not configured")

        # Command callbacks
        self._on_stop: Optional[Callable[[], Awaitable[None]]] = None
        self._on_sell_all: Optional[Callable[[], Awaitable[None]]] = None
        self._on_status: Optional[Callable[[], Awaitable[str]]] = None
        self._on_balance: Optional[Callable[[], Awaitable[str]]] = None

        # Graceful stop callbacks (stop after current market ends)
        self._on_graceful_stop_calculus_maker: Optional[Callable[[], Awaitable[None]]] = None
        self._on_graceful_stop_simple_hedger: Optional[Callable[[], Awaitable[None]]] = None
        self._on_graceful_stop_grid_maker: Optional[Callable[[], Awaitable[None]]] = None
        self._on_graceful_stop_directional: Optional[Callable[[], Awaitable[None]]] = None

        # Polling state
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._last_update_id = 0

    def _is_placeholder(self, value: str) -> bool:
        """Check if a value is a placeholder."""
        if not value:
            return True
        placeholders = ["your_", "xxx", "placeholder", "example", "TOKEN"]
        return any(p in value for p in placeholders)

    @property
    def api_url(self) -> str:
        """Base URL for Telegram API."""
        return f"https://api.telegram.org/bot{self.bot_token}"

    async def _send_request(self, method: str, data: Dict[str, Any]) -> Optional[Dict]:
        """Send a request to Telegram API."""
        if not self.enabled:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.api_url}/{method}"
                async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        text = await response.text()
                        logger.warning(f"Telegram API error {response.status}: {text}")
                        return None
        except Exception as e:
            logger.error(f"Telegram request failed: {type(e).__name__}: {e}")
            return None

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
        reply_markup: Optional[Dict] = None,
    ) -> bool:
        """
        Send a message to the configured chat.

        Args:
            text: Message text (supports HTML formatting)
            parse_mode: "HTML" or "Markdown"
            disable_notification: If True, send silently
            reply_markup: Optional inline keyboard markup

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            logger.debug("Telegram disabled, skipping message")
            return False

        data = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }
        if reply_markup:
            data["reply_markup"] = reply_markup

        result = await self._send_request("sendMessage", data)

        return result is not None and result.get("ok", False)

    async def send_emergency_buttons(self) -> bool:
        """Send emergency control panel with inline buttons."""
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "\u2753 Help", "callback_data": "help"},
                    {"text": "\U0001F4B0 Balances", "callback_data": "balances"},
                ],
                [
                    {"text": "\u2622\ufe0f NUKE ALL", "callback_data": "nuke_all"},
                ],
            ]
        }
        return await self.send_message(
            "<b>\U0001F3AE Control Panel</b>\n\nUse buttons below to control the bot:",
            reply_markup=keyboard,
        )

    async def send_control_panel(self) -> bool:
        """Send the main control panel with inline buttons."""
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "\u2753 Help", "callback_data": "help"},
                    {"text": "\U0001F4B0 Balances", "callback_data": "balances"},
                    {"text": "\U0001F4CA Status", "callback_data": "status"},
                ],
                # Graceful stop buttons (stop after current market)
                [
                    {"text": "\u23F8 Calc", "callback_data": "graceful_stop_calculus_maker"},
                    {"text": "\u23F8 Simple", "callback_data": "graceful_stop_simple_hedger"},
                    {"text": "\u23F8 GRID", "callback_data": "graceful_stop_grid_maker"},
                    {"text": "\u23F8 Dir", "callback_data": "graceful_stop_directional"},
                ],
                [
                    {"text": "\u2622\ufe0f NUKE ALL", "callback_data": "nuke_all"},
                ],
            ]
        }
        return await self.send_message(
            "<b>\U0001F916 Polymarket AMM Bot</b>\n\n"
            "<b>Graceful Stop:</b> Stops after current market ends\n"
            "<b>NUKE ALL:</b> Sell all positions and stop",
            reply_markup=keyboard,
        )

    def _format_embed(
        self,
        title: str,
        description: str,
        color: str,  # "green", "red", "orange", "blue"
        fields: Optional[Dict[str, Any]] = None,
        footer: Optional[str] = None,
    ) -> str:
        """Format message like Discord embed."""
        # Color indicators (emoji circles like Discord's colored sidebar)
        colors = {
            "green": "\U0001F7E2",   # Green circle
            "red": "\U0001F534",     # Red circle
            "orange": "\U0001F7E0",  # Orange circle
            "blue": "\U0001F535",    # Blue circle
        }
        bar = colors.get(color, "\U0001F535")

        # Build message
        text = f"{bar} <b>{title}</b>\n"
        text += f"{description}"

        if fields:
            text += "\n"
            for key, value in fields.items():
                text += f"\n<b>{key}:</b>  {value}"

        # Always include Type and footer
        footer_text = f"Type: {self.trading_mode}"
        if footer:
            footer_text = f"{footer} | {footer_text}"
        text += f"\n\n<i>{footer_text}</i>"

        return text

    async def send_pnl(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a PNL/trade notification (green embed style).

        Args:
            message: Main message
            details: Optional dict of details

        Returns:
            True if sent successfully
        """
        text = self._format_embed(
            title="Trade Update",
            description=message,
            color="green",
            fields=details,
            footer="Polymarket AMM Bot",
        )
        return await self.send_message(text)

    async def send_loss(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a loss alert (red embed style).

        Args:
            message: Main message
            details: Optional dict of details

        Returns:
            True if sent successfully
        """
        text = self._format_embed(
            title="LOSS ALERT",
            description=message,
            color="red",
            fields=details,
            footer="Polymarket AMM Bot",
        )
        return await self.send_message(text, disable_notification=False)

    async def send_outage(
        self,
        message: str,
        outage_type: str = "network",
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send an outage alert (orange embed style).

        Args:
            message: Main message
            outage_type: "network", "api", or "website"
            details: Optional dict of details

        Returns:
            True if sent successfully
        """
        titles = {
            "network": "NETWORK OUTAGE",
            "api": "API OUTAGE",
            "website": "WEBSITE OUTAGE",
        }
        title = titles.get(outage_type.lower(), "OUTAGE")

        text = self._format_embed(
            title=title,
            description=message,
            color="orange",
            fields=details,
            footer="Polymarket AMM Bot",
        )

        return await self.send_message(text, disable_notification=False)

    async def send_info(
        self,
        title: str,
        message: str,
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a general info notification (blue embed style).

        Args:
            title: Notification title
            message: Main message
            details: Optional dict of details

        Returns:
            True if sent successfully
        """
        text = self._format_embed(
            title=title,
            description=message,
            color="blue",
            fields=details,
            footer="Polymarket AMM Bot",
        )
        return await self.send_message(text, disable_notification=True)

    async def send_startup(self) -> bool:
        """Send bot startup notification."""
        return await self.send_info(
            "Bot Started",
            "Polymarket AMM Bot is now running",
            {
                "Mode": "DRY RUN" if self.config.dry_run_mode else "LIVE",
                "Max Position": f"${self.config.max_total_cost}",
            }
        )

    async def send_shutdown(self, reason: str = "Manual shutdown") -> bool:
        """Send bot shutdown notification."""
        return await self.send_info(
            "Bot Stopped",
            f"Polymarket AMM Bot has stopped: {reason}"
        )

    # === Command Handlers ===

    def on_stop(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for /stop command."""
        self._on_stop = callback

    def on_sell_all(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for /sell_all command."""
        self._on_sell_all = callback

    def on_status(self, callback: Callable[[], Awaitable[str]]) -> None:
        """Register callback for /status command (should return status string)."""
        self._on_status = callback

    def on_balance(self, callback: Callable[[], Awaitable[str]]) -> None:
        """Register callback for /balance command (should return balance string)."""
        self._on_balance = callback

    def on_graceful_stop_calculus_maker(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for graceful stop of Calculus Maker mode (stops after current market)."""
        self._on_graceful_stop_calculus_maker = callback

    def on_graceful_stop_simple_hedger(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for graceful stop of Simple Hedger mode (stops after current market)."""
        self._on_graceful_stop_simple_hedger = callback

    def on_graceful_stop_grid_maker(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for graceful stop of Grid Maker mode (stops after current market)."""
        self._on_graceful_stop_grid_maker = callback

    def on_graceful_stop_directional(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for graceful stop of Directional mode (stops after current market)."""
        self._on_graceful_stop_directional = callback

    async def _handle_command(self, command: str, chat_id: str) -> None:
        """Handle incoming command."""
        # Only respond to configured chat
        if str(chat_id) != str(self.chat_id):
            logger.warning(f"Ignoring command from unauthorized chat: {chat_id}")
            return

        command = command.lower().strip()
        logger.info(f"Received command: {command}")

        # Quick trigger for control panel - just send "p"
        if command == "p":
            await self.send_control_panel()
            return

        if command == "/stop":
            if self._on_stop:
                await self.send_message("Stopping bot...")
                await self._on_stop()
                await self.send_message("Bot stopped.")
            else:
                await self.send_message("Stop handler not registered.")

        elif command == "/sell_all" or command == "/emergency_sell":
            if self._on_sell_all:
                await self.send_message("Executing emergency sell...")
                await self._on_sell_all()
                await self.send_message("Emergency sell complete.")
            else:
                await self.send_message("Sell handler not registered.")

        elif command == "/status":
            if self._on_status:
                status = await self._on_status()
                await self.send_message(f"<b>Status</b>\n<pre>{status}</pre>")
            else:
                await self.send_message("Status handler not registered.")

        elif command == "/balance":
            if self._on_balance:
                balance = await self._on_balance()
                await self.send_message(f"<b>Balance</b>\n{balance}")
            else:
                await self.send_message("Balance handler not registered.")

        elif command == "/emergency" or command == "/e":
            await self.send_emergency_buttons()

        elif command == "/panel" or command == "/p":
            await self.send_control_panel()

        elif command == "/help":
            help_text = """<b>\u2753 Help - Available Commands</b>

<b>Quick Access:</b>
<b>p</b> - Show control panel with buttons
/panel - Show control panel with buttons

<b>Status Commands:</b>
/status - Get current bot status
/balance - Get current balance

<b>Control Commands:</b>
/stop - Stop the bot gracefully
/sell_all - Emergency sell all positions

<b>Modes:</b>
\u2022 <b>Calculus Maker</b> - Dynamic mispricing detection
\u2022 <b>Simple Hedger</b> - Flip-based hedging strategy
\u2022 <b>Grid Maker</b> - Gabagool-style hedging
\u2022 <b>Directional</b> - Bias-based with Binance feed"""
            await self.send_message(help_text)

        elif command.startswith("/"):
            await self.send_message(f"Unknown command: {command}\nTry /help or send <b>p</b> for buttons")

    async def _handle_callback(self, callback_id: str, data: str, chat_id: str) -> None:
        """Handle callback query from inline button."""
        if str(chat_id) != str(self.chat_id):
            return

        logger.info(f"Button pressed: {data}")

        # Answer callback to remove loading state
        await self._send_request("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Processing...",
        })

        if data == "emergency_stop_all" or data == "nuke_all":
            # NUKE ALL - Emergency sell and stop all strategies
            await self.send_message(
                "\u2622\ufe0f <b>NUKE ALL INITIATED</b>\n\n"
                "Selling all positions and stopping all strategies..."
            )
            if self._on_sell_all:
                await self._on_sell_all()
            if self._on_stop:
                await self._on_stop()
            await self.send_message(
                "\u2705 <b>NUKE COMPLETE</b>\n\n"
                "All positions sold. All strategies stopped."
            )

        elif data == "help":
            help_text = """<b>\u2753 Help - Available Commands</b>

<b>Buttons:</b>
\u2022 <b>Help</b> - Show this message
\u2022 <b>Balances</b> - Check live & paper balances
\u2022 <b>Status</b> - Get current bot status
\u2022 <b>Calc/Simple/GRID/Dir</b> - Graceful stop strategy
\u2022 <b>NUKE ALL</b> - Emergency sell all & stop

<b>Text Commands:</b>
/status - Get current bot status
/balance - Get current balance
/stop - Stop the bot gracefully
/sell_all - Emergency sell all positions
/panel - Show control panel
/help - Show this message

<b>Modes:</b>
\u2022 <b>Calculus Maker</b> - Dynamic mispricing detection
\u2022 <b>Simple Hedger</b> - Flip-based hedging strategy
\u2022 <b>Grid Maker</b> - Gabagool-style hedging
\u2022 <b>Directional</b> - Bias-based with Binance feed"""
            await self.send_message(help_text)

        elif data == "balances":
            # Show balances for all running strategies
            balance_text = "<b>\U0001F4B0 Balances</b>\n\n"
            if self._on_balance:
                balance = await self._on_balance()
                balance_text += f"<b>Current Bot:</b>\n{balance}"
            else:
                balance_text += "<i>No bot currently running</i>"
            await self.send_message(balance_text)

        elif data == "status":
            if self._on_status:
                status = await self._on_status()
                await self.send_message(f"<b>\U0001F4CA Status</b>\n<pre>{status}</pre>")
            else:
                await self.send_message("<i>Bot not running.</i>")

        elif data == "balance":
            if self._on_balance:
                balance = await self._on_balance()
                await self.send_message(f"<b>\U0001F4B0 Balance</b>\n{balance}")
            else:
                await self.send_message("<i>Bot not running.</i>")

        elif data == "stop":
            if self._on_stop:
                await self.send_message("\u23F9 <b>Stopping all bots immediately...</b>")
                await self._on_stop()
                await self.send_message("\u2705 All bots stopped.")
            else:
                await self.send_message("<i>Stop handler not registered.</i>")

        # Graceful stop handlers (stop after current market ends)
        elif data == "graceful_stop_calculus_maker":
            if self._on_graceful_stop_calculus_maker:
                await self.send_message("\u23F8 <b>Calculus Maker: Will stop after current market...</b>")
                await self._on_graceful_stop_calculus_maker()
                await self.send_message("\u2705 Calculus Maker mode flagged for graceful stop.")
            else:
                await self.send_message("<i>Calculus Maker mode not running.</i>")

        elif data == "graceful_stop_simple_hedger":
            if self._on_graceful_stop_simple_hedger:
                await self.send_message("\u23F8 <b>Simple Hedger: Will stop after current market...</b>")
                await self._on_graceful_stop_simple_hedger()
                await self.send_message("\u2705 Simple Hedger mode flagged for graceful stop.")
            else:
                await self.send_message("<i>Simple Hedger mode not running.</i>")

        elif data == "graceful_stop_grid_maker":
            if self._on_graceful_stop_grid_maker:
                await self.send_message("\u23F8 <b>Grid Maker: Will stop after current market...</b>")
                await self._on_graceful_stop_grid_maker()
                await self.send_message("\u2705 Grid Maker mode flagged for graceful stop.")
            else:
                await self.send_message("<i>Grid Maker mode not running.</i>")

        elif data == "graceful_stop_directional":
            if self._on_graceful_stop_directional:
                await self.send_message("\u23F8 <b>Directional: Will stop after current market...</b>")
                await self._on_graceful_stop_directional()
                await self.send_message("\u2705 Directional mode flagged for graceful stop.")
            else:
                await self.send_message("<i>Directional mode not running.</i>")

    async def _poll_updates(self) -> None:
        """Poll for new messages/commands and button callbacks."""
        while self._running:
            try:
                result = await self._send_request("getUpdates", {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,
                    "allowed_updates": ["message", "callback_query"],
                })

                if result and result.get("ok"):
                    for update in result.get("result", []):
                        self._last_update_id = update["update_id"]

                        # Handle text commands (both /commands and quick triggers)
                        message = update.get("message", {})
                        if message:
                            text = message.get("text", "")
                            chat_id = message.get("chat", {}).get("id", "")
                            if text:  # Handle any text message
                                await self._handle_command(text, chat_id)

                        # Handle button callbacks
                        callback = update.get("callback_query", {})
                        if callback:
                            callback_id = callback.get("id", "")
                            data = callback.get("data", "")
                            chat_id = callback.get("message", {}).get("chat", {}).get("id", "")
                            await self._handle_callback(callback_id, data, chat_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error polling Telegram updates: {e}")
                await asyncio.sleep(5)

    async def start(self) -> None:
        """Start listening for commands (non-blocking)."""
        if not self.enabled:
            logger.info("Telegram not enabled, skipping command listener")
            return

        if self._running:
            return

        # Initialize lock lazily (class-level lock shared across instances)
        if TelegramNotifier._polling_lock is None:
            TelegramNotifier._polling_lock = asyncio.Lock()

        # Use lock to prevent race condition in check-then-set
        async with TelegramNotifier._polling_lock:
            # Check if another instance is already polling to avoid 409 conflicts
            if TelegramNotifier._polling_active:
                logger.info("Telegram polling already active in another instance, skipping (messages will still be sent)")
                return

            self._running = True
            TelegramNotifier._polling_active = True

        self._poll_task = asyncio.create_task(self._poll_updates())
        logger.info("Telegram command listener started")

    async def stop(self) -> None:
        """Stop listening for commands."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            # Reset class-level flag when this instance stops polling
            TelegramNotifier._polling_active = False
        logger.info("Telegram command listener stopped")

    async def test_connection(self) -> bool:
        """
        Test the Telegram connection.

        Returns:
            True if connection successful
        """
        if not self.enabled:
            return False

        result = await self._send_request("getMe", {})
        if result and result.get("ok"):
            bot_name = result.get("result", {}).get("username", "unknown")
            logger.info(f"Telegram connected as @{bot_name}")

            # Send test message
            await self.send_message(
                "<b>Test Message</b>\nPolymarket AMM Bot connected successfully.\n"
                "Send /help to see available commands."
            )
            return True
        return False

    def __repr__(self) -> str:
        """String representation showing enabled status."""
        status = "enabled" if self.enabled else "disabled"
        return f"TelegramNotifier({status})"
