"""Планирование нарезки: окна клипов и таймлайн баннеров.

Чистая математика без FFmpeg. Ожидаемые значения посчитаны вручную по формулам спеки:
p_i = i·T/(K+1),  S_i = p_{i+1} + i·D,  E_i = S_i + D,  итог = T + K·D.
"""
import pytest

from app.config import FPS
from app.models import BannerMode, SlicingMode
from app.services.video_processor import compute_clip_schedule, plan_banner_timeline


def flat(windows):
    return [edge for window in windows for edge in window]


# ---------- таймлайн баннеров ----------


def test_freeze_timeline_matches_spec_numbers():
    tl = plan_banner_timeline(9, 2, 2, BannerMode.FREEZE)
    assert tl.cuts == pytest.approx((0, 3, 6, 9))
    assert tl.freeze_points == pytest.approx((3, 6))
    assert flat(tl.windows) == pytest.approx([3, 5, 8, 10])
    assert tl.total_duration == pytest.approx(13)


def test_freeze_timeline_matches_ui_example_from_spec():
    # «30 сек + 2 паузы по 2.5 сек = 35.0 сек. Точки показа баннера: 10.0 с, 20.0 с»
    tl = plan_banner_timeline(30, 2, 2.5, BannerMode.FREEZE)
    assert tl.freeze_points == pytest.approx((10, 20))
    assert flat(tl.windows) == pytest.approx([10, 12.5, 22.5, 25])
    assert tl.total_duration == pytest.approx(35)


def test_overlay_timeline_does_not_extend_the_clip():
    tl = plan_banner_timeline(9, 2, 2, BannerMode.OVERLAY)
    assert flat(tl.windows) == pytest.approx([3, 5, 6, 8])
    assert tl.total_duration == pytest.approx(9)


def test_overlay_window_is_cut_at_the_end_of_the_clip():
    # пауза 5 с, а после второй точки до конца клипа только 3 с
    tl = plan_banner_timeline(9, 2, 5, BannerMode.OVERLAY)
    assert flat(tl.windows) == pytest.approx([3, 8, 6, 9])


def test_without_banners_the_clip_is_untouched():
    tl = plan_banner_timeline(12, 0, 2.5, BannerMode.FREEZE)
    assert tl.cuts == pytest.approx((0, 12))
    assert tl.windows == ()
    assert tl.freeze_points == ()
    assert tl.total_duration == pytest.approx(12)


def test_cut_points_and_pause_are_snapped_to_the_frame_grid():
    # 7 с на 4 сегмента: 1.75 с = 52.5 кадра — без привязки разрез попал бы между кадрами;
    # пауза 2.33 с → 70 кадров = 2.3333… с
    tl = plan_banner_timeline(7, 3, 2.33, BannerMode.FREEZE)
    for value in (*tl.cuts, tl.banner_duration):
        assert value * FPS == pytest.approx(round(value * FPS), abs=1e-9)
    assert tl.banner_duration == pytest.approx(70 / FPS)
    assert tl.total_duration == pytest.approx(7 + 3 * 70 / FPS)


# ---------- окна клипов в источнике ----------


def test_evenly_spaced_starts_are_spread_over_the_video():
    schedule = compute_clip_schedule(20, 0, 2, 9, SlicingMode.EVENLY_SPACED)
    assert schedule.starts == pytest.approx([0, 11])
    assert schedule.clip_duration == pytest.approx(9)
    assert schedule.warnings == []


def test_single_clip_starts_at_the_offset():
    schedule = compute_clip_schedule(100, 7, 1, 20, SlicingMode.EVENLY_SPACED)
    assert schedule.starts == pytest.approx([7])


def test_sequential_starts_follow_each_other():
    schedule = compute_clip_schedule(100, 10, 3, 20, SlicingMode.SEQUENTIAL)
    assert schedule.starts == pytest.approx([10, 30, 50])
    assert schedule.slicing_mode == SlicingMode.SEQUENTIAL
    assert schedule.warnings == []


def test_sequential_overflow_switches_to_even_spacing_and_warns():
    # 3 клипа по 30 с подряд — 90 с, а в видео только 50 с
    schedule = compute_clip_schedule(50, 0, 3, 30, SlicingMode.SEQUENTIAL)
    assert schedule.slicing_mode == SlicingMode.EVENLY_SPACED
    assert schedule.starts == pytest.approx([0, 10, 20])
    assert any("равномерно" in warning for warning in schedule.warnings)


def test_clip_longer_than_the_source_is_clamped_with_a_warning():
    schedule = compute_clip_schedule(20, 5, 1, 30, SlicingMode.EVENLY_SPACED)
    assert schedule.clip_duration == pytest.approx(15)
    assert schedule.starts == pytest.approx([5])
    assert schedule.warnings


def test_overlapping_clips_are_reported():
    schedule = compute_clip_schedule(31, 0, 5, 30, SlicingMode.EVENLY_SPACED)
    assert schedule.starts == pytest.approx([0, 0.25, 0.5, 0.75, 1])
    assert any("перекрыва" in warning for warning in schedule.warnings)


def test_offset_that_leaves_no_video_is_an_error():
    with pytest.raises(ValueError, match="[Сс]мещение"):
        compute_clip_schedule(20, 19.5, 1, 10, SlicingMode.EVENLY_SPACED)


@pytest.mark.parametrize(
    "length, offset, count, duration, mode",
    [
        (600, 0, 10, 30, SlicingMode.EVENLY_SPACED),
        (600, 25, 7, 45, SlicingMode.SEQUENTIAL),
        (100, 0, 50, 10, SlicingMode.SEQUENTIAL),
        (31, 0, 5, 30, SlicingMode.EVENLY_SPACED),
        (20, 5, 3, 30, SlicingMode.SEQUENTIAL),
    ],
)
def test_every_clip_fits_inside_the_source(length, offset, count, duration, mode):
    schedule = compute_clip_schedule(length, offset, count, duration, mode)
    assert len(schedule.starts) == count
    for start in schedule.starts:
        assert start >= offset - 1e-9
        assert start + schedule.clip_duration <= length + 1e-6


# ---------- обрезка конца (end_offset) ----------


def test_end_offset_shortens_the_usable_range_from_the_end():
    schedule = compute_clip_schedule(20, 2, 2, 5, SlicingMode.EVENLY_SPACED, end_offset=3)
    assert schedule.starts == pytest.approx([2, 12])  # последний клип заканчивается на 17 = 20 − 3
    assert schedule.warnings == []


def test_sequential_fits_exactly_into_the_range_left_by_end_offset():
    schedule = compute_clip_schedule(20, 0, 3, 5, SlicingMode.SEQUENTIAL, end_offset=5)
    assert schedule.slicing_mode is SlicingMode.SEQUENTIAL
    assert schedule.starts == pytest.approx([0, 5, 10])


def test_sequential_falls_back_when_end_offset_leaves_too_little():
    schedule = compute_clip_schedule(20, 0, 4, 5, SlicingMode.SEQUENTIAL, end_offset=5)  # 4×5=20 > 15
    assert schedule.slicing_mode is SlicingMode.EVENLY_SPACED
    assert schedule.starts[-1] + schedule.clip_duration == pytest.approx(15)
    assert any("распределены равномерно" in warning for warning in schedule.warnings)


def test_single_clip_is_not_pushed_past_the_trimmed_end():
    schedule = compute_clip_schedule(20, 4, 1, 30, SlicingMode.EVENLY_SPACED, end_offset=6)
    assert schedule.starts == pytest.approx([4])
    assert schedule.clip_duration == pytest.approx(10)  # 20 − 4 − 6


def test_both_offsets_eating_the_video_is_an_error_in_russian():
    with pytest.raises(ValueError, match="Смещение"):
        compute_clip_schedule(10, 6, 1, 3, SlicingMode.SEQUENTIAL, end_offset=4)
