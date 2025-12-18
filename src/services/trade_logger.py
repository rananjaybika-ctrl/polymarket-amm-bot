"""
Trade Logger service for recording and exporting trade history.

Provides comprehensive logging with CSV export and statistics.
"""

import csv
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from typing import List, Optional, Dict, Any
import uuid

from src.models.market import BTCMarket
from src.models.trade_log import TradeEntry, PairTradeEntry, TradeStats


logger = logging.getLogger(__name__)


class TradeLogger:
    """
    Logs and exports trade history.

    Maintains in-memory log with CSV export capabilities.

    Example:
        logger = TradeLogger()

        # Log a pair trade
        pair = logger.log_pair_trade(
            market=market,
            up_price=0.51, up_size=10,
            down_price=0.51, down_size=10,
        )

        # Export to CSV
        logger.export_pairs_csv("trades.csv")

        # Get statistics
        stats = logger.get_total_stats()
    """

    def __init__(self, log_dir: Optional[Path] = None):
        """
        Initialize TradeLogger.

        Args:
            log_dir: Directory for CSV exports (default: ./logs)
        """
        self.log_dir = log_dir or Path("./logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._trades: List[TradeEntry] = []
        self._pair_trades: List[PairTradeEntry] = []
        self._current_session: str = ""

    def start_session(self) -> str:
        """
        Start a new trading session.

        Returns:
            Session ID
        """
        self._current_session = f"sess-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        logger.info(f"Started logging session: {self._current_session}")
        return self._current_session

    @property
    def session_id(self) -> str:
        """Current session ID."""
        if not self._current_session:
            self.start_session()
        return self._current_session

    def log_trade(
        self,
        market: BTCMarket,
        side: str,
        action: str,
        price: float,
        size: float,
        order_id: str = "",
        fill_status: str = "filled",
        pair_id: Optional[str] = None,
        notes: str = "",
    ) -> TradeEntry:
        """
        Log a single trade.

        Args:
            market: BTCMarket traded
            side: "UP" or "DOWN"
            action: "BUY" or "SELL"
            price: Execution price
            size: Quantity
            order_id: Order ID
            fill_status: "filled", "partial", "cancelled"
            pair_id: ID linking pair trades
            notes: Optional notes

        Returns:
            TradeEntry created
        """
        entry = TradeEntry(
            market_slug=market.slug,
            market_question=market.question,
            condition_id=market.condition_id,
            side=side.upper(),
            action=action.upper(),
            price=price,
            size=size,
            order_id=order_id,
            fill_status=fill_status,
            pair_id=pair_id,
            session_id=self.session_id,
            notes=notes,
        )

        self._trades.append(entry)
        logger.debug(f"Logged trade: {entry}")

        return entry

    def log_pair_trade(
        self,
        market: BTCMarket,
        up_price: float,
        up_size: float,
        down_price: float,
        down_size: float,
        up_order_id: str = "",
        down_order_id: str = "",
        notes: str = "",
    ) -> PairTradeEntry:
        """
        Log a pair trade (Up + Down together).

        Args:
            market: BTCMarket traded
            up_price: Up token price
            up_size: Up quantity
            down_price: Down token price
            down_size: Down quantity
            up_order_id: Up order ID
            down_order_id: Down order ID
            notes: Optional notes

        Returns:
            PairTradeEntry created
        """
        pair_id = str(uuid.uuid4())[:8]

        # Create pair entry
        pair = PairTradeEntry(
            pair_id=pair_id,
            market_slug=market.slug,
            market_question=market.question,
            up_price=up_price,
            up_size=up_size,
            up_order_id=up_order_id,
            down_price=down_price,
            down_size=down_size,
            down_order_id=down_order_id,
            session_id=self.session_id,
            notes=notes,
        )

        self._pair_trades.append(pair)

        # Also log individual trades
        self.log_trade(
            market=market,
            side="UP",
            action="BUY",
            price=up_price,
            size=up_size,
            order_id=up_order_id,
            pair_id=pair_id,
        )
        self.log_trade(
            market=market,
            side="DOWN",
            action="BUY",
            price=down_price,
            size=down_size,
            order_id=down_order_id,
            pair_id=pair_id,
        )

        logger.info(f"Logged pair trade: {pair}")

        return pair

    def get_trades(
        self,
        session_id: Optional[str] = None,
        market_slug: Optional[str] = None,
        side: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[TradeEntry]:
        """
        Get trades with optional filters.

        Args:
            session_id: Filter by session
            market_slug: Filter by market
            side: Filter by side (UP/DOWN)
            start_date: Filter by start date
            end_date: Filter by end date

        Returns:
            List of matching TradeEntry
        """
        trades = self._trades

        if session_id:
            trades = [t for t in trades if t.session_id == session_id]

        if market_slug:
            trades = [t for t in trades if t.market_slug == market_slug]

        if side:
            trades = [t for t in trades if t.side.upper() == side.upper()]

        if start_date:
            trades = [t for t in trades if t.timestamp.date() >= start_date]

        if end_date:
            trades = [t for t in trades if t.timestamp.date() <= end_date]

        return trades

    def get_pair_trades(
        self,
        session_id: Optional[str] = None,
        profitable_only: bool = False,
    ) -> List[PairTradeEntry]:
        """
        Get pair trades with optional filters.

        Args:
            session_id: Filter by session
            profitable_only: Only return profitable pairs

        Returns:
            List of matching PairTradeEntry
        """
        pairs = self._pair_trades

        if session_id:
            pairs = [p for p in pairs if p.session_id == session_id]

        if profitable_only:
            pairs = [p for p in pairs if p.is_profitable]

        return pairs

    def export_csv(self, filepath: Optional[Path] = None) -> Path:
        """
        Export all trades to CSV.

        Args:
            filepath: Output path (default: logs/trades_{date}.csv)

        Returns:
            Path to exported file
        """
        if filepath is None:
            filepath = self.log_dir / f"trades_{date.today().isoformat()}.csv"

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(TradeEntry.csv_headers())
            for trade in self._trades:
                writer.writerow(trade.to_csv_row())

        logger.info(f"Exported {len(self._trades)} trades to {filepath}")
        return filepath

    def export_pairs_csv(self, filepath: Optional[Path] = None) -> Path:
        """
        Export pair trades to CSV.

        Args:
            filepath: Output path (default: logs/pairs_{date}.csv)

        Returns:
            Path to exported file
        """
        if filepath is None:
            filepath = self.log_dir / f"pairs_{date.today().isoformat()}.csv"

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(PairTradeEntry.csv_headers())
            for pair in self._pair_trades:
                writer.writerow(pair.to_csv_row())

        logger.info(f"Exported {len(self._pair_trades)} pair trades to {filepath}")
        return filepath

    def get_session_stats(self, session_id: Optional[str] = None) -> TradeStats:
        """
        Get statistics for a session.

        Args:
            session_id: Session to analyze (default: current)

        Returns:
            TradeStats for session
        """
        session_id = session_id or self._current_session
        pairs = self.get_pair_trades(session_id=session_id)

        stats = TradeStats()
        stats.total_trades = len(pairs)

        for pair in pairs:
            stats.total_pairs += pair.pair_count
            stats.total_cost += pair.total_cost
            stats.total_profit += pair.total_profit

            if pair.is_profitable:
                stats.winning_trades += 1
            else:
                stats.losing_trades += 1

        return stats

    def get_daily_stats(self, day: Optional[date] = None) -> TradeStats:
        """
        Get statistics for a day.

        Args:
            day: Date to analyze (default: today)

        Returns:
            TradeStats for day
        """
        day = day or date.today()

        pairs = [
            p for p in self._pair_trades
            if p.timestamp.date() == day
        ]

        stats = TradeStats()
        stats.total_trades = len(pairs)

        for pair in pairs:
            stats.total_pairs += pair.pair_count
            stats.total_cost += pair.total_cost
            stats.total_profit += pair.total_profit

            if pair.is_profitable:
                stats.winning_trades += 1
            else:
                stats.losing_trades += 1

        return stats

    def get_total_stats(self) -> TradeStats:
        """
        Get all-time statistics.

        Returns:
            TradeStats for all recorded trades
        """
        stats = TradeStats()
        stats.total_trades = len(self._pair_trades)

        for pair in self._pair_trades:
            stats.total_pairs += pair.pair_count
            stats.total_cost += pair.total_cost
            stats.total_profit += pair.total_profit

            if pair.is_profitable:
                stats.winning_trades += 1
            else:
                stats.losing_trades += 1

        return stats

    def clear(self) -> None:
        """Clear all logged trades."""
        self._trades.clear()
        self._pair_trades.clear()
        logger.info("Cleared trade log")

    def __len__(self) -> int:
        """Number of pair trades logged."""
        return len(self._pair_trades)

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"TradeLogger(trades={len(self._trades)}, "
            f"pairs={len(self._pair_trades)}, "
            f"session={self._current_session})"
        )
