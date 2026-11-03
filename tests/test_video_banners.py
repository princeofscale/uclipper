"""Видео-баннер (MP4) и анимированный GIF как всплывающий баннер: кадры, пауза и звук баннера.

Как и остальные тесты движка, проверяется готовый ролик (кадры, длина, частота и громкость звука), а не граф FFmpeg.
Баннер-«светофор»: 1 с красного, потом 1 с синего и звук 1000 Гц — по цвету видно, какой момент баннера на экране.
"""
import pytest

from app.models import BannerMode
from app.services.video_processor import BannerMedia, probe_banner
from tests.conftest import CLIP_START
from tests.media_utils import audio_frequency, luma_at, make_color_video, make_gif, probe, rgb_at, volume_db
from tests.test_render_variants import BLUE, RED, render

TONE_HZ = 1000


def flat(windows):
    return [edge for window in windows for edge in window]


@pytest.fixture(scope="module")
def traffic_banner(media_dir):
    """2 секунды: красный, синий; со звуком 1000 Гц."""
    return make_color_video(media_dir / "traffic_2s.mp4", [("red", 1.0), ("blue", 1.0)], tone_hz=TONE_HZ)


@pytest.fixture(scope="module")
def traffic_banner_silent(media_dir):
    return make_color_video(media_dir / "traffic_silent_2s.mp4", [("red", 1.0), ("blue", 1.0)])


def center(output, t, scratch):
    return rgb_at(output, t, (540, 960), scratch)


# ---------- что за баннер ----------


def test_probe_banner_tells_images_gifs_and_videos_apart(traffic_banner, traffic_banner_silent, solid_banner, tmp_path):
    gif = make_gif(tmp_path / "a.gif", [RED, BLUE])
    assert probe_banner(solid_banner) == BannerMedia("image")
    assert probe_banner(gif) == BannerMedia("gif")
    loud = probe_banner(traffic_banner)
    assert (loud.kind, loud.has_audio) == ("video", True) and loud.duration == pytest.approx(2.0, abs=0.1)
    assert probe_banner(traffic_banner_silent).has_audio is False


# ---------- пауза и картинка ----------


def test_the_pause_is_as_long_as_the_banner_video_not_as_the_slider(synthetic_video, traffic_banner, tmp_path):
    output = tmp_path / "pause.mp4"
    timeline = render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=traffic_banner, banner_count=1, banner_duration=4.5)
    assert timeline.banner_duration == pytest.approx(2.0, abs=0.04)  # 6 с + одна пауза в 2 с, а не в 4.5
    assert flat(timeline.windows) == pytest.approx([3.0, 5.0], abs=0.04)
    assert probe(output).duration == pytest.approx(8.0, abs=0.15)


def test_video_banner_plays_from_its_first_frame_inside_the_pause_only(synthetic_video, traffic_banner, tmp_path):
    output = tmp_path / "play.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=traffic_banner, banner_count=1)  # пауза 3–5 с
    assert rgb_at(output, 3.25, (540, 960), tmp_path) == pytest.approx(RED, abs=40)
    assert rgb_at(output, 4.25, (540, 960), tmp_path) == pytest.approx(BLUE, abs=40)
    for t in (1.0, 2.8, 5.3, 7.0):  # до паузы и после неё баннера нет: в центре серая картинка ролика
        pixel = rgb_at(output, t, (540, 960), tmp_path)
        assert max(abs(a - b) for a, b in zip(pixel, RED)) > 60, f"красный баннер в t={t}"
        assert max(abs(a - b) for a, b in zip(pixel, BLUE)) > 60, f"синий баннер в t={t}"


def test_main_video_stands_still_while_the_banner_video_plays(synthetic_video, traffic_banner, tmp_path):
    output = tmp_path / "still.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=traffic_banner, banner_count=1)
    corner = (60, 960)  # левее баннера (он 918 px шириной по центру), на самой картинке
    assert abs(luma_at(output, 3.2, corner, tmp_path) - luma_at(output, 4.8, corner, tmp_path)) <= 3  # кадр заморожен
    assert luma_at(output, 5.6, corner, tmp_path) > luma_at(output, 4.8, corner, tmp_path) + 10  # после паузы видео побежало


def test_every_pause_restarts_the_banner_video(synthetic_video, traffic_banner, tmp_path):
    output = tmp_path / "restart.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=9.0, banner=traffic_banner, banner_count=2)  # паузы 3–5 и 8–10 с
    assert center(output, 3.25, tmp_path) == pytest.approx(RED, abs=40)
    assert center(output, 8.25, tmp_path) == pytest.approx(RED, abs=40)  # второй показ начался с начала, а не с «середины»
    assert center(output, 9.25, tmp_path) == pytest.approx(BLUE, abs=40)
    assert probe(output).duration == pytest.approx(13.0, abs=0.15)


def test_gif_banner_restarts_in_every_window_too(synthetic_video, tmp_path):
    gif = make_gif(tmp_path / "loop.gif", [RED, BLUE], frame_ms=500)  # круг 1 с
    output = tmp_path / "gif.mp4"
    timeline = render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=gif, banner_count=2, banner_duration=1.5)
    assert flat(timeline.windows) == pytest.approx([2.0, 3.5, 5.5, 7.0], abs=0.04)
    assert center(output, 2.1, tmp_path) == pytest.approx(RED, abs=30)
    assert center(output, 5.6, tmp_path) == pytest.approx(RED, abs=30)  # непрерывная петля показала бы здесь синий (5.6 mod 1 = 0.6)
    assert center(output, 6.1, tmp_path) == pytest.approx(BLUE, abs=30)
    assert center(output, 6.6, tmp_path) == pytest.approx(RED, abs=30)  # окно длиннее круга GIF: анимация пошла по второму кругу


# ---------- звук баннера ----------


def test_banner_sound_fills_the_pause_and_nothing_else(synthetic_video, traffic_banner, tmp_path):
    output = tmp_path / "sound.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=traffic_banner, banner_count=1)
    assert audio_frequency(output, 3.2, 1.5, tmp_path) == pytest.approx(TONE_HZ, abs=60)  # в паузе слышен только звук баннера
    before = audio_frequency(output, 1.0, 1.5, tmp_path)
    assert abs(before - TONE_HZ) > 150  # а до неё — звук ролика (чирп), не баннера
    assert volume_db(output, 5.2, 1.0)[0] > -40  # после паузы звук ролика вернулся


def test_banner_sound_can_be_turned_off(synthetic_video, traffic_banner, tmp_path):
    output = tmp_path / "mute.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=traffic_banner, banner_count=1, banner_sound=False)
    assert volume_db(output, 3.2, 1.5)[1] < -60  # пауза молчит
    assert rgb_at(output, 3.25, (540, 960), tmp_path) == pytest.approx(RED, abs=40)  # а картинка баннера на месте


def test_silent_video_banner_keeps_the_pause_silent(synthetic_video, traffic_banner_silent, tmp_path):
    output = tmp_path / "quiet.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=traffic_banner_silent, banner_count=1)
    assert volume_db(output, 3.2, 1.5)[1] < -60


def test_overlay_mode_mixes_banner_sound_over_the_running_video(synthetic_video, traffic_banner, tmp_path):
    output = tmp_path / "overlay.mp4"
    timeline = render(
        synthetic_video, output, start=CLIP_START, seconds=6.0, banner=traffic_banner,
        banner_mode=BannerMode.OVERLAY, banner_count=1,
    )  # fmt: skip
    assert probe(output).duration == pytest.approx(6.0, abs=0.15)  # видео не останавливается
    assert flat(timeline.windows) == pytest.approx([3.0, 5.0], abs=0.04)
    assert rgb_at(output, 3.25, (540, 960), tmp_path) == pytest.approx(RED, abs=40)
    corner = (60, 960)
    assert luma_at(output, 4.8, corner, tmp_path) > luma_at(output, 3.2, corner, tmp_path) + 10  # ролик продолжает идти
    assert volume_db(output, 3.2, 1.5)[1] > -20  # слышно и баннер, и ролик
