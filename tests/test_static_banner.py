"""Постоянный баннер: лежит в поле Blur-Pad (или у края кадра) весь клип, не темнеет при остановке и не мешает всплывающему.

Источник — 16:9 (1280×720), в кадре 1080×1920 картинка занимает 608 px по высоте, поля сверху и снизу по 656 px.
Баннер-«карточка» PINK 920×360 вписывается в ширину 994 px (92 %) и стоит по центру поля: верхнее поле — y 134…522.
"""
import pytest
from PIL import Image

from app.models import BannerMode, BannerPosition, FrameMode, StaticPosition
from app.services.video_processor import RenderOptions, blur_pad_margin, plan_static_layout, probe_media, render_clip
from tests.conftest import CLIP_START
from tests.media_utils import make_color_video, probe, rgb_at
from tests.test_render_variants import BLUE, RED, is_pink

TOP_CENTER = (540, 328)  # середина верхнего поля
BOTTOM_CENTER = (540, 1592)  # середина нижнего поля


@pytest.fixture(scope="module")
def blue_banner(media_dir):
    path = media_dir / "blue_banner.png"
    Image.new("RGBA", (920, 360), (*BLUE, 255)).save(path)
    return path


@pytest.fixture(scope="module")
def traffic_strip(media_dir):
    """Видео для постоянного баннера: 1 с красного, 1 с синего, без звука."""
    return make_color_video(media_dir / "traffic_strip.mp4", [("red", 1.0), ("blue", 1.0)])


def static_render(source, output, static, *, banner=None, seconds=3.0, **options):
    """Рендерит клип с постоянным баннером `static` (и всплывающим `banner`, если он нужен); `options` — поля RenderOptions."""
    opts = RenderOptions(**options)
    return render_clip(source, output, CLIP_START, seconds, opts, info=probe_media(source), banner=banner, static=static)


# ---------- геометрия ----------


@pytest.mark.parametrize(
    ("size", "margin"),
    [((1280, 720), 656), ((1024, 768), 555), ((1080, 1080), 420), ((720, 1280), 0), ((540, 1170), 0)],
)
def test_blur_pad_margin_is_what_is_left_around_the_picture(size, margin):
    assert blur_pad_margin(*size) == pytest.approx(margin, abs=1)


def test_a_wide_margin_hosts_the_banner_and_a_narrow_one_does_not():
    wide = plan_static_layout(FrameMode.BLUR_PAD, 1280, 720, StaticPosition.TOP, 92)
    assert wide.in_margin is True and (wide.box_w, wide.box_h) == (994, 552)
    assert plan_static_layout(FrameMode.BLUR_PAD, 720, 1280, StaticPosition.TOP, 92).in_margin is False  # полей нет
    assert plan_static_layout(FrameMode.CENTER_CROP, 1280, 720, StaticPosition.TOP, 92).in_margin is False
    assert plan_static_layout(FrameMode.ORIGINAL, 1280, 720, StaticPosition.TOP, 92).in_margin is False


# ---------- где стоит ----------


def test_static_banner_sits_in_the_middle_of_the_top_margin(synthetic_video, solid_banner, tmp_path):
    output = tmp_path / "top.mp4"
    static_render(synthetic_video, output, solid_banner, banner_count=0)
    for point in (TOP_CENTER, (540, 150), (540, 500), (60, 328)):
        assert is_pink(rgb_at(output, 1.0, point, tmp_path)), f"нет баннера в {point}"
    for point in ((540, 90), (540, 560), (20, 328), BOTTOM_CENTER):
        assert not is_pink(rgb_at(output, 1.0, point, tmp_path)), f"баннер вылез в {point}"


def test_static_banner_can_move_to_the_bottom_margin(synthetic_video, solid_banner, tmp_path):
    output = tmp_path / "bottom.mp4"
    static_render(synthetic_video, output, solid_banner, banner_count=0, static_position=StaticPosition.BOTTOM)
    assert is_pink(rgb_at(output, 1.0, BOTTOM_CENTER, tmp_path))
    assert not is_pink(rgb_at(output, 1.0, TOP_CENTER, tmp_path))


def test_static_scale_changes_its_width(synthetic_video, solid_banner, tmp_path):
    output = tmp_path / "small.mp4"
    static_render(synthetic_video, output, solid_banner, banner_count=0, static_scale=50)  # 540 px: x от 270 до 810
    assert is_pink(rgb_at(output, 1.0, (300, 328), tmp_path))
    assert not is_pink(rgb_at(output, 1.0, (200, 328), tmp_path))


def test_without_margins_the_static_banner_lies_over_the_top_edge(synthetic_video, solid_banner, tmp_path):
    output = tmp_path / "edge.mp4"
    static_render(synthetic_video, output, solid_banner, banner_count=0, frame_mode=FrameMode.CENTER_CROP)
    assert is_pink(rgb_at(output, 1.0, (540, 271), tmp_path))  # отступ 77 px от края + половина высоты 389
    assert not is_pink(rgb_at(output, 1.0, (540, 600), tmp_path))
    assert probe(output).height == 1920


# ---------- поведение во времени ----------


def test_static_banner_stays_bright_while_the_picture_is_dimmed_for_a_pause(synthetic_video, solid_banner, blue_banner, tmp_path):
    output = tmp_path / "bright.mp4"
    # пауза 3–5 с: во время неё всплывающий (синий) в центре, а постоянный (розовый) в поле остаётся ярким
    static_render(synthetic_video, output, solid_banner, banner=blue_banner, seconds=6.0, banner_count=1, banner_duration=2.0)
    for t in (1.0, 3.5, 5.5):
        assert is_pink(rgb_at(output, t, TOP_CENTER, tmp_path)), f"розовая полоса пропала или потемнела в t={t}"
    assert rgb_at(output, 3.5, (540, 960), tmp_path) == pytest.approx(BLUE, abs=30)  # всплывающий — в паузе
    assert rgb_at(output, 1.0, (540, 960), tmp_path) != pytest.approx(BLUE, abs=30)


def test_popup_lies_over_the_static_banner_where_they_meet(synthetic_video, solid_banner, blue_banner, tmp_path):
    output = tmp_path / "order.mp4"
    static_render(
        synthetic_video, output, solid_banner, banner=blue_banner, seconds=6.0,
        banner_count=1, banner_duration=2.0, banner_position=BannerPosition.TOP,
    )  # fmt: skip
    # всплывающий сверху занимает y 180…569 и перекрывает розовую полосу (134…522)
    assert rgb_at(output, 3.5, (540, 300), tmp_path) == pytest.approx(BLUE, abs=30)
    assert is_pink(rgb_at(output, 1.0, (540, 300), tmp_path))


def test_video_for_the_static_banner_loops_for_the_whole_clip(synthetic_video, traffic_strip, tmp_path):
    output = tmp_path / "loop.mp4"
    static_render(synthetic_video, output, traffic_strip, seconds=6.0, banner_count=0)
    for t, color in ((0.5, RED), (1.5, BLUE), (2.5, RED), (5.5, BLUE)):  # круг 2 с: красный, синий
        assert rgb_at(output, t, TOP_CENTER, tmp_path) == pytest.approx(color, abs=40), f"t={t}"
    assert probe(output).duration == pytest.approx(6.0, abs=0.15)


def test_static_banner_works_in_overlay_mode_too(synthetic_video, solid_banner, blue_banner, tmp_path):
    output = tmp_path / "overlay.mp4"
    static_render(
        synthetic_video, output, solid_banner, banner=blue_banner, seconds=6.0,
        banner_count=1, banner_duration=2.0, banner_mode=BannerMode.OVERLAY,
    )  # fmt: skip
    assert is_pink(rgb_at(output, 1.0, TOP_CENTER, tmp_path))
    assert is_pink(rgb_at(output, 4.0, TOP_CENTER, tmp_path))
    assert rgb_at(output, 4.0, (540, 960), tmp_path) == pytest.approx(BLUE, abs=30)
