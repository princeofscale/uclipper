"""Конвейер целиком: менеджер задач (источник → нарезка → ролики → ZIP).

Telegram-обвязка проверяется отдельно в `tests/test_bot.py`; здесь — только движок нарезки."""
import io
import shutil
import time
import zipfile

import pytest

from app.models import JobRequest, JobStatus
from app.services.banner_manager import ensure_sfx
from app.services.downloader import save_local_upload
from app.services.jobs import JobManager
from tests.media_utils import probe

WAIT_SECONDS = 180


@pytest.fixture
def manager(settings, banner_id):
    ensure_sfx(settings.sfx_dir)
    # Один работник: эти тесты проверяют сам конвейер, а не параллельность (для неё есть отдельный тест).
    jobs = JobManager(settings, max_workers=1)
    yield jobs
    jobs.shutdown()


@pytest.fixture
def upload_id(settings, synthetic_video):
    with synthetic_video.open("rb") as stream:
        return save_local_upload("source.mp4", stream, settings.uploads_dir)[0]


def request_for(upload, **overrides) -> JobRequest:
    """Запрос из спеки: 2 клипа по 9 с, 2 показа баннера по 2 с (итого 13 с на клип)."""
    fields = dict(
        source_type="upload", upload_id=upload, num_clips=2, clip_duration=9,
        banner_count=2, banner_duration=2, banner_id="banner.png",
    )  # fmt: skip
    return JobRequest(**{**fields, **overrides})


def wait_for(manager, job_id, timeout=WAIT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.get(job_id)
        if state.status in (JobStatus.DONE, JobStatus.ERROR):
            return state
        time.sleep(0.1)
    raise AssertionError(f"задача {job_id} не завершилась за {timeout} с: {manager.get(job_id)}")


# ---------- сценарий из спеки ----------


def test_run_job_end_to_end_upload(manager, settings, upload_id):
    state = manager.run_sync(request_for(upload_id))

    assert state.status is JobStatus.DONE, state.error
    assert state.progress == 100
    assert state.stage == "Готово"
    assert [clip.index for clip in state.clips] == [1, 2]
    assert [clip.source_start for clip in state.clips] == pytest.approx([0.0, 11.0])  # равномерно: шаг (20 − 9) / (2 − 1)
    assert [clip.source_end for clip in state.clips] == pytest.approx([9.0, 20.0])
    for clip in state.clips:
        info = probe(settings.outputs_dir / state.id / clip.filename)
        assert (info.width, info.height, info.vcodec, info.acodec) == (1080, 1920, "h264", "aac")
        assert info.duration == pytest.approx(13.0, abs=0.15)
        assert clip.duration == pytest.approx(13.0, abs=0.05)
        assert clip.banner_points == pytest.approx([3.0, 6.0])  # секунды КЛИПА, где видео встаёт
        assert clip.banner_windows == [pytest.approx((3, 5)), pytest.approx((8, 10))]  # окна на таймлайне ролика
        assert (settings.outputs_dir / state.id / clip.filename).is_file()

    archive = manager.zip_path(state.id)
    assert archive is not None and archive.is_file()
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == ["clip_01.mp4", "clip_02.mp4"]
        assert bundle.testzip() is None
    assert state.archive_name == f"clips_{state.id}.zip"
    assert sum("Клип" in line for line in state.log) >= 2


def test_youtube_job_takes_the_video_from_cache(manager, settings, synthetic_video):
    """Ролик, уже лежащий в workspace/cache, повторно не качается (сеть не нужна)."""
    video_id = "dQw4w9WgXcQ"
    shutil.copy(synthetic_video, settings.cache_dir / f"source_{video_id}.mp4")
    request = JobRequest(source_type="link", url=f"https://www.youtube.com/watch?v={video_id}", num_clips=1, clip_duration=3, banner_count=0)
    state = manager.run_sync(request)
    assert state.status is JobStatus.DONE, state.error
    assert any("кеш" in line for line in state.log)


# ---------- окно клипа не влезает в источник (RF2) ----------


def test_job_clamps_a_clip_longer_than_the_source(manager, upload_id):
    state = manager.run_sync(request_for(upload_id, num_clips=1, clip_duration=30, banner_count=0))
    assert state.status is JobStatus.DONE, state.error
    assert state.clips[0].duration == pytest.approx(20.0, abs=0.1)
    assert any("не помещается" in line for line in state.log)


def test_job_switches_to_even_spacing_when_sequential_clips_do_not_fit(manager, upload_id):
    state = manager.run_sync(request_for(upload_id, num_clips=3, clip_duration=9, banner_count=0, slicing_mode="sequential"))
    assert state.status is JobStatus.DONE, state.error
    assert [clip.source_start for clip in state.clips] == pytest.approx([0.0, 5.5, 11.0])  # 3 × 9 > 20 → равномерно
    assert any("равномерно" in line for line in state.log)


# ---------- ошибки не вылетают наружу (RF2, RF4) ----------


@pytest.mark.parametrize(
    ("overrides", "message_part"),
    [
        ({"upload_id": "0" * 32}, "не найден"),  # id правильной формы, файла нет
        ({"upload_id": "../../etc/passwd"}, "Некорректный"),
        ({"banner_id": "nope.png"}, "не найден"),
        ({"banner_id": "../../secret.png"}, "не найден"),
        ({"start_offset": 19.5}, "Смещение"),  # после смещения меньше секунды видео
    ],
    ids=["no-such-upload", "upload-path-traversal", "no-such-banner", "banner-path-traversal", "offset-past-the-end"],
)
def test_job_error_is_reported_not_raised(manager, upload_id, overrides, message_part):
    state = manager.run_sync(request_for(upload_id, **overrides))
    assert state.status is JobStatus.ERROR
    assert message_part in state.error
    assert "Непредвиденная" not in state.error  # ожидаемые ошибки получают понятный текст, а не общую заглушку
    assert state.clips == []
    assert manager.zip_path(state.id) is None


def test_job_with_a_broken_video_file_is_an_error(manager, settings):
    broken, _ = save_local_upload("broken.mp4", io.BytesIO(b"not a video at all" * 100), settings.uploads_dir)
    state = manager.run_sync(request_for(broken))
    assert state.status is JobStatus.ERROR
    assert "видео" in state.error.lower()


# ---------- очередь, прогресс, остановка ----------


def test_jobs_run_one_after_another(manager, upload_id):
    quick = dict(num_clips=1, clip_duration=3, banner_count=0)
    first = manager.submit(request_for(upload_id, **quick))
    second = manager.submit(request_for(upload_id, **quick))
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        statuses = (manager.get(first).status, manager.get(second).status)
        assert statuses != (JobStatus.RUNNING, JobStatus.RUNNING), "две задачи рендерятся одновременно"
        if all(status in (JobStatus.DONE, JobStatus.ERROR) for status in statuses):
            break
        time.sleep(0.02)
    assert (manager.get(first).status, manager.get(second).status) == (JobStatus.DONE, JobStatus.DONE)


def test_two_jobs_can_render_at_the_same_time(settings, banner_id, upload_id):
    """Многопоточность: пока один человек ждёт свой клип, второй не стоит в очереди."""
    ensure_sfx(settings.sfx_dir)
    jobs = JobManager(settings, max_workers=2)
    try:
        quick = dict(num_clips=1, clip_duration=6, banner_count=0)
        first = jobs.submit(request_for(upload_id, **quick))
        second = jobs.submit(request_for(upload_id, **quick))
        saw_two_running = False
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            states = (jobs.get(first), jobs.get(second))
            if states[0] is not None and states[1] is not None:
                running = [state.status is JobStatus.RUNNING for state in states]
                saw_two_running = saw_two_running or all(running)
                if all(state.status in (JobStatus.DONE, JobStatus.ERROR) for state in states):
                    break
            time.sleep(0.01)
        assert (jobs.get(first).status, jobs.get(second).status) == (JobStatus.DONE, JobStatus.DONE)
        assert saw_two_running, "с двумя работниками нарезки должны идти одновременно"
        assert jobs.max_parallel() == 2
    finally:
        jobs.shutdown()


def test_progress_only_moves_forward_and_ends_at_100(manager, upload_id):
    job = manager.submit(request_for(upload_id, clip_duration=4, banner_count=1, banner_duration=1))
    samples = []
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        state = manager.get(job)
        samples.append(state.progress)
        if state.status in (JobStatus.DONE, JobStatus.ERROR):
            break
        time.sleep(0.05)
    assert state.status is JobStatus.DONE, state.error
    assert samples == sorted(samples)
    assert samples[-1] == 100
    assert len(set(samples)) > 3  # шкала шла постепенно, а не прыгнула с 0 на 100


def test_shutdown_interrupts_a_clip_in_the_middle_of_rendering(manager, settings, upload_id):
    """Один длинный клип (около 24 с ролика): остановка должна оборвать FFmpeg сразу, а не дождаться конца клипа."""
    job = manager.submit(request_for(upload_id, num_clips=1, clip_duration=20))
    deadline = time.monotonic() + WAIT_SECONDS
    while manager.get(job).progress < 25 and time.monotonic() < deadline:
        time.sleep(0.05)
    stopped_at = time.monotonic()
    manager.shutdown()

    state = wait_for(manager, job, timeout=15)
    assert time.monotonic() - stopped_at < 3
    assert state.status is JobStatus.ERROR
    assert "остановлена" in state.error
    assert list((settings.outputs_dir / job).glob("*.mp4")) == []  # недорендеренного файла не осталось


def test_shutdown_cleans_partial_clips_and_reports_the_error(manager, settings, upload_id):
    """Бот не умеет докачивать рендер после рестарта: при выключении файлы задачи удаляются."""
    job = manager.submit(request_for(upload_id, num_clips=5, clip_duration=4, banner_count=1, banner_duration=1))
    deadline = time.monotonic() + WAIT_SECONDS
    while not manager.get(job).clips and time.monotonic() < deadline:
        time.sleep(0.05)
    manager.shutdown()

    state = wait_for(manager, job, timeout=30)
    assert state.status is JobStatus.ERROR
    assert list((settings.outputs_dir / job).glob("*.mp4")) == []  # недоделанные файлы убраны


# ---------- чтение состояния ----------


def test_get_returns_a_copy_and_unknown_ids_are_none(manager, upload_id):
    state = manager.run_sync(request_for(upload_id, num_clips=1, clip_duration=3, banner_count=0))
    state.progress = -5
    state.log.append("мусор")
    again = manager.get(state.id)
    assert again.progress == 100
    assert "мусор" not in again.log
    assert manager.get("нет-такой-задачи") is None
    assert manager.zip_path("нет-такой-задачи") is None


def test_synthetic_source_is_20_seconds_720p_with_audio(synthetic_video):
    info = probe(synthetic_video)
    assert (info.width, info.height, info.vcodec, info.acodec) == (1280, 720, "h264", "aac")
    assert info.duration == pytest.approx(20, abs=0.2)


# ---------- «только звук и нужные окна» (WINDOW_DOWNLOAD=1) ----------

RUTUBE_URL = "https://rutube.ru/video/0123456789abcdef0123456789abcdef/"


def _cut(source, target, start: float, duration: float):
    """Вырезает кусок реальным ffmpeg — так тест повторяет то, что отдала бы площадка."""
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(duration), "-i", str(source),
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(target)],
        check=True, capture_output=True,
    )  # fmt: skip
    return target


def test_windowed_download_renders_clips_from_sections(monkeypatch, tmp_path, settings, banner_id, synthetic_video):
    """Флаг WINDOW_DOWNLOAD: анализ по звуку, рендер из скачанного окна, начало окна = запас вокруг него."""
    import app.services.jobs as jobs_module

    monkeypatch.setenv("WINDOW_DOWNLOAD", "1")
    audio = _cut(synthetic_video, tmp_path / "audio.m4a", 0, 20)
    asked: list[tuple[float, float]] = []

    def fake_section(url, start, end, key, **kwargs):
        asked.append((start, end))
        return _cut(synthetic_video, tmp_path / f"sec_{start:.1f}.mp4", start, end - start)

    monkeypatch.setattr(jobs_module, "download_audio", lambda url, key, cache_dir=None: audio)
    monkeypatch.setattr(jobs_module, "download_section", fake_section)

    jobs = JobManager(settings, max_workers=1)
    try:
        request = JobRequest(
            source_type="link", url=RUTUBE_URL, num_clips=1, clip_duration=3.0,
            start_offset=5.0, slicing_mode="sequential", banner_count=0,
        )  # fmt: skip
        state = jobs.run_sync(request)
    finally:
        jobs.shutdown()

    assert state.status is JobStatus.DONE, state.error
    assert len(state.clips) == 1
    assert state.clips[0].source_start == pytest.approx(5.0, abs=0.2)
    assert asked and asked[0][0] == pytest.approx(2.0, abs=0.1)  # окно с запасом 3 с перед началом
    assert state.timing and "только звук" in state.timing


def test_windowed_download_falls_back_to_the_whole_file_when_sections_are_missing(
    monkeypatch, tmp_path, settings, banner_id, synthetic_video
):
    """Площадка не умеет секции — клип всё равно выходит: он берётся из полного файла."""
    import app.services.jobs as jobs_module

    monkeypatch.setenv("WINDOW_DOWNLOAD", "1")
    audio = _cut(synthetic_video, tmp_path / "audio.m4a", 0, 20)
    monkeypatch.setattr(jobs_module, "download_audio", lambda url, key, cache_dir=None: audio)
    monkeypatch.setattr(jobs_module, "download_section", lambda *a, **k: None)
    monkeypatch.setattr(jobs_module, "download_video_cached", lambda url, height, **k: synthetic_video)

    jobs = JobManager(settings, max_workers=1)
    try:
        request = JobRequest(
            source_type="link", url=RUTUBE_URL, num_clips=1, clip_duration=3.0,
            start_offset=5.0, slicing_mode="sequential", banner_count=0,
        )  # fmt: skip
        state = jobs.run_sync(request)
    finally:
        jobs.shutdown()

    assert state.status is JobStatus.DONE, state.error
    assert state.clips and any("полного файла" in line for line in state.log)


def test_window_flag_does_not_touch_uploaded_files(monkeypatch, settings, banner_id, upload_id):
    """Флаг включает окна только для ссылок: присланный файл качается из чата и живёт локально."""
    import app.services.jobs as jobs_module

    monkeypatch.setenv("WINDOW_DOWNLOAD", "1")
    monkeypatch.setattr(jobs_module, "download_audio", pytest.fail)  # звук с площадки тут не нужен

    jobs = JobManager(settings, max_workers=1)
    try:
        state = jobs.run_sync(request_for(upload_id, num_clips=1, clip_duration=3, banner_count=0))
    finally:
        jobs.shutdown()

    assert state.status is JobStatus.DONE, state.error
    assert state.timing is None or "только звук" not in state.timing  # шли обычным путём


def test_when_the_audio_fails_the_whole_file_is_downloaded_as_before(
    monkeypatch, settings, banner_id, synthetic_video
):
    """Звук не скачался — поведение откатывается к прежнему, а не падает."""
    import app.services.jobs as jobs_module

    monkeypatch.setenv("WINDOW_DOWNLOAD", "1")
    monkeypatch.setattr(jobs_module, "download_audio", lambda *a, **k: None)
    monkeypatch.setattr(jobs_module, "download_video_cached", lambda url, height, **k: synthetic_video)

    jobs = JobManager(settings, max_workers=1)
    try:
        request = JobRequest(
            source_type="link", url=RUTUBE_URL, num_clips=1, clip_duration=3.0,
            start_offset=5.0, slicing_mode="sequential", banner_count=0,
        )  # fmt: skip
        state = jobs.run_sync(request)
    finally:
        jobs.shutdown()

    assert state.status is JobStatus.DONE, state.error
    assert any("Окна не получились" in line for line in state.log)
