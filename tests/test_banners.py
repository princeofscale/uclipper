"""Баннеры проекта и SFX: копирование баннеров, их описание, выбор по id и защита путей."""
import struct
import wave
from pathlib import Path

import pytest

from app.config import ASSETS_DIR, BANNERS
from app.services.banner_manager import (
    BannerError,
    catalog_banners,
    default_banner,
    ensure_banners,
    ensure_sfx,
    find_banner,
    resolve_banner,
)
from tests.media_utils import make_video

TG_GRADER, TG_GRADER_V2 = BANNERS[0], BANNERS[1]


@pytest.fixture(scope="module")
def promo(media_dir):
    """3 секунды 320x180 со звуком — маленький рекламный ролик."""
    return make_video(media_dir / "promo_3s.mp4", size="320x180", seconds=3)


@pytest.fixture(scope="module")
def promo_silent(media_dir):
    return make_video(media_dir / "promo_silent_2s.mp4", size="640x360", seconds=2, audio=False)


@pytest.fixture(scope="module")
def assets(media_dir, promo, promo_silent):
    """Папка-образ `assets/banners`: оба баннера каталога, но маленькие — чтобы тесты шли быстро."""
    folder = media_dir / "banner_assets"
    folder.mkdir(exist_ok=True)
    for spec, source in ((TG_GRADER, promo), (TG_GRADER_V2, promo_silent)):
        (folder / spec.file).write_bytes(source.read_bytes())
    return folder


@pytest.fixture(scope="module")
def assets_real() -> Path:
    """Настоящая папка `assets/banners` репозитория — та, что копируется в образ."""
    return ASSETS_DIR / "banners"


# ---------- баннеры проекта ----------


def test_both_banners_are_copied_from_the_assets(tmp_path, assets):
    target = tmp_path / "banners"
    copied = ensure_banners(target, assets_dir=assets)

    assert [path.name for path in copied] == [spec.file for spec in BANNERS]
    assert (target / TG_GRADER_V2.file).read_bytes() == (assets / TG_GRADER_V2.file).read_bytes()
    assert sorted(path.name for path in target.iterdir()) == sorted(spec.file for spec in BANNERS)  # временных нет


def test_a_banner_the_owner_replaced_is_kept(tmp_path, assets, promo_silent):
    ensure_banners(tmp_path, assets_dir=assets)
    replaced = tmp_path / TG_GRADER.file
    replaced.write_bytes(promo_silent.read_bytes())

    ensure_banners(tmp_path, assets_dir=assets)  # повторный запуск бота

    assert replaced.read_bytes() == promo_silent.read_bytes()


def test_a_missing_asset_is_skipped_instead_of_a_crash(tmp_path, assets, caplog):
    only_second = tmp_path / "assets"
    only_second.mkdir()
    (only_second / TG_GRADER_V2.file).write_bytes((assets / TG_GRADER_V2.file).read_bytes())

    copied = ensure_banners(tmp_path / "banners", assets_dir=only_second)

    assert [path.name for path in copied] == [TG_GRADER_V2.file]
    assert "не найден" in caplog.text


def test_banners_without_assets_at_all_leave_the_bot_without_them(tmp_path):
    assert ensure_banners(tmp_path / "banners", assets_dir=tmp_path / "nope") == []
    assert catalog_banners(tmp_path / "banners") == []
    assert default_banner(tmp_path / "banners") is None


def test_every_copied_banner_is_described_by_its_own_facts(tmp_path, assets):
    ensure_banners(tmp_path, assets_dir=assets)
    banners = catalog_banners(tmp_path)

    assert [banner.id for banner in banners] == [spec.file for spec in BANNERS]  # порядок каталога
    first, second = banners
    assert (first.name, first.hint) == (TG_GRADER.name, TG_GRADER.hint)
    assert (second.name, second.hint) == (TG_GRADER_V2.name, TG_GRADER_V2.hint)
    assert first.path == tmp_path / TG_GRADER.file
    assert (first.width, first.height) == (320, 180)
    assert first.duration == pytest.approx(3.0, abs=0.15)
    assert first.has_audio is True
    assert second.has_audio is False


def test_the_real_banners_in_the_repository_are_readable(assets_real):
    """`assets/banners/` — то, что уедет в образ: оба баннера каталога на месте и читаются как видео."""
    banners = catalog_banners(assets_real)

    assert [banner.id for banner in banners] == [spec.file for spec in BANNERS]
    assert all(banner.duration > 0 and banner.width > 0 and banner.height > 0 for banner in banners)
    assert all(banner.has_audio for banner in banners)


def test_the_default_banner_is_the_first_in_the_catalog(tmp_path, assets):
    ensure_banners(tmp_path, assets_dir=assets)

    assert default_banner(tmp_path).id == TG_GRADER.file


def test_a_silent_banner_says_it_has_no_sound(tmp_path, assets):
    ensure_banners(tmp_path, assets_dir=assets)

    assert find_banner(TG_GRADER_V2.file, tmp_path).has_audio is False


def test_a_file_that_is_not_a_video_is_not_a_banner(tmp_path):
    (tmp_path / TG_GRADER.file).write_bytes(b"this is not a video")

    assert default_banner(tmp_path) is None


def test_replacing_the_banner_file_refreshes_its_description(tmp_path, assets):
    ensure_banners(tmp_path, assets_dir=assets)
    assert default_banner(tmp_path).has_audio is True  # описание попало в кеш
    (tmp_path / TG_GRADER.file).write_bytes((assets / TG_GRADER_V2.file).read_bytes())
    assert default_banner(tmp_path).has_audio is False  # файл другой — описание обновилось


def test_find_banner_knows_only_the_catalog(tmp_path, assets, promo):
    ensure_banners(tmp_path, assets_dir=assets)
    (tmp_path / "owner_extra.mp4").write_bytes(promo.read_bytes())

    assert find_banner(TG_GRADER_V2.file, tmp_path).name == TG_GRADER_V2.name
    assert find_banner("owner_extra.mp4", tmp_path) is None  # не из каталога — экран его не предлагает
    assert find_banner(None, tmp_path) is None
    assert find_banner("", tmp_path) is None


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
