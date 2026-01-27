"""
Display Manager - Live display, logging, notifications

Extracted from run_paper_bot.py to provide a clean interface for:
- Live terminal display (Rich library)
- Web UI state updates
- Telegram/Discord notifications
- CSV trade logging
"""

import csv
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from src.utils.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass
class TradeLog:
    """
    Trade log entry for CSV.
    """
    timestamp: datetime
    market_slug: str
    event_type: str  # TRADE, RESOLUTION, POSITION_UPDATE
    trade_side: str  # UP, DOWN, PAIR
    trade_mode: str  # VOLATILITY, ASYMMETRIC, REBALANCE, etc.
    size_requested: float
    size_filled: float
    price: float
    cost: float
    # Position state
    pos_up_size: float = 0
    pos_up_avg_price: float = 0
    pos_down_size: float = 0
    pos_down_avg_price: float = 0
    pos_hedged_pairs: float = 0
    pos_pair_cost: float = 0
    pos_locked_profit: float = 0
    pos_imbalance: float = 0
    # P&L metrics
    pnl_min: float = 0
    pnl_max: float = 0
    pnl_realized: float = 0
    # Account state
    balance_after: float = 0
    status: str = "SUCCESS"  # SUCCESS, PARTIAL, FAILED, RESOLVED

    def to_row(self) -> List[Any]:
        """Convert to CSV row."""
        return [
            self.timestamp.isoformat(),
            self.market_slug,
            self.event_type,
            self.trade_side,
            self.trade_mode,
            self.size_requested,
            self.size_filled,
            self.price,
            self.cost,
            self.pos_up_size,
            self.pos_up_avg_price,
            self.pos_down_size,
            self.pos_down_avg_price,
            self.pos_hedged_pairs,
            self.pos_pair_cost,
            self.pos_locked_profit,
            self.pos_imbalance,
            self.pnl_min,
            self.pnl_max,
            self.pnl_realized,
            self.balance_after,
            self.status,
        ]


class DisplayManager:
    """
    Manages live display, notifications, and logging.

    Provides a unified interface for:
    - Rich terminal display
    - Web UI state updates via callback
    - Telegram/Discord notifications
    - CSV trade logging with daily rotation
    """

    # CSV headers
    CSV_HEADERS = [
        'timestamp', 'market_slug', 'event_type', 'trade_side', 'trade_mode',
        'size_requested', 'size_filled', 'price', 'cost',
        'pos_up_size', 'pos_up_avg_price', 'pos_down_size', 'pos_down_avg_price',
        'pos_hedged_pairs', 'pos_pair_cost', 'pos_locked_profit', 'pos_imbalance',
        'pnl_min', 'pnl_max', 'pnl_realized', 'balance_after', 'status',
    ]

    def __init__(
        self,
        strategy_name: str = "default",
        csv_base_path: str = "paper_trades.csv",
        live_display_enabled: bool = False,
        web_callback: Optional[Callable[[dict], None]] = None,
        telegram_notifier: Optional["TelegramNotifier"] = None,
        discord_interval_minutes: float = 30.0,
        quiet_mode: bool = False,
    ):
        """
        Initialize display manager.

        Args:
            strategy_name: Name of the strategy (for display)
            csv_base_path: Base path for CSV files (date will be appended)
            live_display_enabled: Enable Rich terminal display
            web_callback: Callback for web UI state updates
            telegram_notifier: Telegram notifier instance
            discord_interval_minutes: How often to send Discord updates
            quiet_mode: Suppress per-second status logs
        """
        self.strategy_name = strategy_name
        self.live_display_enabled = live_display_enabled
        self._web_callback = web_callback
        self._telegram = telegram_notifier
        self.discord_interval = timedelta(minutes=discord_interval_minutes)
        self.quiet_mode = quiet_mode

        # CSV setup with daily rotation
        self._csv_base_name = Path(csv_base_path).stem
        self._csv_dir = Path(csv_base_path).parent or Path(".")
        self._csv_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.csv_path = self._csv_dir / f"{self._csv_base_name}_{self._csv_date}.csv"

        # Rich display
        self._console: Optional["Console"] = None
        self._live: Optional["Live"] = None
        if live_display_enabled:
            try:
                from rich.console import Console
                self._console = Console()
            except ImportError:
                logger.warning("Rich library not available, disabling live display")
                self.live_display_enabled = False

        # State tracking
        self._start_time: Optional[datetime] = None
        self._last_discord_update: Optional[datetime] = None
        self._trade_count = 0
        self._total_profit = 0.0

        # Last display values (for change detection)
        self._last_up_price: float = 0.0
        self._last_down_price: float = 0.0
        self._last_spread: float = 0.0

        # Initialize CSV
        self._init_csv()

    def _init_csv(self) -> None:
        """Initialize CSV file with headers."""
        if not self.csv_path.exists():
            with open(self.csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)
            logger.info(f"Created CSV log: {self.csv_path}")

    def _check_csv_rotation(self) -> None:
        """Check if date changed and rotate to new CSV file."""
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if current_date != self._csv_date:
            logger.info(f"Date changed {self._csv_date} -> {current_date}, rotating CSV")
            self._csv_date = current_date
            self.csv_path = self._csv_dir / f"{self._csv_base_name}_{self._csv_date}.csv"
            self._init_csv()

    def set_start_time(self, start_time: Optional[datetime] = None) -> None:
        """Set the start time for uptime calculations."""
        self._start_time = start_time or datetime.now(timezone.utc)

    def log_trade(self, trade: TradeLog) -> None:
        """Log a trade to CSV."""
        self._check_csv_rotation()

        try:
            with open(self.csv_path, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(trade.to_row())
            self._trade_count += 1
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    def log_trade_simple(
        self,
        market_slug: str,
        side: str,
        price: float,
        size: float,
        trade_mode: str = "NORMAL",
        position_state: Optional[Dict[str, Any]] = None,
        balance: float = 0.0,
        status: str = "SUCCESS",
    ) -> None:
        """
        Simplified trade logging.

        Args:
            market_slug: Market identifier
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size
            trade_mode: Trade type (NORMAL, HEDGE, EMERGENCY, etc.)
            position_state: Current position state dict
            balance: Current balance
            status: Fill status
        """
        pos = position_state or {}

        trade = TradeLog(
            timestamp=datetime.now(timezone.utc),
            market_slug=market_slug,
            event_type="TRADE",
            trade_side=side,
            trade_mode=trade_mode,
            size_requested=size,
            size_filled=size,
            price=price,
            cost=price * size,
            pos_up_size=pos.get("up_shares", 0),
            pos_up_avg_price=pos.get("up_avg_price", 0),
            pos_down_size=pos.get("down_shares", 0),
            pos_down_avg_price=pos.get("down_avg_price", 0),
            pos_hedged_pairs=pos.get("hedged_pairs", 0),
            pos_pair_cost=pos.get("pair_cost", 0),
            pos_locked_profit=pos.get("locked_profit", 0),
            pos_imbalance=pos.get("imbalance", 0),
            balance_after=balance,
            status=status,
        )

        self.log_trade(trade)

    def log_resolution(
        self,
        market_slug: str,
        outcome: str,
        profit: float,
        position_state: Optional[Dict[str, Any]] = None,
        balance: float = 0.0,
    ) -> None:
        """Log a market resolution event."""
        pos = position_state or {}

        trade = TradeLog(
            timestamp=datetime.now(timezone.utc),
            market_slug=market_slug,
            event_type="RESOLUTION",
            trade_side=outcome,
            trade_mode="RESOLUTION",
            size_requested=0,
            size_filled=0,
            price=0,
            cost=0,
            pos_up_size=pos.get("up_shares", 0),
            pos_up_avg_price=pos.get("up_avg_price", 0),
            pos_down_size=pos.get("down_shares", 0),
            pos_down_avg_price=pos.get("down_avg_price", 0),
            pos_hedged_pairs=pos.get("hedged_pairs", 0),
            pos_pair_cost=pos.get("pair_cost", 0),
            pos_locked_profit=pos.get("locked_profit", 0),
            pos_imbalance=pos.get("imbalance", 0),
            pnl_realized=profit,
            balance_after=balance,
            status="RESOLVED",
        )

        self.log_trade(trade)
        self._total_profit += profit

    def build_web_state(
        self,
        market_slug: Optional[str],
        balance: float,
        position_state: Optional[Dict[str, Any]] = None,
        prices: Optional[Dict[str, float]] = None,
        additional_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build state dictionary for web UI.

        Args:
            market_slug: Current market
            balance: Current balance
            position_state: Position state dict
            prices: Current prices {up_bid, up_ask, down_bid, down_ask}
            additional_data: Any additional data to include

        Returns:
            State dictionary for web callback
        """
        pos = position_state or {}
        prices = prices or {}

        uptime_seconds = 0
        if self._start_time:
            uptime_seconds = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        state = {
            "strategy_name": self.strategy_name,
            "running": True,
            "market_slug": market_slug,
            "balance": balance,
            "uptime_seconds": uptime_seconds,
            "trade_count": self._trade_count,
            "total_profit": self._total_profit,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # Position
            "position": {
                "up_shares": pos.get("up_shares", 0),
                "down_shares": pos.get("down_shares", 0),
                "hedged_pairs": pos.get("hedged_pairs", 0),
                "imbalance": pos.get("imbalance", 0),
                "pair_cost": pos.get("pair_cost", 0),
                "locked_profit": pos.get("locked_profit", 0),
            },
            # Prices
            "prices": {
                "up_bid": prices.get("up_bid", 0),
                "up_ask": prices.get("up_ask", 0),
                "down_bid": prices.get("down_bid", 0),
                "down_ask": prices.get("down_ask", 0),
            },
        }

        if additional_data:
            state.update(additional_data)

        return state

    def send_web_update(
        self,
        market_slug: Optional[str],
        balance: float,
        position_state: Optional[Dict[str, Any]] = None,
        prices: Optional[Dict[str, float]] = None,
        additional_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send state update to web UI."""
        if not self._web_callback:
            return

        try:
            state = self.build_web_state(
                market_slug=market_slug,
                balance=balance,
                position_state=position_state,
                prices=prices,
                additional_data=additional_data,
            )
            self._web_callback(state)
        except Exception as e:
            logger.debug(f"Web callback error: {e}")

    def send_trade_event(
        self,
        side: str,
        size: float,
        price: float,
        action: str = "BUY",
    ) -> None:
        """Send trade event to web UI."""
        if not self._web_callback:
            return

        try:
            event = {
                "type": "trade",
                "strategy_name": self.strategy_name,
                "side": side,
                "size": size,
                "price": price,
                "action": action,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._web_callback(event)
        except Exception as e:
            logger.debug(f"Trade event callback error: {e}")

    async def send_telegram_update(
        self,
        message: str,
        is_final: bool = False,
    ) -> None:
        """Send update via Telegram."""
        if not self._telegram:
            return

        try:
            prefix = "[FINAL] " if is_final else ""
            await self._telegram.send_message(f"{prefix}{message}")
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    async def send_discord_update(
        self,
        balance: float,
        total_profit: float,
        position_state: Optional[Dict[str, Any]] = None,
        market_slug: Optional[str] = None,
        is_final: bool = False,
        force: bool = False,
    ) -> None:
        """
        Send periodic update via Discord webhook.

        Args:
            balance: Current balance
            total_profit: Total P&L
            position_state: Current position
            market_slug: Current market
            is_final: True if final update
            force: Send even if interval not elapsed
        """
        now = datetime.now(timezone.utc)

        # Check interval
        if not force and not is_final:
            if self._last_discord_update:
                if now - self._last_discord_update < self.discord_interval:
                    return

        self._last_discord_update = now

        # Build message
        pos = position_state or {}
        uptime_hours = 0
        if self._start_time:
            uptime_hours = (now - self._start_time).total_seconds() / 3600

        status = "FINAL" if is_final else "UPDATE"
        emoji = "" if is_final else ""

        message = (
            f"{emoji} **{self.strategy_name}** {status}\n"
            f"Balance: ${balance:.2f}\n"
            f"P&L: ${total_profit:+.2f}\n"
            f"Trades: {self._trade_count}\n"
            f"Uptime: {uptime_hours:.1f}h\n"
        )

        if market_slug:
            message += f"Market: {market_slug}\n"

        if pos:
            message += (
                f"Position: UP={pos.get('up_shares', 0):.0f} / "
                f"DOWN={pos.get('down_shares', 0):.0f}\n"
            )

        # Send via webhook (implement as needed)
        logger.info(f"[DISCORD] {message}")

    def build_live_display(
        self,
        market_slug: Optional[str],
        balance: float,
        position_state: Optional[Dict[str, Any]] = None,
        prices: Optional[Dict[str, float]] = None,
        velocity_bps: float = 0.0,
        time_remaining_secs: float = 0.0,
    ) -> Optional["Panel"]:
        """
        Build Rich Panel for live terminal display.

        Args:
            market_slug: Current market
            balance: Current balance
            position_state: Position state
            prices: Current prices
            velocity_bps: BTC velocity in bps/sec
            time_remaining_secs: Seconds until resolution

        Returns:
            Rich Panel or None if disabled
        """
        if not self.live_display_enabled or not self._console:
            return None

        try:
            from rich.panel import Panel
            from rich.table import Table
            from rich.text import Text

            pos = position_state or {}
            prices = prices or {}

            # Build table
            table = Table(show_header=False, box=None, padding=(0, 1))

            # Market info
            table.add_row(
                Text("Market:", style="bold"),
                Text(market_slug or "None"),
            )

            # Time remaining
            minutes = int(time_remaining_secs // 60)
            seconds = int(time_remaining_secs % 60)
            table.add_row(
                Text("Time:", style="bold"),
                Text(f"{minutes}:{seconds:02d}"),
            )

            # Balance
            table.add_row(
                Text("Balance:", style="bold"),
                Text(f"${balance:.2f}", style="green" if balance > 0 else "red"),
            )

            # Position
            up = pos.get("up_shares", 0)
            down = pos.get("down_shares", 0)
            imbalance = abs(up - down)
            table.add_row(
                Text("Position:", style="bold"),
                Text(f"UP={up:.0f} | DOWN={down:.0f} | Δ={imbalance:.0f}"),
            )

            # Prices
            up_bid = prices.get("up_bid", 0)
            down_bid = prices.get("down_bid", 0)
            spread = up_bid + down_bid
            table.add_row(
                Text("Prices:", style="bold"),
                Text(f"UP=${up_bid:.2f} | DOWN=${down_bid:.2f} | Σ=${spread:.2f}"),
            )

            # Velocity
            vel_style = "green" if velocity_bps > 0 else "red" if velocity_bps < 0 else "white"
            table.add_row(
                Text("Velocity:", style="bold"),
                Text(f"{velocity_bps:+.2f} bps/s", style=vel_style),
            )

            # P&L
            table.add_row(
                Text("Total P&L:", style="bold"),
                Text(
                    f"${self._total_profit:+.2f}",
                    style="green" if self._total_profit > 0 else "red",
                ),
            )

            # Create panel
            title = f" {self.strategy_name} "
            return Panel(table, title=title, border_style="blue")

        except Exception as e:
            logger.debug(f"Display build error: {e}")
            return None

    def start_live_display(self) -> None:
        """Start the Rich Live display."""
        if not self.live_display_enabled or not self._console:
            return

        try:
            from rich.live import Live

            self._live = Live(
                console=self._console,
                refresh_per_second=4,
                transient=True,
            )
            self._live.start()
        except Exception as e:
            logger.warning(f"Failed to start live display: {e}")

    def stop_live_display(self) -> None:
        """Stop the Rich Live display."""
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def update_live_display(self, panel: Optional["Panel"]) -> None:
        """Update the live display with new panel."""
        if self._live and panel:
            try:
                self._live.update(panel)
            except Exception:
                pass

    def get_metrics(self) -> Dict[str, Any]:
        """Get display manager metrics."""
        uptime = 0
        if self._start_time:
            uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        return {
            "trade_count": self._trade_count,
            "total_profit": self._total_profit,
            "uptime_seconds": uptime,
            "csv_path": str(self.csv_path),
            "live_display_enabled": self.live_display_enabled,
        }
