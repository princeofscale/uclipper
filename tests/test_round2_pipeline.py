"""Раунд 2 в сборе: автообрезка и смещения в задаче, видео-баннер."""

import shutil

import pytest

from app.models import JobRequest, JobStatus
from app.services.banner_manager import ensure_sfx
from app.services.downloader import save_local_upload
from app.services.jobs import JobManager
from tests.media_utils import audio_frequency, make_color_video, make_episode, probe, rgb_at
from tests.test_render_variants import RED

WAIT_SECONDS = 180


@pytest.fixture
def manager(settings):
    ensure_sfx(settings.sfx_dir)
    jobs = JobManager(settings)
    yield jobs
    jobs.shutdown()


@pytest.fixture(scope="module")
def black_edged_episode(media_dir):
    """43 секунды: 4 с чёрного, 36 с сюжета, 3 с чёрного."""
    return make_episode(media_dir / "job_episode.mp4", head_black=4, content=36, tail_black=3)


@pytest.fixture(scope="module")
def plain_episode(media_dir):
    return make_episode(media_dir / "plain_episode.mp4", content=30)


@pytest.fixture(scope="module")
def traffic_video(media_dir):
    """Видео-баннер 2 с: красный, синий, звук 1000 Гц."""
    return make_color_video(media_dir / "round2_traffic.mp4", [("red", 1.0), ("blue", 1.0)], tone_hz=1000)


def put_upload(settings, path):
    with path.open("rb") as stream:
        return save_local_upload("source.mp4", stream, settings.uploads_dir)[0]


def request_for(upload, **overrides) -> JobRequest:
    fields = dict(source_type="upload", upload_id=upload, num_clips=1, clip_duration=6, banner_count=0)
    return JobRequest(**{**fields, **overrides})


# ---------- задача: обрезка начала и конца ----------


def test_auto_trim_takes_clips_only_from_the_real_episode(manager, settings, black_edged_episode):
    upload = put_upload(settings, black_edged_episode)
    state = manager.run_sync(request_for(upload, auto_trim=True, num_clips=2, clip_duration=10))
    assert state.status is JobStatus.DONE, state.error
    assert state.trim.auto is True
    assert (state.trim.start, state.trim.end) == pytest.approx((4.0, 40.0), abs=0.6)
    assert "чёрн" in state.trim.head and "чёрн" in state.trim.tail
    assert [clip.source_start for clip in state.clips] == pytest.approx([state.trim.start, state.trim.end - 10], abs=0.05)
    assert state.clips[-1].source_end <= state.trim.end + 0.05
    assert any("Автообрезка" in line for line in state.log)


def test_auto_trim_that_finds_nothing_says_so_and_keeps_the_whole_video(manager, settings, plain_episode):
    state = manager.run_sync(request_for(put_upload(settings, plain_episode), auto_trim=True))
    assert state.status is JobStatus.DONE, state.error
    assert (state.trim.start, state.trim.head, state.trim.tail) == (0.0, None, None)
    assert state.trim.end == pytest.approx(30.0, abs=0.05)
    assert any("целиком" in line for line in state.log)


def test_manual_offsets_cut_both_ends_when_auto_trim_is_off(manager, settings, synthetic_video):
    state = manager.run_sync(request_for(put_upload(settings, synthetic_video), num_clips=2, clip_duration=5, start_offset=2, end_offset=5))
    assert state.status is JobStatus.DONE, state.error
    assert [clip.source_start for clip in state.clips] == pytest.approx([2.0, 10.0])
    assert [clip.source_end for clip in state.clips] == pytest.approx([7.0, 15.0])  # конец клипа не дальше 20 − 5
    assert (state.trim.auto, state.trim.start, state.trim.end) == (False, 2.0, pytest.approx(15.0))


def test_a_job_without_any_trim_reports_no_trim(manager, settings, synthetic_video):
    state = manager.run_sync(request_for(put_upload(settings, synthetic_video)))
    assert state.status is JobStatus.DONE, state.error
    assert state.trim is None


# ---------- задача: баннеры ----------


def test_job_with_a_video_banner_pauses_for_the_length_of_the_video(manager, settings, synthetic_video, traffic_video):
    shutil.copy(traffic_video, settings.banners_dir / "traffic.mp4")

    state = manager.run_sync(
        request_for(
            put_upload(settings, synthetic_video), clip_duration=6, banner_count=1, banner_duration=4.5,
            banner_id="traffic.mp4",
        )
    )  # fmt: skip
    assert state.status is JobStatus.DONE, state.error
    (clip,) = state.clips
    output = settings.outputs_dir / state.id / clip.filename
    assert clip.duration == pytest.approx(8.0, abs=0.1)  # пауза = длина видео-баннера (2 с), а не 4.5 с с ползунка
    assert clip.banner_windows == [pytest.approx((3, 5), abs=0.05)]
    assert probe(output).duration == pytest.approx(8.0, abs=0.15)
    assert rgb_at(output, 3.25, (540, 960), settings.base_dir) == pytest.approx(RED, abs=40)  # баннер — в паузе
    assert audio_frequency(output, 3.2, 1.5, settings.base_dir) == pytest.approx(1000, abs=60)  # со своим звуком
