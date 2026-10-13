from msdr.decoder.registry import DECODERS, available, describe, effective_binaries


def test_aprs_label_renamed_id_unchanged():
    spec = DECODERS["aprs"]
    assert spec.id == "aprs"
    assert spec.label == "AX.25 packet 1200 (APRS)"
    assert spec.alt_backend == ("direwolf", "-B 1200")


def test_packet9600_spec():
    spec = DECODERS["packet9600"]
    assert spec.label == "AX.25 packet 9600 (G3RUH)"
    assert spec.kind == "text"
    assert spec.demodulators == ("NFM",)
    assert spec.default_bandwidth_hz == 20000
    assert spec.input_rate == 22050
    assert spec.backend == "multimon"
    assert spec.iq_only is True
    assert spec.alt_backend == ("direwolf", "-B 9600")


def test_packet300_spec():
    spec = DECODERS["packet300"]
    assert spec.label == "AX.25 packet 300 (HF)"
    assert spec.demodulators == ("USB", "LSB")
    assert spec.default_bandwidth_hz == 2800
    assert spec.backend == "direwolf"
    assert spec.binaries == ("direwolf",)
    assert spec.install_hint == "brew install direwolf"
    assert spec.alt_backend is None


def test_other_specs_default_iq_only_false():
    assert DECODERS["pocsag"].iq_only is False
    assert DECODERS["cw"].iq_only is False


def test_effective_binaries_prefers_alt_when_present():
    binaries = {"multimon-ng": "/x/multimon-ng", "direwolf": "/x/direwolf"}
    assert effective_binaries(DECODERS["aprs"], binaries) == ("direwolf",)


def test_effective_binaries_falls_back_without_alt():
    binaries = {"multimon-ng": "/x/multimon-ng", "direwolf": None}
    assert effective_binaries(DECODERS["aprs"], binaries) == ("multimon-ng",)


def test_effective_binaries_no_alt_backend_spec():
    binaries = {"multimon-ng": "/x/multimon-ng"}
    assert effective_binaries(DECODERS["pocsag"], binaries) == ("multimon-ng",)


def test_resolve_binaries_includes_direwolf(monkeypatch):
    # packet300 requires direwolf, so it must appear in the resolved name set.
    from msdr.decoder import registry

    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/usr/bin/{name}" if name else None)
    resolved = registry.resolve_binaries([])
    assert "direwolf" in resolved


def test_available_packet300_requires_direwolf(monkeypatch):
    from msdr.decoder import registry

    def which(name):
        return None if name == "direwolf" else f"/x/{name}"

    monkeypatch.setattr(registry.shutil, "which", which)
    result = available([])
    assert result["packet300"] is False


def test_available_packet300_true_with_direwolf(monkeypatch):
    from msdr.decoder import registry

    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/x/{name}")
    result = available([])
    assert result["packet300"] is True


def test_describe_reports_direwolf_binary_when_preferred(monkeypatch):
    from msdr.decoder import registry

    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/x/{name}")
    entries = {e["id"]: e for e in describe([])}
    assert entries["aprs"]["available"] is True
    assert entries["aprs"]["binary"] == "/x/direwolf"


def test_describe_adds_readiness_key_without_dropping_existing_keys(monkeypatch):
    from msdr.decoder import registry

    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    entries = {e["id"]: e for e in describe([])}
    pocsag = entries["pocsag"]
    # Existing keys unchanged (backward compatibility).
    assert set(pocsag) >= {
        "id",
        "label",
        "kind",
        "demodulators",
        "default_bandwidth_hz",
        "phase",
        "available",
        "binary",
        "install_hint",
        "readiness",
    }
    readiness = pocsag["readiness"]
    assert readiness["decoder_id"] == "pocsag"
    assert readiness["installation"]["state"] == "missing"
    assert "usable" in readiness
    assert "validation_stale" in readiness

    wwv = entries["wwv"]["readiness"]
    assert wwv["installation"]["state"] == "builtin"


def test_describe_session_kwarg_reflects_manager_status(monkeypatch):
    from msdr.decoder import registry

    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    session = {
        "decoders": [{"id": "pocsag", "state": "running", "error": None}],
        "events_total": 5,
    }
    entries = {e["id"]: e for e in describe([], session=session)}
    assert entries["pocsag"]["readiness"]["session"]["state"] == "unverified_output"
    # A decoder with no matching channel in `session` stays idle.
    assert entries["flex"]["readiness"]["session"]["state"] == "idle"


def test_registry_includes_ais_and_expanded_families():
    assert {
        "adsb",
        "rs41",
        "ft8",
        "dsc",
        "endoftrain",
        "vor",
        "ils",
    } <= DECODERS.keys()
    spec = DECODERS["ais"]
    assert spec.label == "AIS (GMSK 9600)"
    assert spec.kind == "text"
    assert spec.demodulators == ("NFM",)
    assert spec.default_bandwidth_hz == 25_000
    assert spec.input_rate == 48_000
    assert spec.backend == "python"
    assert spec.binaries == ()
    assert spec.args == ("msdr.decoder.pydecoders.ais:AisDecoder",)
    assert spec.iq_only is True


def test_standalone_ft8_uses_jt9_without_platform_restriction():
    from msdr.decoder.registry import compatibility_issue

    assert DECODERS["ft8"].backend == "standalone_ft8"
    assert DECODERS["ft8"].binaries == ("jt9",)
    assert compatibility_issue(DECODERS["ft8"]) == ""


def test_jt9_bundle_fallback_preserves_explicit_and_path_priority(tmp_path, monkeypatch):
    from msdr.decoder import registry

    bundle = tmp_path / "jt9-bundled"
    bundle.write_text("#!/bin/sh\nexit 0\n")
    bundle.chmod(0o755)
    monkeypatch.setattr(registry, "_jt9_bundle_candidates", lambda: [bundle])
    monkeypatch.setattr(registry.shutil, "which", lambda _: None)
    assert registry.resolve_binaries([])["jt9"] == str(bundle)
    monkeypatch.setattr(registry.shutil, "which", lambda n: "/on/path/jt9" if n == "jt9" else None)
    assert registry.resolve_binaries([])["jt9"] == "/on/path/jt9"
    explicit = tmp_path / "jt9"
    explicit.write_text("#!/bin/sh\nexit 0\n")
    explicit.chmod(0o755)
    assert registry.resolve_binaries([str(tmp_path)])["jt9"] == str(explicit)


def test_acars_spec():
    spec = DECODERS["acars"]
    assert spec.demodulators == ("AM",)
    assert spec.backend == "python"
    assert spec.input_rate == 22050
    assert spec.args == ("msdr.decoder.pydecoders.acars:AcarsDecoder",)


def test_jjy_wwvb_chu_psk31_ale_are_builtin_python_decoders():
    import importlib

    for decoder_id in ("jjy", "wwvb", "chu", "psk31", "ale"):
        spec = DECODERS[decoder_id]
        assert spec.backend == "python" and spec.binaries == ()
        module, cls = spec.args[0].split(":")
        decoder = getattr(importlib.import_module(module), cls)()
        assert decoder.input_rate == spec.input_rate
