"""Хранилище настроек: переживает перезапуск и старые файлы от прошлых версий бота."""
import json

from app.models import ClipSettings
from app.services.user_store import FILE_NAME, UserStore


def test_settings_and_stats_survive_a_restart(tmp_path):
    store = UserStore(tmp_path)
    store.remember_settings(7, ClipSettings(num_clips=9, clip_duration=45.0))
    store.count_job(7, 3)
    store.save()

    again = UserStore(tmp_path)

    assert again.settings(7).num_clips == 9 and again.settings(7).clip_duration == 45.0
    assert again.stats(7) == (3, 1)
    assert again.settings(8) == ClipSettings()  # у других — значения по умолчанию


def test_fields_removed_in_newer_versions_are_ignored_not_fatal(tmp_path):
    # прежняя версия хранила ещё и постоянный баннер: после её обновления файл на сервере остаётся прежним
    legacy = {"7": {"num_clips": 4, "static_banner_id": "strip.png", "static_position": "top", "static_scale": 92, "clips_total": 5}}
    (tmp_path / FILE_NAME).write_text(json.dumps(legacy), "utf-8")

    store = UserStore(tmp_path)

    assert store.settings(7).num_clips == 4
    assert store.stats(7) == (5, 0)


def test_unreadable_entries_fall_back_to_defaults(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"7": {"num_clips": "много"}, "8": "не словарь"}), "utf-8")

    store = UserStore(tmp_path)

    assert store.settings(7) == ClipSettings() and store.settings(8) == ClipSettings()


def test_a_broken_file_starts_empty_instead_of_crashing(tmp_path):
    (tmp_path / FILE_NAME).write_text("{не json", "utf-8")
    assert UserStore(tmp_path).settings(1) == ClipSettings()
