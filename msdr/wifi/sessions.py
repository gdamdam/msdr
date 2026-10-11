"""Explicit recording of the raw Wi-Fi pcap stream into bounded rotating
files under the recordings root (`wifi/<session-id>/part-NNN.pcap`), plus
listing, retention pruning, export (concatenated single-header pcap) and
import/replay of an offline pcap file. See docs/wifi.md ("msdr side").

Ordinary packets never enter `DecodeEventRepo` -- this module only ever
touches its own `root` directory.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from msdr.wifi import pcap, radiotap
from msdr.wifi.observations import NetworkTable
from msdr.wifi.pcap import Record

SESSION_META_NAME = "session.json"

#: One part file is rotated out at this size regardless of the session's own
#: record_max_mb (keeps any single file Wireshark-friendly).
DEFAULT_PART_MAX_BYTES = 50 * 1024 * 1024


def _new_session_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


class WifiRecordingSession:
    """One in-progress recording: rotates `part-NNN.pcap` files under its own
    directory, stopping once `max_bytes`/`max_seconds` is reached."""

    def __init__(
        self,
        directory: Path,
        *,
        channel: int,
        receiver_id: str,
        helper_commit: str | None,
        max_bytes: int,
        max_seconds: float,
        part_max_bytes: int = DEFAULT_PART_MAX_BYTES,
    ) -> None:
        self.id = directory.name
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.channel = channel
        self.receiver_id = receiver_id
        self.helper_commit = helper_commit
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.part_max_bytes = part_max_bytes
        self.started = time.time()
        self.stopped: float | None = None
        self.frames = 0
        self.total_bytes = 0
        self.drops = 0
        self.health_notes: list[str] = []
        self._part_index = 0
        self._part_file = None
        self._part_bytes = 0
        self._open_new_part()

    def _open_new_part(self) -> None:
        path = self.dir / f"part-{self._part_index:03d}.pcap"
        self._part_file = path.open("wb")
        header = pcap.write_header()
        self._part_file.write(header)
        self._part_file.flush()
        self._part_bytes = len(header)
        self._part_index += 1

    def write_record(self, record: Record) -> bool:
        """Append one pcap record. Returns False once a size/duration limit
        has been reached (the caller must stop feeding and finalize); the
        record passed in this final call is still written first."""
        if self.stopped is not None:
            return False
        encoded = pcap.write_record(record.ts, record.data, record.orig_len)
        if self._part_bytes + len(encoded) > self.part_max_bytes:
            assert self._part_file is not None
            self._part_file.close()
            self._open_new_part()
        assert self._part_file is not None
        self._part_file.write(encoded)
        self._part_file.flush()
        self._part_bytes += len(encoded)
        self.total_bytes += len(encoded)
        self.frames += 1
        elapsed = time.time() - self.started
        return self.total_bytes < self.max_bytes and elapsed < self.max_seconds

    def note_drops(self, drops: int) -> None:
        self.drops = drops

    def add_health_note(self, note: str) -> None:
        if note not in self.health_notes:
            self.health_notes.append(note)

    def meta(self) -> dict:
        return {
            "id": self.id,
            "started": self.started,
            "stopped": self.stopped,
            "channel": self.channel,
            "receiver_id": self.receiver_id,
            "helper_commit": self.helper_commit,
            "frames": self.frames,
            "bytes": self.total_bytes,
            "drops": self.drops,
            "health_notes": self.health_notes,
            "imported": False,
            "part_count": self._part_index,
        }

    def finalize(self) -> dict:
        if self._part_file is not None:
            self._part_file.close()
            self._part_file = None
        if self.stopped is None:
            self.stopped = time.time()
        meta = self.meta()
        (self.dir / SESSION_META_NAME).write_text(json.dumps(meta, indent=2))
        return meta


class WifiSessionStore:
    """Sessions on disk under `root` (`<recordings_root>/wifi`): listing,
    retention pruning, export and import/replay."""

    def __init__(self, root: Path) -> None:
        # Never created eagerly here -- recorder_root may point at an
        # unmounted external volume while the wifi feature stays disabled
        # (settings.wifi.enabled defaults False). WifiRecordingSession.mkdir
        # (new_session/import_pcap) is what actually creates it, on first use.
        self.root = root
        self._replay_cache: dict[str, NetworkTable] = {}

    def _session_dirs(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(d for d in self.root.iterdir() if d.is_dir())

    def _session_dir_for(self, session_id: str) -> Path:
        root = self.root.resolve()
        candidate = (root / session_id).resolve()
        if candidate.parent != root or not candidate.is_dir():
            raise KeyError(session_id)
        return candidate

    @staticmethod
    def _read_meta(d: Path) -> dict | None:
        meta_path = d / SESSION_META_NAME
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text())
        except (OSError, ValueError):
            return None

    def prune(self, retention_days: float) -> int:
        """Remove sessions whose stop time (or start time, if never
        finalized) is older than `retention_days`. Returns how many were
        removed."""
        cutoff = time.time() - retention_days * 86400
        removed = 0
        for d in self._session_dirs():
            meta = self._read_meta(d)
            reference = (meta or {}).get("stopped") or (meta or {}).get("started")
            if reference is None:
                # No session.json (finalize() never ran, e.g. the process
                # crashed): age it by its newest file mtime so it is not leaked.
                try:
                    reference = max(
                        [d.stat().st_mtime, *(f.stat().st_mtime for f in d.iterdir())]
                    )
                except OSError:
                    continue
            if reference is not None and reference < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                self._replay_cache.pop(d.name, None)
                removed += 1
        return removed

    def new_session(
        self,
        *,
        channel: int,
        receiver_id: str,
        helper_commit: str | None,
        max_bytes: int,
        max_seconds: float,
    ) -> WifiRecordingSession:
        return WifiRecordingSession(
            self.root / _new_session_id(),
            channel=channel,
            receiver_id=receiver_id,
            helper_commit=helper_commit,
            max_bytes=max_bytes,
            max_seconds=max_seconds,
        )

    def list_sessions(self) -> list[dict]:
        rows = [m for d in self._session_dirs() if (m := self._read_meta(d)) is not None]
        rows.sort(key=lambda m: m.get("started") or 0, reverse=True)
        return rows

    def get(self, session_id: str) -> dict | None:
        try:
            d = self._session_dir_for(session_id)
        except KeyError:
            return None
        return self._read_meta(d)

    def session_records(self, session_id: str):
        """Yield every raw pcap record from a session's `part-*.pcap` files,
        in order -- the same source `replay_table()`/`export()` read, for
        callers (msdr/remoteid/transport.py's offline decode) that want the
        records themselves rather than a replayed `NetworkTable`. Raises
        `KeyError` if the session does not exist."""
        d = self._session_dir_for(session_id)
        for part in sorted(d.glob("part-*.pcap")):
            yield from pcap.read_file(part)

    def export(self, session_id: str) -> Path:
        """Concatenate every part file into one single-header pcap under the
        session's own directory, and return its path."""
        d = self._session_dir_for(session_id)
        parts = sorted(d.glob("part-*.pcap"))
        if not parts:
            raise KeyError(session_id)
        export_path = d / "export.pcap"
        with export_path.open("wb") as out:
            out.write(pcap.write_header())
            for part in parts:
                for record in pcap.read_file(part):
                    out.write(pcap.write_record(record.ts, record.data, record.orig_len))
        return export_path

    def import_pcap(self, path: str | Path, *, configured_channel: int) -> dict:
        """Copy an existing pcap file into a new session marked `imported`
        and replay it into its own `NetworkTable` for offline inspection."""
        session_id = _new_session_id()
        d = self.root / session_id
        d.mkdir(parents=True)
        dest = d / "part-000.pcap"
        shutil.copy(Path(path), dest)
        records = pcap.read_file(dest)
        table = NetworkTable(configured_channel=configured_channel)
        frames = 0
        for record in records:
            if record.linktype != pcap.LINKTYPE_IEEE802_11_RADIOTAP:
                continue
            frames += 1
            rt = radiotap.parse(record.data)
            table.ingest(record, rt)
        self._replay_cache[session_id] = table
        meta = {
            "id": session_id,
            "started": time.time(),
            "stopped": time.time(),
            "channel": configured_channel,
            "receiver_id": "wifi:imported",
            "helper_commit": None,
            "frames": frames,
            "bytes": dest.stat().st_size,
            "drops": 0,
            "health_notes": [],
            "imported": True,
            "part_count": 1,
        }
        (d / SESSION_META_NAME).write_text(json.dumps(meta, indent=2))
        return meta

    def replay_table(self, session_id: str, *, configured_channel: int) -> NetworkTable | None:
        """The session's own `NetworkTable` (built once, then cached) -- used
        by `source=session:<id>` network queries."""
        if session_id in self._replay_cache:
            return self._replay_cache[session_id]
        try:
            d = self._session_dir_for(session_id)
        except KeyError:
            return None
        table = NetworkTable(configured_channel=configured_channel)
        for part in sorted(d.glob("part-*.pcap")):
            for record in pcap.read_file(part):
                if record.linktype != pcap.LINKTYPE_IEEE802_11_RADIOTAP:
                    continue
                rt = radiotap.parse(record.data)
                table.ingest(record, rt)
        self._replay_cache[session_id] = table
        return table
