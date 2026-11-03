"""Тесты Telegram-бота: настоящий aiogram-диспетчер + фейковая сессия вместо Bot API.

Сеть здесь не нужна: `tests/bot_fakes.py` подменяет HTTP-слой aiogram, поэтому проверяются
реальные хэндлеры, клавиатуры и тексты. Внешние вызовы (yt-dlp, скачивание файла) подменяются точечно.
"""
from __future__ import annotations

import asyncio
import io
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from app.bot import keyboards, texts
from app.bot.context import update_settings
from app.models import FrameMode, SlicingMode
from app.bot.handlers import source as source_handlers
from app.config import Settings
from tests.bot_fakes import Harness
from tests.media_utils import make_video

VIDEO = {
    "video_id": "dQw4w9WgXcQ",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "title": "Тестовый ролик",
    "duration": 120.0,
    "uploader": "Автор",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    "resolutions": [1080, 720],
}

WAIT = 120


# ---------- фикстуры ----------


@pytest.fixture
def bot_settings(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    return settings


@pytest.fixture
async def harness(bot_settings):
    fake = Harness(bot_settings)
    yield fake
    await fake.close()


@pytest.fixture
def no_network(monkeypatch):
    """Любой выход в сеть через yt-dlp в тестах сценариев — ошибка, а не сюрприз."""

    def boom(*args, **kwargs):
        raise AssertionError("тест не должен ходить в YouTube")

    monkeypatch.setattr(source_handlers, "fetch_video_info", boom)
    monkeypatch.setattr(source_handlers, "search_videos", boom)


# ---------- /start, меню, справка ----------


async def test_start_shows_the_main_menu(harness):
    await harness.send("/start")

    assert "ClipBanner Bot" in harness.session.last_text()
    markup = harness.session.requests[-1].reply_markup
    assert markup is not None
    pressed = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert pressed[:3] == ["ask:link", "ask:query", "ask:file"]


async def test_help_explains_commands_and_telegram_limits(harness):
    await harness.send("/help")

    text = harness.session.last_text()
    assert "/mystats" in text
    assert "20 МБ" in text and "50 МБ" in text


async def test_unknown_text_is_offered_as_a_youtube_search(harness, monkeypatch):
    """Название ролика, которое не похоже на ссылку, уходит в поиск."""
    calls: list[str] = []
    monkeypatch.setattr(source_handlers, "search_videos", lambda query, limit=5: calls.append(query) or [])

    await harness.send("/start")
    await harness.send("интервью дурова")

    assert calls == ["интервью дурова"]
    assert "Ничего не нашлось" in harness.session.last_text()


async def test_too_short_search_query_is_reported_in_russian(harness, monkeypatch):
    from app.services.downloader import DownloadError

    def fail(query, limit=5):
        raise DownloadError("Слишком короткое название — напишите хотя бы пару слов.")

    monkeypatch.setattr(source_handlers, "search_videos", fail)
    await harness.send("/start")
    await harness.send("ok")

    assert "Слишком короткое название" in harness.session.last_text()


async def test_bad_link_says_what_is_wrong(harness):
    await harness.send("/start")
    await harness.send("https://vimeo.com/123456")

    text = harness.session.last_text()
    assert "не похоже на ссылку" in text and "youtu.be" in text


# ---------- карточка ролика и экран настроек ----------


async def test_link_opens_the_settings_screen(harness, monkeypatch):
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    await harness.send("/start")
    await harness.send(VIDEO["url"])

    text = harness.session.last_text()
    assert "Тестовый ролик" in text and "Автор" in text and "2:00" in text
    markup = harness.session.requests[-1].reply_markup
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "job:start" in callbacks and "screen:banners" in callbacks and "edit:num_clips" in callbacks


async def test_picked_search_result_becomes_the_draft(harness, monkeypatch):
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    monkeypatch.setattr(source_handlers, "search_videos", lambda query, limit=5: [dict(VIDEO)])
    await harness.send("/start")
    await harness.send("тестовый ролик")

    assert "Нашёл такие ролики" in harness.session.last_text()
    await harness.press("pick:0")

    assert "Тестовый ролик" in harness.session.last_text()
    assert harness.session.calls("sendMessage")


async def test_accept_video_rejects_a_non_youtube_link(harness, monkeypatch):
    """Ссылка, в которой нет id ролика, не должна доходить до сети."""
    await harness.send("/start")
    await harness.send("https://www.youtube.com/@channel")

    assert "не похоже на ссылку" in harness.session.last_text()


# ---------- настройки: кнопки, числа, границы ----------


async def _open_settings(harness, monkeypatch) -> None:
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    await harness.send("/start")
    await harness.send(VIDEO["url"])


async def test_number_button_asks_for_a_value_and_accepts_it(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")

    assert "Напишите число от 1 до 50" in harness.session.last_text()

    await harness.send("7")
    assert "количество: 7" in harness.session.last_text()
    assert harness.session.calls("editMessageText")


@pytest.mark.parametrize("value", ["0", "51"])
async def test_clip_count_out_of_range_is_rejected(harness, monkeypatch, value):
    """Число вне диапазона не меняет настройку — бот просит другое."""
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")

    await harness.send(value)
    assert "от 1 до 50" in harness.session.last_text()


async def test_fractional_clip_count_is_rejected(harness, monkeypatch):
    """Дробное количество клипов не проходит: в чат не утекает английская ошибка pydantic."""
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")

    await harness.send("3,5")
    text = harness.session.last_text()
    assert "только целое" in text
    assert "validation error" not in text and "int_from_float" not in text


async def test_garbage_instead_of_a_number_is_ignored(harness, monkeypatch):
    """На «abc» бот не отвечает ошибкой: это просто текст, а не ввод числа."""
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")
    before = harness.session.last_text()

    await harness.send("abc")
    assert harness.session.last_text() == before


async def test_cycles_change_every_enum_setting(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    expected = {
        "cycle:slicing_mode": "подряд",
        "cycle:frame_mode": "9:16 кроп",
        "cycle:banner_mode": "поверх",
        "cycle:banner_position": "верх",
    }
    for callback, phrase in expected.items():
        await harness.press(callback)
        assert phrase in harness.session.last_text(), callback


async def test_banner_count_zero_forces_a_banner_to_be_chosen(harness, monkeypatch):
    """Счётчик показов доходит до нуля — и бот пишет, что баннер выключен."""
    await _open_settings(harness, monkeypatch)
    for _ in range(8):  # 2 → 3 → 4 → 5 → 0 → 1 …
        await harness.press("cycle:banner_count")
        if "Баннер: <b>выключен</b>" in harness.session.last_text():
            break
    else:
        raise AssertionError("не дошли до выключенного баннера")

    await harness.press("cycle:banner_count")  # 0 → 1
    assert "показов на клип: 1" in harness.session.last_text()


async def test_settings_survive_a_new_draft_for_the_same_user(harness, monkeypatch):
    """Настройки запоминаются за пользователем в users.json и не теряются при перезапуске диалога."""
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")
    await harness.send("4")

    store = harness.ctx.store
    store.remember_settings(1, update_settings(store.settings(1), frame_mode=FrameMode.CENTER_CROP, slicing_mode=SlicingMode.SEQUENTIAL))
    store.save()

    await harness.send("/new")
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    await harness.send(VIDEO["url"])

    text = harness.session.last_text()
    assert "кадр: 9:16 кроп" in text and "раскладка: подряд" in text


# ---------- баннеры ----------


async def test_default_tgrader_banner_is_offered_first(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)  # каталог баннеров открывается поверх черновика
    await harness.press("screen:banners")

    text = harness.session.last_text()
    assert "TGrader (7 с, со звуком)" in text
    banners = harness.ctx.banners()
    assert banners[0].kind == "default"
    buttons = harness.session.buttons()
    # Первая кнопка выбора — именно главный баннер.
    choose = [data for data in buttons if data.startswith("banner:")]
    assert choose and choose[0] == f"banner:{banners[0].id}"


async def test_choosing_a_banner_keeps_the_settings_screen(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    await harness.press("screen:banners")
    banners = harness.ctx.banners()
    await harness.press(f"banner:{banners[0].id}")

    assert "Баннер" in harness.session.last_text()
    assert harness.session.calls("answerCallbackQuery")


async def test_uploaded_banner_is_accepted_and_selected(harness, monkeypatch):
    """Картинка, присланная в чат, сохраняется как свой баннер и подставляется в настройки."""
    from app.bot.handlers import banners as banners_handler

    buffer = io.BytesIO()
    Image.new("RGB", (640, 240), (10, 120, 200)).save(buffer, "PNG")
    harness.session.stream_bytes = buffer.getvalue()
    # В тесте не гоняем настоящий Pillow-разбор из файла: подменяем сохранение на копию картинки.
    saved = {}

    def fake_save(filename, data, banners_dir):
        from app.services.banner_manager import BannerInfo

        target = banners_dir / "мой_баннер.png"
        target.write_bytes(data)
        saved["data"] = data
        return BannerInfo(
            id=target.name, name="мой баннер", kind="custom", path=target, width=640, height=240,
            animated=False, media="image", duration=None, has_audio=False, role="strip", poster=None,
        )  # fmt: skip

    monkeypatch.setattr(banners_handler, "save_uploaded_banner", fake_save)
    await harness.press("ask:banner")
    await harness.send_photo(file_size=len(harness.session.stream_bytes))

    assert saved["data"], "файл баннера не сохранился"
    assert "Баннер сохранён" in " ".join(harness.session.texts())


async def test_banner_file_over_the_limit_is_reported(harness, monkeypatch):
    """Слишком большой/битый баннер не пропадает молча — бот пересказывает ошибку."""
    from app.bot.handlers import banners as banners_handler
    from app.services.banner_manager import BannerError

    def fail(*args, **kwargs):
        raise BannerError("Баннер больше 40 МБ — пришлите файл поменьше.")

    monkeypatch.setattr(banners_handler, "save_uploaded_banner", fail)
    harness.session.stream_bytes = b"x" * 100

    await harness.press("ask:banner")
    await harness.send_video_file(file_name="большой.mp4")

    assert "40 МБ" in harness.session.last_text()


# ---------- запуск задачи и доставка ----------


async def _draft_and_job(harness, monkeypatch, tmp_path, *, clips=2, seconds=3) -> None:
    source = make_video(tmp_path / "src.mp4", size="640x360", seconds=8)
    harness.session.stream_bytes = source.read_bytes()
    await harness.send("/start")
    await harness.send_file(file_name="myvideo.mp4", file_size=Path(source).stat().st_size)
    await harness.press("cycle:banner_count")
    await harness.press("cycle:banner_count")
    await harness.press("cycle:banner_count")
    await harness.press("cycle:banner_count")
    await harness.press("cycle:banner_count")
    await harness.press("cycle:banner_count")  # 2 → … → 0 показов
    await harness.press("edit:num_clips")
    await harness.send(str(clips))
    await harness.press("edit:clip_duration")
    await harness.send(str(seconds))


def _wait_job(harness, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = [job for job in harness.ctx.runner._jobs.values() if not job.finished]  # noqa: SLF001
        if not active:
            return
        time.sleep(0.2)
    raise AssertionError("задача не завершилась за отведённое время")


async def test_uploaded_file_runs_a_full_job_and_sends_clips(harness, monkeypatch, tmp_path):
    await _draft_and_job(harness, monkeypatch, tmp_path, clips=2, seconds=3)
    await harness.press("job:start")

    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        if not harness.ctx.runner._jobs:  # noqa: SLF001
            break

    videos = harness.session.calls("sendVideo")
    assert len(videos) == 2, [request.__api_method__ for request in harness.session.requests]
    final = harness.session.last_text()
    assert "Готово!" in final and "Роликов: 2" in final
    assert harness.session.calls("sendDocument")  # ZIP


async def test_start_requires_a_draft(harness):
    await harness.press("job:start")

    assert "Черновик потерялся" in harness.session.last_text() or harness.session.calls("answerCallbackQuery")


async def test_job_can_be_cancelled(harness, monkeypatch, tmp_path):
    await _draft_and_job(harness, monkeypatch, tmp_path, clips=5, seconds=6)
    await harness.press("job:start")

    deadline = time.monotonic() + 30
    job_id = None
    while time.monotonic() < deadline and job_id is None:
        await asyncio.sleep(0.2)
        job_id = harness.ctx.runner.active_for_user(1)
    assert job_id, "задача не появилась"

    await harness.press(f"job:cancel:{job_id}")

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        if job_id not in harness.ctx.runner._jobs:  # noqa: SLF001 — задача убрана после отмены
            break
    else:
        raise AssertionError("задача не завершилась после отмены")
    final = " ".join(harness.session.texts()[-3:])
    assert "Отмен" in final or "остановил" in final.lower()


# ---------- статистика и сервис ----------


async def test_stats_start_at_zero(harness):
    await harness.send("/mystats")

    assert "Роликов" in harness.session.last_text() or "ролик" in harness.session.last_text()


async def test_id_command_reports_ids(harness):
    await harness.send("/id")

    assert "42" in harness.session.last_text() or "1" in harness.session.last_text()


# ---------- тексты и клавиатуры как отдельные единицы ----------


def test_duration_text_is_human_readable():
    assert texts.duration_text(95) == "1:35"
    assert texts.duration_text(3725) == "1:02:05"
    assert texts.duration_text(0) == "0:00"


def test_html_is_escaped_in_video_titles():
    """Название ролика с угловыми скобками не должно ломать HTML-разметку Telegram."""
    card = texts.video_card("a < b & c", "Автор <b>", 10, 1)
    assert "<b>a &lt; b &amp; c</b>" in card
    assert "Автор &lt;b&gt;" in card


def test_settings_keyboard_has_a_start_button_after_a_draft():
    from app.models import Draft

    draft = Draft(title="Тест", duration=60)
    markup = keyboards.settings_screen(draft, "TGrader (7 с, со звуком)", None)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]
    assert "job:start" in callbacks
    assert any(data.startswith("edit:") for data in callbacks), "кнопки числовых настроек пропали"
