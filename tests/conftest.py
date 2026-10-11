import pytest


@pytest.fixture
def ml_available(monkeypatch):
    """Switch dormant RF ML back on for tests that exercise its runtime
    (msdr/rfml/__init__.py AVAILABLE)."""
    from msdr import rfml

    monkeypatch.setattr(rfml, "AVAILABLE", True)


@pytest.fixture(autouse=True)
def _isolate_wifi_helper_search(monkeypatch, tmp_path):
    """msdr.wifi.supervisor.find_helper also looks on PATH, beside the msdr
    checkout and at the old default. Point all three at nothing so a real
    helper on the developer's machine never leaks into a test."""
    from msdr.wifi import supervisor

    missing = tmp_path / "no-helper-here" / "msdr-wifi-capture"
    monkeypatch.setattr(supervisor, "SIBLING_HELPER_PATH", missing)
    monkeypatch.setattr(supervisor, "DEFAULT_HELPER_PATH", str(missing))
    monkeypatch.setattr(supervisor, "HELPER_NAME", "msdr-wifi-capture-not-on-path-in-tests")
