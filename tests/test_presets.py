"""Пресеты: три набора, которые не могут сделать непубликуемый клип."""
from __future__ import annotations

import pytest

from app.models import BannerMode, ClipSettings, FrameMode
from app.services import publishing
from app.services.presets import PRESET_BY_ID, PRESETS, settings_for


def test_every_preset_fits_the_publishing_limit_with_a_ten_second_banner():
    """Самый длинный баннер из каталога — 10 с: даже с ним клип обязан влезать в 59 секунд."""
    for preset in PRESETS:
        settings = settings_for(preset, ClipSettings(), banner_seconds=10.0)
        assert publishing.fits(settings, 10.0), preset.id
        assert settings.banner_count >= 1, preset.id


def test_a_long_banner_shortens_the_clip_instead_of_breaking_the_limit():
    settings = settings_for(PRESET_BY_ID["podcast"], ClipSettings(), banner_seconds=20.0)

    assert publishing.total_seconds(settings, 20.0) <= publishing.LIMIT_SECONDS
    assert settings.banner_count == 1  # показ остался, укоротился сам клип


def test_preset_sets_numbers_frame_and_banner_position():
    settings = settings_for(PRESET_BY_ID["stream"], ClipSettings(), banner_seconds=7.5)

    assert settings.num_clips == 2 and settings.clip_duration == 48.0
    assert settings.frame_mode is FrameMode.BLUR_PAD
    assert settings.banner_count == 1


def test_preset_keeps_the_persons_own_choices():
    base = ClipSettings(banner_id="TGrader-banner-7s.mp4", banner_sound=False, max_height=720, auto_trim=False)

    settings = settings_for(PRESET_BY_ID["hook"], base, banner_seconds=7.5)

    assert settings.banner_id == "TGrader-banner-7s.mp4" and settings.banner_sound is False
    assert settings.auto_trim is False  # края ролика пресет не трогает
    assert settings.max_height == 720  # качество не поднимаем выше того, что человек выбрал


def test_preset_without_a_banner_file_goes_without_banners():
    settings = settings_for(PRESET_BY_ID["podcast"], ClipSettings(), banner_seconds=None)

    assert settings.banner_count == 0
    assert settings.banner_mode is BannerMode.FREEZE  # режим показа остался прежним, показов просто ноль


def test_a_short_source_gets_fewer_clips_so_they_do_not_overlap():
    settings = settings_for(PRESET_BY_ID["podcast"], ClipSettings(), banner_seconds=7.5, source_seconds=60.0)

    assert settings.num_clips == 1  # в минуту помещается ровно один клип по 45 с


@pytest.mark.parametrize("preset", PRESETS, ids=[preset.id for preset in PRESETS])
def test_presets_differ_in_more_than_a_name(preset):
    settings = settings_for(preset, ClipSettings(), banner_seconds=7.5)

    assert settings.num_clips == preset.clips and settings.frame_mode is preset.frame
