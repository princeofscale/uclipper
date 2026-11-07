"""Защита общего сервера при открытом доступе: сколько нарезок идёт сразу, место на диске, размер кеша и учёт задач."""
from __future__ import annotations

import os
import time

import pytest

from app.config import Settings
from app.models import JobStatus
from app.services import capacity
from app.services.capacity import (
    DEFAULT_CACHE_LIMIT_GB,
    DEFAULT_DOWNLOAD_THREADS,
    DEFAULT_MIN_FREE_GB,
    MAX_DOWNLOAD_THREADS,
    cache_limit_bytes_from_env,
    cpu_limit,
    default_max_jobs,
    disk_is_low,
    download_threads_from_env,
    ffmpeg_threads,
    free_bytes,
    jobs_limit_reached,
    max_jobs_from_env,
    min_free_bytes_from_env,
)
from app.services.janitor import enforce_cache_limit
from app.services.jobs import JobManager

GB = 1024**3
HOUR = 3600


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("MAX_JOBS", "MIN_FREE_GB", "CACHE_LIMIT_GB", "DOWNLOAD_THREADS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def manager(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    jobs = JobManager(settings)
    yield jobs
    jobs.shutdown()


def free(monkeypatch, gigabytes: float | None) -> None:
    monkeypatch.setattr(capacity, "free_bytes", lambda path: None if gigabytes is None else int(gigabytes * GB))


# ---------- пределы из окружения ----------


def test_limits_have_sane_defaults():
    # Нарезок одновременно — по одной на разрешённое ядро, но не больше трёх: каждая получает свой поток ffmpeg.
    assert max_jobs_from_env() == default_max_jobs()
    assert 1 <= default_max_jobs() <= capacity.PARALLEL_CAP
    assert min_free_bytes_from_env() == int(DEFAULT_MIN_FREE_GB * GB)
    assert cache_limit_bytes_from_env() == int(DEFAULT_CACHE_LIMIT_GB * GB)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3", 3), (" 10 ", 10), ("0", 0), ("-4", 0), ("много", default_max_jobs()), ("", default_max_jobs()), ("2,9", 2)],
)
def test_max_jobs_reads_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("MAX_JOBS", raw)
    assert max_jobs_from_env() == expected


def test_downloads_take_eight_pieces_at_a_time_by_default():
    # замер на сервере (RuTube, 340 МБ): по одному ≈70 с, на восьми потоках ≈40 с; на 16 лучше не становится
    assert download_threads_from_env() == DEFAULT_DOWNLOAD_THREADS == 8


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12),
        (" 6 ", 6),
        ("1", 1),  # по одному, как раньше
        ("0", 1),  # «0 потоков» бессмысленно: это тот же один
        ("-3", 1),
        ("99", MAX_DOWNLOAD_THREADS),  # больше площадка всё равно не отдаст, а на неё сыплются отказы
        ("много", DEFAULT_DOWNLOAD_THREADS),
        ("", DEFAULT_DOWNLOAD_THREADS),
        ("3,9", 3),
    ],
)
def test_download_threads_read_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("DOWNLOAD_THREADS", raw)
    assert download_threads_from_env() == expected


def test_disk_limits_accept_fractions_and_zero_switches_them_off(monkeypatch):
    monkeypatch.setenv("MIN_FREE_GB", "1,5")
    monkeypatch.setenv("CACHE_LIMIT_GB", "0")
    assert min_free_bytes_from_env() == int(1.5 * GB)
    assert cache_limit_bytes_from_env() == 0
    monkeypatch.setenv("MIN_FREE_GB", "не число")
    assert min_free_bytes_from_env() == int(DEFAULT_MIN_FREE_GB * GB)


def test_free_bytes_measures_a_real_folder_and_survives_a_missing_one(tmp_path):
    assert free_bytes(tmp_path) > 0
    assert free_bytes(tmp_path / "нет" / "такой" / "папки") is None


# ---------- можно ли принять ещё одну задачу ----------


def test_several_jobs_run_at_once_by_default():
    slots = default_max_jobs()
    assert slots >= 1
    assert not jobs_limit_reached(slots - 1)  # есть свободное место — берём
    assert jobs_limit_reached(slots)  # места кончились
    assert jobs_limit_reached(slots + 3)


def test_the_jobs_limit_follows_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_JOBS", "2")
    assert not jobs_limit_reached(1) and jobs_limit_reached(2)
    monkeypatch.setenv("MAX_JOBS", "0")  # 0 — без предела
    assert not jobs_limit_reached(500)


def test_little_free_space_is_noticed(monkeypatch, tmp_path):
    free(monkeypatch, DEFAULT_MIN_FREE_GB - 0.1)
    assert disk_is_low(tmp_path)
    free(monkeypatch, DEFAULT_MIN_FREE_GB)  # ровно столько, сколько нужно, — ещё можно
    assert not disk_is_low(tmp_path)


def test_the_free_space_limit_can_be_switched_off(monkeypatch, tmp_path):
    free(monkeypatch, 0.1)
    monkeypatch.setenv("MIN_FREE_GB", "0")
    assert not disk_is_low(tmp_path)


def test_when_the_disk_cannot_be_measured_work_goes_on(monkeypatch, tmp_path):
    free(monkeypatch, None)
    assert not disk_is_low(tmp_path)


# ---------- учёт задач ----------


def test_ahead_of_counts_only_the_waiting_jobs(manager):
    """Идущие задачи места уже заняли, но «впереди» для ждущего только те, кто раньше в очереди."""
    ids = [manager._create() for _ in range(4)]  # noqa: SLF001
    manager._update(ids[0], status=JobStatus.DONE)  # noqa: SLF001
    manager._update(ids[1], status=JobStatus.RUNNING)  # noqa: SLF001

    assert [manager.ahead_of(job_id) for job_id in ids] == [0, 0, 0, 1]
    assert manager.ahead_of("нет такой") == 0


def test_a_delivered_job_is_forgotten_so_that_memory_does_not_grow(manager):
    job = manager._create()  # noqa: SLF001
    manager._update(job, status=JobStatus.DONE)  # noqa: SLF001
    folder = manager.job_dir(job)
    folder.mkdir(parents=True)
    (folder / "clip_01.mp4").write_bytes(b"x")

    manager.discard(job)

    assert manager.get(job) is None and not folder.exists()


def test_a_job_cancelled_in_the_queue_ends_at_once_and_never_runs(manager):
    job = manager._create()  # noqa: SLF001

    assert manager.cancel(job) is True

    state = manager.get(job)
    assert state.status is JobStatus.CANCELLED
    assert manager.cancel(job) is False  # уже закончилась
    manager._worker(None, job, None, None)  # noqa: SLF001 — дошла очередь: запрос даже не читается
    assert manager.get(job).status is JobStatus.CANCELLED


def test_a_running_job_is_asked_to_stop_not_cancelled_on_the_spot(manager):
    job = manager._create()  # noqa: SLF001
    manager._update(job, status=JobStatus.RUNNING)  # noqa: SLF001

    assert manager.cancel(job) is True

    state = manager.get(job)
    assert state.status is JobStatus.RUNNING and state.stage == "Останавливаю"


# ---------- размер кеша ----------


def _file(path, size: int, age_hours: float, now: float) -> None:
    path.write_bytes(b"x" * size)
    stamp = now - age_hours * HOUR
    os.utime(path, (stamp, stamp))


def _settings(tmp_path) -> Settings:
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    return settings


def test_a_cache_over_the_limit_loses_the_oldest_entries_first(tmp_path):
    settings, now = _settings(tmp_path), time.time()
    oldest, middle, newest = (settings.cache_dir / name for name in ("a.mp4", "b.mp4", "c.mp4"))
    _file(oldest, 400, 30, now)
    _file(middle, 400, 20, now)
    _file(newest, 400, 10, now)

    removed = enforce_cache_limit(settings, 1000, now=now)

    assert removed == 1 and not oldest.exists() and middle.exists() and newest.exists()


def test_removal_stops_as_soon_as_the_cache_fits(tmp_path):
    settings, now = _settings(tmp_path), time.time()
    files = [settings.cache_dir / f"{n}.mp4" for n in range(4)]
    for age, path in zip((40, 30, 20, 10), files, strict=True):
        _file(path, 400, age, now)

    assert enforce_cache_limit(settings, 900, now=now) == 2  # 1600 → 800: убрано два самых старых
    assert [path.exists() for path in files] == [False, False, True, True]


def test_a_cache_within_the_limit_or_without_one_is_left_alone(tmp_path):
    settings, now = _settings(tmp_path), time.time()
    path = settings.cache_dir / "a.mp4"
    _file(path, 400, 30, now)

    assert enforce_cache_limit(settings, 400, now=now) == 0  # ровно по пределу — убирать нечего
    assert enforce_cache_limit(settings, 0, now=now) == 0  # 0 — без предела
    assert path.exists()


def test_recent_entries_survive_even_over_the_limit(tmp_path):
    """Свежий источник может быть нужен идущей прямо сейчас задаче: из-под неё файл не вытаскиваем."""
    settings, now = _settings(tmp_path), time.time()
    fresh = [settings.cache_dir / "x.mp4", settings.cache_dir / "y.mp4"]
    for path in fresh:
        _file(path, 600, 1, now)
    old = settings.cache_dir / "old.mp4"
    _file(old, 600, 30, now)

    assert enforce_cache_limit(settings, 1000, now=now) == 1  # ушёл только старый; свежие всё ещё выше предела
    assert not old.exists() and all(path.exists() for path in fresh)


def test_folders_in_the_cache_count_with_their_contents(tmp_path):
    settings, now = _settings(tmp_path), time.time()
    folder = settings.cache_dir / "parts"
    folder.mkdir()
    _file(folder / "video.part", 700, 30, now)
    _file(folder / "audio.part", 700, 30, now)
    os.utime(folder, (now - 30 * HOUR, now - 30 * HOUR))

    assert enforce_cache_limit(settings, 1000, now=now) == 1
    assert not folder.exists()


def test_only_the_cache_is_capped(tmp_path):
    settings, now = _settings(tmp_path), time.time()
    other = [settings.uploads_dir / "u.mp4", settings.outputs_dir / "o.mp4", settings.banners_dir / "b.mp4"]
    for path in other:
        _file(path, 5000, 100, now)

    assert enforce_cache_limit(settings, 10, now=now) == 0
    assert all(path.exists() for path in other)


# ---------- процессор: потоков у ffmpeg не больше, чем контейнеру разрешено ----------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("max 100000\n", None),  # лимита нет
        ("100000 100000\n", 1),  # одно ядро
        ("150000 100000\n", 2),  # полтора ядра: два потока, чтобы выбрать их целиком
        ("200000 100000\n", 2),  # compose: cpus: 2.0
        ("50000 100000\n", 1),  # меньше ядра — всё равно хотя бы один поток
        ("100000 0\n", None),  # бессмыслица не должна ронять рендер
        ("мусор\n", None),
        ("", None),
    ],
)
def test_the_cpu_limit_is_read_from_the_container_cgroup(tmp_path, content, expected):
    (tmp_path / "cpu.max").write_text(content)

    assert cpu_limit(tmp_path) == expected


def test_a_machine_without_cgroups_has_no_cpu_limit(tmp_path):
    assert cpu_limit(tmp_path) is None


def test_ffmpeg_threads_follow_the_container_limit(monkeypatch):
    monkeypatch.delenv("FFMPEG_THREADS", raising=False)
    monkeypatch.setattr(capacity, "cpu_limit", lambda *args, **kwargs: 1)

    assert ffmpeg_threads() == 1


def test_ffmpeg_threads_are_up_to_ffmpeg_when_nothing_limits_the_cpu(monkeypatch):
    monkeypatch.delenv("FFMPEG_THREADS", raising=False)
    monkeypatch.setattr(capacity, "cpu_limit", lambda *args, **kwargs: None)

    assert ffmpeg_threads() is None


@pytest.mark.parametrize(("raw", "expected"), [("3", 3), ("1", 1), ("0", None), ("2,5", 2)])
def test_ffmpeg_threads_can_be_set_by_hand(monkeypatch, raw, expected):
    monkeypatch.setenv("FFMPEG_THREADS", raw)
    monkeypatch.setattr(capacity, "cpu_limit", lambda *args, **kwargs: 1)  # ручная настройка важнее лимита

    assert ffmpeg_threads() == expected
