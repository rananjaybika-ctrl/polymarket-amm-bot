#!/usr/bin/env python3
"""
Test Telegram bot connection and commands.

Usage:
    python scripts/test_telegram.py

Setup (if not done):
    1. Message @BotFather on Telegram, send /newbot
    2. Copy token to TELEGRAM_BOT_TOKEN in .env
    3. Message your bot (any message)
    4. Run: curl https://api.telegram.org/bot<TOKEN>/getUpdates
    5. Copy chat_id to TELEGRAM_CHAT_ID in .env
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.config import Config
from src.utils.telegram_notifier import TelegramNotifier


async def main():
    print("=" * 50)
    print("Telegram Bot Test")
    print("=" * 50)

    # Load config
    try:
        config = Config()
    except Exception as e:
        print(f"Error loading config: {e}")
        return

    # Check if configured
    token = config.telegram_bot_token
    chat_id = config.telegram_chat_id

    if not token or "your_" in token:
        print("\nTELEGRAM_BOT_TOKEN not configured!")
        print("\nSetup instructions:")
        print("1. Open Telegram and message @BotFather")
        print("2. Send /newbot and follow prompts")
        print("3. Copy the token to TELEGRAM_BOT_TOKEN in .env")
        return

    if not chat_id or "your_" in chat_id:
        print("\nTELEGRAM_CHAT_ID not configured!")
        print("\nTo get your chat_id:")
        print(f"1. Message your bot (any message)")
        print(f"2. Run: curl https://api.telegram.org/bot{token}/getUpdates")
        print("3. Find 'chat':{'id': YOUR_ID} in the response")
        print("4. Copy YOUR_ID to TELEGRAM_CHAT_ID in .env")
        return

    print(f"\nBot Token: {token[:10]}...{token[-5:]}")
    print(f"Chat ID: {chat_id}")

    # Create notifier
    notifier = TelegramNotifier(config)

    if not notifier.enabled:
        print("\nTelegram notifier not enabled - check configuration")
        return

    # Test connection
    print("\nTesting connection...")
    success = await notifier.test_connection()

    if success:
        print("Connection successful!")
        print("\nTest notifications:")

        # Send test notifications
        await notifier.send_pnl(
            "Test trade completed: +$1.50",
            {"Market": "BTC 15-min Up", "Shares": "10"}
        )
        print("  - Sent PNL notification")

        await notifier.send_info(
            "Test Info",
            "This is a test info message",
            {"Key": "Value"}
        )
        print("  - Sent info notification")

        print("\nCheck your Telegram for messages!")
        print("\nTo test commands, start the bot and send /help")

        # Optionally listen for commands
        print("\n" + "-" * 50)
        print("Starting command listener (Ctrl+C to stop)...")
        print("Send /help in Telegram to see available commands")
        print("-" * 50)

        # Register test callbacks
        async def test_stop():
            print("[Command] /stop received")

        async def test_sell():
            print("[Command] /sell_all received")

        async def test_status():
            return "Bot is running\nBalance: $100.00\nPositions: 0"

        async def test_balance():
            return "$100.00 USDC"

        notifier.on_stop(test_stop)
        notifier.on_sell_all(test_sell)
        notifier.on_status(test_status)
        notifier.on_balance(test_balance)

        await notifier.start()

        try:
            # Keep running until interrupted
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
            await notifier.stop()

    else:
        print("Connection failed!")
        print("Check your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")


if __name__ == "__main__":
    asyncio.run(main())
