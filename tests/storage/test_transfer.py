import pytest
from pydantic import ValidationError

from msdr.engine.history import HistoryStack
from msdr.engine.state import ReceiverState
from msdr.history.labels import LabelsRepo
from msdr.library.models import ReceiverProfile, Signal, Tag
from msdr.storage.db import Database
from msdr.storage.repos import (
    HistoryRepo,
    JournalEntry,
    JournalRepo,
    ListRepo,
    ProfileRepo,
    SavedSearchRepo,
    SignalRepo,
    StateRepo,
    TagRepo,
)
from msdr.storage.transfer import export_all, import_all

WIRE_PROPS = {
    "device_vfo_frequency": 145_800_000,
    "device_center_frequency": 145_800_000,
    "device_sample_rate": 2_000_000,
    "demodulator": "NFM",
    "filter_bandwidth": 12_500,
    "lna_state": 3,
    "active_antenna": "Antenna B",
    "squelch_enable": False,
    "squelch_threshold": -100.0,
    "agc_enable": True,
    "agc_threshold": -30.0,
    "noise_reduction_enable": False,
    "noise_reduction_strength": 0,
}


@pytest.fixture
def make_db():
    created: list[Database] = []

    def factory() -> Database:
        database = Database(":memory:")
        database.connect()
        created.append(database)
        return database

    yield factory
    for database in created:
        database.close()


def _seed(db: Database) -> None:
    tag_repo = TagRepo(db)
    tag = Tag(name="Aviation", kind="category")
    tag_repo.add(tag)

    profile_repo = ProfileRepo(db)
    profile = ReceiverProfile(name="VHF Airband", antenna="Discone")
    profile_repo.add(profile)

    signal_repo = SignalRepo(db)
    signal_repo.add(
        Signal(
            name="Metro Tower",
            frequency_hz=120_500_000,
            profile_id=profile.id,
            tag_ids=[tag.id],
            latitude=37.7749,
            longitude=-122.4194,
        )
    )

    state = ReceiverState.from_props(WIRE_PROPS, source="capture")
    StateRepo(db).add(state)

    stack = HistoryStack()
    stack.push(state)
    HistoryRepo(db).save(stack)

    JournalRepo(db).add(
        JournalEntry(kind="capture", state_id=state.id, at="2026-01-01T00:00:00Z")
    )


def _without_exported_at(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "exported_at"}


def test_export_import_export_round_trip(make_db):
    source = make_db()
    _seed(source)
    first_export = export_all(source)

    target = make_db()
    report = import_all(target, first_export)
    assert report.added == {
        "tags": 1,
        "profiles": 1,
        "receiver_states": 1,
        "signals": 1,
        "journal_entries": 1,
    }
    assert report.updated == {}

    second_export = export_all(target)
    assert _without_exported_at(second_export) == _without_exported_at(first_export)


def test_second_import_is_idempotent(make_db):
    source = make_db()
    _seed(source)
    doc = export_all(source)

    target = make_db()
    import_all(target, doc)
    before = export_all(target)

    report = import_all(target, doc)
    assert report.added == {}
    assert report.updated == {}
    assert report.skipped == {
        "tags": 1,
        "profiles": 1,
        "receiver_states": 1,
        "signals": 1,
        "journal_entries": 1,
        "lists": 1,  # the built-in Favorites list, present on both sides
    }

    after = export_all(target)
    assert _without_exported_at(after) == _without_exported_at(before)


def test_import_bad_format_raises(make_db):
    target = make_db()
    with pytest.raises(ValueError):
        import_all(target, {"format": "other/1"})


def test_import_remaps_tag_ids_matched_by_name(make_db):
    # Reproduces F1: the target's own install of the category tags gets fresh
    # uuid7 ids, so a tag that merges by name (not id) must have the signal's
    # tag_ids rewritten to the local id, or the signal_tags FK insert fails.
    source = make_db()
    _seed(source)
    doc = export_all(source)

    target = make_db()
    local_tag = Tag(name="Aviation", kind="category")
    TagRepo(target).add(local_tag)

    import_all(target, doc)

    [imported_signal] = SignalRepo(target).list()
    assert imported_signal.tag_ids == [local_tag.id]


def test_export_import_preserves_signal_and_tag_colors(make_db):
    source = make_db()
    tag = Tag(name="Aviation", kind="category", color="violet")
    TagRepo(source).add(tag)
    signal = Signal(
        name="Metro Tower", frequency_hz=120_500_000, tag_ids=[tag.id], color="orange"
    )
    SignalRepo(source).add(signal)
    doc = export_all(source)

    target = make_db()
    import_all(target, doc)

    [imported_tag] = TagRepo(target).list()
    [imported_signal] = SignalRepo(target).list()
    assert imported_tag.color == "violet"
    assert imported_signal.color == "orange"


def test_export_import_preserves_signal_satellite(make_db):
    source = make_db()
    signal = Signal(name="Meteor pass", frequency_hz=137_900_000, satellite="M2-4")
    SignalRepo(source).add(signal)
    doc = export_all(source)

    target = make_db()
    import_all(target, doc)

    [imported_signal] = SignalRepo(target).list()
    assert imported_signal.satellite == "M2-4"


def test_export_import_preserves_signal_decoders(make_db):
    source = make_db()
    signal = Signal(name="Pager", frequency_hz=152_840_000, decoders=["pocsag", "flex"])
    SignalRepo(source).add(signal)
    doc = export_all(source)

    target = make_db()
    import_all(target, doc)

    [imported_signal] = SignalRepo(target).list()
    assert imported_signal.decoders == ["pocsag", "flex"]


def test_export_import_preserves_signal_antenna(make_db):
    source = make_db()
    signal = Signal(name="Discone hit", frequency_hz=146_520_000, antenna="Antenna B")
    SignalRepo(source).add(signal)
    doc = export_all(source)

    target = make_db()
    import_all(target, doc)

    [imported_signal] = SignalRepo(target).list()
    assert imported_signal.antenna == "Antenna B"


def test_legacy_export_without_antenna_imports_as_none(make_db):
    # A pre-antenna export document has no "antenna" key at all -- the field
    # must default to None rather than raising (Signal.antenna: str | None).
    source = make_db()
    SignalRepo(source).add(Signal(name="Legacy", frequency_hz=100_000_000))
    doc = export_all(source)
    for raw in doc["signals"]:
        raw.pop("antenna", None)

    target = make_db()
    import_all(target, doc)

    [imported_signal] = SignalRepo(target).list()
    assert imported_signal.antenna is None


def test_legacy_export_without_decoders_imports_as_empty_list(make_db):
    # A pre-decoders export document has no "decoders" key at all -- the
    # field must default to [] rather than raising (Signal.decoders: list[str] = []).
    source = make_db()
    SignalRepo(source).add(Signal(name="Legacy", frequency_hz=100_000_000))
    doc = export_all(source)
    for raw in doc["signals"]:
        raw.pop("decoders", None)

    target = make_db()
    import_all(target, doc)

    [imported_signal] = SignalRepo(target).list()
    assert imported_signal.decoders == []


def test_legacy_export_without_satellite_imports_as_none(make_db):
    # A pre-satellite export document has no "satellite" key at all -- the
    # field must default to None rather than raising.
    doc = {
        "format": "msdr/1",
        "exported_at": "2026-01-01T00:00:00Z",
        "profiles": [],
        "tags": [],
        "signals": [
            {
                "id": "sig-1",
                "name": "Meteor pass",
                "status": "unknown",
                "frequency_hz": 137_900_000,
            }
        ],
        "receiver_states": [],
        "journal_entries": [],
    }
    target = make_db()
    import_all(target, doc)

    [imported_signal] = SignalRepo(target).list()
    assert imported_signal.satellite is None


def test_legacy_export_without_color_imports_as_none(make_db):
    # A pre-colors export document has no "color" key at all on its tags/
    # signals -- the field must default to None rather than raising.
    doc = {
        "format": "msdr/1",
        "exported_at": "2026-01-01T00:00:00Z",
        "profiles": [],
        "tags": [{"id": "tag-1", "name": "Aviation", "kind": "category"}],
        "signals": [
            {
                "id": "sig-1",
                "name": "Metro Tower",
                "status": "unknown",
                "frequency_hz": 120_500_000,
                "tag_ids": ["tag-1"],
            }
        ],
        "receiver_states": [],
        "journal_entries": [],
    }
    target = make_db()
    import_all(target, doc)

    [imported_tag] = TagRepo(target).list()
    [imported_signal] = SignalRepo(target).list()
    assert imported_tag.color is None
    assert imported_signal.color is None


def test_import_failure_midway_leaves_db_unchanged(make_db):
    source = make_db()
    _seed(source)
    doc = export_all(source)
    # Corrupt a later item (journal entry) so the import fails after tags,
    # profiles, states and signals have already been written for this call.
    doc["journal_entries"][0]["kind"] = "not-a-real-kind"

    target = make_db()
    with pytest.raises(ValidationError):
        import_all(target, doc)

    after = export_all(target)
    assert after["tags"] == []
    assert after["profiles"] == []
    assert after["signals"] == []
    assert after["receiver_states"] == []
    assert after["journal_entries"] == []


def test_export_import_round_trips_lists_items_and_saved_searches(make_db):
    # A backup must not silently drop lists (incl. Favorites membership and
    # order) or saved searches; a same-named local list is merged into.
    src = make_db()
    a = Signal(name="A", frequency_hz=6_070_000)
    b = Signal(name="B", frequency_hz=9_420_000)
    SignalRepo(src).add(a)
    SignalRepo(src).add(b)
    lists = ListRepo(src)
    evening = lists.create("Evening SW")
    lists.add_items(evening["id"], [b.id, a.id])
    lists.add_items("favorites", [a.id])
    SavedSearchRepo(src).create("evening", {"list": evening["id"], "sort": "frequency"})
    doc = export_all(src)

    dst = make_db()
    local = ListRepo(dst).create("evening sw")  # same name, different id and case
    report = import_all(dst, doc)

    assert "lists" not in report.added  # both lists matched (by id / by name)
    dst_lists = ListRepo(dst)
    assert dst_lists.item_ids(local["id"]) == [b.id, a.id]
    assert dst_lists.item_ids("favorites") == [a.id]
    (search,) = SavedSearchRepo(dst).list()
    assert search["params"]["list"] == local["id"]

    again = import_all(dst, doc)
    assert "list_items" not in again.added and "saved_searches" not in again.added


def test_export_import_round_trip_preserves_list_item_skip(make_db):
    # skip is a per-membership flag (scanner sweep exclusion); a backup
    # round trip must not silently turn skip=True back into False.
    src = make_db()
    a = Signal(name="A", frequency_hz=6_070_000)
    b = Signal(name="B", frequency_hz=9_420_000)
    SignalRepo(src).add(a)
    SignalRepo(src).add(b)
    lists = ListRepo(src)
    evening = lists.create("Evening SW")
    lists.add_items(evening["id"], [a.id, b.id])
    lists.set_item_skip(evening["id"], a.id, True)
    doc = export_all(src)

    dst = make_db()
    import_all(dst, doc)
    rows = {r["signal_id"]: r["skip"] for r in ListRepo(dst).item_rows(evening["id"])}
    assert rows[a.id] is True
    assert rows[b.id] is False


def test_import_list_item_without_skip_key_defaults_false(make_db):
    # Older backups predate the `skip` column and won't have the key at all.
    src = make_db()
    a = Signal(name="A", frequency_hz=6_070_000)
    SignalRepo(src).add(a)
    lists = ListRepo(src)
    evening = lists.create("Evening SW")
    lists.add_items(evening["id"], [a.id])
    doc = export_all(src)
    del doc["list_items"][0]["skip"]

    dst = make_db()
    import_all(dst, doc)
    rows = ListRepo(dst).item_rows(evening["id"])
    assert rows == [{"signal_id": a.id, "skip": False}]


def test_legacy_export_without_lists_imports_unchanged(make_db):
    src = make_db()
    SignalRepo(src).add(Signal(name="A", frequency_hz=6_070_000))
    doc = export_all(src)
    for key in ("lists", "list_items", "saved_searches"):
        doc.pop(key)
    dst = make_db()
    import_all(dst, doc)
    assert [lst["id"] for lst in ListRepo(dst).all()] == ["favorites"]


def test_device_labels_export_import_merge(make_db):
    source = make_db()
    labels = LabelsRepo(source)
    labels.put("wifi", "aa:bb:cc:dd:ee:01", name="Home AP", note="", cls="mine")
    labels.put("ble", "c0:11:22:33:44:55", name="Ruuvi", note="balcony", cls="known")
    doc = export_all(source)
    idents = {lab["ident"] for lab in doc["device_labels"]}
    assert idents == {"aa:bb:cc:dd:ee:01", "c0:11:22:33:44:55"}

    target = make_db()
    LabelsRepo(target).put("ble", "c0:11:22:33:44:55", name="Old name", note="", cls="known")
    target.conn.execute("UPDATE device_labels SET updated_at = 0")
    doc["device_labels"].append(
        {"receiver": "wifi", "ident": "not-a-mac", "name": "x", "note": "", "class": "mine",
         "created_at": 1.0, "updated_at": 1.0}
    )
    report = import_all(target, doc)
    assert report.added["device_labels"] == 1
    assert report.updated["device_labels"] == 1
    assert report.skipped["device_labels"] == 1
    assert LabelsRepo(target).get("ble", "c0:11:22:33:44:55")["name"] == "Ruuvi"

    again = import_all(target, export_all(source))
    assert again.skipped["device_labels"] == 2 and "device_labels" not in again.added


def test_legacy_export_without_labels_imports(make_db):
    doc = export_all(make_db())
    del doc["device_labels"]
    report = import_all(make_db(), doc)
    assert "device_labels" not in report.added
