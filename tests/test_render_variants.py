"""Варианты рендера: режимы кадра, баннер поверх идущего видео, SFX, источники без звука и «кривые» источники.

Как и в test_engine.py, проверяется готовый ролик (ffprobe/ffmpeg/Pillow), а не текст графа FFmpeg.
"""
import subprocess
from types import SimpleNamespace

import pytest

from app.models import BannerMode, BannerPosition, FrameMode
from app.services import video_processor
from app.services.banner_manager import ensure_sfx
from app.services.video_processor import MediaError, RenderOptions, probe_media, render_clip
from tests.conftest import CLIP_START, PINK
from tests.media_utils import (
    bright_rows,
    bright_span,
    frame_png,
    luma_at,
    make_gif,
    make_video,
    mean_abs_diff,
    probe,
    rgb_at,
    volume_db,
)

RED, BLUE = (255, 0, 0), (0, 0, 255)


def render(source, output, *, start=0.0, seconds=3.0, banner=None, sfx=None, **options):
    """Рендерит клип; `options` — поля RenderOptions. Возвращает таймлайн."""
    return render_clip(source, output, start, seconds, RenderOptions(**options), info=probe_media(source), banner=banner, sfx=sfx)


def is_pink(pixel, tolerance=30):
    return max(abs(a - b) for a, b in zip(pixel, PINK)) < tolerance


# ---------- режимы кадра ----------


@pytest.mark.parametrize(
    ("mode", "size", "edge", "edge_minus_middle"),
    [
        (FrameMode.BLUR_PAD, (1080, 1920), (540, 100), (-55, -30)),  # за краями картинки — затемнённый размытый фон
        (FrameMode.CENTER_CROP, (1080, 1920), (540, 100), (-6, 6)),  # картинка на весь кадр: края как центр
        (FrameMode.ORIGINAL, (1920, 1080), (50, 50), (-6, 6)),
    ],
)
def test_each_frame_mode_has_its_own_size_and_composition(synthetic_video, tmp_path, mode, size, edge, edge_minus_middle):
    output = tmp_path / "mode.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=3.0, frame_mode=mode, banner_count=0)
    info = probe(output)
    assert (info.width, info.height) == size
    middle = luma_at(output, 1.0, (size[0] // 2, size[1] // 2), tmp_path)
    low, high = edge_minus_middle
    assert low <= luma_at(output, 1.0, edge, tmp_path) - middle <= high


@pytest.mark.parametrize(
    ("mode", "center"),
    [
        (FrameMode.BLUR_PAD, (540, 960)),
        (FrameMode.CENTER_CROP, (540, 960)),
        (FrameMode.ORIGINAL, (960, 540)),
    ],
)
def test_a_source_with_non_square_pixels_is_not_squeezed(mode, center, tmp_path):
    """Анаморфный ролик (хранится 960×720, но пиксель 4:3 — на экране это 16:9) не должен сплющиваться.

    В кадре белый квадрат 200×200 «на экране» (хранится как 150×200). В готовом клипе он обязан остаться квадратом.
    """
    source = make_video(
        tmp_path / "anamorphic.mp4", size="960x720", seconds=3, audio=False,
        video_filter="geq=lum='if(between(X,W/2-75,W/2+75)*between(Y,H/2-100,H/2+100),255,60)':cb=128:cr=128,setsar=4/3",
    )  # fmt: skip
    pixel_shape = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=sample_aspect_ratio", "-of", "csv=p=0", str(source)],
        capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    assert pixel_shape == "4:3", "тестовый ролик должен быть анаморфным"

    output = tmp_path / "clip.mp4"
    render(source, output, seconds=2.0, frame_mode=mode, banner_count=0)

    across = bright_span(output, 1.0, center[1], tmp_path)
    down = bright_rows(output, 1.0, center[0], tmp_path)
    assert across is not None and down is not None, "белый квадрат пропал из кадра"
    width, height = across[1] - across[0] + 1, down[1] - down[0] + 1
    assert width / height == pytest.approx(1.0, abs=0.05), f"квадрат стал {width}×{height}"


def test_render_hands_the_cpu_budget_to_ffmpeg(synthetic_video, tmp_path, monkeypatch):
    seen = []
    real_run = video_processor._run_ffmpeg
    monkeypatch.setattr(video_processor, "ffmpeg_threads", lambda: 1)
    monkeypatch.setattr(video_processor, "_run_ffmpeg", lambda command, total, progress: (seen.append(command), real_run(command, total, progress))[1])

    render(synthetic_video, tmp_path / "budget.mp4", seconds=1.0, banner_count=0)

    assert len(seen) == 1 and "-filter_threads" in seen[0]


def test_original_mode_pads_a_4_3_source_with_black_bars(quad_video, tmp_path):
    output = tmp_path / "pad.mp4"
    render(quad_video, output, start=2.0, seconds=2.0, frame_mode=FrameMode.ORIGINAL, banner_count=0)
    assert luma_at(output, 0.5, (60, 540), tmp_path) < 10  # слева чёрная полоса
    assert luma_at(output, 0.5, (1860, 540), tmp_path) < 10  # справа тоже
    assert luma_at(output, 0.5, (960, 540), tmp_path) > 60  # в середине — картинка


def test_center_crop_of_a_tall_source_keeps_its_vertical_center(tall_video, tmp_path):
    """Источник уже, чем 9:16: масштаб по ширине, лишнее срезается сверху и снизу поровну."""
    output = tmp_path / "tall.mp4"
    render(tall_video, output, seconds=2.0, frame_mode=FrameMode.CENTER_CROP, banner_count=0)
    rows = bright_rows(output, 1.0, 540, tmp_path)
    assert rows is not None
    assert (rows[0] + rows[1]) / 2 == pytest.approx(960, abs=10)  # полоса была ровно посередине высоты


@pytest.mark.parametrize(
    ("face_x", "stripe_center"),
    [
        (None, None),  # лица нет → окно по центру, полоса на 70% ширины в кадр не попадает
        (0.0, None),  # лицо у левого края → окно прижато к левому краю
        (0.7, 540),  # окно центрируется на лице → полоса ровно посередине кадра
        (0.99, 56),  # у правого края окно упирается в границу картинки
    ],
)
def test_center_crop_follows_the_detected_face(stripe_video, tmp_path, monkeypatch, face_x, stripe_center):
    asked = []

    def fake_detector(source, clip_start, clip_duration):
        asked.append((source, clip_start, clip_duration))
        return face_x

    monkeypatch.setattr(video_processor, "detect_face_center_x", fake_detector)
    output = tmp_path / "face.mp4"
    render(stripe_video, output, start=1.0, seconds=2.0, frame_mode=FrameMode.CENTER_CROP, banner_count=0)

    assert asked == [(stripe_video, 1.0, 2.0)]  # лицо ищут именно в окне клипа
    span = bright_span(output, 1.0, 960, tmp_path)
    if stripe_center is None:
        assert span is None
    else:
        assert span is not None
        assert (span[0] + span[1]) / 2 == pytest.approx(stripe_center, abs=8)


# ---------- положение и размер баннера ----------


@pytest.mark.parametrize(
    ("position", "inside_y", "outside_y"),
    [(BannerPosition.TOP, 190, 170), (BannerPosition.BOTTOM, 1670, 1690)],  # спека: верх y=180, низ y=H-h-240
)
def test_banner_position_top_and_bottom(synthetic_video, solid_banner, tmp_path, position, inside_y, outside_y):
    output = tmp_path / "pos.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=3.0, banner=solid_banner, banner_count=1, banner_duration=1.0, banner_position=position)
    assert is_pink(rgb_at(output, 2.0, (540, inside_y), tmp_path))
    assert not is_pink(rgb_at(output, 2.0, (540, outside_y), tmp_path))


def test_banner_scale_sets_its_width(synthetic_video, solid_banner, tmp_path):
    output = tmp_path / "scale.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=3.0, banner=solid_banner, banner_count=1, banner_duration=1.0, banner_scale=50)
    # 50% от 1080 = 540 px: баннер занимает x от 270 до 810
    assert is_pink(rgb_at(output, 2.0, (300, 960), tmp_path))
    assert not is_pink(rgb_at(output, 2.0, (250, 960), tmp_path))


def test_original_mode_sizes_the_banner_by_the_short_side_of_the_frame(synthetic_video, solid_banner, tmp_path):
    """85% от короткой стороны 1080 = 918 px (а не 85% от 1920): баннер на 1920x1080 занимает x от 501 до 1419."""
    output = tmp_path / "wide.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=3.0, banner=solid_banner, frame_mode=FrameMode.ORIGINAL, banner_count=1, banner_duration=1.0)
    for x in (520, 960, 1400):
        assert is_pink(rgb_at(output, 2.0, (x, 540), tmp_path)), f"баннера нет в x={x}"
    for x in (400, 1500):
        assert not is_pink(rgb_at(output, 2.0, (x, 540), tmp_path)), f"баннер шире, чем должен, в x={x}"


def test_animated_gif_banner_keeps_animating_for_the_whole_window(synthetic_video, tmp_path):
    gif = make_gif(tmp_path / "two_colors.gif", [RED, BLUE], frame_ms=500)  # петля 1 с: красный 0–0.5 с, синий 0.5–1 с
    output = tmp_path / "gif.mp4"
    render(synthetic_video, output, start=CLIP_START, seconds=6.0, banner=gif, banner_count=1, banner_duration=2.0)  # окно 3–5 с
    assert probe(output).duration == pytest.approx(8.0, abs=0.15)
    assert rgb_at(output, 3.25, (540, 960), tmp_path) == pytest.approx(RED, abs=30)
    assert rgb_at(output, 3.75, (540, 960), tmp_path) == pytest.approx(BLUE, abs=30)
    assert rgb_at(output, 4.25, (540, 960), tmp_path) == pytest.approx(RED, abs=30)  # без цикла GIF застыл бы на последнем кадре


# ---------- баннер поверх идущего видео (overlay) ----------


@pytest.fixture(scope="module")
def overlay_clip(synthetic_video, solid_banner, tmp_path_factory):
    output = tmp_path_factory.mktemp("overlay") / "clip.mp4"
    timeline = render(
        synthetic_video, output, start=CLIP_START, seconds=9.0, banner=solid_banner,
        banner_mode=BannerMode.OVERLAY, banner_count=2, banner_duration=2.0,
    )  # fmt: skip
    return SimpleNamespace(path=output, timeline=timeline, scratch=tmp_path_factory.mktemp("overlay_frames"))


def test_overlay_mode_keeps_the_clip_length(overlay_clip):
    assert probe(overlay_clip.path).duration == pytest.approx(9.0, abs=0.15)
    assert list(overlay_clip.timeline.windows) == [pytest.approx((3, 5)), pytest.approx((6, 8))]


def test_overlay_mode_neither_stops_nor_dims_the_picture(overlay_clip, synthetic_video):
    """Яркость в t ролика = яркость источника в (старт + t) и до, и во время показа баннера."""
    path, scratch = overlay_clip.path, overlay_clip.scratch
    for t_out in (1.0, 4.0, 7.0):  # до окон / внутри первого / внутри второго
        got = luma_at(path, t_out, (540, 700), scratch)
        want = luma_at(synthetic_video, CLIP_START + t_out, (640, 360), scratch)
        assert got == pytest.approx(want, abs=4), f"картинка в {t_out} с"
    assert mean_abs_diff(frame_png(path, 3.4, scratch / "o1.png"), frame_png(path, 4.8, scratch / "o2.png")) > 8


def test_overlay_mode_does_not_pause_the_sound(overlay_clip):
    assert volume_db(overlay_clip.path, 3.2, 1.6)[0] > -30
    assert volume_db(overlay_clip.path, 6.2, 1.6)[0] > -30


def test_overlay_banner_is_visible_only_inside_its_windows(overlay_clip):
    path, scratch = overlay_clip.path, overlay_clip.scratch
    for t in (4.0, 7.0):
        assert is_pink(rgb_at(path, t, (540, 960), scratch)), f"баннера нет в {t} с"
    for t in (1.0, 5.5, 8.5):
        assert not is_pink(rgb_at(path, t, (540, 960), scratch)), f"баннер виден вне окна в {t} с"


# ---------- звуковой эффект ----------


@pytest.fixture(scope="module")
def sfx_clip(synthetic_video, solid_banner, tmp_path_factory):
    sound = ensure_sfx(tmp_path_factory.mktemp("sfx"))
    output = tmp_path_factory.mktemp("sfx_clip") / "clip.mp4"
    render(
        synthetic_video, output, start=CLIP_START, seconds=9.0, banner=solid_banner, sfx=sound,
        banner_count=2, banner_duration=2.0, enable_sfx=True,
    )  # fmt: skip
    return output


def test_sfx_pop_sounds_at_the_start_of_each_pause_and_only_there(sfx_clip):
    for start in (3.0, 8.0):  # начала пауз на таймлайне ролика
        assert volume_db(sfx_clip, start + 0.05, 0.15)[1] > -35, f"хлопка нет в {start} с"
        assert volume_db(sfx_clip, start + 0.4, 1.5)[1] < -60, f"после хлопка пауза должна молчать ({start} с)"


def test_sfx_does_not_make_the_original_sound_quieter(sfx_clip, freeze_clip):
    """Тот же клип без SFX (freeze_clip) звучит так же: микс не занижает исходный звук."""
    with_sfx = volume_db(sfx_clip, 0.2, 2.6)[0]
    assert with_sfx == pytest.approx(volume_db(freeze_clip.path, 0.2, 2.6)[0], abs=0.5)


def test_sfx_is_the_only_sound_on_a_source_without_audio(silent_video, solid_banner, tmp_path):
    output = tmp_path / "pops.mp4"
    sound = ensure_sfx(tmp_path / "sfx")
    render(silent_video, output, seconds=6.0, banner=solid_banner, sfx=sound, banner_count=2, banner_duration=2.0, enable_sfx=True)
    # границы 0, 2, 4, 6 → паузы на ролике (2–4) и (6–8) при длине 10 с
    for start in (2.0, 6.0):
        assert volume_db(output, start + 0.05, 0.15)[1] > -35, f"хлопка нет в {start} с"
    for start, length in [(0.0, 1.9), (2.4, 1.5), (4.0, 1.9), (6.4, 1.5), (8.0, 1.9)]:
        assert volume_db(output, start, length)[1] < -60, f"лишний звук в {start}–{start + length} с"


def test_sfx_with_a_single_banner(silent_video, solid_banner, tmp_path):
    output = tmp_path / "one_pop.mp4"
    sound = ensure_sfx(tmp_path / "sfx")
    render(silent_video, output, seconds=4.0, banner=solid_banner, sfx=sound, banner_count=1, banner_duration=2.0, enable_sfx=True)  # пауза 2–4 с
    assert volume_db(output, 2.05, 0.15)[1] > -35
    assert volume_db(output, 0.0, 1.9)[1] < -60
    assert volume_db(output, 2.4, 1.5)[1] < -60


def test_enabled_sfx_needs_a_sound_file(synthetic_video, solid_banner, tmp_path):
    with pytest.raises(MediaError, match="звук"):
        render(synthetic_video, tmp_path / "x.mp4", banner=solid_banner, enable_sfx=True)
    assert not (tmp_path / "x.mp4").exists()


# ---------- источники без звука и со звуком короче видео ----------


@pytest.mark.parametrize(
    ("banner_mode", "banner_count", "expected_seconds"),
    [(BannerMode.FREEZE, 2, 10.0), (BannerMode.OVERLAY, 2, 6.0), (BannerMode.FREEZE, 0, 6.0)],
    ids=["freeze", "overlay", "plain-cut"],
)
def test_source_without_audio_still_gets_a_silent_aac_track(silent_video, solid_banner, tmp_path, banner_mode, banner_count, expected_seconds):
    output = tmp_path / "silent.mp4"
    render(silent_video, output, seconds=6.0, banner=solid_banner, banner_mode=banner_mode, banner_count=banner_count, banner_duration=2.0)
    info = probe(output)
    assert info.acodec == "aac"
    assert info.duration == pytest.approx(expected_seconds, abs=0.15)
    assert volume_db(output, 0.0, expected_seconds - 0.3)[1] < -60


def test_freeze_works_on_a_source_without_audio(silent_video, solid_banner, tmp_path):
    output = tmp_path / "silent_freeze.mp4"
    render(silent_video, output, seconds=6.0, banner=solid_banner, banner_count=2, banner_duration=2.0)  # пауза 2–4 с
    frozen = mean_abs_diff(frame_png(output, 2.4, tmp_path / "f1.png"), frame_png(output, 3.8, tmp_path / "f2.png"))
    playing = mean_abs_diff(frame_png(output, 0.4, tmp_path / "p1.png"), frame_png(output, 1.6, tmp_path / "p2.png"))
    assert frozen < 1.5
    assert playing > 5


def test_audio_shorter_than_video_is_padded_with_silence(short_audio_video, solid_banner, tmp_path):
    """Звук источника кончается на 3-й секунде; ролик всё равно получает цельную дорожку нужной длины."""
    output = tmp_path / "short_audio.mp4"
    render(short_audio_video, output, seconds=6.0, banner=solid_banner, banner_count=2, banner_duration=2.0)
    info = probe(output)
    assert info.acodec == "aac"
    assert info.duration == pytest.approx(10.0, abs=0.15)
    assert info.aduration == pytest.approx(info.vduration, abs=0.15)
    assert volume_db(output, 0.2, 1.6)[0] > -30  # звук, пока он есть в источнике
    assert volume_db(output, 8.0, 1.9)[1] < -60  # последний сегмент — источник уже молчит


# ---------- «кривые» источники во всех режимах кадра ----------


@pytest.mark.parametrize("source_name", ["portrait_video", "quad_video", "odd_video"])
@pytest.mark.parametrize(
    ("mode", "size"),
    [(FrameMode.BLUR_PAD, (1080, 1920)), (FrameMode.CENTER_CROP, (1080, 1920)), (FrameMode.ORIGINAL, (1920, 1080))],
)
def test_unusual_sources_render_in_every_frame_mode(source_name, mode, size, request, solid_banner, tmp_path):
    source = request.getfixturevalue(source_name)  # 720x1280, 642x482 и нечётный 641x481
    output = tmp_path / "unusual.mp4"
    render(source, output, seconds=3.0, banner=solid_banner, frame_mode=mode, banner_count=1, banner_duration=1.0)  # пауза 1.5–2.5 с
    info = probe(output)
    assert (info.width, info.height) == size
    assert (info.vcodec, info.acodec) == ("h264", "aac")
    assert info.duration == pytest.approx(4.0, abs=0.15)
    assert is_pink(rgb_at(output, 2.0, (size[0] // 2, size[1] // 2), tmp_path))
