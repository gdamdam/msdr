from msdr.decoder.readiness import (
    _version_cache,
    cached_version,
    installation_for,
    readiness_for,
    session_for,
)
from msdr.decoder.registry import DECODERS


def test_python_backend_is_builtin():
    installation = installation_for(DECODERS["wwv"], {})
    assert installation.state == "builtin"
    assert installation.backend == "python"
    assert installation.binary is None
    assert installation.version  # msdr's own version, e.g. "0.24.0"


def test_missing_binary_reports_missing():
    installation = installation_for(DECODERS["pocsag"], {"multimon-ng": None})
    assert installation.state == "missing"
    assert installation.install_hint


def test_present_binary_without_version_probe_still_present(monkeypatch):
    # Force the version probe to fail (unreliable binary/path) -- Installation
    # must still say "present" with version=None, never raise.
    monkeypatch.setattr("msdr.decoder.readiness._probe_version", lambda backend, binary: None)
    _version_cache.clear()
    installation = installation_for(DECODERS["pocsag"], {"multimon-ng": "/x/multimon-ng"})
    assert installation.state == "present"
    assert installation.binary == "/x/multimon-ng"
    assert installation.version is None


def test_aprs_readiness_reports_preferred_direwolf(monkeypatch):
    monkeypatch.setattr("msdr.decoder.readiness.cached_version", lambda backend, binary: "1.8.1")
    installation = installation_for(
        DECODERS["aprs"],
        {"direwolf": "/x/direwolf", "multimon-ng": "/x/multimon-ng"},
    )
    assert installation.backend == "direwolf"
    assert installation.binary == "/x/direwolf"
    assert installation.version == "1.8.1"


def test_version_probe_is_cached_per_binary_path(monkeypatch):
    calls = []

    def fake_probe(backend, binary):
        calls.append(binary)
        return "9.9"

    monkeypatch.setattr("msdr.decoder.readiness._probe_version", fake_probe)
    _version_cache.clear()
    assert cached_version("multimon", "/x/multimon-ng") == "9.9"
    assert cached_version("multimon", "/x/multimon-ng") == "9.9"
    assert calls == ["/x/multimon-ng"]  # second call was served from cache


def test_dsdfme_version_probe_is_always_none():
    # dsd-fme's "-h" banner has no reliable version string on a macOS install
    # ("Build Version: AW -128-NOTFOUND") -- documented as unreliable, so
    # the probe never attempts to parse it.
    from msdr.decoder.readiness import _probe_version

    assert _probe_version("dsdfme", "/x/dsd-fme") is None


def test_session_for_no_manager_status_is_idle():
    assert session_for("pocsag", None).state == "idle"


def test_session_for_unknown_decoder_is_idle():
    status = {"decoders": [{"id": "flex", "state": "running", "error": None}], "events_total": 0}
    assert session_for("pocsag", status).state == "idle"


def test_session_for_error_channel():
    status = {
        "decoders": [{"id": "pocsag", "state": "error", "error": "boom"}],
        "events_total": 0,
    }
    session = session_for("pocsag", status)
    assert session.state == "error"
    assert session.detail == "boom"


def test_session_for_running_with_no_events_is_acquiring():
    status = {"decoders": [{"id": "pocsag", "state": "running", "error": None}], "events_total": 0}
    assert session_for("pocsag", status).state == "acquiring"


def test_session_for_running_with_events_is_unverified_output():
    status = {"decoders": [{"id": "pocsag", "state": "running", "error": None}], "events_total": 3}
    session = session_for("pocsag", status)
    assert session.state == "unverified_output"
    assert session.events == 3
    assert session.valid_frames == 0  # no integrity signal yet


def test_readiness_for_combines_all_three_facts():
    status = {"decoders": [{"id": "pocsag", "state": "running", "error": None}], "events_total": 1}
    readiness = readiness_for(DECODERS["pocsag"], {"multimon-ng": None}, status)
    assert readiness.decoder_id == "pocsag"
    assert readiness.installation.state == "missing"
    assert readiness.validation.state == "live_output_observed"
    assert readiness.session.state == "unverified_output"
    api = readiness.as_api()
    assert api["decoder_id"] == "pocsag"
    assert api["usable"] is False  # missing installation
    assert "validation_stale" in api


def test_session_for_uses_per_decoder_facts_not_manager_total():
    status = {
        "decoders": [
            {"id": "pocsag", "state": "running", "error": None, "events": 3, "valid_frames": 0},
            {"id": "flex", "state": "running", "error": None, "events": 0, "valid_frames": 0},
        ],
        "events_total": 3,
    }
    assert session_for("pocsag", status).state == "unverified_output"
    # flex produced nothing itself: the manager-wide total must not leak in.
    assert session_for("flex", status).state == "acquiring"


def test_session_for_valid_frames_synchronized_quiet_unsupported():
    def one(**facts):
        return {"decoders": [{"id": "x", "state": "running", "error": None, **facts}]}

    valid = session_for("x", one(events=2, valid_frames=2))
    assert (valid.state, valid.valid_frames, valid.events) == ("valid_frames", 2, 2)
    assert session_for("x", one(events=0, synchronized=True)).state == "synchronized"
    assert session_for("x", one(events=0, synchronized=None)).state == "acquiring"
    quiet = {**one(events=0), "audio": {"silent": True}}
    assert session_for("x", quiet).state == "quiet"
    unsupported = session_for("x", one(events=0, unsupported="needs NFM"))
    assert unsupported.state == "unsupported_input" and unsupported.detail == "needs NFM"


def test_session_for_accepts_single_facts_dict():
    from msdr.decoder.readiness import decoder_facts

    facts = decoder_facts("aprs", state="running", events=1, valid_frames=1)
    assert facts["session_state"] == "valid_frames"
    assert session_for("aprs", facts).state == "valid_frames"


def test_session_for_band_sums_per_decoder_status():
    status = {
        "decoders": [],
        "band": {
            "channels": [
                {
                    "decoders": ["aprs", "pocsag"],
                    "events": 5,
                    "state": "running",
                    "decoder_status": [
                        {"id": "aprs", "state": "running", "events": 2, "valid_frames": 2},
                        {"id": "pocsag", "state": "running", "events": 3, "valid_frames": 0},
                    ],
                },
                {
                    "decoders": ["aprs"],
                    "events": 1,
                    "state": "running",
                    "decoder_status": [
                        {"id": "aprs", "state": "running", "events": 1, "valid_frames": 1}
                    ],
                },
            ]
        },
    }
    aprs = session_for("aprs", status)
    assert (aprs.state, aprs.events, aprs.valid_frames) == ("valid_frames", 3, 3)
    assert session_for("pocsag", status).state == "unverified_output"


def test_native_in_process_backends_are_builtin_without_executables():
    for decoder in ("rs41", "dsc", "endoftrain", "vor", "ils", "lora"):
        installation = installation_for(DECODERS[decoder], {})
        assert installation.state == "builtin"
        assert installation.backend == DECODERS[decoder].backend
        assert installation.version


def test_ft8_native_bundle_version_from_official_metadata(tmp_path):
    import plistlib

    from msdr.decoder.readiness import _probe_version

    contents = tmp_path / "wsjtx.app" / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "org.k1jt.wsjtx", "CFBundleVersion": "3.0.2"})
    )
    assert _probe_version("standalone_ft8", str(contents / "MacOS" / "jt9")) == "3.0.2"
