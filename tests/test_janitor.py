"""Уборка рабочей папки: старое уходит, свежее, баннеры и настройки остаются."""
from __future__ import annotations

import os
import time

from app.config import Settings
from app.services.janitor import max_age_days_from_env, prune_workspace

DAY = 86400


def _age(path, days: float, now: float) -> None:
    stamp = now - days * DAY
    os.utime(path, (stamp, stamp))


def test_old_entries_are_removed_and_fresh_ones_stay(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    now = time.time()
    old_file = settings.cache_dir / "old.mp4"
    old_file.write_bytes(b"x")
    fresh_file = settings.cache_dir / "fresh.mp4"
    fresh_file.write_bytes(b"x")
    old_job = settings.outputs_dir / "job-old"
    old_job.mkdir()
    (old_job / "clip.mp4").write_bytes(b"x")
    for item in (old_file, old_job / "clip.mp4", old_job):
        _age(item, 10, now)

    removed = prune_workspace(settings, 3, now=now)

    assert removed == 2
    assert not old_file.exists() and not old_job.exists()
    assert fresh_file.exists()


def test_a_job_folder_with_a_fresh_file_inside_is_kept(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    now = time.time()
    job = settings.outputs_dir / "job-running"
    job.mkdir()
    old = job / "source.mp4"
    old.write_bytes(b"x")
    (job / "clip_1.mp4").write_bytes(b"x")  # свежий — задача ещё идёт
    _age(old, 10, now)
    _age(job, 10, now)

    assert prune_workspace(settings, 3, now=now) == 0
    assert job.exists()


def test_banners_sfx_and_user_settings_are_never_touched(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    now = time.time()
    kept = [settings.banners_dir / "b.png", settings.sfx_dir / "pop.wav", tmp_path / "users.json"]
    for path in kept:
        path.write_bytes(b"x")
        _age(path, 365, now)

    assert prune_workspace(settings, 1, now=now) == 0
    assert all(path.exists() for path in kept)


def test_zero_days_disables_cleanup(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    now = time.time()
    old = settings.cache_dir / "old.mp4"
    old.write_bytes(b"x")
    _age(old, 100, now)

    assert prune_workspace(settings, 0, now=now) == 0
    assert old.exists()


def test_cleanup_days_env(monkeypatch):
    monkeypatch.setenv("CLEANUP_DAYS", "7")
    assert max_age_days_from_env() == 7
    monkeypatch.setenv("CLEANUP_DAYS", "мусор")
    assert max_age_days_from_env() == 3
    monkeypatch.setenv("CLEANUP_DAYS", "0")
    assert max_age_days_from_env() == 0
    monkeypatch.delenv("CLEANUP_DAYS")
    assert max_age_days_from_env() == 3
