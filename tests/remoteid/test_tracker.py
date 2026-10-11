"""msdr/remoteid/tracker.py: per-UAS aggregation, dedup, staleness and
cross-transport correlation/conflict (docs/remote-id.md)."""

from __future__ import annotations

from msdr.remoteid import messages, tracker
from msdr.remoteid.tracker import Tracker

UAS_ID_A = bytes([0x11]) + bytes(19)
UAS_ID_B = bytes([0x22]) + bytes(19)


def _basic_id(uas_id: bytes) -> messages.BasicId:
    return messages.BasicId(
        protocol_version=2, ua_type=2, id_type=1, uas_id_bytes=uas_id, uas_id_text="SERIAL"
    )


def _location(
    *, lat: float | None, lon: float | None, status: int = 2, ts: float | None = 10.0
) -> messages.Location:
    return messages.Location(
        protocol_version=2,
        status=status,
        height_type=0,
        direction_deg=90.0,
        speed_horizontal_ms=5.0,
        speed_vertical_ms=0.0,
        latitude_deg=lat,
        longitude_deg=lon,
        altitude_baro_m=100.0,
        altitude_geo_m=110.0,
        height_m=50.0,
        horiz_accuracy_enum=11,
        vert_accuracy_enum=6,
        baro_accuracy_enum=6,
        speed_accuracy_enum=3,
        timestamp_accuracy_enum=1,
        timestamp_s=ts,
    )


def _system(*, op_lat: float | None, op_lon: float | None, location_type: int) -> messages.System:
    return messages.System(
        protocol_version=2,
        operator_location_type=location_type,
        classification_type=0,
        operator_latitude_deg=op_lat,
        operator_longitude_deg=op_lon,
        area_count=1,
        area_radius_m=0,
        area_ceiling_m=None,
        area_floor_m=None,
        category_eu=0,
        class_eu=0,
        operator_altitude_geo_m=None,
        timestamp_s_since_2019=28_000_000,
    )


def _pack(*msgs: messages.DecodedMessage) -> messages.MessagePack:
    return messages.MessagePack(protocol_version=2, message_count=len(msgs), messages=tuple(msgs))


# --------------------------------------------------------------------------
# Basic aggregation: position types kept separate, session-scoped key before
# a Basic ID is seen, promoted to the UAS ID once one arrives.
# --------------------------------------------------------------------------


def test_session_scoped_key_before_basic_id_seen():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=100.0)
    groups = t.groups(now=100.0)
    assert len(groups) == 1
    assert groups[0].uas_id is None
    assert groups[0].key == "wifi:aa"
    assert groups[0].tracks[0].drone_position.latitude_deg == 45.0


def test_drone_position_decodes_accuracy_enums():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=100.0)
    track = t.groups(now=100.0)[0].tracks[0]
    # _location()'s fixture uses horiz_accuracy_enum=11, vert_accuracy_enum=6.
    assert track.drone_position.horiz_accuracy_m == messages.decode_horizontal_accuracy_m(11)
    assert track.drone_position.vert_accuracy_m == messages.decode_vertical_accuracy_m(6)


def test_drone_operator_takeoff_positions_kept_separate():
    t = Tracker()
    t.ingest_wifi_pack(
        "wifi:aa",
        1,
        _pack(
            _basic_id(UAS_ID_A),
            _location(lat=45.0, lon=-122.0),
            _system(op_lat=46.0, op_lon=-123.0, location_type=1),  # LIVE_GNSS -> operator position
        ),
        received_at=100.0,
    )
    t.ingest_wifi_pack(
        "wifi:aa",
        2,
        _pack(_system(op_lat=47.0, op_lon=-124.0, location_type=0)),  # TAKEOFF -> takeoff position
        received_at=101.0,
    )
    track = t.groups(now=101.0)[0].tracks[0]
    assert track.drone_position.latitude_deg == 45.0
    assert track.operator_position.latitude_deg == 46.0
    assert track.takeoff_position.latitude_deg == 47.0
    # Each has its own freshness timestamp.
    assert track.drone_position.received_at == 100.0
    assert track.operator_position.received_at == 100.0
    assert track.takeoff_position.received_at == 101.0


def test_unknown_lat_lon_leaves_position_untouched():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=100.0)
    t.ingest_wifi_pack("wifi:aa", 2, _pack(_location(lat=None, lon=None)), received_at=200.0)
    track = t.groups(now=200.0)[0].tracks[0]
    # The unknown-lat/lon update still replaces drone_position (it is the
    # latest Location message) -- but its own fields decode to None, never
    # a stale previous number silently kept under a new lat/lon.
    assert track.drone_position.latitude_deg is None
    assert track.drone_position.received_at == 200.0


def test_self_id_and_operator_id_are_metadata_only():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=100.0)
    t.ingest_wifi_pack(
        "wifi:aa",
        2,
        _pack(messages.SelfId(protocol_version=2, desc_type=0, description="Hello")),
        received_at=150.0,
    )
    track = t.groups(now=150.0)[0].tracks[0]
    assert track.self_id == "Hello"
    # Position freshness is untouched by a metadata-only update.
    assert track.drone_position.received_at == 100.0


# --------------------------------------------------------------------------
# Dedup of repeated message-counter bursts.
# --------------------------------------------------------------------------


def test_duplicate_counter_burst_does_not_reprocess():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=100.0)
    # Same counter resent moments later with (hypothetically) different
    # content -- real senders don't do this, but the dedup must key on the
    # counter, not content, exactly like opendroneid's own convention.
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=1.0, lon=2.0)), received_at=100.5)
    track = t.groups(now=100.5)[0].tracks[0]
    assert track.drone_position.latitude_deg == 45.0
    assert track.drone_position.received_at == 100.0
    # Overall liveness still advances even though the message itself is a duplicate.
    assert track.last_seen == 100.5


def test_new_counter_is_applied():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=100.0)
    t.ingest_wifi_pack("wifi:aa", 2, _pack(_location(lat=46.0, lon=-123.0)), received_at=101.0)
    track = t.groups(now=101.0)[0].tracks[0]
    assert track.drone_position.latitude_deg == 46.0


def test_ble_and_wifi_dedup_are_independent_per_address():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=100.0)
    t.ingest_ble_message("ble:bb", 1, _location(lat=50.0, lon=-120.0), received_at=100.0)
    groups = t.groups(now=100.0)
    assert len(groups) == 2  # distinct addresses, no shared UAS ID yet


# --------------------------------------------------------------------------
# Stale-position detection.
# --------------------------------------------------------------------------


def test_stale_position_detection():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0)), received_at=0.0)
    track = t.groups(now=0.0)[0].tracks[0]
    assert not track.is_stale(now=30.0, max_age_s=60.0)
    assert track.is_stale(now=100.0, max_age_s=60.0)


def test_no_position_is_always_stale():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_basic_id(UAS_ID_A)), received_at=0.0)
    track = t.groups(now=0.0)[0].tracks[0]
    assert track.is_stale(now=0.0, max_age_s=60.0)


def test_timestamp_wrap_never_affects_staleness():
    # Location.timestamp_s wrapping from 3599.9 to 0.1 across the hour must
    # never look like the position moved "backward in time" -- staleness is
    # judged purely by received_at (wall/pcap clock), which only advances.
    t = Tracker()
    t.ingest_wifi_pack(
        "wifi:aa", 1, _pack(_location(lat=45.0, lon=-122.0, ts=3599.9)), received_at=1000.0
    )
    t.ingest_wifi_pack(
        "wifi:aa", 2, _pack(_location(lat=45.1, lon=-122.1, ts=0.1)), received_at=1001.0
    )
    track = t.groups(now=1001.0)[0].tracks[0]
    assert not track.is_stale(now=1001.0, max_age_s=60.0)
    assert track.drone_position.reported_timestamp_s == 0.1
    assert track.drone_position.received_at == 1001.0


# --------------------------------------------------------------------------
# Correlation across transports, and conflict when an identifier is shared.
# --------------------------------------------------------------------------


def test_wifi_and_ble_correlate_only_on_identical_uas_id():
    t = Tracker()
    t.ingest_wifi_pack(
        "wifi:aa", 1, _pack(_basic_id(UAS_ID_A), _location(lat=45.0, lon=-122.0)), received_at=100.0
    )
    t.ingest_ble_message("ble:bb", 1, _basic_id(UAS_ID_A), received_at=100.0)
    groups = t.groups(now=100.0)
    assert len(groups) == 1
    group = groups[0]
    assert group.uas_id == UAS_ID_A
    assert not group.conflict
    addresses = {tr.address for tr in group.tracks}
    assert addresses == {"wifi:aa", "ble:bb"}


def test_different_uas_ids_never_grouped():
    t = Tracker()
    t.ingest_wifi_pack("wifi:aa", 1, _pack(_basic_id(UAS_ID_A)), received_at=100.0)
    t.ingest_ble_message("ble:bb", 1, _basic_id(UAS_ID_B), received_at=100.0)
    groups = t.groups(now=100.0)
    assert len(groups) == 2
    assert {g.uas_id for g in groups} == {UAS_ID_A, UAS_ID_B}


def test_two_drones_sharing_an_identifier_reports_conflict():
    t = Tracker()
    # Paris-ish and London-ish, both "fresh" at the same instant: too far
    # apart (> default 5 km) to be the same physical aircraft.
    t.ingest_wifi_pack(
        "wifi:aa",
        1,
        _pack(_basic_id(UAS_ID_A), _location(lat=48.8566, lon=2.3522)),
        received_at=100.0,
    )
    t.ingest_ble_message("ble:bb", 1, _basic_id(UAS_ID_A), received_at=100.0)
    t.ingest_ble_message("ble:bb", 2, _location(lat=51.5074, lon=-0.1278), received_at=100.0)

    groups = t.groups(now=100.0, stale_after_s=60.0)
    assert len(groups) == 1
    group = groups[0]
    assert group.uas_id == UAS_ID_A
    assert group.conflict is True
    # Both reports stay individually inspectable -- never averaged/merged.
    assert len(group.tracks) == 2
    lats = sorted(tr.drone_position.latitude_deg for tr in group.tracks if tr.drone_position)
    assert lats == [48.8566, 51.5074]


def test_shared_identifier_without_position_conflict_is_not_flagged():
    t = Tracker()
    t.ingest_wifi_pack(
        "wifi:aa", 1, _pack(_basic_id(UAS_ID_A), _location(lat=45.0, lon=-122.0)), received_at=100.0
    )
    # Second address reports the same ID but no position of its own (e.g. it
    # only carried Self-ID/Operator-ID so far) -- nothing to disagree with.
    t.ingest_ble_message("ble:bb", 1, _basic_id(UAS_ID_A), received_at=100.0)
    groups = t.groups(now=100.0)
    assert len(groups) == 1
    assert groups[0].conflict is False


def test_recent_messages_bounded_and_ordered():
    t = Tracker()
    for i in range(25):
        t.ingest_wifi_pack(
            "wifi:aa", i, _pack(_location(lat=float(i), lon=0.0)), received_at=float(i)
        )
    track = t.groups(now=24.0)[0].tracks[0]
    assert len(track.recent_messages) == tracker.RECENT_MESSAGES_MAXLEN
    received_ats = [ts for ts, _ in track.recent_messages]
    assert received_ats == sorted(received_ats)
    assert received_ats[-1] == 24.0


def test_shared_identifier_with_stale_second_position_is_not_flagged():
    t = Tracker()
    t.ingest_wifi_pack(
        "wifi:aa",
        1,
        _pack(_basic_id(UAS_ID_A), _location(lat=45.0, lon=-122.0)),
        received_at=1000.0,
    )
    t.ingest_ble_message("ble:bb", 1, _basic_id(UAS_ID_A), received_at=0.0)
    t.ingest_ble_message("ble:bb", 2, _location(lat=51.5074, lon=-0.1278), received_at=0.0)
    # "now" is far enough past the BLE report that it is stale -- a conflict
    # must not be raised against a position no longer believed current.
    groups = t.groups(now=1000.0, stale_after_s=60.0)
    assert len(groups) == 1
    assert groups[0].conflict is False


def test_haversine_clamps_antipodal_rounding():
    assert tracker._haversine_m(100.0, 0.0, 80.0, 180.0) >= 0.0


def test_out_of_range_fix_is_invalid_and_groups_never_raises():
    t = Tracker()
    for addr, lat, lon in (("aa", 100.0, 0.0), ("bb", -80.0, 180.0)):
        pack = _pack(_basic_id(UAS_ID_A), _location(lat=lat, lon=lon))
        t.ingest_wifi_pack(addr, 1, pack, received_at=1.0)
    assert t._tracks["aa"].drone_position.has_position is False
    assert t.groups(now=2.0)[0].conflict is False


def test_distinct_same_type_messages_with_same_counter_are_both_kept():
    t = Tracker()
    other = messages.BasicId(
        protocol_version=2, ua_type=2, id_type=1, uas_id_bytes=UAS_ID_B, uas_id_text="OTHER"
    )
    t.ingest_wifi_pack("aa", 5, _pack(_basic_id(UAS_ID_A), other), received_at=1.0)
    t.ingest_wifi_pack("aa", 5, _pack(_basic_id(UAS_ID_A), other), received_at=2.0)  # repeat
    assert len(t._tracks["aa"].recent_messages) == 2
    assert t._tracks["aa"].uas_id == UAS_ID_B


def test_tracks_are_capped_evicting_least_recently_heard():
    t = Tracker()
    for i in range(tracker.MAX_TRACKS + 3):
        t.ingest_wifi_pack(f"a{i}", 1, _pack(_basic_id(UAS_ID_A)), received_at=float(i))
    assert len(t._tracks) == tracker.MAX_TRACKS
    assert "a0" not in t._tracks and f"a{tracker.MAX_TRACKS + 2}" in t._tracks
