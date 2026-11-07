"""Тесты звука: чистые функции, разбор вывода FFmpeg и переписывание файла на месте.

Медиа генерируются самим ffmpeg в `tmp_path`, поэтому тесты не зависят от файлов в репозитории. Замеры
проверяются не «примерно похоже на правду» в вакууме, а на разнице: один и тот же тон, записанный на 20 дБ
тише, обязан отстоять по `integrated` ровно на 20 LU, а клип, прогнанный через `normalize_file`, — попасть
в цель. Так тест ловит именно ошибку разбора или фильтра, а не дрейф абсолютных значений между версиями
FFmpeg.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.services.audio import (
    TARGET_LUFS,
    Loudness,
    banner_gain_db,
    limit_file,
    measure,
    median_db,
    normalize_file,
)
from tests.media_utils import make_video, run_ffmpeg

# Дефолтная амплитуда lavfi-источника `sine` — не полная шкала, поэтому «тихо» и «громко» задаём поправкой
# громкости, а не абсолютными числами: тесту важно расстояние между замерами, а не конкретные LUFS.
QUIET_DB = -24.0
LOUD_DB = 0.0


def tone_file(path: Path, *, volume_db: float, seconds: int = 2) -> Path:
    """Файл с одним тоном заданной громкости — минимальный материал, на котором видно работу замера."""
    run_ffmpeg(
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-af", f"volume={volume_db}dB",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", str(path),
    )  # fmt: skip
    return path


def clip_with_tone(path: Path, *, volume_db: float, seconds: int = 3) -> Path:
    """Клип с картинкой и звуком — как у бота: по нему проверяется, что правка звука не трогает видео."""
    run_ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-af", f"volume={volume_db}dB",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", str(path),
    )  # fmt: skip
    return path


def stream(path: Path, kind: str) -> dict[str, Any]:
    """Поток `kind` (`video`/`audio`) по ffprobe: `bit_rate` показывает, перекодировали дорожку или скопировали."""
    data = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    return next(item for item in data["streams"] if item["codec_type"] == kind)


# ---------------------------------------------------------------------------
# Чистые функции: медиана и поправка баннеру
# ---------------------------------------------------------------------------


def test_median_db_takes_middle_level() -> None:
    assert median_db([-30.0, -20.0, -25.0]) == -25.0


def test_median_db_averages_middle_pair_for_even_count() -> None:
    assert median_db([-30.0, -20.0, -10.0, -40.0]) == -25.0


def test_median_db_ignores_single_outlier() -> None:
    # Ровный фон и один крик: «типичная» громкость должна остаться на фоне, иначе баннер получит лишние 3 дБ.
    assert median_db([-30.0] * 9 + [0.0]) == -30.0


def test_median_db_is_none_for_empty_list() -> None:
    assert median_db([]) is None


def test_banner_gain_lowers_banner_that_is_louder_than_content() -> None:
    # Реальные замеры из задачи: баннер TGrader −18,9 LUFS против контента −27,3 → приглушить на 8,4 дБ.
    banner = Loudness(integrated=-18.9, true_peak=-4.0, lra=3.0)
    assert banner_gain_db(banner, -27.3) == pytest.approx(-8.4)


def test_banner_gain_raises_banner_that_is_quieter_than_content() -> None:
    banner = Loudness(integrated=-27.3, true_peak=-4.0, lra=3.0)
    assert banner_gain_db(banner, -18.9) == pytest.approx(8.4)


def test_banner_gain_is_clamped_to_plus_limit() -> None:
    banner = Loudness(integrated=-90.0, true_peak=-4.0, lra=3.0)
    assert banner_gain_db(banner, -20.0) == pytest.approx(18.0)


def test_banner_gain_is_clamped_to_minus_limit() -> None:
    banner = Loudness(integrated=-5.0, true_peak=-4.0, lra=3.0)
    assert banner_gain_db(banner, -40.0) == pytest.approx(-18.0)


def test_banner_gain_respects_custom_limit() -> None:
    banner = Loudness(integrated=-90.0, true_peak=-4.0, lra=3.0)
    assert banner_gain_db(banner, -20.0, limit_db=6.0) == pytest.approx(6.0)


def test_banner_gain_is_rounded_to_tenth_of_db() -> None:
    # -27.3 − (−21.66) = −5.64 дБ: в файл и в логи идёт −5.6, а не −5.639999...
    banner = Loudness(integrated=-21.66, true_peak=-4.0, lra=3.0)
    assert banner_gain_db(banner, -27.3) == pytest.approx(-5.6)


# ---------------------------------------------------------------------------
# Замер: разбор вывода ffmpeg на настоящих файлах
# ---------------------------------------------------------------------------


def test_measure_reads_loudness_from_generated_tone(ffmpeg_tools, tmp_path: Path) -> None:
    measured = measure(tone_file(tmp_path / "tone.m4a", volume_db=LOUD_DB))
    assert measured is not None
    # Полный тон на полной шкале не может быть тише −40 LUFS или громче 0 LUFS: это проверка, что разбор
    # взял сводку ebur128, а не первую попавшуюся строку из бегущего вывода.
    assert -40.0 < measured.integrated <= 0.0
    assert measured.true_peak <= measured.integrated + 12  # пик не может быть сильно ниже средней громкости
    assert measured.lra >= 0.0


def test_measure_sees_the_volume_difference_between_files(ffmpeg_tools, tmp_path: Path) -> None:
    quiet = measure(tone_file(tmp_path / "quiet.m4a", volume_db=QUIET_DB))
    loud = measure(tone_file(tmp_path / "loud.m4a", volume_db=LOUD_DB))
    assert quiet is not None and loud is not None
    # Разница громкостей 24 дБ — главная проверка: так видно, что замер действительно меряет, а не выдумывает.
    assert loud.integrated - quiet.integrated == pytest.approx(LOUD_DB - QUIET_DB, abs=1.5)


def test_measure_returns_none_for_non_media_file(ffmpeg_tools, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is definitely not a media file")
    assert measure(broken) is None


def test_measure_returns_none_when_file_is_missing(ffmpeg_tools, tmp_path: Path) -> None:
    assert measure(tmp_path / "nope.mp4") is None


def test_measure_returns_none_for_video_without_audio(ffmpeg_tools, tmp_path: Path) -> None:
    # ebur128 на файле без звука проходит «вхолостую» с кодом 0 и без сводки — разбор обязан это заметить.
    silent = make_video(tmp_path / "silent.mp4", size="160x120", seconds=1, audio=False)
    assert measure(silent) is None


# ---------------------------------------------------------------------------
# Нормализация: цель по громкости, видео без перекода
# ---------------------------------------------------------------------------


def test_normalize_file_brings_clip_to_target_loudness(ffmpeg_tools, tmp_path: Path) -> None:
    clip = clip_with_tone(tmp_path / "quiet_clip.mp4", volume_db=QUIET_DB)
    before = measure(clip)
    assert before is not None and before.integrated < -25  # исходник действительно тише нормы

    assert normalize_file(clip) is True

    after = measure(clip)
    assert after is not None
    assert after.integrated == pytest.approx(TARGET_LUFS, abs=1.5)


def test_normalize_file_copies_video_instead_of_reencoding(ffmpeg_tools, tmp_path: Path) -> None:
    clip = clip_with_tone(tmp_path / "clip.mp4", volume_db=QUIET_DB)
    before = stream(clip, "video")

    assert normalize_file(clip) is True

    after = stream(clip, "video")
    assert after["codec_name"] == before["codec_name"]
    assert after["width"] == before["width"] and after["height"] == before["height"]
    # Равный битрейт — прямое доказательство `-c:v copy`: перекод x264 на том же контенте дал бы другие кадры
    # и другой битрейт, а перенос пакетов повторяет его байт в байт.
    assert after["bit_rate"] == before["bit_rate"]


def test_normalize_file_writes_aac_stereo_44100(ffmpeg_tools, tmp_path: Path) -> None:
    clip = clip_with_tone(tmp_path / "clip.mp4", volume_db=QUIET_DB)
    assert normalize_file(clip) is True
    audio = stream(clip, "audio")
    assert audio["codec_name"] == "aac"
    assert int(audio["sample_rate"]) == 44100
    assert int(audio["channels"]) == 2
    assert 100_000 <= int(audio["bit_rate"]) <= 200_000  # 160k с разбросом AAC-кодирования


def test_normalize_file_leaves_no_temp_files_behind(ffmpeg_tools, tmp_path: Path) -> None:
    clip = clip_with_tone(tmp_path / "clip.mp4", volume_db=QUIET_DB)
    assert normalize_file(clip) is True
    # Переписывание идёт через заготовку рядом с файлом; забытая заготовка — это мусор в папке готовых клипов.
    assert sorted(path.name for path in tmp_path.iterdir()) == [clip.name]


def test_normalize_file_returns_false_for_non_media_and_keeps_it_intact(ffmpeg_tools, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is definitely not a media file")
    assert normalize_file(broken) is False
    assert broken.read_bytes() == b"this is definitely not a media file"


def test_normalize_file_returns_false_for_missing_file(ffmpeg_tools, tmp_path: Path) -> None:
    assert normalize_file(tmp_path / "nope.mp4") is False


# ---------------------------------------------------------------------------
# Ограничитель пиков
# ---------------------------------------------------------------------------


def test_limit_file_cuts_true_peaks(ffmpeg_tools, tmp_path: Path) -> None:
    # +40 дБ загоняет тон в потолок: true peak уходит выше 0 dBFS, и это ровно тот случай, ради которого
    # ограничитель нужен (площадка перекодирует звук и превращает такие пики в щелчки).
    clip = clip_with_tone(tmp_path / "hot.mp4", volume_db=40.0)
    before = measure(clip)
    assert before is not None and before.true_peak > 1.0

    assert limit_file(clip) is True

    after = measure(clip)
    assert after is not None
    assert after.true_peak < before.true_peak - 6
    assert after.true_peak <= 0.5


def test_limit_file_copies_video_instead_of_reencoding(ffmpeg_tools, tmp_path: Path) -> None:
    clip = clip_with_tone(tmp_path / "hot.mp4", volume_db=40.0)
    before = stream(clip, "video")
    assert limit_file(clip) is True
    assert stream(clip, "video")["bit_rate"] == before["bit_rate"]


def test_limit_file_returns_false_for_non_media(ffmpeg_tools, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is definitely not a media file")
    assert limit_file(broken) is False
    assert broken.read_bytes() == b"this is definitely not a media file"


def test_audio_already_at_target_is_left_alone(ffmpeg_tools, tmp_path: Path) -> None:
    """Если громкость уже в норме, перекодировать звук незачем: это лишний проход по файлу."""
    clip = clip_with_tone(tmp_path / "ok_clip.mp4", volume_db=-6.0)
    assert normalize_file(clip) is True  # сначала приводим к цели
    before_mtime = clip.stat().st_mtime_ns
    audio_before = stream(clip, "audio")

    assert normalize_file(clip) is False, "второй раз нормализовать нечего"
    assert clip.stat().st_mtime_ns == before_mtime, "файл не должен был измениться"
    assert stream(clip, "audio") == audio_before
