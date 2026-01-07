#!/usr/bin/env python3
"""
Redeem Winning Positions via Builder Relayer.

This script finds all redeemable positions and redeems them using
Polymarket's Builder Relayer for gas-free transactions.

Prerequisites:
    pip install py-builder-relayer-client py-builder-signing-sdk

Required .env variables:
    WALLET_PRIVATE_KEY=0x...
    FUNDER_ADDRESS=0x...
    BUILDER_API_KEY=...
    BUILDER_SECRET=...
    BUILDER_PASSPHRASE=...

Usage:
    python scripts/redeem_winnings.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")


def mask(value: str, show_chars: int = 4) -> str:
    """Mask sensitive values for display."""
    if not value:
        return "NOT SET"
    if len(value) <= show_chars * 2:
        return "***"
    return f"{value[:show_chars]}...{value[-show_chars:]}"


def main():
    print("=" * 60)
    print("POLYMARKET WINNINGS REDEMPTION")
    print("=" * 60)

    # 1. Check credentials
    print("\n[1] Checking credentials...")

    private_key = os.getenv("WALLET_PRIVATE_KEY")
    funder_address = os.getenv("FUNDER_ADDRESS")
    api_key = os.getenv("BUILDER_API_KEY")
    secret = os.getenv("BUILDER_SECRET")
    passphrase = os.getenv("BUILDER_PASSPHRASE")

    missing = []
    if not private_key:
        missing.append("WALLET_PRIVATE_KEY")
    if not funder_address:
        missing.append("FUNDER_ADDRESS")
    if not api_key:
        missing.append("BUILDER_API_KEY")
    if not secret:
        missing.append("BUILDER_SECRET")
    if not passphrase:
        missing.append("BUILDER_PASSPHRASE")

    if missing:
        print(f"    Missing: {', '.join(missing)}")
        print("\n    Add these to your .env file.")
        print("    Get Builder credentials from: https://polymarket.com/settings?tab=builder")
        sys.exit(1)

    print(f"    Private Key:  {mask(private_key)}")
    print(f"    Funder:       {funder_address}")
    print(f"    API Key:      {mask(api_key)}")
    print("    All credentials present")

    # 2. Import dependencies
    print("\n[2] Importing dependencies...")
    try:
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import TransactionType
        from web3 import Web3
        import httpx
        print("    All imports successful")
    except ImportError as e:
        print(f"    Import failed: {e}")
        print("\n    Install: pip install py-builder-relayer-client py-builder-signing-sdk web3 httpx")
        sys.exit(1)

    # 3. Create RelayClient
    print("\n[3] Creating RelayClient...")
    relayer_url = "https://relayer-v2.polymarket.com"
    chain_id = 137

    try:
        builder_creds = BuilderApiKeyCreds(
            key=api_key,
            secret=secret,
            passphrase=passphrase
        )
        builder_config = BuilderConfig(local_builder_creds=builder_creds)

        client = RelayClient(
            relayer_url=relayer_url,
            chain_id=chain_id,
            private_key=private_key,
            builder_config=builder_config
        )
        print("    RelayClient created")
    except Exception as e:
        print(f"    Failed: {e}")
        sys.exit(1)

    # 4. Get wallet addresses
    print("\n[4] Wallet Addresses:")
    signer_address = client.signer.address()
    print(f"    EOA (Signer):  {signer_address}")
    print(f"    Funder:        {funder_address}")

    # 5. Check Safe deployment
    print("\n[5] Safe Deployment Status:")
    try:
        is_deployed = client.get_deployed(funder_address)
        if is_deployed:
            print(f"    Safe IS DEPLOYED")
        else:
            print(f"    Safe NOT deployed - will be deployed on first tx")
    except Exception as e:
        print(f"    Check failed: {e}")

    # 6. Fetch redeemable positions
    print("\n[6] Fetching Redeemable Positions...")
    try:
        pos_url = f"https://data-api.polymarket.com/positions?user={funder_address}&redeemable=true&sizeThreshold=0"

        with httpx.Client(timeout=30) as h_client:
            resp = h_client.get(pos_url)
            resp.raise_for_status()
            positions = resp.json()

        if not positions:
            print("    No redeemable positions found")
            print("\n" + "=" * 60)
            print("Nothing to redeem!")
            print("=" * 60)
            return

        # Group by condition_id
        seen_conditions = set()
        redeem_list = []
        total_value = 0.0

        for pos in positions:
            condition_id = pos.get("conditionId")
            if not condition_id or condition_id in seen_conditions:
                continue

            size = float(pos.get("size", 0))
            title = pos.get("title", "Unknown")[:50]
            outcome = pos.get("outcome", "?")

            print(f"    Found: {outcome} {size:.2f} shares - {title}")
            print(f"           Condition: {condition_id[:30]}...")

            redeem_list.append(condition_id)
            seen_conditions.add(condition_id)
            total_value += size

        print(f"\n    Total: {len(redeem_list)} conditions, ~${total_value:.2f} value")

    except Exception as e:
        print(f"    Failed to fetch positions: {e}")
        sys.exit(1)

    # 7. Confirm redemption
    print("\n[7] Confirm Redemption")
    response = input(f"    Redeem {len(redeem_list)} position(s) worth ~${total_value:.2f}? [y/N]: ").strip().lower()
    if response not in ("y", "yes"):
        print("\n    Cancelled.")
        return

    # 8. Execute redemptions
    print("\n[8] Executing Redemptions...")

    from py_builder_relayer_client.models import SafeTransaction, OperationType

    # Constants
    CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    CTF_REDEEM_ABI = [{
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ]
    }]

    def encode_redeem_tx(condition_id: str) -> str:
        w3 = Web3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_REDEEM_ABI
        )
        calldata = contract.encode_abi(
            "redeemPositions",
            [
                Web3.to_checksum_address(USDC_ADDRESS),
                bytes(32),  # parentCollectionId = 0
                bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id),
                [1, 2]  # YES + NO index sets
            ]
        )
        # Return as hex string
        return calldata if isinstance(calldata, str) else "0x" + calldata.hex()

    # Build transactions
    transactions = []
    for cond_id in redeem_list:
        calldata = encode_redeem_tx(cond_id)
        tx = SafeTransaction(
            to=Web3.to_checksum_address(CTF_ADDRESS),
            operation=OperationType.Call,
            data=calldata,
            value="0"
        )
        transactions.append(tx)

    # Execute
    try:
        print(f"    Submitting {len(transactions)} transaction(s) to relayer...")
        response = client.execute(transactions, "Redeem Winning Positions")

        # Extract tx hash
        if isinstance(response, dict):
            tx_hash = response.get('txHash') or response.get('tx_hash') or response.get('transaction_hash')
        else:
            tx_hash = getattr(response, 'transaction_hash', None) or getattr(response, 'tx_hash', None) or getattr(response, 'transaction_id', None)

        print(f"\n    SUCCESS!")
        print(f"    Transaction: {tx_hash}")
        print(f"    View: https://polygonscan.com/tx/{tx_hash}")

    except Exception as e:
        print(f"    FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Summary
    print("\n" + "=" * 60)
    print("REDEMPTION COMPLETE")
    print("=" * 60)
    print(f"Redeemed {len(redeem_list)} position(s)")
    print(f"Expected USDC: ~${total_value:.2f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
