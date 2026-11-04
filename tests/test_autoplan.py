"""Автоподбор нарезки: правила по длине ролика и цели («TikTok», «Shorts», универсально)."""
from __future__ import annotations

import pytest

from app.models import BannerMode, ClipSettings, FrameMode, Goal, SlicingMode
from app.services.autoplan import GOALS, SHORTS_LIMIT, clip_count, plan_auto


def plan(duration, goal=Goal.TIKTOK, *, banner=7.5, base=None):
    return plan_auto(duration, goal, base or ClipSettings(banner_id="b.mp4"), banner_seconds=banner)


@pytest.mark.parametrize(
    ("goal", "minutes", "clips", "seconds"),
    [
        (Goal.TIKTOK, 60, 8, 45.0),  # 30 по правилу, но не больше 8
        (Goal.SHORTS, 60, 6, 50.0),
        (Goal.UNIVERSAL, 60, 6, 30.0),
        (Goal.TIKTOK, 10, 5, 45.0),  # по клипу на 2 минуты
        (Goal.SHORTS, 9, 3, 50.0),  # по клипу на 3 минуты
        (Goal.UNIVERSAL, 8, 2, 30.0),  # по клипу на 4 минуты
    ],
)
def test_clip_count_and_length_follow_the_goal(goal, minutes, clips, seconds):
    result = plan(minutes * 60, goal)
    assert (result.settings.num_clips, result.settings.clip_duration) == (clips, seconds)
    assert not result.shortened


@pytest.mark.parametrize("goal", [Goal.TIKTOK, Goal.SHORTS])
def test_tiktok_and_shorts_clips_are_at_least_45_seconds_without_the_banner(goal):
    """Короткие по 20–25 с не держали зрителя: для TikTok и Shorts клип — от 45 с (баннер сверху не в счёт)."""
    assert GOALS[goal].clip_seconds >= 45.0
    for duration in (50, 70, 80, 120, 600, 3600, 5 * 3600):
        assert plan(duration, goal).settings.clip_duration >= 45.0, (goal, duration)
    for duration in (80, 120, 600, 3600, 5 * 3600):  # ролик заведомо больше клипа: длина ровно номинальная
        assert plan(duration, goal).settings.clip_duration == GOALS[goal].clip_seconds  # баннер длины не съедает: он сверху


def test_universal_clips_stay_short():
    assert plan(3600, Goal.UNIVERSAL).settings.clip_duration == 30.0


def test_clip_count_is_at_least_one_and_never_crowds_the_video():
    spec = GOALS[Goal.TIKTOK]
    assert clip_count(10, spec, 5.0) == 1
    assert clip_count(0, spec, 45.0) == 5  # длина неизвестна → середина
    for duration in (60, 100, 300, 1200, 7200):
        count = clip_count(duration, spec, spec.clip_seconds)
        assert count * spec.clip_seconds <= duration  # клипы помещаются в ролик


def test_total_length_with_the_banner_stays_within_shorts_limits():
    for goal in Goal:
        for duration in (30, 120, 900, 3600, 18000):
            result = plan(duration, goal)
            s = result.settings
            assert s.clip_duration + s.banner_count * 7.5 <= SHORTS_LIMIT + 1e-9, (goal, duration)


def test_banner_count_is_reduced_until_the_clip_fits():
    spec_long = plan(3600, Goal.UNIVERSAL, banner=20.0)  # 30 + 2·20 = 70 > 59 → один показ
    assert spec_long.settings.banner_count == 1
    assert spec_long.settings.clip_duration + 20.0 == pytest.approx(50.0)


def test_universal_shows_two_banners_and_blur_pad():
    result = plan(3600, Goal.UNIVERSAL)
    assert result.settings.banner_count == 2 and result.settings.frame_mode is FrameMode.BLUR_PAD


@pytest.mark.parametrize("goal", list(Goal))
def test_auto_always_puts_the_whole_video_in_the_middle_with_blurred_fields(goal):
    """Ошибка из жизни: «на весь экран» растягивал полосу 405×720 из 720p до 1080×1920 — мыло, а человек хотел
    горизонтальное видео по центру с полями сверху и снизу. Авто такого больше не выбирает: вручную — пожалуйста."""
    assert plan(3600, goal).settings.frame_mode is FrameMode.BLUR_PAD


def test_without_a_banner_file_there_are_no_banners():
    result = plan(3600, Goal.UNIVERSAL, banner=None)
    assert result.settings.banner_count == 0


def test_short_video_is_taken_whole():
    result = plan(20, Goal.TIKTOK)
    assert result.shortened and result.settings.num_clips == 1 and result.settings.clip_duration == 20
    shorter = plan(30, Goal.TIKTOK)  # меньше 45 с — клип получается короче обычного, и бот об этом скажет
    assert shorter.shortened and shorter.settings.clip_duration == 30
    almost = plan(46, Goal.TIKTOK)  # почти как клип: берётся целиком, но это не «укороченный» клип
    assert almost.settings.num_clips == 1 and almost.settings.clip_duration == 46 and not almost.shortened


@pytest.mark.parametrize("goal", [Goal.TIKTOK, Goal.SHORTS, Goal.UNIVERSAL])
def test_a_whole_video_with_the_banner_still_fits_the_limit(goal):
    """Источник 52–67 с «целиком» вместе с баннером уходил за 59 с — теперь клип подрезается."""
    for duration in range(30, 91):
        s = plan(duration, goal).settings
        assert s.clip_duration + s.banner_count * 7.5 <= SHORTS_LIMIT + 1e-9, (goal, duration)


def test_the_trimmed_whole_video_is_not_called_shortened():
    result = plan(60, Goal.TIKTOK)
    assert result.settings.clip_duration == 51.0 and not result.shortened  # 59 − 7,5 с баннера, округлено вниз


def test_without_a_banner_the_whole_video_may_be_the_full_limit():
    assert plan(65, Goal.TIKTOK, banner=None).settings.clip_duration == 59.0


def test_tiny_video_gets_a_minimal_clip():
    result = plan(3, Goal.TIKTOK)
    assert result.settings.clip_duration == 5.0 and result.shortened


def test_auto_settings_are_the_documented_ones():
    s = plan(3600).settings
    assert s.slicing_mode is SlicingMode.HIGHLIGHTS and s.auto_trim
    assert (s.start_offset, s.end_offset) == (0.0, 0.0)
    assert s.banner_mode is BannerMode.FREEZE and s.banner_scale == 85 and s.banner_sound and not s.enable_sfx


def test_very_long_sources_are_downloaded_in_lower_quality():
    assert plan(60 * 60).settings.max_height == 1080
    assert plan(91 * 60).settings.max_height == 720


def test_settings_the_plan_does_not_own_are_kept():
    base = ClipSettings(banner_id="mine.mp4", banner_duration=3.0, max_height=360)
    s = plan(3600, base=base).settings
    assert s.banner_id == "mine.mp4" and s.banner_duration == 3.0


def test_every_goal_has_a_label_a_blurb_and_an_emoji():
    assert set(GOALS) == set(Goal)
    for spec in GOALS.values():
        assert spec.emoji and spec.label and spec.blurb
        assert spec.clip_seconds + spec.banners * 7.5 <= SHORTS_LIMIT
