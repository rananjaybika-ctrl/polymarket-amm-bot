#!/usr/bin/env python3
"""
Withdraw USDC from Gnosis Safe to any address.

Usage:
    python scripts/withdraw_from_safe.py <destination_address> <amount_usdc>

Example:
    python scripts/withdraw_from_safe.py 0x1404341D718bbd4e5683877fa57f1249016B8989 50.00
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/withdraw_from_safe.py <destination_address> <amount_usdc>")
        print("Example: python scripts/withdraw_from_safe.py 0x1404341D718bbd4e5683877fa57f1249016B8989 50.00")
        sys.exit(1)

    destination = sys.argv[1]
    amount_usdc = float(sys.argv[2])

    # Validate address
    if not destination.startswith("0x") or len(destination) != 42:
        print(f"ERROR: Invalid address format: {destination}")
        sys.exit(1)

    print("=" * 60)
    print("WITHDRAW USDC FROM GNOSIS SAFE")
    print("=" * 60)

    # Get credentials
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    api_key = os.getenv("BUILDER_API_KEY")
    secret = os.getenv("BUILDER_SECRET")
    passphrase = os.getenv("BUILDER_PASSPHRASE")

    if not all([private_key, api_key, secret, passphrase]):
        print("ERROR: Missing credentials in .env")
        sys.exit(1)

    # Import dependencies
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import SafeTransaction, OperationType
    from web3 import Web3

    # Create RelayClient
    builder_creds = BuilderApiKeyCreds(key=api_key, secret=secret, passphrase=passphrase)
    builder_config = BuilderConfig(local_builder_creds=builder_creds)

    client = RelayClient(
        relayer_url="https://relayer-v2.polymarket.com",
        chain_id=137,
        private_key=private_key,
        builder_config=builder_config
    )

    # Get Safe address and check balance
    safe_address = client.get_expected_safe()
    print(f"Safe Address: {safe_address}")
    print(f"Destination:  {destination}")
    print(f"Amount:       ${amount_usdc:.2f} USDC")

    # Check Safe USDC balance
    USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    usdc_abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=usdc_abi)

    balance_wei = usdc.functions.balanceOf(Web3.to_checksum_address(safe_address)).call()
    balance_usdc = balance_wei / 1_000_000
    print(f"Safe Balance: ${balance_usdc:.2f} USDC")

    if amount_usdc > balance_usdc:
        print(f"\nERROR: Insufficient balance. Requested ${amount_usdc:.2f}, available ${balance_usdc:.2f}")
        sys.exit(1)

    # Confirm
    print()
    response = input(f"Withdraw ${amount_usdc:.2f} USDC to {destination}? [y/N]: ").strip().lower()
    if response not in ("y", "yes"):
        print("Cancelled.")
        return

    # Build USDC transfer transaction
    amount_wei = int(amount_usdc * 1_000_000)

    transfer_abi = [{
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "outputs": [{"name": "", "type": "bool"}]
    }]

    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=transfer_abi
    )

    calldata = usdc_contract.encode_abi("transfer", [
        Web3.to_checksum_address(destination),
        amount_wei
    ])

    tx = SafeTransaction(
        to=Web3.to_checksum_address(USDC_ADDRESS),
        operation=OperationType.Call,
        data=calldata,
        value="0"
    )

    # Execute
    print("\nSubmitting withdrawal to Builder Relayer...")
    try:
        response = client.execute([tx], f"Withdraw {amount_usdc} USDC to {destination[:10]}...")

        if isinstance(response, dict):
            tx_hash = response.get('txHash') or response.get('transaction_hash')
        else:
            tx_hash = getattr(response, 'transaction_hash', None) or getattr(response, 'tx_hash', None)

        print(f"\nSUCCESS!")
        print(f"Transaction: {tx_hash}")
        print(f"View: https://polygonscan.com/tx/{tx_hash}")

    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
