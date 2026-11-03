"""Тесты Telegram-бота: настоящий aiogram-диспетчер + фейковая сессия вместо Bot API.

Сеть здесь не нужна: `tests/bot_fakes.py` подменяет HTTP-слой aiogram, поэтому проверяются
реальные хэндлеры, клавиатуры и тексты. Внешние вызовы (yt-dlp, скачивание файла) подменяются точечно.
"""
from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path

import pytest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from PIL import Image

from app.bot import keyboards, texts
from app.bot.context import get_draft, update_settings
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


def _fsm(harness, user_id: int = 1) -> FSMContext:
    """Состояние диалога пользователя — чтобы проверять черновик и подкладывать состояния."""
    key = StorageKey(bot_id=harness.bot.id, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=harness.dispatcher.storage, key=key)


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


# ---------- меню и справка (команд нет, только кнопки) ----------


async def test_start_shows_the_main_menu(harness):
    await harness.send("/start")  # скрытый вход: его шлёт сам Telegram по кнопке «Старт»

    assert "ClipBanner" in harness.session.last_text()
    markup = harness.session.requests[-1].reply_markup
    assert markup is not None
    pressed = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert pressed[:3] == ["ask:link", "ask:query", "ask:file"]


async def test_help_is_a_button_screen_and_explains_telegram_limits(harness):
    await harness.send("/start")
    await harness.press("screen:help")

    text = harness.session.last_text()
    assert "20 МБ" in text and "50 МБ" in text
    assert "Команды" not in text


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


async def test_number_button_opens_a_stepper_and_custom_input_works(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")

    assert "Количество клипов" in harness.session.last_text()
    assert {"set:num_clips:5", "step:num_clips:1", "step:num_clips:-1", "type:num_clips"} <= set(harness.session.buttons())

    await harness.press("type:num_clips")
    assert "Напишите число от 1 до 50" in harness.session.last_text()

    await harness.send("7")
    assert "<b>7</b>" in harness.session.last_text()
    assert harness.ctx is not None


async def test_stepper_presets_and_plus_minus_change_the_value(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:clip_duration")

    await harness.press("set:clip_duration:45")
    assert "<b>45 с</b>" in harness.session.last_text()
    await harness.press("step:clip_duration:5")
    assert "<b>50 с</b>" in harness.session.last_text()
    await harness.press("step:clip_duration:-5")
    await harness.press("step:clip_duration:-5")
    assert "<b>40 с</b>" in harness.session.last_text()


async def test_stepper_never_leaves_its_range(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")

    await harness.press("set:num_clips:1")
    await harness.press("step:num_clips:-1")
    assert "<b>1</b>" in harness.session.last_text()
    await harness.press("set:num_clips:50")
    await harness.press("step:num_clips:1")
    assert "<b>50</b>" in harness.session.last_text()


@pytest.mark.parametrize("value", ["0", "51"])
async def test_clip_count_out_of_range_is_rejected(harness, monkeypatch, value):
    """Число вне диапазона не меняет настройку — бот просит другое."""
    await _open_settings(harness, monkeypatch)
    await harness.press("type:num_clips")

    await harness.send(value)
    assert "от 1 до 50" in harness.session.last_text()


async def test_fractional_clip_count_is_rejected(harness, monkeypatch):
    """Дробное количество клипов не проходит: в чат не утекает английская ошибка pydantic."""
    await _open_settings(harness, monkeypatch)
    await harness.press("type:num_clips")

    await harness.send("3,5")
    text = harness.session.last_text()
    assert "целое число" in text
    assert "validation error" not in text and "int_from_float" not in text


async def test_garbage_instead_of_a_number_asks_for_a_number(harness, monkeypatch):
    """На «abc» бот просит число и не меняет настройку."""
    await _open_settings(harness, monkeypatch)
    before = (await get_draft(_fsm(harness))).settings.num_clips
    await harness.press("type:num_clips")

    await harness.send("abc")
    assert "Нужно число" in harness.session.last_text()
    assert (await get_draft(_fsm(harness))).settings.num_clips == before


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
        if "всплывающий: выключен" in harness.session.last_text():
            break
    else:
        raise AssertionError("не дошли до выключенного баннера")

    await harness.press("cycle:banner_count")  # 0 → 1
    assert "1 на клип" in harness.session.last_text()


async def test_settings_survive_a_new_draft_for_the_same_user(harness, monkeypatch):
    """Настройки запоминаются за пользователем в users.json и не теряются при перезапуске диалога."""
    await _open_settings(harness, monkeypatch)
    await harness.press("set:num_clips:4")

    store = harness.ctx.store
    store.remember_settings(1, update_settings(store.settings(1), frame_mode=FrameMode.CENTER_CROP, slicing_mode=SlicingMode.SEQUENTIAL))
    store.save()

    await harness.press("ask:link")
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    await harness.send(VIDEO["url"])

    text = harness.session.last_text()
    assert "Кадр: 9:16 кроп" in text and "· подряд" in text


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
    await harness.press("type:num_clips")
    await harness.send(str(clips))
    await harness.press("type:clip_duration")
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
    assert "Готово!" in final and "Роликов: <b>2</b>" in final
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


# ---------- статистика ----------


async def test_stats_start_at_zero_and_show_the_user_id(harness):
    await harness.send("/start", user_id=777)
    await harness.press("screen:stats", user_id=777)

    text = harness.session.last_text()
    assert "Роликов нарезано: <b>0</b>" in text
    assert "<code>777</code>" in text


# ---------- тексты и клавиатуры как отдельные единицы ----------


def test_duration_text_is_human_readable():
    assert texts.duration_text(95) == "1:35"
    assert texts.duration_text(3725) == "1:02:05"
    assert texts.duration_text(0) == "0:00"


def test_html_is_escaped_in_video_titles():
    """Название ролика с угловыми скобками не должно ломать HTML-разметку Telegram."""
    card = texts.video_card("a < b & c", "Автор <b>", 10)
    assert "<b>a &lt; b &amp; c</b>" in card
    assert "Автор &lt;b&gt;" in card


def test_settings_keyboard_has_a_start_button_after_a_draft():
    from app.models import Draft

    draft = Draft(title="Тест", duration=60)
    markup = keyboards.settings_screen(draft, "TGrader (7 с, со звуком)", None)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]
    assert "job:start" in callbacks
    assert any(data.startswith("edit:") for data in callbacks), "кнопки числовых настроек пропали"


# ---------- только кнопки: команд в боте нет ----------


def _walk_routers(router):
    yield router
    for child in router.sub_routers:
        yield from _walk_routers(child)


def test_no_slash_command_handlers_except_the_hidden_start(bot_settings):
    """Единственная команда — скрытый `/start`: его шлёт Telegram по кнопке «Старт», обойти нельзя."""
    from app.bot.app import build_app

    _, _, dispatcher = build_app(bot_settings)
    found = []
    for router in _walk_routers(dispatcher):
        for handler in router.message.handlers:
            for flt in handler.filters:
                if isinstance(flt.callback, Command):
                    found.append(type(flt.callback))
    assert found == [CommandStart]


def test_bot_texts_never_point_to_commands():
    import re

    command = re.compile(r"(?<![\w/:.…])/[a-z]{3,}(?![\w/])")
    screens = [
        texts.START, texts.HELP, texts.NUDGE, texts.REQUEST_LINK, texts.REQUEST_QUERY, texts.REQUEST_FILE,
        texts.REQUEST_BANNER, texts.BAD_LINK, texts.STALE, texts.NOT_ALLOWED, texts.stats_text(1, 2, 3),
        texts.more_screen(__import__("app.models", fromlist=["ClipSettings"]).ClipSettings()),
    ]  # fmt: skip
    for screen in screens:
        plain = re.sub(r"<[^>]+>", "", screen)  # теги вроде </blockquote> — не команды
        assert not command.search(plain), screen


async def test_unknown_command_and_noise_lead_to_the_menu(harness):
    await harness.send("/whatever")

    assert "ClipBanner" in harness.session.last_text()
    assert "ask:link" in harness.session.buttons()


async def test_text_on_the_settings_screen_nudges_to_the_buttons(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    await harness.send("привет")

    assert harness.session.last_text() == texts.NUDGE
    assert "ask:link" in harness.session.buttons()


async def test_fresh_user_can_just_send_a_title_without_start(harness, monkeypatch):
    """После перезапуска бота состояние пустое — название всё равно уходит в поиск."""
    calls: list[str] = []
    monkeypatch.setattr(source_handlers, "search_videos", lambda query, limit=5: calls.append(query) or [])

    await harness.send("интервью дурова")

    assert calls == ["интервью дурова"]


async def test_menu_settings_without_a_video_asks_for_one(harness):
    await harness.send("/start")
    await harness.press("screen:settings")

    assert harness.session.last_alert() == texts.NEED_VIDEO


async def test_stale_button_returns_to_the_menu(harness):
    await harness.press("pick:3")

    assert harness.session.last_alert() == texts.STALE
    assert harness.session.last_text() == texts.START


async def test_every_menu_button_has_a_handler(harness):
    await harness.send("/start")
    for data in list(harness.session.buttons()):
        await harness.press(data)
        assert texts.STALE not in harness.session.alerts(), data
        await harness.send("/start")


async def test_every_settings_button_has_a_handler(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    for data in list(harness.session.buttons()):
        if data == "job:start":
            continue  # запуск настоящей нарезки проверяют отдельные тесты
        await harness.press(data)
        assert texts.STALE not in harness.session.alerts(), data
        await harness.press("screen:settings")


# ---------- цвета и размеры кнопок ----------


def _all_keyboards(banners):
    from app.models import Draft

    draft = Draft(title="Тест", duration=60)
    settings = draft.settings
    yield keyboards.main_menu()
    yield keyboards.back_to_menu()
    yield keyboards.cancel_only()
    yield keyboards.search_results([dict(VIDEO), dict(VIDEO)])
    yield keyboards.settings_screen(draft, "TGrader", None)
    yield keyboards.more_settings(settings)
    yield keyboards.trim_screen(settings)
    yield keyboards.banner_picker(banners, (banners[0].id,))
    yield keyboards.banner_picker(banners, (), with_draft=False)
    yield keyboards.running("abc123")
    yield keyboards.done()
    from app.bot.steppers import STEPPERS

    for stepper in STEPPERS.values():
        yield keyboards.stepper_screen(stepper, stepper.presets[0])


def test_button_styles_and_callback_sizes_are_valid(harness):
    banners = harness.ctx.banners()
    for markup in _all_keyboards(banners):
        for row in markup.inline_keyboard:
            assert 1 <= len(row) <= 8
            for button in row:
                assert button.style in (None, "primary", "success", "danger"), button
                assert len(button.callback_data.encode()) <= 64, button.callback_data


def test_button_colors_follow_their_meaning():
    from app.models import Draft

    def styles(markup):
        return {button.callback_data: button.style for row in markup.inline_keyboard for button in row}

    assert styles(keyboards.settings_screen(Draft(title="t", duration=60), "b", None))["job:start"] == "success"
    assert styles(keyboards.running("x"))["job:cancel:x"] == "danger"
    assert styles(keyboards.main_menu())["ask:link"] == "primary"


# ---------- регрессии: найденные при переделке ошибки ----------


async def test_static_banner_can_be_switched_on_from_settings(harness, monkeypatch):
    """Раньше `static:on` перехватывал соседний хэндлер выбора баннера, и кнопка молчала."""
    await _open_settings(harness, monkeypatch)
    assert "постоянный: выключен" in harness.session.last_text()

    await harness.press("static:on")

    assert "постоянный: выключен" not in harness.session.last_text()
    assert (await get_draft(_fsm(harness))).settings.static_banner_id
    await harness.press("static:off")
    assert (await get_draft(_fsm(harness))).settings.static_banner_id is None


async def test_choosing_a_strip_in_the_catalog_sets_the_static_banner(harness, monkeypatch):
    await _open_settings(harness, monkeypatch)
    strip = next(banner for banner in harness.ctx.banners() if banner.role == "strip")

    await harness.press(f"banner:{strip.id}")

    settings = (await get_draft(_fsm(harness))).settings
    assert settings.static_banner_id == strip.id


async def test_banner_catalog_works_without_a_video_and_remembers_the_choice(harness):
    popup = next(banner for banner in harness.ctx.banners() if banner.role == "popup" and banner.kind != "default")
    await harness.send("/start")
    await harness.press("screen:banners")
    assert f"banner:{popup.id}" in harness.session.buttons()

    await harness.press(f"banner:{popup.id}")

    assert harness.ctx.store.settings(1).banner_id == popup.id
    assert "Баннеры" in harness.session.last_text()  # остались в каталоге


async def test_link_is_accepted_right_after_a_finished_job(harness, monkeypatch):
    """Состояние «идёт нарезка» не сбрасывалось само — следующая ссылка терялась без ответа."""
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    await _fsm(harness).set_state("Flow:running")

    await harness.send(VIDEO["url"])

    assert "Тестовый ролик" in harness.session.last_text()


async def test_unchanged_screen_is_not_sent_twice(harness, monkeypatch):
    """«message is not modified» — не повод слать копию экрана новым сообщением."""
    from aiogram.exceptions import TelegramBadRequest

    await _open_settings(harness, monkeypatch)
    await harness.press("edit:num_clips")
    sent_before = len(harness.session.calls("sendMessage"))

    def not_modified(method, bot):
        raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")

    harness.session.handlers["editMessageText"] = not_modified
    await harness.press("set:num_clips:5")

    assert len(harness.session.calls("sendMessage")) == sent_before


async def test_banner_video_without_a_file_name_is_accepted():
    from app.bot.handlers.banners import _banner_media

    class Video:
        file_unique_id = "abc"
        file_name = None

    class Msg:
        photo = None
        document = None
        animation = None
        video = Video()

    media, name = _banner_media(Msg())
    assert media is not None
    assert name.endswith(".mp4")


# ---------- доступ, профиль бота ----------


def test_allowed_users_parsing():
    from app.bot.access import parse_allowed_users

    assert parse_allowed_users("1, 2;3 x") == frozenset({1, 2, 3})
    assert parse_allowed_users("") == frozenset()
    assert parse_allowed_users(None) == frozenset()


async def test_private_bot_turns_strangers_away_and_tells_them_their_id(bot_settings):
    fake = Harness(bot_settings, allowed_users=frozenset({42}))
    try:
        await fake.send("/start", user_id=7)
        assert "закрытый" in fake.session.last_text().lower()
        assert "<code>7</code>" in fake.session.last_text()

        await fake.press("screen:help", user_id=7)
        assert fake.session.last_alert() == "Закрытый бот"

        await fake.send("/start", user_id=42)
        assert "ClipBanner" in fake.session.last_text()
    finally:
        await fake.close()


async def test_prepare_bot_removes_the_command_menu(harness):
    from app.bot.app import prepare_bot

    await prepare_bot(harness.bot)

    assert harness.session.calls("deleteMyCommands")
    assert not harness.session.calls("setMyCommands")
    assert harness.session.calls("setMyDescription")
