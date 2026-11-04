"""Яркие моменты по звуку: чистые функции выбора окон и настоящий анализ звука через ffmpeg."""
from __future__ import annotations

import numpy as np
import pytest

from app.models import JobRequest, JobStatus, SlicingMode
from app.services.banner_manager import ensure_sfx
from app.services.downloader import save_local_upload
from app.services.highlights import (
    edge_reserve,
    find_highlights,
    loudness_profile,
    pick_windows,
    window_scores,
)
from app.services.jobs import JobManager
from app.services.video_processor import probe_media
from tests.media_utils import make_video, run_ffmpeg

# ---------- чистые функции ----------


def flat(seconds, level=-40.0):
    return [level] * seconds


def with_loud(seconds, *spans, base=-40.0, loud=-10.0):
    levels = [base] * seconds
    for start, end in spans:
        levels[start:end] = [loud] * (end - start)
    return levels


def test_window_scores_prefer_loud_and_lively_windows():
    levels = with_loud(60, (20, 30))
    scores = window_scores(levels, 10)
    assert int(np.argmax(scores)) in range(18, 23)  # окно накрывает громкий отрезок (± вклад «живости» на краях)


def test_a_window_inside_a_pause_is_penalised():
    levels = [-20.0] * 20 + [-70.0] * 10 + [-20.0] * 20
    scores = window_scores(levels, 10)
    assert scores[0] > scores[20]  # окно без тишины лучше окна, целиком лежащего в паузе


def test_no_windows_when_the_recording_is_too_short():
    assert window_scores(flat(5), 10).size == 0
    assert pick_windows(flat(5), 1, 10) == []
    assert pick_windows(flat(100), 0, 10) == []
    assert pick_windows(flat(25), 3, 10) == []  # три окна по 10 с в 25 с не поместятся


def test_one_clip_takes_the_loudest_window():
    starts = pick_windows(with_loud(100, (40, 50)), 1, 10)
    assert len(starts) == 1 and abs(starts[0] - 40) <= 2


def test_each_clip_comes_from_its_own_part_of_the_video():
    levels = with_loud(120, (5, 15), (65, 75), (100, 110))  # самый громкий кусок — не в каждой трети
    starts = pick_windows(levels, 3, 10)
    assert len(starts) == 3
    assert starts[0] < 40 <= starts[1] < 80 <= starts[2] or starts == sorted(starts)
    assert abs(starts[0] - 5) <= 3 and abs(starts[1] - 65) <= 3 and abs(starts[2] - 100) <= 3


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8])
def test_windows_never_overlap_and_stay_inside_the_recording(count):
    rng = np.random.default_rng(count)
    levels = (rng.normal(-30, 8, 300)).tolist()
    starts = pick_windows(levels, count, 20)
    assert len(starts) == count
    assert starts == sorted(starts)
    for first, second in zip(starts, starts[1:]):
        assert second - first >= 20
    assert starts[0] >= 0 and starts[-1] + 20 <= len(levels)


def test_tight_recording_still_fits_every_clip():
    starts = pick_windows(flat(60), 3, 20)  # ровно впритык: 3 × 20 с
    assert starts == [0, 20, 40]


# ---------- настоящий звук ----------


@pytest.fixture(scope="module")
def loud_in_two_places(tmp_path_factory, ffmpeg_tools):
    """40 секунд тихого тона и два громких отрезка: 8–14 с и 26–32 с."""
    path = tmp_path_factory.mktemp("highlights") / "loud.mp4"
    amplitude = "0.02+0.7*(between(t,8,14)+between(t,26,32))"
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=10:d=40,format=yuv420p",
        "-f", "lavfi", "-i", f"aevalsrc='({amplitude})*sin(2*PI*330*t)':s=8000:d=40",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


def test_loudness_profile_has_one_value_per_second_and_sees_the_loud_parts(loud_in_two_places):
    levels = loudness_profile(loud_in_two_places, 0, 40)
    assert 38 <= len(levels) <= 41
    assert np.mean(levels[9:13]) > np.mean(levels[16:22]) + 15  # громкий отрезок заметно громче тихого


def test_loudness_profile_respects_the_requested_range(loud_in_two_places):
    levels = loudness_profile(loud_in_two_places, 20, 40)
    assert 18 <= len(levels) <= 21
    assert np.mean(levels[7:11]) > np.mean(levels[0:4]) + 15  # 27–31 с громче 20–24 с


def test_loudness_profile_is_empty_for_silence_less_or_tiny_inputs(loud_in_two_places, silent_video):
    assert loudness_profile(silent_video, 0, 8) == []
    assert loudness_profile(loud_in_two_places, 5, 5.5) == []


def test_find_highlights_returns_source_times_inside_the_requested_range(loud_in_two_places):
    starts = find_highlights(loud_in_two_places, 0, 40, 2, 6)
    assert len(starts) == 2
    assert abs(starts[0] - 8) <= 2 and abs(starts[1] - 26) <= 2

    shifted = find_highlights(loud_in_two_places, 20, 40, 1, 6)
    assert len(shifted) == 1 and abs(shifted[0] - 26) <= 2  # время считается от начала источника, не от смещения


def test_find_highlights_is_empty_without_audio(silent_video):
    assert find_highlights(silent_video, 0, 8, 1, 3) == []


# ---------- движок: режим «яркие моменты» ----------


@pytest.fixture
def manager(settings):
    ensure_sfx(settings.sfx_dir)
    jobs = JobManager(settings)
    yield jobs
    jobs.shutdown()


def upload_of(settings, path):
    with path.open("rb") as stream:
        return save_local_upload("source.mp4", stream, settings.uploads_dir)[0]


def highlight_request(upload, **overrides):
    fields = dict(
        source_type="upload", upload_id=upload, num_clips=2, clip_duration=6, banner_count=0,
        slicing_mode=SlicingMode.HIGHLIGHTS,
    )  # fmt: skip
    return JobRequest(**{**fields, **overrides})


def test_job_cuts_the_loud_moments(manager, settings, loud_in_two_places):
    state = manager.run_sync(highlight_request(upload_of(settings, loud_in_two_places)))

    assert state.status is JobStatus.DONE, state.error
    starts = [clip.source_start for clip in state.clips]
    assert len(starts) == 2 and abs(starts[0] - 8) <= 2 and abs(starts[1] - 26) <= 2
    assert state.total_clips == 2
    assert any("Яркие моменты:" in line for line in state.log)


def test_job_falls_back_to_even_spacing_when_there_is_no_sound(manager, settings, silent_video):
    state = manager.run_sync(highlight_request(upload_of(settings, silent_video), num_clips=2, clip_duration=3))

    assert state.status is JobStatus.DONE, state.error
    assert [clip.source_start for clip in state.clips] == pytest.approx([0.0, 5.0], abs=0.1)  # равномерно по 8 с
    assert any("Яркие моменты найти не вышло" in line for line in state.log)


def test_total_clips_is_known_while_the_job_runs(manager, settings, synthetic_video):
    state = manager.run_sync(
        highlight_request(upload_of(settings, synthetic_video), num_clips=3, clip_duration=4, slicing_mode=SlicingMode.SEQUENTIAL)
    )
    assert state.status is JobStatus.DONE and state.total_clips == 3 == len(state.clips)


# ---------- заставка, реклама и титры: края длинного ролика не берём ----------


@pytest.mark.parametrize(
    ("length", "needed", "head", "tail"),
    [
        (4014, 360, 200.7, 120.0),  # серия на час: 5% — заставка и реклама, 3% — титры
        (10800, 360, 240.0, 120.0),  # трёхчасовой подкаст: больше четырёх минут в начале не отбрасываем
        (600, 135, 30.0, 18.0),
        (300, 45, 15.0, 10.0),  # короче пяти минут края не трогаем — см. ниже
    ],
)
def test_long_videos_reserve_their_edges(length, needed, head, tail):
    assert edge_reserve(length, needed) == pytest.approx((head, tail), abs=0.1)


@pytest.mark.parametrize("length", [20, 120, 299])
def test_short_videos_keep_their_edges(length):
    """В короткой записи заставка редко длинная, а отбросить пришлось бы заметную долю."""
    assert edge_reserve(length, 45) == (0.0, 0.0)


def test_the_reserve_never_squeezes_the_clips_out():
    for length in range(300, 4000, 37):
        for needed in (45, 135, 360, 600):
            head, tail = edge_reserve(length, needed)
            assert head >= 0 and tail >= 0
            if length >= needed * 1.5:
                assert length - head - tail >= needed * 1.5 - 1e-6, (length, needed)
            else:
                assert (head, tail) == (0.0, 0.0), (length, needed)  # клипы и так занимают почти весь ролик


@pytest.fixture(scope="module")
def loud_intro(tmp_path_factory, ffmpeg_tools):
    """12 минут: громкая заставка 0–30 с, тихий разговор и живой громкий кусок 400–420 с."""
    path = tmp_path_factory.mktemp("intro") / "intro.mp4"
    amplitude = "0.02+0.8*between(t,0,30)+0.35*between(t,400,420)"
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=5:d=720,format=yuv420p",
        "-f", "lavfi", "-i", f"aevalsrc='({amplitude})*sin(2*PI*330*t)':s=8000:d=720",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


def schedule_of(manager, settings, path, **overrides):
    job_id = manager._create()  # noqa: SLF001
    request = highlight_request(upload_of(settings, path), **overrides)
    schedule = manager._schedule(request, job_id, path, probe_media(path), 0.0, 0.0)  # noqa: SLF001
    return schedule, manager.get(job_id).log


def test_highlights_do_not_start_in_the_intro_of_a_long_video(manager, settings, loud_intro):
    """Ошибка из жизни: первый клип брали с самого начала серии — с заставки и рекламы спонсоров: она громче разговора."""
    schedule, log = schedule_of(manager, settings, loud_intro, num_clips=1, clip_duration=20, auto_trim=False)

    assert schedule.starts[0] >= 36 - 0.5  # пять процентов от 12 минут
    assert abs(schedule.starts[0] - 400) <= 5  # а берём живой кусок из середины
    assert any("заставк" in line for line in log)


def test_even_spacing_fallback_also_skips_the_edges(manager, settings, tmp_path):
    silent = make_video(tmp_path / "silent_12min.mp4", size="160x90", seconds=720, fps=5, audio=False)

    schedule, _ = schedule_of(manager, settings, silent, num_clips=2, clip_duration=20, auto_trim=False)

    assert schedule.starts[0] == pytest.approx(36.0, abs=0.1)  # раньше первый клип начинался с нуля
    assert schedule.starts[-1] + schedule.clip_duration <= 720 - 21.6 + 0.1  # и не заходит в титры


def test_manual_modes_still_start_where_asked(manager, settings, tmp_path):
    """Края отбрасывает только «яркие моменты»: «По всему ролику» и «С самого начала» делают ровно то, что просили."""
    silent = make_video(tmp_path / "plain_12min.mp4", size="160x90", seconds=720, fps=5, audio=False)

    evenly, _ = schedule_of(manager, settings, silent, num_clips=2, clip_duration=20, slicing_mode=SlicingMode.EVENLY_SPACED, auto_trim=False)

    assert evenly.starts[0] == 0.0
