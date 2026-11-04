"""Баннер проекта и SFX: копирование главного баннера, его описание и защита путей."""
import struct
import wave

import pytest

from app.config import DEFAULT_BANNER_FILE, DEFAULT_BANNER_LABEL
from app.services.banner_manager import (
    BannerError,
    default_banner,
    ensure_default_banner,
    ensure_sfx,
    resolve_banner,
)
from tests.media_utils import make_video


@pytest.fixture(scope="module")
def promo(media_dir):
    """3 секунды 320x180 со звуком — маленький рекламный ролик."""
    return make_video(media_dir / "promo_3s.mp4", size="320x180", seconds=3)


@pytest.fixture(scope="module")
def promo_silent(media_dir):
    return make_video(media_dir / "promo_silent_2s.mp4", size="640x360", seconds=2, audio=False)


# ---------- главный баннер ----------


def test_the_default_banner_is_copied_from_the_assets(tmp_path, promo):
    banners = tmp_path / "banners"
    target = ensure_default_banner(banners, asset=promo)
    assert target == banners / DEFAULT_BANNER_FILE
    assert target.read_bytes() == promo.read_bytes()
    assert [path.name for path in banners.iterdir()] == [DEFAULT_BANNER_FILE]  # временных файлов не осталось


def test_the_default_banner_keeps_a_file_the_owner_replaced(tmp_path, promo, promo_silent):
    banners = tmp_path / "banners"
    ensure_default_banner(banners, asset=promo)
    (banners / DEFAULT_BANNER_FILE).write_bytes(promo_silent.read_bytes())
    ensure_default_banner(banners, asset=promo)  # повторный запуск бота
    assert (banners / DEFAULT_BANNER_FILE).read_bytes() == promo_silent.read_bytes()


def test_a_missing_asset_means_no_banner_instead_of_a_crash(tmp_path, caplog):
    assert ensure_default_banner(tmp_path / "banners", asset=tmp_path / "nope.mp4") is None
    assert default_banner(tmp_path / "banners") is None
    assert "не найден" in caplog.text


def test_the_banner_is_described_by_its_own_facts(tmp_path, promo):
    ensure_default_banner(tmp_path, asset=promo)
    banner = default_banner(tmp_path)
    assert banner.id == DEFAULT_BANNER_FILE
    assert banner.name == DEFAULT_BANNER_LABEL
    assert banner.path == tmp_path / DEFAULT_BANNER_FILE
    assert (banner.width, banner.height) == (320, 180)
    assert banner.duration == pytest.approx(3.0, abs=0.15)
    assert banner.has_audio is True


def test_a_silent_banner_says_it_has_no_sound(tmp_path, promo_silent):
    ensure_default_banner(tmp_path, asset=promo_silent)
    assert default_banner(tmp_path).has_audio is False


def test_a_file_that_is_not_a_video_is_not_a_banner(tmp_path):
    (tmp_path / DEFAULT_BANNER_FILE).write_bytes(b"this is not a video")
    assert default_banner(tmp_path) is None


def test_replacing_the_banner_file_refreshes_its_description(tmp_path, promo, promo_silent):
    ensure_default_banner(tmp_path, asset=promo)
    assert default_banner(tmp_path).has_audio is True  # описание попало в кеш
    (tmp_path / DEFAULT_BANNER_FILE).write_bytes(promo_silent.read_bytes())
    assert default_banner(tmp_path).has_audio is False  # файл другой — описание обновилось


# ---------- SFX ----------


def read_stereo_wav(path):
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (2, 44100, 2)
        frames = wav.getnframes()
        samples = struct.unpack(f"<{frames * 2}h", wav.readframes(frames))
    return frames, samples[0::2]  # левый канал


def frequency(samples, start_s, end_s, rate=44100):
    segment = samples[int(start_s * rate) : int(end_s * rate)]
    crossings = sum(1 for a, b in zip(segment, segment[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (end_s - start_s)


def test_sfx_is_a_quarter_second_stereo_chime_without_clicks(tmp_path):
    path = ensure_sfx(tmp_path)
    assert path.name == "pop.wav"
    frames, left = read_stereo_wav(path)
    assert frames == 11025  # 0.25 с при 44100 Гц
    assert max(abs(s) for s in left) > 3000  # слышно
    assert abs(left[0]) < 50 and abs(left[-1]) < 50  # нет щелчка ни в начале, ни в конце


def test_sfx_pitch_rises_from_520_to_880_hz(tmp_path):
    _, left = read_stereo_wav(ensure_sfx(tmp_path))
    assert 480 <= frequency(left, 0.0, 0.04) <= 640  # в начале ≈ 520–580 Гц
    assert 700 <= frequency(left, 0.15, 0.20) <= 860  # к 0.175 с ≈ 770 Гц


def test_sfx_is_not_regenerated_when_it_exists(tmp_path):
    path = ensure_sfx(tmp_path)
    path.write_bytes(b"custom")
    assert ensure_sfx(tmp_path).read_bytes() == b"custom"


# ---------- безопасность путей ----------


@pytest.mark.parametrize(
    "banner_id",
    ["../secret.png", "/etc/passwd", "sub/dir.png", "..\\secret.png", "..", ".hidden.png", "missing.png", "banner.txt", ""],
)
def test_resolve_banner_refuses_anything_but_a_banner_file_in_the_folder(tmp_path, banner_id):
    banners = tmp_path / "banners"
    banners.mkdir()
    (banners / "banner.png").write_bytes(b"x")
    (banners / ".hidden.png").write_bytes(b"x")
    (banners / "banner.txt").write_bytes(b"x")
    (tmp_path / "secret.png").write_bytes(b"x")  # сосед, до которого тянется `../`
    with pytest.raises(BannerError):
        resolve_banner(banner_id, banners)


def test_resolve_banner_returns_the_existing_picture_or_video(tmp_path):
    (tmp_path / "card.png").write_bytes(b"x")
    (tmp_path / "clip.mp4").write_bytes(b"x")
    assert resolve_banner("card.png", tmp_path) == tmp_path / "card.png"
    assert resolve_banner("clip.mp4", tmp_path) == tmp_path / "clip.mp4"
