"""Баннеры и SFX: пресеты, загрузка пользовательских картинок, защита путей."""
import io
import struct
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.services import banner_manager
from app.services.banner_manager import (
    BannerError,
    ensure_presets,
    ensure_sfx,
    ensure_strip_presets,
    list_banners,
    resolve_banner,
    save_uploaded_banner,
)
from tests.media_utils import make_video, run_ffmpeg

PRESET_FILES = ["preset_tgrader.png", "preset_rubup.png", "preset_musor_drop.png"]


def image_bytes(fmt, size, color=(200, 30, 60)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


# ---------- пресеты ----------


def test_ensure_presets_creates_the_three_banners_from_the_spec(tmp_path):
    paths = ensure_presets(tmp_path)
    assert [p.name for p in paths] == PRESET_FILES
    for path in paths:
        with Image.open(path) as image:
            assert image.size == (920, 360)
            assert image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0  # скруглённый угол прозрачен
            assert image.getpixel((460, 180))[3] == 255  # тело карточки непрозрачно
    assert len({p.read_bytes() for p in paths}) == 3  # у каждого бренда своя картинка


def test_ensure_presets_keeps_a_banner_the_user_replaced(tmp_path):
    ensure_presets(tmp_path)
    Image.new("RGBA", (920, 360), (1, 2, 3, 255)).save(tmp_path / "preset_rubup.png")
    ensure_presets(tmp_path)  # повторный запуск приложения
    with Image.open(tmp_path / "preset_rubup.png") as image:
        assert image.getpixel((460, 180)) == (1, 2, 3, 255)


def test_presets_are_still_generated_when_the_system_has_no_fonts(tmp_path, monkeypatch):
    monkeypatch.setattr(banner_manager, "FONT_CANDIDATES", ())
    paths = ensure_presets(tmp_path)
    assert all(p.stat().st_size > 0 for p in paths)


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


# ---------- загрузка своих баннеров ----------


def test_uploaded_jpg_becomes_a_rounded_png_within_the_width_limit(tmp_path):
    info = save_uploaded_banner("Моя реклама.JPG", image_bytes("JPEG", (3000, 1000)), tmp_path)
    assert info.kind == "custom"
    assert info.id.isascii() and info.id.endswith(".png")
    assert info.animated is False
    assert (info.width, info.height) == (1600, 533)
    with Image.open(tmp_path / info.id) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0  # углы скруглены
        assert image.getpixel((800, 266))[3] == 255


def test_uploaded_animated_gif_keeps_its_animation(tmp_path):
    frames = [Image.new("RGB", (200, 80), color) for color in ((255, 0, 0), (0, 0, 255))]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    info = save_uploaded_banner("anim.gif", buffer.getvalue(), tmp_path)
    assert info.id.endswith(".gif")
    assert info.animated is True


@pytest.mark.parametrize(
    "filename, data",
    [
        ("fake.png", b"this is not an image"),
        ("empty.png", b""),
        ("virus.exe", image_bytes("PNG", (10, 10))),
        ("huge.png", b"\0" * (10 * 1024 * 1024 + 1)),
    ],
)
def test_broken_or_foreign_uploads_are_rejected_and_leave_nothing_behind(tmp_path, filename, data):
    with pytest.raises(BannerError):
        save_uploaded_banner(filename, data, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_list_banners_shows_presets_first_then_custom(tmp_path):
    ensure_presets(tmp_path)
    custom = save_uploaded_banner("a.png", image_bytes("PNG", (400, 200)), tmp_path)
    listed = list_banners(tmp_path)
    assert [b.id for b in listed] == PRESET_FILES + [custom.id]
    assert [b.kind for b in listed] == ["preset", "preset", "preset", "custom"]
    assert [b.name for b in listed[:3]] == ["TGrader", "RubUp", "Musor Drop"]
    assert listed[0].path == tmp_path / "preset_tgrader.png"


# ---------- безопасность путей ----------


@pytest.mark.parametrize(
    "banner_id",
    ["../secret.png", "/etc/passwd", "sub/dir.png", "..\\secret.png", "..", ".hidden.png", "missing.png", "preset_rubup.txt", ""],
)
def test_resolve_banner_refuses_anything_but_a_banner_file_in_the_folder(tmp_path, banner_id):
    banners = tmp_path / "banners"
    ensure_presets(banners)
    (tmp_path / "secret.png").write_bytes(b"x")  # сосед, до которого тянется `../`
    with pytest.raises(BannerError):
        resolve_banner(banner_id, banners)


def test_resolve_banner_returns_the_existing_file(tmp_path):
    ensure_presets(tmp_path)
    assert resolve_banner("preset_rubup.png", tmp_path) == tmp_path / "preset_rubup.png"


# ---------- видео-баннеры (MP4) ----------


@pytest.fixture(scope="module")
def promo_mp4(media_dir):
    """3 секунды 320x180 со звуком — маленький рекламный ролик."""
    return make_video(media_dir / "promo_3s.mp4", size="320x180", seconds=3).read_bytes()


@pytest.fixture(scope="module")
def promo_mp4_silent(media_dir):
    return make_video(media_dir / "promo_silent_2s.mp4", size="320x180", seconds=2, audio=False).read_bytes()


def test_uploaded_mp4_is_kept_as_a_video_banner_with_its_facts(tmp_path, promo_mp4):
    info = save_uploaded_banner("TGrader promo.MP4", promo_mp4, tmp_path)
    assert info.media == "video" and info.animated is True and info.kind == "custom"
    assert info.id.isascii() and info.id.endswith(".mp4")
    assert (info.width, info.height) == (320, 180)
    assert info.duration == pytest.approx(3.0, abs=0.15)
    assert info.has_audio is True
    assert info.role == "popup"
    assert (tmp_path / info.id).read_bytes() == promo_mp4  # видео не перекодируется


def test_video_banner_gets_a_poster_picture_for_the_interface(tmp_path, promo_mp4):
    info = save_uploaded_banner("promo.mp4", promo_mp4, tmp_path)
    assert info.poster == tmp_path / "posters" / f"{Path(info.id).stem}.jpg"
    with Image.open(tmp_path / "posters" / f"{Path(info.id).stem}.jpg") as poster:
        assert poster.size[0] > 0
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == [info.id]  # во временные файлы ничего не утекло


def test_silent_video_banner_says_it_has_no_sound(tmp_path, promo_mp4_silent):
    info = save_uploaded_banner("quiet.mp4", promo_mp4_silent, tmp_path)
    assert info.has_audio is False


def test_a_too_long_video_is_refused_with_the_limit_in_the_message(tmp_path, media_dir):
    long_video = make_video(media_dir / "long_21s.mp4", size="64x36", seconds=21, audio=False).read_bytes()
    with pytest.raises(BannerError, match="20 с"):
        save_uploaded_banner("long.mp4", long_video, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_huge_video_file_is_refused_before_it_is_even_probed(tmp_path):
    with pytest.raises(BannerError, match="40 МБ"):
        save_uploaded_banner("huge.mp4", b"\0" * (40 * 1024 * 1024 + 1), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_video_extension_over_garbage_or_audio_only_is_refused(tmp_path, media_dir):
    audio_only = media_dir / "sound_only.mp4"
    run_ffmpeg("-f", "lavfi", "-i", "sine=d=2", "-c:a", "aac", str(audio_only))
    for name, data in (("fake.mp4", b"this is not a video"), ("sound.mp4", audio_only.read_bytes())):
        with pytest.raises(BannerError, match="видео"):
            save_uploaded_banner(name, data, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_list_banners_describes_video_and_image_banners_differently(tmp_path, promo_mp4):
    ensure_presets(tmp_path)
    video = save_uploaded_banner("promo.mp4", promo_mp4, tmp_path)
    picture = save_uploaded_banner("wide.png", image_bytes("PNG", (3000, 1000)), tmp_path)
    by_id = {banner.id: banner for banner in list_banners(tmp_path)}
    assert by_id["preset_tgrader.png"].media == "image" and by_id["preset_tgrader.png"].duration is None
    assert by_id["preset_tgrader.png"].poster is None
    assert by_id[video.id].media == "video" and by_id[video.id].duration == pytest.approx(3.0, abs=0.15)
    assert by_id[picture.id].role == "strip"  # 3 : 1 — это полоса, а не карточка


def test_gif_banner_reports_its_loop_length(tmp_path):
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (200, 80), color) for color in ((255, 0, 0), (0, 0, 255), (0, 255, 0))]
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    info = save_uploaded_banner("anim.gif", buffer.getvalue(), tmp_path)
    assert info.media == "gif" and info.duration == pytest.approx(0.6)


def test_a_video_banner_the_user_copied_in_by_hand_is_listed_and_gets_a_poster(tmp_path, promo_mp4):
    (tmp_path / "my_own.mp4").write_bytes(promo_mp4)
    (listed,) = list_banners(tmp_path)
    assert listed.id == "my_own.mp4" and listed.media == "video" and listed.name == "my own"
    assert (tmp_path / "posters" / "my_own.jpg").is_file()


def test_resolve_banner_accepts_video_files(tmp_path, promo_mp4):
    (tmp_path / "clip.mp4").write_bytes(promo_mp4)
    assert resolve_banner("clip.mp4", tmp_path) == tmp_path / "clip.mp4"


# ---------- полосы для постоянного баннера ----------

STRIP_FILES = ["strip_tgrader.png", "strip_rubup.png", "strip_musor_drop.png"]


def test_strip_presets_are_wide_rounded_banners_for_the_margin(tmp_path):
    paths = ensure_strip_presets(tmp_path)
    assert [path.name for path in paths] == STRIP_FILES
    for path in paths:
        with Image.open(path) as image:
            assert image.size == (1080, 300) and image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0
            assert image.getpixel((540, 150))[3] == 255
    assert len({path.read_bytes() for path in paths}) == 3


def test_strip_presets_do_not_overwrite_a_replaced_file(tmp_path):
    ensure_strip_presets(tmp_path)
    Image.new("RGBA", (1080, 300), (9, 9, 9, 255)).save(tmp_path / "strip_rubup.png")
    ensure_strip_presets(tmp_path)
    with Image.open(tmp_path / "strip_rubup.png") as image:
        assert image.getpixel((540, 150)) == (9, 9, 9, 255)


def test_list_banners_puts_strips_after_the_popup_presets_and_marks_their_role(tmp_path):
    ensure_presets(tmp_path)
    ensure_strip_presets(tmp_path)
    custom = save_uploaded_banner("a.png", image_bytes("PNG", (400, 200)), tmp_path)
    listed = list_banners(tmp_path)
    assert [b.id for b in listed] == PRESET_FILES + STRIP_FILES + [custom.id]
    assert [b.role for b in listed] == ["popup"] * 3 + ["strip"] * 3 + ["popup"]
    assert [b.kind for b in listed] == ["preset"] * 6 + ["custom"]
    assert listed[3].name == "TGrader (полоса)"
