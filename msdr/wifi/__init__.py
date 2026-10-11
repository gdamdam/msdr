"""Passive Wi-Fi reception: pcap framing, radiotap and 802.11 management-frame
parsing, per-BSSID aggregation and offline replay (see docs/wifi.md). Capture
lifecycle (`supervisor.py`) and session recording (`sessions.py`) are a
separate slice of this package, built against the same `pcap`/`radiotap`/
`dot11`/`observations` pipeline."""

from msdr.wifi.dot11 import Dot11Error, ManagementFrame
from msdr.wifi.observations import NetworkTable
from msdr.wifi.pcap import PcapStreamReader, Record
from msdr.wifi.radiotap import ParseError, Radiotap
from msdr.wifi.replay import replay_file

__all__ = [
    "Dot11Error",
    "ManagementFrame",
    "NetworkTable",
    "ParseError",
    "PcapStreamReader",
    "Radiotap",
    "Record",
    "replay_file",
]
