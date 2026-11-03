"""Общие фикстуры: настройки во временной папке и синтетические медиафайлы."""
from __future__ import annotations

import shutil

import pytest
from PIL import Image

from types import SimpleNamespace

from app.config import Settings
from app.services.video_processor import RenderOptions, probe_media, render_clip
from tests.media_utils import make_video

PINK = (255, 51, 102)  # цвет сплошного тестового баннера
CLIP_START = 8.0  # секунда источника, с которой начинается клип (яркость там уже высокая)
CLIP_SECONDS = 9.0
PAUSE = 2.0
# окна остановки на таймлайне ролика: (3–5 с) и (8–10 с); итог 9 + 2·2 = 13 с


@pytest.fixture(scope="session")
def ffmpeg_tools():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("нужны ffmpeg и ffprobe в PATH")


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory, ffmpeg_tools):
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def synthetic_video(media_dir):
    """20 секунд 1280x720 со звуком — как требует спека."""
    return make_video(media_dir / "source_20s.mp4")


@pytest.fixture(scope="session")
def silent_video(media_dir):
    """Источник без аудиодорожки."""
    return make_video(media_dir / "silent_8s.mp4", size="640x360", seconds=8, audio=False)


@pytest.fixture(scope="session")
def portrait_video(media_dir):
    """Вертикальный источник 720x1280."""
    return make_video(media_dir / "portrait_720x1280.mp4", size="720x1280", seconds=5)


@pytest.fixture(scope="session")
def quad_video(media_dir):
    """Источник не 16:9: 642x482."""
    return make_video(media_dir / "quad_642x482.mp4", size="642x482", seconds=5)


@pytest.fixture(scope="session")
def odd_video(media_dir):
    """Нечётные размеры 641x481 (x264 такие не кодирует — берём FFV1 в mkv)."""
    return make_video(media_dir / "odd_641x481.mkv", size="641x481", seconds=5, vcodec="ffv1")


@pytest.fixture(scope="session")
def short_audio_video(media_dir):
    """Видео 8 с, а звук кончается на 3-й секунде — так часто бывает у скачанных роликов."""
    return make_video(media_dir / "short_audio_8s.mp4", size="640x360", seconds=8, audio_seconds=3)


@pytest.fixture(scope="session")
def stripe_video(media_dir):
    """Серый кадр 1280x720 с белой вертикальной полосой, центр которой — на 70% ширины (x=896)."""
    picture = "geq=lum=48:cb=128:cr=128,drawbox=x=886:y=0:w=20:h=720:color=white:t=fill"
    return make_video(media_dir / "stripe_4s.mp4", seconds=4, video_filter=picture)


@pytest.fixture(scope="session")
def tall_video(media_dir):
    """Источник выше, чем 9:16 (540x1170, как запись экрана телефона), с белой горизонтальной полосой по центру высоты."""
    picture = "geq=lum=48:cb=128:cr=128,drawbox=x=0:y=575:w=540:h=20:color=white:t=fill"
    return make_video(media_dir / "tall_540x1170.mp4", size="540x1170", seconds=3, video_filter=picture)


@pytest.fixture(scope="session")
def solid_banner(media_dir):
    """Сплошной непрозрачный баннер 920x360 цвета PINK — его легко найти по цвету пикселя."""
    path = media_dir / "solid_banner.png"
    Image.new("RGBA", (920, 360), (*PINK, 255)).save(path)
    return path


@pytest.fixture(scope="session")
def freeze_clip(synthetic_video, solid_banner, tmp_path_factory):
    """Клип из спеки: T=9 с, K=2 остановки по D=2 с, Blur-Pad, без SFX. Рендерится один раз за сессию."""
    output = tmp_path_factory.mktemp("freeze") / "clip.mp4"
    options = RenderOptions(banner_count=2, banner_duration=PAUSE)
    timeline = render_clip(synthetic_video, output, CLIP_START, CLIP_SECONDS, options, info=probe_media(synthetic_video), banner=solid_banner)
    return SimpleNamespace(path=output, timeline=timeline, scratch=tmp_path_factory.mktemp("frames"))


@pytest.fixture
def settings(tmp_path):
    result = Settings.for_root(tmp_path)
    result.ensure_dirs()
    return result
