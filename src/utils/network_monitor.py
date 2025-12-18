"""
Network monitoring and failover system for macOS.

This module provides WiFi connectivity monitoring and automatic failover
to backup networks when the primary network fails. Designed for macOS
using system commands to manage WiFi connections.

Usage:
    from src.config import Config
    from src.utils.network_monitor import NetworkMonitor

    config = Config()
    monitor = NetworkMonitor(config)

    # Check current status
    print(f"Current network: {monitor.get_current_network()}")
    print(f"Internet available: {monitor.check_internet()}")

    # Start monitoring (runs in background)
    await monitor.start_monitoring()
"""

import asyncio
import subprocess
import socket
import time
import logging
from typing import Optional, Callable, List, Tuple
from dataclasses import dataclass
from datetime import datetime

from src.config import Config


# Set up logging
logger = logging.getLogger(__name__)


@dataclass
class NetworkStatus:
    """Current network status snapshot."""
    current_network: str
    is_connected: bool
    latency_ms: Optional[float]
    is_on_backup: bool
    last_check: datetime


class NetworkMonitorError(Exception):
    """Base exception for network monitor errors."""
    pass


class NetworkMonitor:
    """
    WiFi network monitor with automatic failover.

    Monitors internet connectivity and automatically switches to backup
    WiFi networks when the primary connection fails. Continuously polls
    to restore primary network when available.

    macOS-specific: Uses system commands to interact with WiFi.

    Attributes:
        config: Bot configuration with network settings
        is_on_backup: True if currently on a backup network
        current_network: Name of the currently connected WiFi
    """

    def __init__(self, config: Config):
        """
        Initialize the network monitor.

        Args:
            config: Configuration object with network settings
        """
        self.config = config
        self.current_network: str = ""
        self.is_on_backup: bool = False
        self.last_check_time: Optional[datetime] = None
        self.connection_lost_count: int = 0
        self._monitoring: bool = False
        self._on_network_change: Optional[Callable[[str, str], None]] = None

        # Network priority order
        self._networks: List[Tuple[str, str]] = [
            (config.primary_wifi, ""),  # Primary doesn't need password (already saved)
            (config.backup_wifi_1, config.backup_wifi_1_password),
            (config.backup_wifi_2, config.backup_wifi_2_password),
        ]

    def get_current_network(self) -> str:
        """
        Get the currently connected WiFi network name (SSID).

        macOS-specific: Uses networksetup command.

        Returns:
            WiFi network name, or empty string if not connected
        """
        try:
            result = subprocess.run(
                ["networksetup", "-getairportnetwork", "en0"],
                capture_output=True,
                text=True,
                timeout=5
            )

            # Output format: "Current Wi-Fi Network: NetworkName"
            output = result.stdout.strip()
            if "Current Wi-Fi Network:" in output:
                network = output.split("Current Wi-Fi Network:")[1].strip()
                self.current_network = network
                logger.debug(f"Current network: {network}")
                return network
            elif "not associated" in output.lower():
                self.current_network = ""
                return ""
            else:
                logger.warning(f"Unexpected network output: {output}")
                return ""

        except subprocess.TimeoutExpired:
            logger.error("Timeout getting current network")
            return ""
        except Exception as e:
            logger.error(f"Error getting current network: {e}")
            return ""

    def check_internet(self, timeout: float = 2.0) -> Tuple[bool, Optional[float]]:
        """
        Check if internet connectivity is available.

        Tests connectivity by attempting to connect to a reliable DNS server.

        Args:
            timeout: Connection timeout in seconds

        Returns:
            Tuple of (is_connected: bool, latency_ms: float or None)
        """
        test_hosts = [
            ("8.8.8.8", 53),      # Google DNS
            ("1.1.1.1", 53),      # Cloudflare DNS
            ("208.67.222.222", 53),  # OpenDNS
        ]

        for host, port in test_hosts:
            try:
                start_time = time.time()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((host, port))
                latency = (time.time() - start_time) * 1000  # Convert to ms
                sock.close()

                if result == 0:
                    logger.debug(f"Internet check OK via {host} (latency: {latency:.0f}ms)")
                    self.last_check_time = datetime.now()
                    return True, latency

            except socket.timeout:
                continue
            except Exception as e:
                logger.debug(f"Connection to {host} failed: {e}")
                continue

        logger.warning("Internet check FAILED - all hosts unreachable")
        self.connection_lost_count += 1
        return False, None

    def is_primary_available(self) -> bool:
        """
        Check if the primary WiFi network is within range.

        Scans for available networks and checks if primary is in the list.

        Returns:
            True if primary network is available
        """
        if not self.config.primary_wifi:
            logger.warning("No primary WiFi configured")
            return False

        available = self.get_available_networks()
        return self.config.primary_wifi in available

    def get_available_networks(self) -> List[str]:
        """
        Scan for available WiFi networks.

        macOS-specific: Uses system_profiler or wdutil command.

        Returns:
            List of available network names (SSIDs)
        """
        networks = []

        # Method 1: Try system_profiler (works on all macOS versions)
        try:
            result = subprocess.run(
                ["system_profiler", "SPAirPortDataType"],
                capture_output=True,
                text=True,
                timeout=15
            )

            if result.returncode == 0:
                # Parse the output for network names
                # Look for lines with "PHY Mode:" which indicate network entries
                lines = result.stdout.split("\n")
                current_ssid = None

                for line in lines:
                    line = line.strip()
                    # Network names appear as keys followed by colons
                    if line and ":" in line and not line.startswith("PHY Mode"):
                        # Check if this could be an SSID (not a known system key)
                        key = line.split(":")[0].strip()
                        skip_keys = [
                            "Software Versions", "Interfaces", "Status",
                            "Current Network Information", "PHY Mode",
                            "BSSID", "Channel", "Country Code", "Security",
                            "RSSI", "Noise", "Transmit Rate", "MCS Index",
                            "Card Type", "Firmware Version", "MAC Address",
                            "Locale", "Supported PHY Modes", "Supported Channels",
                            "Wake On Wireless", "AirDrop", "Auto Unlock"
                        ]
                        if key and key not in skip_keys and not key.startswith("en"):
                            # This might be an SSID
                            if len(key) < 50:  # Reasonable SSID length
                                networks.append(key)

                if networks:
                    logger.debug(f"Available networks: {networks}")
                    return list(set(networks))  # Remove duplicates

        except subprocess.TimeoutExpired:
            logger.warning("Timeout using system_profiler")
        except Exception as e:
            logger.debug(f"system_profiler failed: {e}")

        # Method 2: Try the legacy airport command (older macOS)
        try:
            airport_paths = [
                "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport",
                "/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport",
            ]

            for airport_path in airport_paths:
                try:
                    result = subprocess.run(
                        [airport_path, "-s"],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )

                    if result.returncode == 0 and result.stdout.strip():
                        lines = result.stdout.strip().split("\n")
                        for line in lines[1:]:  # Skip header
                            parts = line.strip().split()
                            if parts:
                                for i, part in enumerate(parts):
                                    if ":" in part and len(part) == 17:
                                        ssid = " ".join(parts[:i])
                                        if ssid:
                                            networks.append(ssid)
                                        break

                        if networks:
                            logger.debug(f"Available networks: {networks}")
                            return networks
                except FileNotFoundError:
                    continue

        except Exception as e:
            logger.debug(f"airport command failed: {e}")

        logger.warning("Could not scan for available networks")
        return networks

    def switch_network(self, ssid: str, password: str = "") -> bool:
        """
        Switch to a specific WiFi network.

        macOS-specific: Uses networksetup command.

        Args:
            ssid: Network name to connect to
            password: Network password (empty if saved/open network)

        Returns:
            True if switch was successful
        """
        if not ssid:
            logger.error("No SSID provided for network switch")
            return False

        logger.info(f"Switching to network: {ssid}")

        try:
            # Build command
            cmd = ["networksetup", "-setairportnetwork", "en0", ssid]
            if password:
                cmd.append(password)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15
            )

            # Check if switch was successful
            if result.returncode != 0:
                logger.error(f"Network switch failed: {result.stderr}")
                return False

            # Wait for connection to establish
            for _ in range(10):  # Wait up to 10 seconds
                time.sleep(1)
                current = self.get_current_network()
                if current == ssid:
                    # Verify internet works on new network
                    connected, latency = self.check_internet()
                    if connected:
                        logger.info(f"Successfully switched to {ssid} (latency: {latency:.0f}ms)")
                        return True

            logger.warning(f"Connected to {ssid} but no internet")
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout switching to {ssid}")
            return False
        except Exception as e:
            logger.error(f"Error switching network: {e}")
            return False

    def failover(self) -> bool:
        """
        Attempt to switch to a backup network.

        Tries backup networks in order until one works.

        Returns:
            True if successfully switched to a working backup network
        """
        logger.warning("Initiating failover to backup network")

        # Try each backup network
        for ssid, password in self._networks[1:]:  # Skip primary
            if not ssid:
                continue

            logger.info(f"Trying backup network: {ssid}")

            if self.switch_network(ssid, password):
                self.is_on_backup = True
                self.current_network = ssid
                logger.info(f"Failover successful: now on {ssid}")

                # Notify callback if set
                if self._on_network_change:
                    self._on_network_change("failover", ssid)

                return True

        logger.error("Failover failed: no backup networks available")
        return False

    def check_and_restore_primary(self) -> bool:
        """
        Check if primary network is available and switch back if so.

        Only runs when currently on a backup network.

        Returns:
            True if successfully restored to primary network
        """
        if not self.is_on_backup:
            return False

        if not self.config.primary_wifi:
            return False

        # Check if primary is available
        if not self.is_primary_available():
            logger.debug("Primary network not in range")
            return False

        # Try to switch back
        logger.info(f"Primary network {self.config.primary_wifi} available, attempting restore")

        if self.switch_network(self.config.primary_wifi):
            self.is_on_backup = False
            self.current_network = self.config.primary_wifi
            logger.info("Restored to primary network")

            # Notify callback if set
            if self._on_network_change:
                self._on_network_change("restored", self.config.primary_wifi)

            return True

        logger.warning("Failed to restore to primary network")
        return False

    async def start_monitoring(
        self,
        callback: Optional[Callable[[str, str], None]] = None
    ) -> None:
        """
        Start the network monitoring loop.

        Runs continuously, checking connectivity at regular intervals
        and performing failover/restore as needed.

        Args:
            callback: Optional function called on network changes.
                     Receives (event_type, network_name) where event_type
                     is 'failover', 'restored', or 'disconnected'
        """
        self._monitoring = True
        self._on_network_change = callback

        logger.info(
            f"Starting network monitor "
            f"(poll interval: {self.config.network_poll_interval}s)"
        )

        # Initial status
        self.current_network = self.get_current_network()
        connected, latency = self.check_internet()

        logger.info(
            f"Initial status: network={self.current_network}, "
            f"connected={connected}, latency={latency}ms"
        )

        while self._monitoring:
            try:
                # Check internet connectivity
                connected, latency = self.check_internet()

                if not connected:
                    # Internet is down - try failover
                    logger.warning("Internet connection lost")

                    if callback:
                        callback("disconnected", self.current_network)

                    if not self.failover():
                        # All networks failed - wait and retry
                        logger.error("All networks failed, waiting before retry")
                        await asyncio.sleep(self.config.network_poll_interval)
                        continue

                elif self.is_on_backup:
                    # On backup network - try to restore primary
                    self.check_and_restore_primary()

                # Wait for next poll interval
                await asyncio.sleep(self.config.network_poll_interval)

            except asyncio.CancelledError:
                logger.info("Network monitoring cancelled")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(self.config.network_poll_interval)

        self._monitoring = False

    def stop_monitoring(self) -> None:
        """Stop the network monitoring loop."""
        logger.info("Stopping network monitor")
        self._monitoring = False

    def get_status(self) -> NetworkStatus:
        """
        Get current network status snapshot.

        Returns:
            NetworkStatus object with current state
        """
        current = self.get_current_network()
        connected, latency = self.check_internet()

        return NetworkStatus(
            current_network=current,
            is_connected=connected,
            latency_ms=latency,
            is_on_backup=self.is_on_backup,
            last_check=datetime.now()
        )

    def __repr__(self) -> str:
        """String representation showing current status."""
        status = "backup" if self.is_on_backup else "primary"
        return f"NetworkMonitor(network={self.current_network}, status={status})"
