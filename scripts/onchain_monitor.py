#!/usr/bin/env python3
"""
Polymarket On-Chain Monitor

Real-time monitoring of Polymarket trades directly from Polygon blockchain.
Based on: "Decoding the Digital Tea Leaves" - Polymarket On-Chain Data Analysis

Key Contracts:
- NegRisk_CTFExchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a (multi-outcome markets)
- CTF Exchange: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E (binary markets)
- Conditional Tokens Framework: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045

Events:
- OrderFilled: Individual trade fills
- OrdersMatched: Summary of matched orders
- PositionsSplit: Token minting (new positions)
- PositionsMerge: Token burning (positions closed)
- PositionsConverted: Portfolio rebalancing

Usage:
    python scripts/onchain_monitor.py --live              # Real-time monitoring
    python scripts/onchain_monitor.py --wallet GABAGOOL  # Track Gabagool
    python scripts/onchain_monitor.py --block 51866068   # Analyze specific block
    python scripts/onchain_monitor.py --recent 100       # Last N blocks
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from decimal import Decimal

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError:
    print("ERROR: web3 not installed. Run: pip install web3")
    sys.exit(1)


# =============================================================================
# CONSTANTS
# =============================================================================

# Polygon RPC endpoints (use multiple for reliability)
RPC_ENDPOINTS = [
    "https://polygon-rpc.com/",
    "https://rpc-mainnet.matic.network",
    "https://polygon-mainnet.public.blastapi.io",
]

# Key contract addresses on Polygon
CONTRACTS = {
    "NegRisk_CTFExchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "CTF_Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "CTF": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "NegRiskAdapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    "USDC": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
}

# Known wallet addresses
KNOWN_WALLETS = {
    "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d": "GABAGOOL",
    "0xC5d563A36AE78145C45a50134d48A1215220f80a": "NegRisk_Exchange",
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E": "CTF_Exchange",
}

# Event signatures (keccak256 hashes)
EVENT_SIGNATURES = {
    # OrderFilled(bytes32 orderHash, address maker, address taker, uint256 makerAssetId,
    #             uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee)
    "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6": "OrderFilled",

    # OrdersMatched(bytes32 takerOrderHash, address takerOrderMaker, uint256 makerAssetId,
    #               uint256 takerAssetId, uint256 makerAmountFilled, uint256 takerAmountFilled)
    "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c": "OrdersMatched",
}

# NegRisk_CTFExchange ABI (minimal - just events we need)
NEGRISK_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "orderHash", "type": "bytes32"},
            {"indexed": True, "name": "maker", "type": "address"},
            {"indexed": True, "name": "taker", "type": "address"},
            {"indexed": False, "name": "makerAssetId", "type": "uint256"},
            {"indexed": False, "name": "takerAssetId", "type": "uint256"},
            {"indexed": False, "name": "makerAmountFilled", "type": "uint256"},
            {"indexed": False, "name": "takerAmountFilled", "type": "uint256"},
            {"indexed": False, "name": "fee", "type": "uint256"},
        ],
        "name": "OrderFilled",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "takerOrderHash", "type": "bytes32"},
            {"indexed": True, "name": "takerOrderMaker", "type": "address"},
            {"indexed": False, "name": "makerAssetId", "type": "uint256"},
            {"indexed": False, "name": "takerAssetId", "type": "uint256"},
            {"indexed": False, "name": "makerAmountFilled", "type": "uint256"},
            {"indexed": False, "name": "takerAmountFilled", "type": "uint256"},
        ],
        "name": "OrdersMatched",
        "type": "event",
    },
]

# CTF ABI (for PositionsSplit and PositionsMerge)
CTF_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "stakeholder", "type": "address"},
            {"indexed": False, "name": "collateralToken", "type": "address"},
            {"indexed": True, "name": "parentCollectionId", "type": "bytes32"},
            {"indexed": True, "name": "conditionId", "type": "bytes32"},
            {"indexed": False, "name": "partition", "type": "uint256[]"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "PositionSplit",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "stakeholder", "type": "address"},
            {"indexed": False, "name": "collateralToken", "type": "address"},
            {"indexed": True, "name": "parentCollectionId", "type": "bytes32"},
            {"indexed": True, "name": "conditionId", "type": "bytes32"},
            {"indexed": False, "name": "partition", "type": "uint256[]"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "PositionsMerge",
        "type": "event",
    },
]


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class DecodedTrade:
    """Decoded trade from OrderFilled event."""
    block_number: int
    tx_hash: str
    log_index: int
    timestamp: Optional[datetime]

    order_hash: str
    maker: str
    taker: str
    maker_asset_id: int
    taker_asset_id: int
    maker_amount: int
    taker_amount: int
    fee: int

    # Derived fields
    maker_label: str = ""
    taker_label: str = ""
    trade_type: str = ""  # "BUY", "SELL", "MINT", "BURN"
    price: float = 0.0
    size_tokens: float = 0.0
    size_usdc: float = 0.0

    def __post_init__(self):
        """Calculate derived fields."""
        # Label known wallets
        self.maker_label = KNOWN_WALLETS.get(self.maker.lower(), self.maker[:10] + "...")
        self.taker_label = KNOWN_WALLETS.get(self.taker.lower(), self.taker[:10] + "...")

        # Determine trade type and calculate price
        # makerAssetId = 0 means USDC, non-zero means outcome token
        maker_is_usdc = self.maker_asset_id == 0
        taker_is_usdc = self.taker_asset_id == 0

        if maker_is_usdc and not taker_is_usdc:
            # Maker provides USDC, receives tokens = MAKER IS BUYING
            self.trade_type = "BUY"
            self.size_usdc = self.maker_amount / 1e6  # USDC has 6 decimals
            self.size_tokens = self.taker_amount / 1e6  # Tokens also 6 decimals
            self.price = self.size_usdc / self.size_tokens if self.size_tokens > 0 else 0

        elif not maker_is_usdc and taker_is_usdc:
            # Maker provides tokens, receives USDC = MAKER IS SELLING
            self.trade_type = "SELL"
            self.size_tokens = self.maker_amount / 1e6
            self.size_usdc = self.taker_amount / 1e6
            self.price = self.size_usdc / self.size_tokens if self.size_tokens > 0 else 0

        elif maker_is_usdc and taker_is_usdc:
            # Both USDC - shouldn't happen normally
            self.trade_type = "USDC_SWAP"
            self.size_usdc = self.maker_amount / 1e6

        else:
            # Both are tokens - token swap (rare)
            self.trade_type = "TOKEN_SWAP"
            self.size_tokens = self.maker_amount / 1e6

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            "block": self.block_number,
            "tx": self.tx_hash[:16] + "...",
            "maker": self.maker_label,
            "taker": self.taker_label,
            "type": self.trade_type,
            "price": f"${self.price:.4f}",
            "tokens": f"{self.size_tokens:.2f}",
            "usdc": f"${self.size_usdc:.2f}",
            "fee": self.fee / 1e6 if self.fee > 0 else 0,
        }

    def __str__(self) -> str:
        """Pretty print."""
        ts = self.timestamp.strftime("%H:%M:%S") if self.timestamp else "??:??:??"
        return (
            f"[{ts}] {self.trade_type:6} | "
            f"${self.price:.4f} x {self.size_tokens:,.0f} = ${self.size_usdc:,.2f} | "
            f"{self.maker_label} → {self.taker_label}"
        )


@dataclass
class MonitorStats:
    """Statistics from monitoring session."""
    start_block: int = 0
    end_block: int = 0
    trades_seen: int = 0
    total_volume_usdc: float = 0.0
    unique_makers: set = field(default_factory=set)
    unique_takers: set = field(default_factory=set)
    buys: int = 0
    sells: int = 0
    gabagool_trades: List[DecodedTrade] = field(default_factory=list)


# =============================================================================
# ON-CHAIN MONITOR
# =============================================================================

class OnChainMonitor:
    """
    Monitor Polymarket trades directly from Polygon blockchain.

    Features:
    - Real-time trade monitoring via polling
    - Historical block analysis
    - Wallet filtering (track specific addresses)
    - Trade decoding and price calculation
    """

    def __init__(self, rpc_url: Optional[str] = None):
        """Initialize monitor with Polygon RPC connection."""
        self.rpc_url = rpc_url or RPC_ENDPOINTS[0]
        self.web3: Optional[Web3] = None
        self.negrisk_contract = None
        self.ctf_contract = None
        self.stats = MonitorStats()

    def connect(self) -> bool:
        """Connect to Polygon RPC."""
        for url in ([self.rpc_url] + RPC_ENDPOINTS):
            try:
                self.web3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 30}))
                # Inject PoA middleware for Polygon
                self.web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

                if self.web3.is_connected():
                    print(f"[OK] Connected to Polygon via {url}")
                    print(f"     Latest block: {self.web3.eth.block_number:,}")

                    # Setup contracts (both exchanges use same ABI for OrderFilled)
                    negrisk_addr = Web3.to_checksum_address(CONTRACTS["NegRisk_CTFExchange"])
                    self.negrisk_contract = self.web3.eth.contract(
                        address=negrisk_addr,
                        abi=NEGRISK_ABI
                    )

                    # CTF Exchange uses same OrderFilled event structure
                    ctf_exchange_addr = Web3.to_checksum_address(CONTRACTS["CTF_Exchange"])
                    self.ctf_exchange_contract = self.web3.eth.contract(
                        address=ctf_exchange_addr,
                        abi=NEGRISK_ABI  # Same ABI for OrderFilled
                    )

                    ctf_addr = Web3.to_checksum_address(CONTRACTS["CTF"])
                    self.ctf_contract = self.web3.eth.contract(
                        address=ctf_addr,
                        abi=CTF_ABI
                    )

                    return True
            except Exception as e:
                print(f"[WARN] Failed to connect to {url}: {e}")
                continue

        print("[ERROR] Could not connect to any Polygon RPC")
        return False

    def get_block_timestamp(self, block_number: int) -> Optional[datetime]:
        """Get timestamp for a block."""
        try:
            block = self.web3.eth.get_block(block_number)
            return datetime.fromtimestamp(block['timestamp'], tz=timezone.utc)
        except:
            return None

    def fetch_logs(
        self,
        from_block: int,
        to_block: int,
        contract_address: Optional[str] = None,
        include_both_exchanges: bool = True,
    ) -> List[Dict]:
        """Fetch event logs from blockchain.

        Args:
            from_block: Start block
            to_block: End block
            contract_address: Specific contract (overrides include_both_exchanges)
            include_both_exchanges: If True, fetch from both CTF and NegRisk exchanges
        """
        if not self.web3:
            return []

        all_logs = []

        if contract_address:
            addresses = [contract_address]
        elif include_both_exchanges:
            # Search BOTH exchanges - Gabagool trades on both!
            addresses = [
                CONTRACTS["NegRisk_CTFExchange"],  # Multi-outcome (BTC 15-min)
                CONTRACTS["CTF_Exchange"],          # Binary markets
            ]
        else:
            addresses = [CONTRACTS["NegRisk_CTFExchange"]]

        for address in addresses:
            try:
                logs = self.web3.eth.get_logs({
                    'fromBlock': from_block,
                    'toBlock': to_block,
                    'address': Web3.to_checksum_address(address),
                })
                all_logs.extend(logs)
            except Exception as e:
                print(f"[ERROR] Failed to fetch logs from {address[:12]}...: {e}")

        return all_logs

    def decode_order_filled(self, log: Dict) -> Optional[DecodedTrade]:
        """Decode an OrderFilled event log."""
        try:
            # Check event signature
            topic0 = '0x' + log['topics'][0].hex()
            if topic0 != "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6":
                return None

            # Decode using contract ABI
            decoded = self.negrisk_contract.events.OrderFilled().process_log(log)
            args = decoded['args']

            # Get block timestamp
            timestamp = self.get_block_timestamp(log['blockNumber'])

            return DecodedTrade(
                block_number=log['blockNumber'],
                tx_hash=log['transactionHash'].hex(),
                log_index=log['logIndex'],
                timestamp=timestamp,
                order_hash='0x' + args['orderHash'].hex(),
                maker=args['maker'],
                taker=args['taker'],
                maker_asset_id=args['makerAssetId'],
                taker_asset_id=args['takerAssetId'],
                maker_amount=args['makerAmountFilled'],
                taker_amount=args['takerAmountFilled'],
                fee=args['fee'],
            )
        except Exception as e:
            print(f"[ERROR] Failed to decode OrderFilled: {e}")
            return None

    def analyze_block(self, block_number: int, wallet_filter: Optional[str] = None) -> List[DecodedTrade]:
        """Analyze all trades in a single block."""
        trades = []
        logs = self.fetch_logs(block_number, block_number)

        for log in logs:
            trade = self.decode_order_filled(log)
            if trade:
                # Apply wallet filter if specified
                if wallet_filter:
                    wallet_lower = wallet_filter.lower()
                    if trade.maker.lower() != wallet_lower and trade.taker.lower() != wallet_lower:
                        continue

                trades.append(trade)
                self.stats.trades_seen += 1
                self.stats.total_volume_usdc += trade.size_usdc
                self.stats.unique_makers.add(trade.maker.lower())
                self.stats.unique_takers.add(trade.taker.lower())

                if trade.trade_type == "BUY":
                    self.stats.buys += 1
                elif trade.trade_type == "SELL":
                    self.stats.sells += 1

                # Track Gabagool specifically
                gabagool = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"
                if trade.maker.lower() == gabagool or trade.taker.lower() == gabagool:
                    self.stats.gabagool_trades.append(trade)

        return trades

    def analyze_range(
        self,
        from_block: int,
        to_block: int,
        wallet_filter: Optional[str] = None,
        verbose: bool = True,
    ) -> List[DecodedTrade]:
        """Analyze trades across a block range."""
        all_trades = []
        self.stats.start_block = from_block
        self.stats.end_block = to_block

        total_blocks = to_block - from_block + 1

        if verbose:
            print(f"\nAnalyzing blocks {from_block:,} to {to_block:,} ({total_blocks:,} blocks)")
            print("=" * 70)

        # Process in chunks to avoid RPC limits
        chunk_size = 100
        for chunk_start in range(from_block, to_block + 1, chunk_size):
            chunk_end = min(chunk_start + chunk_size - 1, to_block)

            logs = self.fetch_logs(chunk_start, chunk_end)

            for log in logs:
                trade = self.decode_order_filled(log)
                if trade:
                    if wallet_filter:
                        wallet_lower = wallet_filter.lower()
                        if trade.maker.lower() != wallet_lower and trade.taker.lower() != wallet_lower:
                            continue

                    all_trades.append(trade)
                    self.stats.trades_seen += 1
                    self.stats.total_volume_usdc += trade.size_usdc
                    self.stats.unique_makers.add(trade.maker.lower())
                    self.stats.unique_takers.add(trade.taker.lower())

                    if trade.trade_type == "BUY":
                        self.stats.buys += 1
                    elif trade.trade_type == "SELL":
                        self.stats.sells += 1

                    gabagool = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"
                    if trade.maker.lower() == gabagool or trade.taker.lower() == gabagool:
                        self.stats.gabagool_trades.append(trade)

                    if verbose:
                        print(trade)

            if verbose and chunk_end < to_block:
                progress = (chunk_end - from_block + 1) / total_blocks * 100
                print(f"  ... {progress:.0f}% ({self.stats.trades_seen} trades found)")

        return all_trades

    async def monitor_live(
        self,
        wallet_filter: Optional[str] = None,
        poll_interval: float = 2.0,
    ):
        """Monitor trades in real-time."""
        if not self.web3:
            print("[ERROR] Not connected")
            return

        print("\n" + "=" * 70)
        print("LIVE ON-CHAIN MONITOR")
        print("=" * 70)
        print(f"Contract: NegRisk_CTFExchange")
        print(f"Address:  {CONTRACTS['NegRisk_CTFExchange']}")
        if wallet_filter:
            label = KNOWN_WALLETS.get(wallet_filter.lower(), wallet_filter[:16] + "...")
            print(f"Filter:   {label}")
        print(f"Polling:  Every {poll_interval}s")
        print("=" * 70)
        print("\nWaiting for trades...\n")

        last_block = self.web3.eth.block_number

        try:
            while True:
                current_block = self.web3.eth.block_number

                if current_block > last_block:
                    # Process new blocks
                    for block_num in range(last_block + 1, current_block + 1):
                        trades = self.analyze_block(block_num, wallet_filter)
                        for trade in trades:
                            print(trade)

                    last_block = current_block

                await asyncio.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\n\nStopping monitor...")
            self.print_stats()

    def print_stats(self):
        """Print monitoring statistics."""
        print("\n" + "=" * 70)
        print("MONITORING STATISTICS")
        print("=" * 70)
        print(f"Blocks analyzed: {self.stats.start_block:,} to {self.stats.end_block:,}")
        print(f"Total trades:    {self.stats.trades_seen:,}")
        print(f"Total volume:    ${self.stats.total_volume_usdc:,.2f}")
        print(f"Buys/Sells:      {self.stats.buys} / {self.stats.sells}")
        print(f"Unique makers:   {len(self.stats.unique_makers)}")
        print(f"Unique takers:   {len(self.stats.unique_takers)}")

        if self.stats.gabagool_trades:
            print(f"\nGABAGOOL TRADES: {len(self.stats.gabagool_trades)}")
            gabagool_volume = sum(t.size_usdc for t in self.stats.gabagool_trades)
            print(f"Gabagool volume: ${gabagool_volume:,.2f}")

            # Show last 5 Gabagool trades
            print("\nRecent Gabagool trades:")
            for trade in self.stats.gabagool_trades[-5:]:
                print(f"  {trade}")


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Monitor Polymarket trades on-chain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Real-time monitoring
  python scripts/onchain_monitor.py --live

  # Track Gabagool in real-time
  python scripts/onchain_monitor.py --live --wallet GABAGOOL

  # Analyze specific block
  python scripts/onchain_monitor.py --block 51866068

  # Analyze recent blocks
  python scripts/onchain_monitor.py --recent 100

  # Analyze block range
  python scripts/onchain_monitor.py --from-block 51866000 --to-block 51866100
        """
    )

    parser.add_argument("--live", action="store_true", help="Monitor live trades")
    parser.add_argument("--block", type=int, help="Analyze specific block")
    parser.add_argument("--recent", type=int, help="Analyze last N blocks")
    parser.add_argument("--from-block", type=int, help="Start block for range analysis")
    parser.add_argument("--to-block", type=int, help="End block for range analysis")
    parser.add_argument("--wallet", type=str, help="Filter by wallet (address or 'GABAGOOL')")
    parser.add_argument("--rpc", type=str, help="Custom RPC URL")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval for live mode")

    args = parser.parse_args()

    # Resolve wallet alias
    wallet_filter = None
    if args.wallet:
        if args.wallet.upper() == "GABAGOOL":
            wallet_filter = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"
        else:
            wallet_filter = args.wallet

    # Initialize monitor
    monitor = OnChainMonitor(rpc_url=args.rpc)
    if not monitor.connect():
        sys.exit(1)

    # Execute based on mode
    if args.live:
        asyncio.run(monitor.monitor_live(wallet_filter, args.poll_interval))

    elif args.block:
        print(f"\nAnalyzing block {args.block:,}")
        trades = monitor.analyze_block(args.block, wallet_filter)
        if trades:
            print(f"\nFound {len(trades)} trades:")
            for trade in trades:
                print(trade)
        else:
            print("No trades found in this block")
        monitor.print_stats()

    elif args.recent:
        current = monitor.web3.eth.block_number
        from_block = current - args.recent
        trades = monitor.analyze_range(from_block, current, wallet_filter)
        monitor.print_stats()

    elif args.from_block and args.to_block:
        trades = monitor.analyze_range(args.from_block, args.to_block, wallet_filter)
        monitor.print_stats()

    else:
        # Default: show recent activity
        print("\nNo mode specified. Use --live, --block, --recent, or --from-block/--to-block")
        print("\nShowing last 10 blocks as demo...")
        current = monitor.web3.eth.block_number
        trades = monitor.analyze_range(current - 10, current, wallet_filter, verbose=True)
        monitor.print_stats()


if __name__ == "__main__":
    main()
