"""Import a pcap file through the same parse/aggregate pipeline the live
capture uses, for development and offline inspection (`POST /api/wifi/import`,
and tests)."""

from __future__ import annotations

from pathlib import Path

from msdr.wifi import pcap, radiotap
from msdr.wifi.observations import NetworkTable


def replay_file(path: str | Path, configured_channel: int) -> NetworkTable:
    table = NetworkTable(configured_channel=configured_channel)
    for record in pcap.read_file(path):
        if record.linktype != pcap.LINKTYPE_IEEE802_11_RADIOTAP:
            continue
        rt = radiotap.parse(record.data)
        table.ingest(record, rt)
    return table
