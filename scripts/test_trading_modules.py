#!/usr/bin/env python3
"""
Test script for new trading modules.

Exercises PositionManager, FillProcessor, and DisplayManager
without touching actual trading code.

Usage:
    python scripts/test_trading_modules.py
"""

import asyncio
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def test_position_manager():
    """Test PositionManager functionality."""
    print("\n" + "="*60)
    print("Testing PositionManager")
    print("="*60)

    from src.trading.position_manager import PositionManager, PositionState, ImbalanceInfo

    # Create manager
    pm = PositionManager(
        trading_mode="paper",
        hard_max_imbalance=10,
        max_imbalance_pct=0.20,
        hedge_trigger_pct=0.15,
    )
    print("✓ PositionManager created")

    # Test position tracking
    market = "btc-15m-up-down-2026-01-25"

    # Update position
    pm.update_position(market, up_shares=25, down_shares=20, up_avg_price=0.52, down_avg_price=0.48)
    pos = pm.get_position(market)

    assert pos.up_shares == 25, f"Expected 25, got {pos.up_shares}"
    assert pos.down_shares == 20, f"Expected 20, got {pos.down_shares}"
    assert pos.hedged_pairs == 20, f"Expected 20 hedged pairs, got {pos.hedged_pairs}"
    assert pos.imbalance == 5, f"Expected 5 imbalance, got {pos.imbalance}"
    assert pos.deficit_side == "DOWN", f"Expected DOWN deficit, got {pos.deficit_side}"
    print(f"✓ Position tracking: UP={pos.up_shares}, DOWN={pos.down_shares}")
    print(f"  Hedged pairs: {pos.hedged_pairs}, Imbalance: {pos.imbalance}")
    print(f"  Deficit side: {pos.deficit_side}, Pair cost: ${pos.pair_cost:.4f}")

    # Test fill recording
    pm.record_fill(market, "DOWN", 0.49, 5)
    pos = pm.get_position(market)
    assert pos.down_shares == 25, f"Expected 25 DOWN after fill, got {pos.down_shares}"
    assert pos.imbalance == 0, f"Expected 0 imbalance after fill, got {pos.imbalance}"
    print(f"✓ Fill recorded: DOWN 5 @ $0.49")
    print(f"  New position: UP={pos.up_shares}, DOWN={pos.down_shares}, Imbalance={pos.imbalance}")

    # Test imbalance analysis
    pm.update_position(market, up_shares=30, down_shares=20)
    info = pm.analyze_imbalance(market, time_remaining_secs=600, target_shares=50)

    assert info.imbalance_shares == 10, f"Expected 10 imbalance, got {info.imbalance_shares}"
    assert info.deficit_side == "DOWN", f"Expected DOWN deficit, got {info.deficit_side}"
    print(f"✓ Imbalance analysis:")
    print(f"  Shares: {info.imbalance_shares}, Pct: {info.imbalance_pct:.1%}")
    print(f"  Status: {info.status}, Should hedge: {info.should_hedge}")

    # Test emergency threshold (time-based)
    info_early = pm.analyze_imbalance(market, time_remaining_secs=600)  # 10 min
    info_late = pm.analyze_imbalance(market, time_remaining_secs=120)   # 2 min
    print(f"✓ Time-based thresholds:")
    print(f"  Early (10min): emergency={info_early.is_emergency}")
    print(f"  Late (2min): emergency={info_late.is_emergency}")

    # Test emergency market tracking
    pm.mark_emergency_triggered(market)
    assert pm.is_emergency_triggered(market), "Market should be marked emergency"
    print(f"✓ Emergency tracking works")

    # Test reset
    pm.reset_market(market)
    assert not pm.is_emergency_triggered(market), "Market should be reset"
    print(f"✓ Market reset works")

    # Test metrics
    metrics = pm.get_metrics()
    print(f"✓ Metrics: {metrics}")

    print("\n✅ PositionManager: ALL TESTS PASSED")
    return True


def test_fill_processor():
    """Test FillProcessor functionality."""
    print("\n" + "="*60)
    print("Testing FillProcessor")
    print("="*60)

    from src.trading.fill_processor import FillProcessor, FillEvent, PendingOrder

    # Track fills via callback
    received_fills = []
    def on_fill(fill: FillEvent):
        received_fills.append(fill)

    # Create processor
    fp = FillProcessor(
        trading_mode="paper",
        rest_verify_interval=30.0,
        on_fill_callback=on_fill,
    )
    print("✓ FillProcessor created")

    # Test pending order tracking
    fp.add_pending_order(
        order_id="order-001",
        side="UP",
        price=0.52,
        size=10,
        strategy="aggressive",
        is_expensive_side=True,
    )

    pending = fp.get_pending_order("order-001")
    assert pending is not None, "Pending order should exist"
    assert pending.side == "UP", f"Expected UP, got {pending.side}"
    assert pending.is_expensive_side, "Should be marked as expensive side"
    print(f"✓ Pending order added: {pending.side} {pending.size} @ ${pending.price}")

    # Test fill processing
    fill = fp.process_fill(
        side="UP",
        price=0.52,
        size=10,
        order_id="order-001",
        source="paper",
    )

    assert fill is not None, "Fill should be processed"
    assert fill.side == "UP", f"Expected UP, got {fill.side}"
    assert fill.size == 10, f"Expected 10, got {fill.size}"
    assert len(received_fills) == 1, f"Callback should receive 1 fill, got {len(received_fills)}"
    print(f"✓ Fill processed: {fill.side} {fill.size} @ ${fill.price}")
    print(f"  Callback received: {len(received_fills)} fill(s)")

    # Test duplicate rejection
    fill2 = fp.process_fill(
        side="UP",
        price=0.52,
        size=10,
        order_id="order-001",  # Same order ID
        source="paper",
    )
    assert fill2 is None, "Duplicate fill should be rejected"
    assert len(received_fills) == 1, "Callback should not receive duplicate"
    print(f"✓ Duplicate fill rejected")

    # Test pending order removal
    pending = fp.get_pending_order("order-001")
    assert pending is None, "Pending order should be removed after fill"
    print(f"✓ Pending order removed after fill")

    # Test multiple fills
    for i in range(5):
        fp.add_pending_order(f"order-{i+10}", "DOWN", 0.48, 5, "test")
        fp.process_fill("DOWN", 0.48, 5, f"order-{i+10}", "paper")

    assert len(received_fills) == 6, f"Expected 6 fills, got {len(received_fills)}"
    print(f"✓ Multiple fills processed: {len(received_fills)} total")

    # Test reset
    fp.reset()
    assert len(fp._pending_orders) == 0, "Pending orders should be cleared"
    print(f"✓ Reset clears pending orders")

    # Test metrics
    metrics = fp.get_metrics()
    print(f"✓ Metrics: {metrics}")

    print("\n✅ FillProcessor: ALL TESTS PASSED")
    return True


def test_display_manager():
    """Test DisplayManager functionality."""
    print("\n" + "="*60)
    print("Testing DisplayManager")
    print("="*60)

    from src.trading.display import DisplayManager, TradeLog
    import tempfile
    import os

    # Create temp directory for CSV
    temp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(temp_dir, "test_trades.csv")

    # Track web updates
    web_updates = []
    def on_web_update(state):
        web_updates.append(state)

    # Create manager
    dm = DisplayManager(
        strategy_name="test_strategy",
        csv_base_path=csv_path,
        live_display_enabled=False,
        web_callback=on_web_update,
        quiet_mode=True,
    )
    dm.set_start_time()
    print("✓ DisplayManager created")

    # Test trade logging
    dm.log_trade_simple(
        market_slug="btc-15m-test",
        side="UP",
        price=0.52,
        size=10,
        trade_mode="NORMAL",
        position_state={"up_shares": 10, "down_shares": 0, "imbalance": 10},
        balance=100.0,
    )

    assert dm._trade_count == 1, f"Expected 1 trade, got {dm._trade_count}"
    print(f"✓ Trade logged: UP 10 @ $0.52")

    # Verify CSV was written
    assert os.path.exists(dm.csv_path), "CSV file should exist"
    with open(dm.csv_path, 'r') as f:
        lines = f.readlines()
    assert len(lines) == 2, f"Expected 2 lines (header + trade), got {len(lines)}"
    print(f"✓ CSV written: {dm.csv_path}")

    # Test resolution logging
    dm.log_resolution(
        market_slug="btc-15m-test",
        outcome="UP",
        profit=5.20,
        balance=105.20,
    )

    assert dm._total_profit == 5.20, f"Expected $5.20 profit, got ${dm._total_profit}"
    print(f"✓ Resolution logged: profit=${dm._total_profit}")

    # Test web state building
    state = dm.build_web_state(
        market_slug="btc-15m-test",
        balance=105.20,
        position_state={"up_shares": 0, "down_shares": 0, "hedged_pairs": 0},
        prices={"up_bid": 0.52, "up_ask": 0.53, "down_bid": 0.47, "down_ask": 0.48},
    )

    assert state["strategy_name"] == "test_strategy", f"Wrong strategy name"
    assert state["balance"] == 105.20, f"Wrong balance"
    assert state["trade_count"] == 2, f"Wrong trade count"
    print(f"✓ Web state built:")
    print(f"  Strategy: {state['strategy_name']}")
    print(f"  Balance: ${state['balance']:.2f}")
    print(f"  Trades: {state['trade_count']}")

    # Test web callback
    dm.send_web_update(
        market_slug="btc-15m-test",
        balance=105.20,
    )
    assert len(web_updates) == 1, f"Expected 1 web update, got {len(web_updates)}"
    print(f"✓ Web callback triggered: {len(web_updates)} update(s)")

    # Test trade event
    dm.send_trade_event("DOWN", 5, 0.48, "BUY")
    assert len(web_updates) == 2, f"Expected 2 web updates, got {len(web_updates)}"
    print(f"✓ Trade event sent")

    # Test metrics
    metrics = dm.get_metrics()
    assert metrics["trade_count"] == 2, f"Wrong trade count in metrics"
    assert metrics["total_profit"] == 5.20, f"Wrong profit in metrics"
    print(f"✓ Metrics: {metrics}")

    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)
    print(f"✓ Temp files cleaned up")

    print("\n✅ DisplayManager: ALL TESTS PASSED")
    return True


async def test_async_features():
    """Test async features (WebSocket setup, etc.)."""
    print("\n" + "="*60)
    print("Testing Async Features")
    print("="*60)

    from src.trading.fill_processor import FillProcessor
    from src.trading.position_manager import PositionManager

    # Test FillProcessor queue
    fp = FillProcessor(trading_mode="paper")

    # Simulate adding to queue
    fp._fill_queue.put_nowait({
        "side": "UP",
        "price": 0.52,
        "size": 10,
        "order_id": "async-001",
    })
    fp._fill_queue.put_nowait({
        "side": "DOWN",
        "price": 0.48,
        "size": 10,
        "order_id": "async-002",
    })

    # Process queue
    fills = await fp.process_fill_queue()
    assert len(fills) == 2, f"Expected 2 fills from queue, got {len(fills)}"
    print(f"✓ Async fill queue: processed {len(fills)} fills")

    # Test PositionManager (sync_from_api would need real client)
    pm = PositionManager(trading_mode="paper")
    existing = await pm.check_existing_positions(None)  # Should return empty for paper
    assert existing["total"] == 0, "Paper mode should have no existing positions"
    print(f"✓ Async position check: {existing}")

    print("\n✅ Async Features: ALL TESTS PASSED")
    return True


async def test_integration():
    """Test modules working together."""
    print("\n" + "="*60)
    print("Testing Module Integration")
    print("="*60)

    from src.trading.position_manager import PositionManager
    from src.trading.fill_processor import FillProcessor
    from src.trading.display import DisplayManager
    import tempfile
    import os

    # Create temp directory
    temp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(temp_dir, "integration_test.csv")

    market = "test-market"

    # Create all managers (no callback to avoid async issues)
    pm = PositionManager(trading_mode="paper")
    fp = FillProcessor(trading_mode="paper")
    dm = DisplayManager(strategy_name="integration_test", csv_base_path=csv_path, quiet_mode=True)
    dm.set_start_time()

    print("✓ All managers created")

    # Simulate trading cycle - manually coordinate the modules
    trades = [
        ("UP", 0.52, 10),
        ("DOWN", 0.48, 10),
        ("UP", 0.51, 5),
        ("DOWN", 0.49, 5),
    ]

    fills_processed = 0
    for i, (side, price, size) in enumerate(trades):
        order_id = f"order-{i}"

        # 1. Add pending order
        fp.add_pending_order(order_id, side, price, size, "test")

        # 2. Process fill
        fill = fp.process_fill(side, price, size, order_id, "paper")
        if fill:
            fills_processed += 1

            # 3. Update position
            pm.record_fill(market, fill.side, fill.price, fill.size)

            # 4. Log trade
            pos = pm.get_position(market)
            dm.log_trade_simple(
                market_slug=market,
                side=fill.side,
                price=fill.price,
                size=fill.size,
                position_state=pos.to_dict(),
            )

    # Verify integration
    pos = pm.get_position(market)
    assert pos.up_shares == 15, f"Expected 15 UP, got {pos.up_shares}"
    assert pos.down_shares == 15, f"Expected 15 DOWN, got {pos.down_shares}"
    assert pos.hedged_pairs == 15, f"Expected 15 pairs, got {pos.hedged_pairs}"
    print(f"✓ Position updated: UP={pos.up_shares}, DOWN={pos.down_shares}")

    assert fills_processed == 4, f"Expected 4 fills, got {fills_processed}"
    print(f"✓ Fills processed: {fills_processed}")

    assert dm._trade_count == 4, f"Expected 4 trades logged, got {dm._trade_count}"
    print(f"✓ Trades logged: {dm._trade_count}")

    # Verify CSV
    with open(dm.csv_path, 'r') as f:
        lines = f.readlines()
    assert len(lines) == 5, f"Expected 5 CSV lines, got {len(lines)}"
    print(f"✓ CSV has {len(lines)} lines (1 header + 4 trades)")

    # Check metrics
    pm_metrics = pm.get_metrics()
    fp_metrics = fp.get_metrics()
    dm_metrics = dm.get_metrics()

    print(f"✓ PositionManager metrics: {pm_metrics}")
    print(f"✓ FillProcessor metrics: {fp_metrics}")
    print(f"✓ DisplayManager metrics: {dm_metrics}")

    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)

    print("\n✅ Integration: ALL TESTS PASSED")
    return True


def main():
    """Run all tests."""
    print("\n" + "#"*60)
    print("# TRADING MODULES TEST SUITE")
    print("#"*60)
    print(f"# Time: {datetime.now(timezone.utc).isoformat()}")
    print("#"*60)

    results = {}

    # Run sync tests
    try:
        results["PositionManager"] = test_position_manager()
    except Exception as e:
        print(f"\n❌ PositionManager FAILED: {e}")
        results["PositionManager"] = False

    try:
        results["FillProcessor"] = test_fill_processor()
    except Exception as e:
        print(f"\n❌ FillProcessor FAILED: {e}")
        results["FillProcessor"] = False

    try:
        results["DisplayManager"] = test_display_manager()
    except Exception as e:
        print(f"\n❌ DisplayManager FAILED: {e}")
        results["DisplayManager"] = False

    # Run async tests
    try:
        results["AsyncFeatures"] = asyncio.run(test_async_features())
    except Exception as e:
        print(f"\n❌ AsyncFeatures FAILED: {e}")
        results["AsyncFeatures"] = False

    # Run integration test (async)
    try:
        results["Integration"] = asyncio.run(test_integration())
    except Exception as e:
        print(f"\n❌ Integration FAILED: {e}")
        results["Integration"] = False

    # Summary
    print("\n" + "#"*60)
    print("# TEST SUMMARY")
    print("#"*60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, result in results.items():
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"  {name}: {status}")

    print(f"\n  Total: {passed}/{total} passed")
    print("#"*60)

    if passed == total:
        print("\n🎉 ALL TESTS PASSED - Modules ready for integration!")
        return 0
    else:
        print("\n⚠️  Some tests failed - check output above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
