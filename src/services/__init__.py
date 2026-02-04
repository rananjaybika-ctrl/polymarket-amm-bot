"""
Services for Polymarket AMM Bot.
"""

from src.services.market_finder import MarketFinder
from src.services.pair_analyzer import PairAnalyzer, PairOpportunity
from src.services.market_rotator import (
    MarketRotator,
    RotationEvent,
    RotationReason,
    SessionStats,
    SessionEndReason,
)
from src.services.order_executor import (
    OrderExecutor,
    OrderInfo,
    PairExecutionResult,
    OrderSide,
    ExecutionStatus,
)
from src.services.position_tracker import (
    PositionTracker,
    PortfolioSummary,
)
from src.services.balance_manager import (
    BalanceManager,
    RecoveryAction,
    RecoveryRecommendation,
    TradeValidation,
)
from src.services.trade_logger import TradeLogger
from src.services.paper_trading import (
    PaperTradingEngine,
    SimulationConfig,
    SimulationStats,
)
# Note: Position is now in src.models.position (consolidated from PaperPosition and LivePosition)
from src.services.dry_run import (
    DryRunSimulator,
    SimulationReport,
    run_dry_run,
)
from src.services.auto_redeemer import AutoRedeemer
from src.services.orderbook_cache import OrderbookCache, OrderbookManager
from src.services.volatility_tracker import (
    LiveZScoreTracker,
    OUParams,
    create_zscore_tracker,
    create_aggressive_tracker,
    create_balanced_tracker,
    create_conservative_tracker,
)
from src.services.spike_event_handler import (
    SpikeEventHandler,
    SpikeSignal,
)

__all__ = [
    "MarketFinder",
    "PairAnalyzer",
    "PairOpportunity",
    "MarketRotator",
    "RotationEvent",
    "RotationReason",
    "SessionStats",
    "SessionEndReason",
    "OrderExecutor",
    "OrderInfo",
    "PairExecutionResult",
    "OrderSide",
    "ExecutionStatus",
    "PositionTracker",
    "PortfolioSummary",
    "BalanceManager",
    "RecoveryAction",
    "RecoveryRecommendation",
    "TradeValidation",
    "TradeLogger",
    "PaperTradingEngine",
    "SimulationConfig",
    "SimulationStats",
    "DryRunSimulator",
    "SimulationReport",
    "run_dry_run",
    "AutoRedeemer",
    "OrderbookCache",
    "OrderbookManager",
    "LiveZScoreTracker",
    "OUParams",
    "create_zscore_tracker",
    "create_aggressive_tracker",
    "create_balanced_tracker",
    "create_conservative_tracker",
    "SpikeEventHandler",
    "SpikeSignal",
]
