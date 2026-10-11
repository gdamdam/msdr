import errno
import os
import stat
from pathlib import Path

import pytest

from msdr.api.schemas import SettingsPatch
from msdr.settings import FreeDATAIntegrationSettings, Settings


def test_integrations_default_off_and_nested_patch_stays_partial():
    settings = Settings()
    assert settings.integrations.js8call.enabled is False
    assert settings.integrations.pat.enabled is False
    assert settings.integrations.freedata.enabled is False

    patch = SettingsPatch.model_validate({
        "integrations": {"js8call": {"enabled": True}},
    })
    assert patch.model_dump(exclude_unset=True) == {
        "integrations": {"js8call": {"enabled": True}},
    }

    freedata_patch = SettingsPatch.model_validate({
        "integrations": {"freedata": {"enabled": True}},
    })
    assert freedata_patch.model_dump(exclude_unset=True) == {
        "integrations": {"freedata": {"enabled": True}},
    }


def test_freedata_endpoint_requires_base_http_url():
    assert FreeDATAIntegrationSettings(base_url="http://localhost:5000/").base_url == "http://localhost:5000"
    with pytest.raises(ValueError):
        FreeDATAIntegrationSettings(base_url="http://localhost:5000/freedata/messages")


def test_settings_save_keeps_previous_valid_copy_and_load_recovers(tmp_path):
    path = tmp_path / "settings.json"
    Settings(region="US").save(path)
    Settings(region="EU").save(path)

    assert Settings.model_validate_json(path.read_text()).region == "EU"
    backup = path.with_name("settings.json.bak")
    assert Settings.model_validate_json(backup.read_text()).region == "US"

    path.write_text('{"interrupted":')
    assert Settings.load(path).region == "US"
    Settings(region="CA").save(path)
    path.write_text("broken again")
    assert Settings.load(path).region == "US"


def test_settings_load_defaults_if_only_copy_is_corrupt(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not json")
    assert Settings.load(path) == Settings()


def test_failed_primary_replace_keeps_current_and_recovery_copy(tmp_path, monkeypatch):
    import msdr.settings as settings_module

    path = tmp_path / "settings.json"
    Settings(region="US").save(path)
    Settings(region="EU").save(path)
    real_replace = settings_module.os.replace

    def fail_primary_replace(source, destination):
        if Path(destination) == path:
            raise OSError("injected replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(settings_module.os, "replace", fail_primary_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        Settings(region="CA").save(path)

    assert Settings.load(path).region == "EU"
    backup = path.with_name("settings.json.bak")
    assert Settings.model_validate_json(backup.read_text()).region == "EU"


def test_settings_save_tolerates_unsupported_directory_fsync(tmp_path, monkeypatch):
    import msdr.settings as settings_module

    real_fsync = settings_module.os.fsync

    def fsync_file_but_reject_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        return real_fsync(fd)

    monkeypatch.setattr(settings_module.os, "fsync", fsync_file_but_reject_directory)
    path = tmp_path / "settings.json"
    Settings(region="EU").save(path)
    assert Settings.load(path).region == "EU"


def test_settings_with_removed_rfml_section_still_load(tmp_path):
    """The experimental RF ML feature was removed; settings.json files written
    while it existed (survey.ml, features.ml_controls, validation_samples_dir)
    must keep loading with every other setting intact."""
    import json

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "region": "EU",
        "validation_samples_dir": "/tmp/rfml-validation",
        "features": {"survey": False, "ml_controls": False},
        "survey": {
            "threshold_db": 9.5,
            "ml": {
                "enabled": True,
                "model_path": "~/.cache/msdr/models/xcit_narrowband.ckpt",
                "detector": {"enabled": True, "window_samples": 4096},
            },
        },
    }))
    loaded = Settings.load(path)
    assert loaded.region == "EU"
    assert loaded.survey.threshold_db == 9.5
    assert loaded.features.survey is False
    assert "ml" not in loaded.survey.model_dump()
    assert "ml_controls" not in loaded.features.model_dump()
