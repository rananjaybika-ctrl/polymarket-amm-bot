"""
Dry Run Simulator for strategy validation.

Runs the complete trading loop in simulation mode
to validate strategy before going live.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.models.market import BTCMarket
from src.services.market_finder import MarketFinder
from src.services.market_rotator import MarketRotator, SessionEndReason
from src.services.pair_analyzer import PairAnalyzer, PairOpportunity
from src.services.paper_trading import (
    PaperTradingEngine,
    SimulationConfig,
)


logger = logging.getLogger(__name__)


@dataclass
class MarketResult:
    """Results from trading a single market."""
    market_slug: str
    market_question: str
    opportunities_found: int = 0
    trades_attempted: int = 0
    trades_successful: int = 0
    pairs_traded: int = 0
    total_cost: float = 0.0
    expected_profit: float = 0.0
    realized_pnl: float = 0.0
    winning_side: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_slug": self.market_slug,
            "opportunities_found": self.opportunities_found,
            "trades_attempted": self.trades_attempted,
            "trades_successful": self.trades_successful,
            "pairs_traded": self.pairs_traded,
            "total_cost": self.total_cost,
            "expected_profit": self.expected_profit,
            "realized_pnl": self.realized_pnl,
        }


@dataclass
class SimulationReport:
    """Comprehensive report from dry run simulation."""
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: float = 0.0

    # Market stats
    markets_analyzed: int = 0
    markets_traded: int = 0
    market_results: List[MarketResult] = field(default_factory=list)

    # Trade stats
    total_opportunities: int = 0
    profitable_opportunities: int = 0
    trades_attempted: int = 0
    trades_successful: int = 0
    trades_partial: int = 0
    trades_failed: int = 0

    # Financial stats
    initial_balance: float = 0.0
    final_balance: float = 0.0
    total_cost: float = 0.0
    total_profit: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    # Performance metrics
    win_rate: float = 0.0
    avg_profit_per_pair: float = 0.0
    roi_percent: float = 0.0

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60

    def to_dict(self) -> Dict[str, Any]:
        return {
            "duration_minutes": f"{self.duration_minutes:.1f}",
            "markets_analyzed": self.markets_analyzed,
            "markets_traded": self.markets_traded,
            "total_opportunities": self.total_opportunities,
            "profitable_opportunities": self.profitable_opportunities,
            "trades_attempted": self.trades_attempted,
            "trades_successful": self.trades_successful,
            "win_rate": f"{self.win_rate:.1%}",
            "initial_balance": f"${self.initial_balance:.2f}",
            "final_balance": f"${self.final_balance:.2f}",
            "total_profit": f"${self.total_profit:.4f}",
            "realized_pnl": f"${self.realized_pnl:.4f}",
            "roi_percent": f"{self.roi_percent:.2f}%",
        }


class DryRunSimulator:
    """
    Dry run simulator for strategy validation.

    Runs the complete trading loop with paper trading
    to validate strategy before going live.

    Example:
        simulator = DryRunSimulator(initial_balance=100.0)
        report = await simulator.run(
            duration_minutes=60,
            check_interval=5.0,
        )
        print(f"ROI: {report.roi_percent:.2f}%")
    """

    def __init__(
        self,
        initial_balance: float = 100.0,
        max_pairs_per_trade: int = 10,
        min_profit_threshold: float = 0.001,  # $0.001 per pair minimum
        sim_config: Optional[SimulationConfig] = None,
    ):
        """
        Initialize dry run simulator.

        Args:
            initial_balance: Starting paper balance
            max_pairs_per_trade: Maximum pairs per trade
            min_profit_threshold: Minimum profit per pair to trade
            sim_config: Simulation configuration
        """
        self.initial_balance = initial_balance
        self.max_pairs_per_trade = max_pairs_per_trade
        self.min_profit_threshold = min_profit_threshold
        self.sim_config = sim_config or SimulationConfig()

        # Components (initialized on run)
        self._client: Optional[PolymarketClient] = None
        self._finder: Optional[MarketFinder] = None
        self._rotator: Optional[MarketRotator] = None
        self._analyzer: Optional[PairAnalyzer] = None
        self._engine: Optional[PaperTradingEngine] = None

        # State
        self._running = False
        self._current_market_result: Optional[MarketResult] = None

    async def run(
        self,
        duration_minutes: float = 60.0,
        check_interval: float = 5.0,
        max_markets: int = 10,
        continuous: bool = True,
    ) -> SimulationReport:
        """
        Run the dry run simulation.

        Args:
            duration_minutes: How long to run simulation
            check_interval: Seconds between opportunity checks
            max_markets: Maximum markets to trade (0 = unlimited)
            continuous: Use continuous mode (vs session mode)

        Returns:
            SimulationReport with all results
        """
        report = SimulationReport(
            start_time=datetime.now(timezone.utc),
            initial_balance=self.initial_balance,
        )

        logger.info(f"Starting dry run simulation for {duration_minutes} minutes")

        try:
            # Initialize components
            await self._initialize()

            # Create rotator
            self._rotator = MarketRotator(
                finder=self._finder,
                continuous=continuous,
                max_markets=max_markets if not continuous else 1000,
                market_window_minutes=60,
            )

            # Start session
            if not await self._rotator.start_session():
                logger.warning("No markets available for dry run")
                report.end_time = datetime.now(timezone.utc)
                return report

            self._running = True
            end_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)

            # Main simulation loop
            while self._running and datetime.now(timezone.utc) < end_time:
                market = self._rotator.current_market

                if not market:
                    logger.warning("No current market")
                    break

                # Initialize market result tracking
                if (self._current_market_result is None or
                    self._current_market_result.market_slug != market.slug):
                    self._current_market_result = MarketResult(
                        market_slug=market.slug,
                        market_question=market.question,
                    )
                    report.market_results.append(self._current_market_result)
                    report.markets_analyzed += 1

                # Analyze for opportunities
                opportunity = await self._analyzer.analyze_market(market)
                report.total_opportunities += 1
                self._current_market_result.opportunities_found += 1

                if opportunity.is_profitable:
                    report.profitable_opportunities += 1

                    # Check profit threshold
                    if opportunity.profit_per_pair >= self.min_profit_threshold:
                        # Calculate trade size
                        available = self._engine.balance
                        max_from_balance = int(available / opportunity.pair_cost)
                        size = min(
                            self.max_pairs_per_trade,
                            max_from_balance,
                            int(opportunity.executable_size),
                        )

                        if size > 0:
                            # Execute paper trade
                            result = await self._engine.execute_paper_trade(
                                opportunity, size
                            )

                            report.trades_attempted += 1
                            self._current_market_result.trades_attempted += 1

                            if result.success:
                                report.trades_successful += 1
                                self._current_market_result.trades_successful += 1
                                self._current_market_result.pairs_traded += size
                                self._current_market_result.total_cost += result.actual_cost

                                # Track profit
                                pair_profit = size * (1.0 - (
                                    result.up_order.filled_price +
                                    result.down_order.filled_price
                                ))
                                self._current_market_result.expected_profit += pair_profit
                                report.total_profit += pair_profit

                                if self._current_market_result.pairs_traded > 0:
                                    report.markets_traded = len([
                                        r for r in report.market_results
                                        if r.pairs_traded > 0
                                    ])

                                logger.info(
                                    f"Paper trade success: {size} pairs, "
                                    f"profit ${pair_profit:.4f}"
                                )

                            elif result.up_order.filled_size > 0 or result.down_order.filled_size > 0:
                                report.trades_partial += 1
                                logger.info("Paper trade partial fill")
                            else:
                                report.trades_failed += 1
                                logger.info("Paper trade failed")

                # Check for rotation
                if self._rotator.should_rotate():
                    # Resolve current market before rotating
                    if self._current_market_result and self._current_market_result.pairs_traded > 0:
                        await self._resolve_market(self._current_market_result, report)

                    if not await self._rotator.rotate():
                        logger.info("Rotation failed, ending simulation")
                        break

                # Check session completion
                if self._rotator.is_session_complete():
                    logger.info("Session complete")
                    break

                # Wait before next check
                await asyncio.sleep(check_interval)

            # Resolve any remaining positions
            for pos in self._engine.positions:
                market_result = next(
                    (r for r in report.market_results if r.market_slug == pos.market_slug),
                    None
                )
                if market_result:
                    await self._resolve_market(market_result, report)

        except Exception as e:
            logger.error(f"Dry run error: {e}")
            raise

        finally:
            self._running = False
            await self._cleanup()

        # Finalize report
        report.end_time = datetime.now(timezone.utc)
        report.duration_seconds = (report.end_time - report.start_time).total_seconds()
        report.final_balance = self._engine.balance if self._engine else self.initial_balance
        report.total_cost = self._engine.stats.total_cost if self._engine else 0
        report.realized_pnl = self._engine.get_realized_pnl() if self._engine else 0
        report.unrealized_pnl = self._engine.get_total_pnl() if self._engine else 0

        # Calculate metrics
        if report.trades_attempted > 0:
            report.win_rate = report.trades_successful / report.trades_attempted

        if report.trades_successful > 0:
            report.avg_profit_per_pair = report.total_profit / report.trades_successful

        if report.initial_balance > 0:
            report.roi_percent = (
                (report.final_balance - report.initial_balance) /
                report.initial_balance * 100
            )

        logger.info(
            f"Dry run complete: {report.duration_minutes:.1f} min, "
            f"{report.markets_traded} markets, "
            f"ROI: {report.roi_percent:.2f}%"
        )

        return report

    async def _initialize(self) -> None:
        """Initialize all components."""
        config = Config()
        self._client = PolymarketClient(config)
        await self._client.connect()

        self._finder = MarketFinder()
        self._analyzer = PairAnalyzer(self._client)
        self._engine = PaperTradingEngine(
            config=self.sim_config,
            initial_balance=self.initial_balance,
        )

        logger.info("Dry run components initialized")

    async def _cleanup(self) -> None:
        """Cleanup components."""
        if self._client:
            await self._client.disconnect()
        if self._finder:
            await self._finder.close()

    async def _resolve_market(
        self,
        market_result: MarketResult,
        report: SimulationReport,
    ) -> None:
        """Resolve a market and calculate P&L."""
        import random

        # Simulate market resolution (random winner for now)
        # In production, this would come from actual market data
        winner = random.choice(["UP", "DOWN"])
        market_result.winning_side = winner

        pnl = self._engine.resolve_market(market_result.market_slug, winner)
        market_result.realized_pnl = pnl
        report.realized_pnl += pnl

        logger.info(f"Resolved {market_result.market_slug}: {winner}, P&L ${pnl:.4f}")

    def stop(self) -> None:
        """Stop the simulation."""
        self._running = False
        logger.info("Dry run stopping...")


async def run_dry_run(
    duration_minutes: float = 5.0,
    initial_balance: float = 100.0,
) -> SimulationReport:
    """
    Convenience function to run a dry run simulation.

    Args:
        duration_minutes: How long to run
        initial_balance: Starting balance

    Returns:
        SimulationReport with results
    """
    simulator = DryRunSimulator(initial_balance=initial_balance)
    return await simulator.run(duration_minutes=duration_minutes)
