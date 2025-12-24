"""
Health Monitor Service for Trading Bot

Monitors bot health, detects outages, and provides auto-recovery capabilities.
Addresses issues:
- Detect when no trades made for extended period
- Monitor strategy health and auto-restart on crash
- Send alerts via Telegram/Discord when issues detected
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, Any, List
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    """Health status levels."""
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    DEAD = "dead"


class OutageType(Enum):
    """Types of outages detected."""
    NO_TRADES = "no_trades"
    STRATEGY_CRASHED = "strategy_crashed"
    API_FAILURE = "api_failure"
    MARKET_TRANSITION_FAILED = "market_transition_failed"
    PROCESS_UNRESPONSIVE = "process_unresponsive"


@dataclass
class HealthEvent:
    """Record of a health event."""
    timestamp: datetime
    event_type: OutageType
    strategy_name: str
    details: str
    recovered: bool = False
    recovery_time: Optional[datetime] = None


@dataclass
class StrategyHealth:
    """Health status for a single strategy."""
    strategy_name: str
    last_trade_time: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    last_market_slug: Optional[str] = None
    trade_count: int = 0
    error_count: int = 0
    restart_count: int = 0
    status: HealthStatus = HealthStatus.HEALTHY
    last_error: Optional[str] = None
    events: List[HealthEvent] = field(default_factory=list)

    def record_trade(self, market_slug: str = None):
        """Record a successful trade."""
        self.last_trade_time = datetime.now(timezone.utc)
        self.last_heartbeat = self.last_trade_time
        self.trade_count += 1
        if market_slug:
            self.last_market_slug = market_slug
        self.status = HealthStatus.HEALTHY

    def record_heartbeat(self):
        """Record a heartbeat (bot is alive but may not have traded)."""
        self.last_heartbeat = datetime.now(timezone.utc)

    def record_error(self, error: str):
        """Record an error."""
        self.error_count += 1
        self.last_error = error

    def record_restart(self):
        """Record a restart."""
        self.restart_count += 1
        self.error_count = 0  # Reset error count on restart

    def time_since_last_trade(self) -> Optional[timedelta]:
        """Get time since last trade."""
        if not self.last_trade_time:
            return None
        return datetime.now(timezone.utc) - self.last_trade_time

    def time_since_heartbeat(self) -> Optional[timedelta]:
        """Get time since last heartbeat."""
        if not self.last_heartbeat:
            return None
        return datetime.now(timezone.utc) - self.last_heartbeat


class HealthMonitor:
    """
    Monitors health of trading strategies and provides alerts/auto-recovery.

    Features:
    - Track last trade time per strategy
    - Detect trade gaps > threshold (default 20 mins)
    - Detect strategy crashes
    - Auto-restart crashed strategies
    - Send alerts via callbacks
    """

    DEFAULT_TRADE_GAP_THRESHOLD_MINUTES = 20
    DEFAULT_HEARTBEAT_THRESHOLD_SECONDS = 60
    DEFAULT_CHECK_INTERVAL_SECONDS = 30
    MAX_RESTART_ATTEMPTS = 3
    RESTART_COOLDOWN_SECONDS = 300  # 5 minutes between restarts

    def __init__(
        self,
        trade_gap_threshold_minutes: float = DEFAULT_TRADE_GAP_THRESHOLD_MINUTES,
        heartbeat_threshold_seconds: float = DEFAULT_HEARTBEAT_THRESHOLD_SECONDS,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        on_alert: Optional[Callable[[str, str, HealthStatus], Any]] = None,
        on_restart: Optional[Callable[[str], Any]] = None,
    ):
        """
        Initialize health monitor.

        Args:
            trade_gap_threshold_minutes: Alert if no trades for this long
            heartbeat_threshold_seconds: Consider unresponsive if no heartbeat
            check_interval_seconds: How often to check health
            on_alert: Callback(strategy_name, message, severity) for alerts
            on_restart: Callback(strategy_name) when auto-restart triggered
        """
        self.trade_gap_threshold = timedelta(minutes=trade_gap_threshold_minutes)
        self.heartbeat_threshold = timedelta(seconds=heartbeat_threshold_seconds)
        self.check_interval = check_interval_seconds
        self.on_alert = on_alert
        self.on_restart = on_restart

        self._strategies: Dict[str, StrategyHealth] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._restart_timestamps: Dict[str, datetime] = {}
        self._alert_cooldowns: Dict[str, datetime] = {}

    def register_strategy(self, strategy_name: str) -> StrategyHealth:
        """Register a strategy for monitoring."""
        if strategy_name not in self._strategies:
            self._strategies[strategy_name] = StrategyHealth(strategy_name=strategy_name)
            logger.info(f"[HEALTH] Registered strategy: {strategy_name}")
        return self._strategies[strategy_name]

    def unregister_strategy(self, strategy_name: str):
        """Unregister a strategy from monitoring."""
        if strategy_name in self._strategies:
            del self._strategies[strategy_name]
            logger.info(f"[HEALTH] Unregistered strategy: {strategy_name}")

    def record_trade(self, strategy_name: str, market_slug: str = None):
        """Record a trade for a strategy."""
        health = self.register_strategy(strategy_name)
        health.record_trade(market_slug)

    def record_heartbeat(self, strategy_name: str):
        """Record a heartbeat for a strategy."""
        health = self.register_strategy(strategy_name)
        health.record_heartbeat()

    def record_error(self, strategy_name: str, error: str):
        """Record an error for a strategy."""
        health = self.register_strategy(strategy_name)
        health.record_error(error)

    def record_market_transition(self, strategy_name: str, from_market: str, to_market: str):
        """Record a successful market transition."""
        health = self.register_strategy(strategy_name)
        health.last_market_slug = to_market
        health.record_heartbeat()
        logger.debug(f"[HEALTH] {strategy_name}: Market transition {from_market} -> {to_market}")

    def get_health(self, strategy_name: str) -> Optional[StrategyHealth]:
        """Get health status for a strategy."""
        return self._strategies.get(strategy_name)

    def get_all_health(self) -> Dict[str, StrategyHealth]:
        """Get health status for all strategies."""
        return self._strategies.copy()

    def get_status_summary(self) -> Dict[str, Any]:
        """Get summary of all strategy health for API responses."""
        summary = {
            "overall_status": HealthStatus.HEALTHY.value,
            "strategies": {},
            "alerts": [],
        }

        worst_status = HealthStatus.HEALTHY

        for name, health in self._strategies.items():
            time_since_trade = health.time_since_last_trade()
            time_since_hb = health.time_since_heartbeat()

            summary["strategies"][name] = {
                "status": health.status.value,
                "last_trade": health.last_trade_time.isoformat() if health.last_trade_time else None,
                "last_heartbeat": health.last_heartbeat.isoformat() if health.last_heartbeat else None,
                "trade_count": health.trade_count,
                "error_count": health.error_count,
                "restart_count": health.restart_count,
                "last_error": health.last_error,
                "last_market": health.last_market_slug,
                "seconds_since_trade": time_since_trade.total_seconds() if time_since_trade else None,
                "seconds_since_heartbeat": time_since_hb.total_seconds() if time_since_hb else None,
            }

            if health.status.value > worst_status.value:
                worst_status = health.status

            # Collect recent alerts
            for event in health.events[-5:]:  # Last 5 events
                summary["alerts"].append({
                    "timestamp": event.timestamp.isoformat(),
                    "strategy": event.strategy_name,
                    "type": event.event_type.value,
                    "details": event.details,
                    "recovered": event.recovered,
                })

        summary["overall_status"] = worst_status.value
        return summary

    async def start(self):
        """Start the health monitoring loop."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"[HEALTH] Monitor started (check interval: {self.check_interval}s, trade gap threshold: {self.trade_gap_threshold})")

    async def stop(self):
        """Stop the health monitoring loop."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[HEALTH] Monitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_all_strategies()
            except Exception as e:
                logger.error(f"[HEALTH] Monitor error: {e}")

            await asyncio.sleep(self.check_interval)

    async def _check_all_strategies(self):
        """Check health of all registered strategies."""
        now = datetime.now(timezone.utc)

        for name, health in self._strategies.items():
            # Check for trade gaps
            if health.last_trade_time:
                gap = now - health.last_trade_time

                if gap > self.trade_gap_threshold:
                    # No trades for too long
                    if health.status != HealthStatus.CRITICAL:
                        health.status = HealthStatus.WARNING if gap < self.trade_gap_threshold * 2 else HealthStatus.CRITICAL

                        event = HealthEvent(
                            timestamp=now,
                            event_type=OutageType.NO_TRADES,
                            strategy_name=name,
                            details=f"No trades for {gap.total_seconds()/60:.1f} minutes",
                        )
                        health.events.append(event)

                        await self._send_alert(
                            name,
                            f"No trades for {gap.total_seconds()/60:.1f} minutes (last: {health.last_market_slug})",
                            health.status
                        )

            # Check for heartbeat timeout (more severe - bot may be crashed)
            if health.last_heartbeat:
                hb_gap = now - health.last_heartbeat

                if hb_gap > self.heartbeat_threshold * 5:  # 5x heartbeat = probably crashed
                    if health.status != HealthStatus.DEAD:
                        health.status = HealthStatus.DEAD

                        event = HealthEvent(
                            timestamp=now,
                            event_type=OutageType.STRATEGY_CRASHED,
                            strategy_name=name,
                            details=f"No heartbeat for {hb_gap.total_seconds():.0f} seconds",
                        )
                        health.events.append(event)

                        await self._send_alert(
                            name,
                            f"Strategy appears DEAD - no heartbeat for {hb_gap.total_seconds():.0f}s",
                            HealthStatus.DEAD
                        )

                        # Trigger auto-restart
                        await self._try_auto_restart(name)

    async def _send_alert(self, strategy_name: str, message: str, severity: HealthStatus):
        """Send an alert via callback with cooldown."""
        # Check cooldown (don't spam alerts)
        cooldown_key = f"{strategy_name}:{severity.value}"
        now = datetime.now(timezone.utc)

        if cooldown_key in self._alert_cooldowns:
            last_alert = self._alert_cooldowns[cooldown_key]
            if now - last_alert < timedelta(minutes=5):
                return  # Still in cooldown

        self._alert_cooldowns[cooldown_key] = now

        logger.warning(f"[HEALTH ALERT] {strategy_name}: {message} (severity: {severity.value})")

        if self.on_alert:
            try:
                result = self.on_alert(strategy_name, message, severity)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"[HEALTH] Failed to send alert: {e}")

    async def _try_auto_restart(self, strategy_name: str):
        """Attempt to auto-restart a strategy."""
        now = datetime.now(timezone.utc)

        # Check restart cooldown
        if strategy_name in self._restart_timestamps:
            last_restart = self._restart_timestamps[strategy_name]
            if now - last_restart < timedelta(seconds=self.RESTART_COOLDOWN_SECONDS):
                logger.warning(f"[HEALTH] {strategy_name}: Restart cooldown active, skipping")
                return

        # Check max restart attempts
        health = self._strategies.get(strategy_name)
        if health and health.restart_count >= self.MAX_RESTART_ATTEMPTS:
            logger.error(f"[HEALTH] {strategy_name}: Max restart attempts ({self.MAX_RESTART_ATTEMPTS}) reached")
            await self._send_alert(
                strategy_name,
                f"Max restart attempts reached ({self.MAX_RESTART_ATTEMPTS}). Manual intervention required.",
                HealthStatus.CRITICAL
            )
            return

        # Trigger restart
        self._restart_timestamps[strategy_name] = now
        if health:
            health.record_restart()

        logger.info(f"[HEALTH] Triggering auto-restart for {strategy_name}")

        if self.on_restart:
            try:
                result = self.on_restart(strategy_name)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"[HEALTH] Failed to trigger restart: {e}")


# Singleton instance for global access
_health_monitor: Optional[HealthMonitor] = None


def get_health_monitor() -> HealthMonitor:
    """Get or create the global health monitor instance."""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = HealthMonitor()
    return _health_monitor


def set_health_monitor(monitor: HealthMonitor):
    """Set the global health monitor instance."""
    global _health_monitor
    _health_monitor = monitor
