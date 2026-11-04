"""Доставка клипа в Telegram: размеры и обложка.

Без них Telegram сам гадает, какой формы показать предпросмотр, и вертикальный ролик 9:16 иногда выглядит
в чате сплюснутым. Поэтому бот называет настоящие размеры отправляемого файла и прикладывает обложку.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from PIL import Image

from app.bot import delivery
from app.bot.delivery import send_clip
from app.models import ClipResult
from tests.bot_fakes import FAKE_TOKEN, FakeSession
from tests.media_utils import make_video


@pytest.fixture
async def bot():
    session = FakeSession()
    yield Bot(token=FAKE_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await session.close()


def _clip(duration: float = 2.0) -> ClipResult:
    return ClipResult(index=1, filename="clip_01.mp4", source_start=0.0, source_end=duration, duration=duration)


async def test_a_vertical_clip_goes_out_with_its_real_size_and_a_cover_of_the_same_shape(bot, ffmpeg_tools, tmp_path):
    clip_file = make_video(tmp_path / "clip_01.mp4", size="360x640", seconds=2, audio=False)

    assert await send_clip(bot, 1, _clip(), clip_file, tmp_path, total=1)

    request = bot.session.calls("sendVideo")[0]
    assert (request.width, request.height) == (360, 640)
    cover = Path(request.thumbnail.path)
    assert cover.stat().st_size < 200 * 1024  # лимит Bot API на обложку
    with Image.open(cover) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 320  # и по стороне
        assert image.width / image.height == pytest.approx(360 / 640, abs=0.02)  # обложка той же формы, что ролик


async def test_the_size_describes_the_file_that_was_really_sent(bot, ffmpeg_tools, tmp_path, monkeypatch):
    """Не влезший в лимит клип сжимают: Telegram должен получить размеры сжатого файла, а не исходного."""
    original = make_video(tmp_path / "clip_01.mp4", size="360x640", seconds=2, audio=False)
    shrunk = make_video(tmp_path / "tg_clip_01.mp4", size="180x320", seconds=2, audio=False)
    monkeypatch.setattr(delivery, "prepare_for_telegram", lambda path, work_dir: (shrunk, True))

    assert await send_clip(bot, 1, _clip(), original, tmp_path, total=1)

    request = bot.session.calls("sendVideo")[0]
    assert (request.width, request.height) == (180, 320)


async def test_the_clip_still_goes_out_when_the_cover_cannot_be_made(bot, ffmpeg_tools, tmp_path, monkeypatch):
    clip_file = make_video(tmp_path / "clip_01.mp4", size="360x640", seconds=2, audio=False)
    monkeypatch.setattr(delivery, "make_cover", lambda video, target: None)

    assert await send_clip(bot, 1, _clip(), clip_file, tmp_path, total=1)

    request = bot.session.calls("sendVideo")[0]
    assert request.thumbnail is None
    assert (request.width, request.height) == (360, 640)
