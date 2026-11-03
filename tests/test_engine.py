"""Движок FFmpeg: один проход на клип, заморозка кадра и звука под баннер.

Проверяется результат, а не текст графа: готовые ролики разбираются ffprobe/ffmpeg и Pillow.
Источник «кодирует время» (яркость растёт, звук — чирп), поэтому видно, из какой секунды взят каждый кусок.
"""
import pytest

from app.services.video_processor import MediaError, MediaInfo, RenderOptions, probe_media, render_clip
from tests.conftest import CLIP_START, PINK
from tests.media_utils import (
    audio_frequency,
    count_frames,
    frame_png,
    luma_at,
    mean_abs_diff,
    probe,
    rgb_at,
    volume_db,
)

# ---------- чтение медиа ----------


def test_probe_media_reports_streams(synthetic_video, silent_video):
    info = probe_media(synthetic_video)
    assert (info.width, info.height, info.has_audio) == (1280, 720, True)
    assert (info.video_codec, info.audio_codec) == ("h264", "aac")
    assert info.duration == pytest.approx(20, abs=0.2)
    assert probe_media(silent_video).has_audio is False


def test_probe_media_rejects_a_file_that_is_not_a_video(tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is not a video")
    with pytest.raises(MediaError):
        probe_media(fake)


# ---------- клип с остановками (сценарий из спеки) ----------


def test_freeze_clip_has_the_format_and_length_from_the_spec(freeze_clip):
    info = probe(freeze_clip.path)
    assert (info.width, info.height) == (1080, 1920)
    assert (info.vcodec, info.acodec) == ("h264", "aac")
    assert info.duration == pytest.approx(13.0, abs=0.15)
    assert count_frames(freeze_clip.path) == pytest.approx(390, abs=2)
    assert list(freeze_clip.timeline.windows) == [pytest.approx((3, 5)), pytest.approx((8, 10))]


def test_picture_stands_still_inside_the_windows_and_moves_outside(freeze_clip):
    path, scratch = freeze_clip.path, freeze_clip.scratch
    first_pause = mean_abs_diff(frame_png(path, 3.4, scratch / "p1a.png"), frame_png(path, 4.8, scratch / "p1b.png"))
    second_pause = mean_abs_diff(frame_png(path, 8.4, scratch / "p2a.png"), frame_png(path, 9.8, scratch / "p2b.png"))
    playing = mean_abs_diff(frame_png(path, 0.5, scratch / "m1.png"), frame_png(path, 1.5, scratch / "m2.png"))
    assert first_pause < 1.5
    assert second_pause < 1.5
    assert playing > 8  # без этого проверка «стоит» прошла бы и на статичном видео


def test_sound_stops_inside_the_windows_and_plays_outside(freeze_clip):
    path = freeze_clip.path
    assert volume_db(path, 3.2, 1.6)[1] < -60  # первая пауза — тишина
    assert volume_db(path, 8.2, 1.6)[1] < -60  # вторая пауза — тишина
    assert volume_db(path, 0.2, 2.6)[0] > -30  # до паузы звук есть
    assert volume_db(path, 5.2, 2.6)[0] > -30  # между паузами тоже


def test_content_resumes_exactly_where_it_stopped(freeze_clip, synthetic_video):
    """Кадр и звук на ролике в t_out соответствуют секунде источника t_src = старт + t_out − суммарные паузы."""
    path, scratch = freeze_clip.path, freeze_clip.scratch
    for t_out, t_src in [(1.0, 9.0), (6.0, 12.0), (11.0, 15.0)]:  # до пауз / между ними / после обеих
        got = luma_at(path, t_out, (540, 700), scratch)
        want = luma_at(synthetic_video, t_src, (640, 360), scratch)
        assert got == pytest.approx(want, abs=4), f"картинка в {t_out} с ролика"
        # звук: частота чирпа в начале окна 0.1 с — 200 + 200·(t_src + 0.05)
        assert audio_frequency(path, t_out, 0.1, scratch) == pytest.approx(200 + 200 * (t_src + 0.05), abs=80), f"звук в {t_out} с ролика"


def test_banner_is_visible_only_while_the_video_is_frozen(freeze_clip):
    path, scratch = freeze_clip.path, freeze_clip.scratch
    center = (540, 960)
    for t in (4.0, 8.9, 9.8):  # внутри окон
        assert rgb_at(path, t, center, scratch) == pytest.approx(PINK, abs=15), f"баннера нет в {t} с"
    for t in (1.0, 6.5, 11.5):  # вне окон
        pixel = rgb_at(path, t, center, scratch)
        assert max(abs(a - b) for a, b in zip(pixel, PINK)) > 60, f"баннер виден вне окна в {t} с"


def test_banner_window_matches_the_frozen_frames_to_the_frame(freeze_clip):
    """Баннер появляется на первом стоп-кадре и исчезает на первом кадре возобновлённого видео — ни кадром раньше/позже."""
    path, scratch = freeze_clip.path, freeze_clip.scratch

    def banner_on(frame: int) -> bool:  # кадр № frame — это время frame/30; ищем чуть раньше, между соседними кадрами
        pixel = rgb_at(path, (frame - 0.5) / 30, (540, 960), scratch)
        return max(abs(a - b) for a, b in zip(pixel, PINK)) < 30

    # первая пауза: кадры 90…149 (3.0–4.97 с), вторая: 240…299 (8.0–9.97 с)
    assert [banner_on(frame) for frame in (89, 90, 149, 150)] == [False, True, True, False]
    assert [banner_on(frame) for frame in (239, 240, 299, 300)] == [False, True, True, False]


def test_frozen_frame_is_dimmed_by_45_percent_only_during_the_pause(freeze_clip, synthetic_video):
    path, scratch = freeze_clip.path, freeze_clip.scratch
    above_banner = (540, 700)  # чёткое видео над баннером
    stopped_at = CLIP_START + 3.0 - 1 / 30  # последний кадр сегмента перед первой паузой
    reference = luma_at(synthetic_video, stopped_at, (640, 360), scratch)
    assert luma_at(path, 4.0, above_banner, scratch) / reference == pytest.approx(0.55, abs=0.06)
    playing_ref = luma_at(synthetic_video, CLIP_START + 1.0, (640, 360), scratch)
    assert luma_at(path, 1.0, above_banner, scratch) / playing_ref == pytest.approx(1.0, abs=0.05)


# ---------- без баннеров, прогресс, ошибки ----------


def test_clip_without_banners_is_a_plain_vertical_cut(synthetic_video, tmp_path):
    output = tmp_path / "plain.mp4"
    timeline = render_clip(synthetic_video, output, 2.0, 5.0, RenderOptions(banner_count=0), info=probe_media(synthetic_video))
    info = probe(output)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration == pytest.approx(5.0, abs=0.15)
    assert timeline.windows == ()


def test_progress_callback_reports_growing_fractions_up_to_one(synthetic_video, tmp_path):
    seen = []
    render_clip(synthetic_video, tmp_path / "p.mp4", 0.0, 6.0, RenderOptions(banner_count=0), info=probe_media(synthetic_video), on_progress=seen.append)
    assert len(seen) >= 2
    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0, abs=0.02)


def test_banner_shows_need_a_banner_file(synthetic_video, tmp_path):
    with pytest.raises(MediaError, match="баннер"):
        render_clip(synthetic_video, tmp_path / "x.mp4", 0.0, 6.0, RenderOptions(banner_count=2), info=probe_media(synthetic_video))
    assert not (tmp_path / "x.mp4").exists()


def test_ffmpeg_failure_is_reported_and_leaves_no_partial_file(tmp_path):
    info = MediaInfo(duration=10, width=1280, height=720, has_audio=True, video_codec="h264", audio_codec="aac")
    with pytest.raises(MediaError, match="missing.mp4"):
        render_clip(tmp_path / "missing.mp4", tmp_path / "x.mp4", 0.0, 5.0, RenderOptions(banner_count=0), info=info)
    assert not (tmp_path / "x.mp4").exists()
