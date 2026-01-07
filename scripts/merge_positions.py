#!/usr/bin/env python3
"""
Merge UP+DOWN position pairs back to USDC.

Burns matching pairs of UP and DOWN tokens to receive $1 USDC per pair.
Works with both Gnosis Safe (gasless) and Magic/EOA wallets (pays gas).

Usage:
    python scripts/merge_positions.py                    # Interactive mode
    python scripts/merge_positions.py <condition_id> <amount>  # Direct mode

Example:
    python scripts/merge_positions.py 0xabc123... 50
"""

import os
import sys
import asyncio
from pathlib import Path
from typing import List, Dict, Optional
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

# Contract addresses
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# mergePositions ABI
CTF_MERGE_ABI = [{
    "name": "mergePositions",
    "type": "function",
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "partition", "type": "uint256[]"},
        {"name": "amount", "type": "uint256"}
    ]
}]


async def fetch_positions(wallet_address: str) -> List[Dict]:
    """Fetch all positions for a wallet from Data API."""
    import httpx

    url = f"https://data-api.polymarket.com/positions?user={wallet_address}&sizeThreshold=0"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def find_mergeable_pairs(positions: List[Dict]) -> List[Dict]:
    """
    Group positions by condition_id and find pairs with both UP and DOWN.

    Returns list of:
        {condition_id, title, up_balance, down_balance, max_mergeable}
    """
    # Group by condition_id
    by_condition = {}
    for pos in positions:
        cond_id = pos.get("conditionId")
        if not cond_id:
            continue

        if cond_id not in by_condition:
            by_condition[cond_id] = {
                "condition_id": cond_id,
                "title": pos.get("title", "Unknown")[:60],
                "up_balance": 0,
                "down_balance": 0,
            }

        size = float(pos.get("size", 0))
        outcome = pos.get("outcome", "").lower()

        if "yes" in outcome or "up" in outcome:
            by_condition[cond_id]["up_balance"] = size
        elif "no" in outcome or "down" in outcome:
            by_condition[cond_id]["down_balance"] = size

    # Find pairs with both sides
    mergeable = []
    for cond_id, data in by_condition.items():
        up = data["up_balance"]
        down = data["down_balance"]
        if up > 0 and down > 0:
            data["max_mergeable"] = int(min(up, down))
            mergeable.append(data)

    return mergeable


def encode_merge_calldata(condition_id: str, amount: int) -> str:
    """Encode mergePositions calldata."""
    from web3 import Web3

    w3 = Web3()
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=CTF_MERGE_ABI
    )

    # Convert condition_id to bytes32
    cond_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)

    calldata = contract.encode_abi(
        "mergePositions",
        [
            Web3.to_checksum_address(USDC_ADDRESS),
            bytes(32),  # parentCollectionId = 0
            cond_bytes,
            [1, 2],  # partition: YES + NO
            amount * 1_000_000  # Convert to base units (USDC has 6 decimals)
        ]
    )

    return calldata if isinstance(calldata, str) else "0x" + calldata.hex()


def merge_via_builder_relayer(condition_id: str, amount: int, private_key: str) -> str:
    """Execute merge via Builder Relayer (gasless, Safe wallet only)."""
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import SafeTransaction, OperationType
    from web3 import Web3

    api_key = os.getenv("BUILDER_API_KEY")
    secret = os.getenv("BUILDER_SECRET")
    passphrase = os.getenv("BUILDER_PASSPHRASE")

    if not all([api_key, secret, passphrase]):
        raise ValueError("Missing Builder credentials (BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE)")

    # Create RelayClient
    builder_creds = BuilderApiKeyCreds(key=api_key, secret=secret, passphrase=passphrase)
    builder_config = BuilderConfig(local_builder_creds=builder_creds)

    client = RelayClient(
        relayer_url="https://relayer-v2.polymarket.com",
        chain_id=137,
        private_key=private_key,
        builder_config=builder_config
    )

    # Build merge transaction
    calldata = encode_merge_calldata(condition_id, amount)

    tx = SafeTransaction(
        to=Web3.to_checksum_address(CTF_ADDRESS),
        operation=OperationType.Call,
        data=calldata,
        value="0"
    )

    # Execute
    response = client.execute([tx], f"Merge {amount} pairs")

    # Extract tx hash
    if isinstance(response, dict):
        return response.get('txHash') or response.get('transaction_hash') or response.get('tx_hash')
    return getattr(response, 'transaction_hash', None) or getattr(response, 'tx_hash', None)


def merge_via_web3(condition_id: str, amount: int, private_key: str, proxy_address: str) -> str:
    """Execute merge via direct web3 call (pays gas, Magic/EOA wallet)."""
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware

    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    account = w3.eth.account.from_key(private_key)
    signer_address = account.address

    # Build contract
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=CTF_MERGE_ABI
    )

    # Convert condition_id to bytes32
    cond_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)

    # Build transaction
    tx = contract.functions.mergePositions(
        Web3.to_checksum_address(USDC_ADDRESS),
        bytes(32),  # parentCollectionId = 0
        cond_bytes,
        [1, 2],  # partition
        amount * 1_000_000
    ).build_transaction({
        'from': signer_address,
        'gas': 200000,
        'gasPrice': w3.eth.gas_price,
        'nonce': w3.eth.get_transaction_count(signer_address),
        'chainId': 137
    })

    # Sign and send
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    # Wait for confirmation
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt['status'] != 1:
        raise Exception(f"Transaction failed: {tx_hash.hex()}")

    return tx_hash.hex()


def get_wallet_info():
    """Get wallet type and address from config."""
    wallet_type = os.getenv("WALLET_TYPE", "eoa").lower()
    private_key = os.getenv("WALLET_PRIVATE_KEY")

    if wallet_type == "gnosis_safe":
        # For Safe wallet, get expected safe address
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
        from py_builder_relayer_client.client import RelayClient

        api_key = os.getenv("BUILDER_API_KEY")
        secret = os.getenv("BUILDER_SECRET")
        passphrase = os.getenv("BUILDER_PASSPHRASE")

        builder_creds = BuilderApiKeyCreds(key=api_key, secret=secret, passphrase=passphrase)
        builder_config = BuilderConfig(local_builder_creds=builder_creds)

        client = RelayClient(
            relayer_url="https://relayer-v2.polymarket.com",
            chain_id=137,
            private_key=private_key,
            builder_config=builder_config
        )
        wallet_address = client.get_expected_safe()
    else:
        # For Magic/EOA, use funder_address or derive from key
        wallet_address = os.getenv("FUNDER_ADDRESS")
        if not wallet_address:
            from web3 import Web3
            w3 = Web3()
            account = w3.eth.account.from_key(private_key)
            wallet_address = account.address

    return wallet_type, wallet_address, private_key


async def interactive_mode():
    """Run in interactive mode - fetch positions and let user choose."""
    print("=" * 60)
    print("MERGE POSITIONS - Convert UP+DOWN pairs to USDC")
    print("=" * 60)

    # Get wallet info
    wallet_type, wallet_address, private_key = get_wallet_info()

    print(f"\nWallet Type: {wallet_type.upper()}")
    print(f"Wallet Address: {wallet_address}")

    if wallet_type == "gnosis_safe":
        print("Method: Builder Relayer (gasless)")
    else:
        print("Method: Direct web3 (pays ~$0.01 gas)")

    # Fetch positions
    print("\nFetching positions...")
    positions = await fetch_positions(wallet_address)

    if not positions:
        print("No positions found.")
        return

    # Find mergeable pairs
    mergeable = find_mergeable_pairs(positions)

    if not mergeable:
        print("No mergeable pairs found (need both UP and DOWN positions).")
        return

    # Display options
    print(f"\nMergeable Pairs Found ({len(mergeable)}):")
    print("-" * 60)

    for i, pair in enumerate(mergeable, 1):
        print(f"[{i}] {pair['title']}")
        print(f"    UP: {pair['up_balance']:.0f} | DOWN: {pair['down_balance']:.0f}")
        print(f"    Max Merge: {pair['max_mergeable']} pairs -> ${pair['max_mergeable']:.2f} USDC")
        print()

    # Get user selection
    print("-" * 60)
    selection = input(f"Select pair to merge (1-{len(mergeable)}, 'all', or 'q' to quit): ").strip().lower()

    if selection == 'q':
        print("Cancelled.")
        return

    if selection == 'all':
        # Merge all pairs
        for pair in mergeable:
            await execute_merge(pair, pair['max_mergeable'], wallet_type, private_key)
    else:
        try:
            idx = int(selection) - 1
            if 0 <= idx < len(mergeable):
                pair = mergeable[idx]

                # Get amount
                amount_input = input(f"Amount to merge (max {pair['max_mergeable']}, or 'max'): ").strip().lower()

                if amount_input == 'max':
                    amount = pair['max_mergeable']
                else:
                    amount = int(amount_input)
                    if amount > pair['max_mergeable']:
                        print(f"Amount exceeds max ({pair['max_mergeable']})")
                        return
                    if amount < 1:
                        print("Amount must be at least 1")
                        return

                await execute_merge(pair, amount, wallet_type, private_key)
            else:
                print("Invalid selection")
        except ValueError:
            print("Invalid input")


async def execute_merge(pair: Dict, amount: int, wallet_type: str, private_key: str):
    """Execute the merge transaction."""
    print(f"\nMerging {amount} pairs from '{pair['title'][:40]}...'")

    try:
        if wallet_type == "gnosis_safe":
            tx_hash = merge_via_builder_relayer(pair['condition_id'], amount, private_key)
        else:
            proxy_address = os.getenv("FUNDER_ADDRESS", "")
            tx_hash = merge_via_web3(pair['condition_id'], amount, private_key, proxy_address)

        print(f"\nSUCCESS!")
        print(f"Transaction: {tx_hash}")
        print(f"View: https://polygonscan.com/tx/{tx_hash}")
        print(f"Received: ${amount:.2f} USDC")

    except Exception as e:
        print(f"\nERROR: {e}")


async def direct_mode(condition_id: str, amount: int):
    """Run in direct mode with provided condition_id and amount."""
    print("=" * 60)
    print("MERGE POSITIONS - Direct Mode")
    print("=" * 60)

    wallet_type, wallet_address, private_key = get_wallet_info()

    print(f"\nWallet: {wallet_address}")
    print(f"Condition ID: {condition_id[:20]}...")
    print(f"Amount: {amount} pairs")

    # Confirm
    response = input(f"\nMerge {amount} pairs? [y/N]: ").strip().lower()
    if response not in ("y", "yes"):
        print("Cancelled.")
        return

    pair = {"condition_id": condition_id, "title": "Direct merge"}
    await execute_merge(pair, amount, wallet_type, private_key)


def main():
    if len(sys.argv) >= 3:
        # Direct mode
        condition_id = sys.argv[1]
        amount = int(sys.argv[2])
        asyncio.run(direct_mode(condition_id, amount))
    else:
        # Interactive mode
        asyncio.run(interactive_mode())


if __name__ == "__main__":
    main()
