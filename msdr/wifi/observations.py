"""`NetworkTable`: aggregates parsed beacons/probe responses by BSSID for the
Discover "Wi-Fi" tab. Distinct BSSIDs that happen to share an SSID are never
merged -- the table is keyed strictly by BSSID.

Wall-clock fields (first_seen/last_seen) come from the pcap record's own
timestamp (`Record.ts`), so replay reproduces the original session's times
rather than "now". `updated_at` is separate: processing wall clock, used only
by `snapshot_changed_since` to drive the 1 Hz UI push.
"""

from __future__ import annotations

import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass

from msdr import oui
from msdr.remoteid import messages as remoteid_messages
from msdr.remoteid import transport as remoteid_transport
from msdr.wifi import dot11, radiotap
from msdr.wifi.pcap import Record

DEFAULT_MAX_BSSIDS = 5000
DEFAULT_SIGNAL_MAXLEN = 300
SSID_HISTORY_MAXLEN = 16
# Typical signal = median of this many most recent samples (same source as
# the latest): one odd frame -- e.g. a raw PHY code reported in another
# modulation's format -- can't jump a network to the top.
SIGNAL_MEDIAN_WINDOW = 30
DEFAULT_FRAME_RING_MAXLEN = 50
DUPLICATE_WINDOW_S = 1.0


@dataclass(frozen=True)
class SignalSample:
    ts: float
    raw_code: int
    source: str  # "vendor" | "dbm"


class NetworkEntry:
    """Mutable per-BSSID aggregate. Not part of the public contract on its
    own -- callers go through `NetworkTable.rows`/`details`/`frame`."""

    def __init__(self, bssid: str, first_seen: float, signal_maxlen: int, frame_maxlen: int):
        self.bssid = bssid
        self.ssid_text = ""
        self.ssid_hidden = True
        # Capped at SSID_HISTORY_MAXLEN (a hostile AP can rotate SSIDs on one
        # BSSID indefinitely); _ssid_order tracks recency for eviction.
        self.ssid_history: set[str] = set()
        self._ssid_order: deque[str] = deque()
        self.channel: int | None = None
        self.channel_source: str = "configured"
        self.channel_mismatch = False
        self.security_summary = "Open"
        self.first_seen = first_seen
        self.last_seen = first_seen
        self.beacons = 0
        self.probe_responses = 0
        self.bad_fcs_seen = 0
        self.duplicates = 0
        self.signal_history: deque[SignalSample] = deque(maxlen=signal_maxlen)
        self.recent_frames: deque[bytes] = deque(maxlen=frame_maxlen)
        # Kept in lockstep with recent_frames (same maxlen, appended together
        # for every frame -- good-FCS and attributable bad-FCS alike) so its
        # index always matches recent_frames'/NetworkTable.frame()'s "n".
        self.recent_meta: deque[dict] = deque(maxlen=frame_maxlen)
        self.latest_frame: dot11.ManagementFrame | None = None
        self.beacon_interval: int | None = None
        self.cap_ess = False
        self.cap_ibss = False
        self.cap_privacy = False
        self.updated_at = time.time()
        self._dedup_key: tuple[int, bytes] | None = None
        self._dedup_ts: float | None = None

    def typical_signal(self) -> int | None:
        if not self.signal_history:
            return None
        source = self.signal_history[-1].source
        recent = list(self.signal_history)[-SIGNAL_MEDIAN_WINDOW:]
        codes = sorted(s.raw_code for s in recent if s.source == source)
        return codes[len(codes) // 2]

    def summary(self) -> dict:
        latest_signal = self.signal_history[-1] if self.signal_history else None
        return {
            "bssid": self.bssid,
            "ssid": self.ssid_text,
            "ssid_hidden": self.ssid_hidden,
            # Names this BSSID has ever revealed (beacons, or probe responses
            # to a client that knew the name) -- a hidden network's real name.
            "revealed_ssids": sorted(self.ssid_history),
            "channel": self.channel,
            "channel_source": self.channel_source,
            "channel_mismatch": self.channel_mismatch,
            "security_summary": self.security_summary,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "beacons": self.beacons,
            "probe_responses": self.probe_responses,
            "bad_fcs_seen": self.bad_fcs_seen,
            "duplicates": self.duplicates,
            "signal_raw_code": latest_signal.raw_code if latest_signal else None,
            "signal_source": latest_signal.source if latest_signal else None,
            "signal_median": self.typical_signal(),
            "signal_samples": len(self.signal_history),
        }

    def details(self) -> dict:
        d = self.summary()
        d.update(
            ssid_history=sorted(self.ssid_history),
            ies=list(self.latest_frame.ies) if self.latest_frame else [],
            rsn=self.latest_frame.rsn if self.latest_frame else None,
            wpa1=self.latest_frame.wpa1 if self.latest_frame else None,
            ht_present=self.latest_frame.ht_present if self.latest_frame else False,
            vht_present=self.latest_frame.vht_present if self.latest_frame else False,
            he_present=self.latest_frame.he_present if self.latest_frame else False,
            country=self.latest_frame.country if self.latest_frame else None,
            beacon_interval=self.beacon_interval,
            cap_ess=self.cap_ess,
            cap_ibss=self.cap_ibss,
            cap_privacy=self.cap_privacy,
            supported_rates_mbps=(
                list(self.latest_frame.supported_rates_mbps) if self.latest_frame else []
            ),
            signal_history=[
                {"ts": s.ts, "raw_code": s.raw_code, "source": s.source}
                for s in self.signal_history
            ],
            recent_frame_count=len(self.recent_frames),
            recent=[{**meta, "n": i} for i, meta in enumerate(self.recent_meta)],
        )
        return d


# 802.11 frame type/subtype names for the live feed (IEEE 802.11-2020 9.2.4.1.3).
# Coarse security buckets for the network summary, from dot11._security_summary.
_SECURITY_ORDER = ("Open", "WEP", "WPA1", "WPA2", "WPA2/WPA3", "WPA3", "Enterprise", "OWE", "Other")


def _security_bucket(summary: str | None) -> str:
    s = summary or ""
    if s in ("Open", "WEP", "OWE", "WPA2/WPA3"):
        return s
    if "Enterprise" in s:
        return "Enterprise"
    for prefix in ("WPA3", "WPA2", "WPA1"):
        if s.startswith(prefix):
            return prefix
    return "Other"


def _ap_group_key(bssid: str) -> str:
    """Same heuristic as wifi-view.js's apGroupKey: a router derives extra
    BSSIDs by flipping the locally-administered bit and the last digit."""
    octets = bssid.lower().split(":")
    if len(octets) != 6:
        return bssid
    first = f"{int(octets[0], 16) & ~0x02:02x}"
    return ":".join([first, *octets[1:5], octets[5][:1]])


_FRAME_NAMES = {
    (0, 0): "assoc request", (0, 1): "assoc response", (0, 2): "reassoc request",
    (0, 3): "reassoc response", (0, 4): "probe request", (0, 5): "probe response",
    (0, 8): "beacon", (0, 9): "ATIM", (0, 10): "disassociation", (0, 11): "authentication",
    (0, 12): "deauthentication", (0, 13): "action", (0, 14): "action no ack",
    (1, 4): "beamforming report poll", (1, 5): "NDP announcement", (1, 8): "block ack request",
    (1, 9): "block ack", (1, 10): "PS-Poll", (1, 11): "RTS", (1, 12): "CTS", (1, 13): "ACK",
    (1, 14): "CF-End",
    (2, 0): "data", (2, 4): "null data", (2, 8): "QoS data", (2, 12): "QoS null",
}
_KIND = {0: "management", 1: "control", 2: "data", 3: "extension"}
LIVE_FEED_MAXLEN = 500


def _mac(data: bytes, offset: int) -> str | None:
    if len(data) < offset + 6:
        return None
    return ":".join(f"{b:02x}" for b in data[offset : offset + 6])


class NetworkTable:
    def __init__(
        self,
        configured_channel: int,
        max_bssids: int = DEFAULT_MAX_BSSIDS,
        signal_maxlen: int = DEFAULT_SIGNAL_MAXLEN,
        frame_ring_maxlen: int = DEFAULT_FRAME_RING_MAXLEN,
    ) -> None:
        self.configured_channel = configured_channel
        self.max_bssids = max_bssids
        self.signal_maxlen = signal_maxlen
        self.frame_ring_maxlen = frame_ring_maxlen
        self._entries: OrderedDict[str, NetworkEntry] = OrderedDict()
        self.bad_fcs_global = 0
        self.parse_errors_global = 0
        self.evictions = 0
        # Live feed: one summary per received frame of any type, bounded; read
        # incrementally by sequence number (GET /api/wifi/live?after=).
        self.live: deque[dict] = deque(maxlen=LIVE_FEED_MAXLEN)
        self.live_seq = 0
        # Device history (docs/history.md): set by the supervisor; receives
        # one add() per trusted, non-duplicate beacon/probe response.
        self.history = None
        # Live Remote ID tracker (docs/remote-id.md "Live"): set by the
        # service (msdr.api.services.remoteid), synced each tick the same way
        # `history` is -- see the sink attached right below where this one is
        # fed, in `_ingest`.
        self.remoteid = None

    def clear(self) -> None:
        """Forget every observed network and the table counters, so the
        list starts over (capture keeps running; the live feed is separate)."""
        self._entries.clear()
        self.bad_fcs_global = 0
        self.parse_errors_global = 0
        self.evictions = 0

    def live_since(self, after: int = 0, kind: str = "all", limit: int = 200) -> dict:
        """Frames with seq > after (oldest first), optionally management only."""
        items = [e for e in self.live if e["seq"] > after and (kind == "all" or e["kind"] == kind)]
        return {"items": items[-limit:], "seq": self.live_seq, "maxlen": LIVE_FEED_MAXLEN}

    def _live_append(self, record: Record, rt, frame_bytes: bytes, frame) -> None:
        fc = frame_bytes[0] if frame_bytes else 0
        ftype, subtype = (fc >> 2) & 0x3, fc >> 4
        self.live_seq += 1
        signal = None
        if not isinstance(rt, radiotap.ParseError):
            if rt.dbm_antsignal is not None:
                signal = {"kind": "dbm", "value": rt.dbm_antsignal}
            else:
                raw = next(iter(rt.vendor.values()), b"") if rt.vendor else b""
                if raw:
                    signal = {"kind": "raw", "value": raw[0]}
        self.live.append({
            "seq": self.live_seq,
            "ts": record.ts,
            "kind": _KIND.get(ftype, "unknown"),
            "name": _FRAME_NAMES.get((ftype, subtype), f"type {ftype} subtype {subtype}"),
            "length": len(frame_bytes),
            "bad_fcs": bool(
                not isinstance(rt, radiotap.ParseError) and rt.flags and rt.flags.bad_fcs
            ),
            "source": _mac(frame_bytes, 10),
            "destination": _mac(frame_bytes, 4),
            "bssid": getattr(frame, "bssid", None),
            "ssid": (None if frame is None or getattr(frame, "ssid_hidden", True)
                     else getattr(frame, "ssid_text", None)),
            "channel": getattr(frame, "ds_channel", None),
            "signal": signal,
        })

    def __len__(self) -> int:
        return len(self._entries)

    def _touch(self, bssid: str, ts: float) -> NetworkEntry:
        entry = self._entries.get(bssid)
        if entry is not None:
            self._entries.move_to_end(bssid)
            return entry
        entry = NetworkEntry(
            bssid=bssid,
            first_seen=ts,
            signal_maxlen=self.signal_maxlen,
            frame_maxlen=self.frame_ring_maxlen,
        )
        self._entries[bssid] = entry
        if len(self._entries) > self.max_bssids:
            self._entries.popitem(last=False)
            self.evictions += 1
        return entry

    def _ingest_nan(self, record: Record, frame_bytes: bytes, fcs_present: bool) -> None:
        nan = remoteid_transport.extract_nan_remote_id(frame_bytes, fcs_present=fcs_present)
        if nan is None:
            return
        source, wifi_rid = nan
        if not isinstance(wifi_rid.pack, remoteid_messages.RemoteIdError):
            self.remoteid.ingest_wifi_pack(
                f"wifi:{source}", wifi_rid.message_counter, wifi_rid.pack, received_at=record.ts
            )

    def ingest(self, record: Record, rt: radiotap.Radiotap | radiotap.ParseError) -> None:
        frame = self._ingest(record, rt)
        header = 0 if isinstance(rt, radiotap.ParseError) else rt.header_length
        self._live_append(record, rt, record.data[header:], frame)

    def _ingest(self, record: Record, rt: radiotap.Radiotap | radiotap.ParseError):
        """Update the table; returns the parsed management frame or None."""
        if isinstance(rt, radiotap.ParseError):
            self.parse_errors_global += 1
            return

        fcs_present = rt.flags.fcs_present if rt.flags else False
        bad_fcs = rt.flags.bad_fcs if rt.flags else False
        frame_bytes = record.data[rt.header_length :]

        result = dot11.parse(frame_bytes, fcs_present=fcs_present)
        if isinstance(result, dot11.Dot11Error):
            if result.kind == "unsupported_frame":
                # Not a beacon/probe-response at all (control/data/other
                # management frames) -- expected and uninteresting, never a
                # parse error or an FCS-quality signal. One exception: Remote
                # ID over Wi-Fi NAN rides in action frames (docs/remote-id.md).
                if self.remoteid is not None and not bad_fcs:
                    self._ingest_nan(record, frame_bytes, fcs_present)
                return
            if bad_fcs:
                self.bad_fcs_global += 1
            else:
                self.parse_errors_global += 1
            return
        frame = result

        if bad_fcs:
            # Attributable (we could still read the header/BSSID), but a
            # bad-FCS frame's content is never trusted for observations --
            # still shown in "recent broadcasts" (bad_fcs=True) so an
            # operator can see it was heard.
            # Only count it against a network already confirmed by good frames:
            # a corrupted header can carry a corrupted BSSID, and creating an
            # entry from it fills the table with phantom networks (this adapter
            # sees 20-85 % bad FCS). last_seen stays tied to trusted frames.
            entry = self._entries.get(frame.bssid)
            if entry is None:
                self.bad_fcs_global += 1
                return frame
            entry.bad_fcs_seen += 1
            entry.updated_at = time.time()
            entry.recent_frames.append(frame_bytes)
            entry.recent_meta.append(
                {
                    "ts": record.ts,
                    "subtype": frame.subtype,
                    "length": len(frame_bytes),
                    "bad_fcs": True,
                }
            )
            return frame

        entry = self._touch(frame.bssid, record.ts)

        dedup_key = (frame.frame_timestamp, frame_bytes)
        if (
            entry._dedup_key == dedup_key
            and entry._dedup_ts is not None
            and abs(record.ts - entry._dedup_ts) <= DUPLICATE_WINDOW_S
        ):
            entry.duplicates += 1
            entry.last_seen = max(entry.last_seen, record.ts)
            return frame
        entry._dedup_key = dedup_key
        entry._dedup_ts = record.ts

        if not frame.ssid_hidden:
            if frame.ssid_text in entry._ssid_order:
                entry._ssid_order.remove(frame.ssid_text)
            entry.ssid_history.add(frame.ssid_text)
            entry._ssid_order.append(frame.ssid_text)
            while len(entry._ssid_order) > SSID_HISTORY_MAXLEN:
                entry.ssid_history.discard(entry._ssid_order.popleft())
        entry.ssid_text = frame.ssid_text
        entry.ssid_hidden = frame.ssid_hidden

        if frame.ds_channel is not None:
            entry.channel = frame.ds_channel
            entry.channel_source = "ds_ie"
            entry.channel_mismatch = frame.ds_channel != self.configured_channel
        else:
            entry.channel = self.configured_channel
            entry.channel_source = "configured"
            entry.channel_mismatch = False

        entry.security_summary = frame.security_summary
        entry.latest_frame = frame
        entry.beacon_interval = frame.beacon_interval
        entry.cap_ess = frame.cap_ess
        entry.cap_ibss = frame.cap_ibss
        entry.cap_privacy = frame.cap_privacy
        entry.first_seen = min(entry.first_seen, record.ts)
        entry.last_seen = max(entry.last_seen, record.ts)
        if frame.subtype == dot11.SUBTYPE_BEACON:
            entry.beacons += 1
        else:
            entry.probe_responses += 1

        raw_code: int | None = None
        source: str | None = None
        raw = radiotap.msdr_raw(rt)
        if raw is not None and raw.packet_power_code is not None:
            # The msdr-raw payload's first byte is its version, not a signal code.
            raw_code, source = raw.packet_power_code, "vendor"
        elif rt.vendor:
            payload = rt.vendor[min(rt.vendor.keys())]
            if payload:
                raw_code, source = payload[0], "vendor"
        if raw_code is None and rt.dbm_antsignal is not None:
            raw_code, source = rt.dbm_antsignal, "dbm"
        if raw_code is not None and source is not None:
            entry.signal_history.append(
                SignalSample(ts=record.ts, raw_code=raw_code, source=source)
            )
        if self.history is not None:
            self.history.add(
                "wifi",
                frame.bssid,
                record.ts,
                raw_code,
                name=None if frame.ssid_hidden else frame.ssid_text,
                kind="bssid",
                signal_kind="dbm" if source == "dbm" else "raw",
            )
        if self.remoteid is not None:
            wifi_rid = remoteid_transport.extract_wifi_remote_id(frame)
            if wifi_rid is not None and not isinstance(
                wifi_rid.pack, remoteid_messages.RemoteIdError
            ):
                self.remoteid.ingest_wifi_pack(
                    f"wifi:{frame.bssid}",
                    wifi_rid.message_counter,
                    wifi_rid.pack,
                    received_at=record.ts,
                )

        entry.recent_frames.append(frame_bytes)
        entry.recent_meta.append(
            {
                "ts": record.ts,
                "subtype": frame.subtype,
                "length": len(frame_bytes),
                "bad_fcs": False,
            }
        )
        entry.updated_at = time.time()
        return frame

    # Sortable columns -> key on a NetworkEntry. None sorts last in both
    # directions (hidden SSIDs, unknown channel/signal); BSSID breaks ties.
    SORT_KEYS = {
        "ssid": lambda e: (e.ssid_text or "").casefold() if not e.ssid_hidden else None,
        "bssid": lambda e: e.bssid,
        "channel": lambda e: e.channel,
        "security": lambda e: e.security_summary,
        "signal": lambda e: e.typical_signal(),
        "first_seen": lambda e: e.first_seen,
        "last_seen": lambda e: e.last_seen,
        "packets": lambda e: e.beacons + e.probe_responses,
    }

    def network_summary(self, now: float, window_s: float = 60.0, top_makers: int = 6) -> dict:
        """What's around: networks heard in the last `window_s` by band,
        security, maker and hidden/probable-access-point counts
        (docs/wifi.md "Network summary"). Infrastructure only -- msdr never
        tracks clients."""
        recent = [e for e in self._entries.values() if now - e.last_seen <= window_s]
        bands: Counter = Counter()
        security: Counter = Counter()
        makers: Counter = Counter()
        hidden = 0
        local_admin = 0
        access_points: set[str] = set()
        for e in recent:
            bands["2.4 GHz" if (e.channel or 0) <= 14 else "5 GHz"] += 1
            security[_security_bucket(e.security_summary)] += 1
            hidden += e.ssid_hidden
            is_local = oui.is_locally_administered(e.bssid)
            local_admin += is_local
            makers[oui.vendor(e.bssid) or ("Locally administered" if is_local else "Unknown")] += 1
            access_points.add(_ap_group_key(e.bssid))
        tails = ("Locally administered", "Unknown")
        named = [(m, n) for m, n in makers.most_common() if m not in tails]
        maker_rows = [{"label": m, "count": n} for m, n in named[:top_makers]]
        rest = sum(n for _, n in named[top_makers:])
        if rest:
            maker_rows.append({"label": "Other makers", "count": rest})
        maker_rows += [{"label": t, "count": makers[t]} for t in tails if makers[t]]
        return {
            "window_s": window_s,
            "total": len(recent),
            "access_points": len(access_points),
            "hidden": hidden,
            "local_admin": local_admin,
            "bands": [{"label": b, "count": n} for b, n in bands.most_common()],
            "security": [
                {"label": b, "count": security[b]} for b in _SECURITY_ORDER if security[b]
            ],
            "makers": maker_rows,
        }

    def rows(
        self,
        q: str | None = None,
        channel: int | None = None,
        offset: int = 0,
        limit: int = 100,
        sort: str = "first_seen",
        order: str = "asc",
    ) -> tuple[list[dict], int]:
        needle = q.lower() if q else None
        candidates = list(self._entries.values())
        if needle is not None:
            candidates = [
                e
                for e in candidates
                if needle in e.ssid_text.lower() or needle in e.bssid.lower()
            ]
        if channel is not None:
            candidates = [e for e in candidates if e.channel == channel]
        key = self.SORT_KEYS.get(sort, self.SORT_KEYS["first_seen"])
        # Stable two-pass sort: BSSID ascending first, then the column, so
        # equal values keep a fixed order and rows never jitter between polls.
        candidates.sort(key=lambda e: e.bssid)
        present = [e for e in candidates if key(e) is not None]
        missing = [e for e in candidates if key(e) is None]
        present.sort(key=key, reverse=(order == "desc"))
        candidates = present + missing
        total = len(candidates)
        page = candidates[offset : offset + limit]
        return [e.summary() for e in page], total

    def details(self, bssid: str) -> dict | None:
        entry = self._entries.get(bssid)
        return entry.details() if entry is not None else None

    def frame(self, bssid: str, n: int) -> bytes | None:
        entry = self._entries.get(bssid)
        if entry is None:
            return None
        frames = entry.recent_frames
        if n < 0 or n >= len(frames):
            return None
        return frames[n]

    def snapshot_changed_since(self, t: float) -> list[str]:
        return [e.bssid for e in self._entries.values() if e.updated_at > t]

    def since(self) -> float | None:
        """First frame time of the table -- the earliest first_seen among
        current entries, or None when nothing has been observed yet."""
        if not self._entries:
            return None
        return min(e.first_seen for e in self._entries.values())
