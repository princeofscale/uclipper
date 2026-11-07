"""Тесты Telegram-бота: настоящий aiogram-диспетчер + фейковая сессия вместо Bot API.

Сеть здесь не нужна: `tests/bot_fakes.py` подменяет HTTP-слой aiogram, поэтому проверяются
реальные хэндлеры, клавиатуры и тексты. Внешние вызовы (yt-dlp, скачивание файла) подменяются точечно.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.bot import keyboards, texts
from app.bot.busy import Busy, Cause
from app.bot.context import QUERY_KEY, SCOPE_KEY, get_draft
from app.bot.handlers import source as source_handlers
from app.bot.handlers import trends as trend_handlers
from app.bot.rich import plain_text
from app.bot.runner import RunningJob
from app.bot.states import Flow
from app.config import BANNERS, Settings
from app.models import CutMode, FrameMode, Goal, JobState, JobStatus, SlicingMode
from app.services import capacity
from app.services.platforms import EVERYWHERE, SEARCHABLE
from app.services.platforms import TRENDING as TREND_PLATFORMS
from tests.bot_fakes import Harness, _message
from tests.media_utils import make_video
from tests.rich_check import assert_valid_rich

VIDEO = {
    "platform": "youtube",
    "video_id": "dQw4w9WgXcQ",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "title": "Тестовый ролик",
    "duration": 120.0,
    "uploader": "Автор",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
}

TRENDING = [
    {
        "platform": "youtube",
        "video_id": f"trend{index:06d}",
        "title": f"Подкаст номер {index}",
        "duration": 3600.0 + index,
        "uploader": f"Канал {index}",
        "views": 1_500_000 // index,
        "url": f"https://www.youtube.com/watch?v=trend{index:06d}",
        "thumbnail": "https://i.ytimg.com/vi/x/hqdefault.jpg",
    }
    for index in range(1, 4)
]

WAIT = 120
MENU_BUTTONS = ["ask:link", "ask:query", "ask:file", "screen:trends", "screen:banners", "screen:stats", "screen:help"]
SOURCE_BUTTONS = MENU_BUTTONS[:4]  # кнопки, которые начинают новую нарезку: пока бот занят, они красные и не работают
INFO_BUTTONS = MENU_BUTTONS[4:]  # справочные кнопки работают всегда
SEARCH_PLATFORMS = ["all", "youtube", "rutube", "dailymotion"]  # «Везде» и площадки с поиском по названию
PROMPT_CHIPS = [f"qp:{choice}" for choice in SEARCH_PLATFORMS]  # выбор площадки на экране «Что искать?»
RETRY_CHIPS = [f"qs:{choice}" for choice in SEARCH_PLATFORMS]  # он же под результатами: тот же запрос на другой площадке


def _fsm(harness, user_id: int = 1) -> FSMContext:
    """Состояние диалога пользователя — чтобы проверять черновик и подкладывать состояния."""
    key = StorageKey(bot_id=harness.bot.id, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=harness.dispatcher.storage, key=key)


async def _draft(harness):
    return await get_draft(_fsm(harness))


def _fake_search(results=None, calls: list | None = None):
    """Подмена `search_videos`: запоминает (запрос, площадка) и отдаёт заданные ролики (пусто — «ничего не нашлось»)."""

    def search(query, limit=8, *, platform=EVERYWHERE):
        if calls is not None:
            calls.append((query, platform))
        return [dict(item) for item in (results or [])]

    return search


def _styles(harness) -> dict:
    """Цвет каждой кнопки последнего экрана: callback_data → стиль."""
    return {button.callback_data: button.style for row in harness.session.keyboard() for button in row}


def _occupy(harness, user_id: int = 2, *, progress: float = 42.0, stage: str = "Рендер клипа 3 из 8", delivering: bool = False) -> str:
    """Подкладывает нарезку, которая сейчас идёт у `user_id`: настоящая для теста бота слишком долгая.

    `delivering` — клипы уже готовы, и бот досылает их в чат (задача ещё не убрана из живых).
    """
    manager = harness.ctx.manager
    job_id = manager._create()  # noqa: SLF001
    status = JobStatus.DONE if delivering else JobStatus.RUNNING
    manager._update(job_id, status=status, progress=100.0 if delivering else progress, stage="Готово" if delivering else stage)  # noqa: SLF001
    running = RunningJob(
        job_id=job_id, chat_id=user_id, user_id=user_id, message=None, state=manager.get(job_id), finished=delivering
    )
    harness.ctx.runner._jobs[job_id] = running  # noqa: SLF001
    return job_id


def _release(harness, job_id: str) -> None:
    """Задача закончилась и убрана из живых — бот снова свободен."""
    harness.ctx.runner._jobs.pop(job_id, None)  # noqa: SLF001


# ---------- фикстуры ----------


@pytest.fixture
def bot_settings(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    return settings


@pytest.fixture(autouse=True)
def no_platform_traffic(monkeypatch):
    """Любой выход к площадкам (yt-dlp, RuTube) в этих тестах — ошибка, а не сюрприз: нужное подменяется точечно."""
    import urllib.request

    import yt_dlp

    class Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError("тест не должен ходить к площадкам")

    def forbidden_urlopen(*args, **kwargs):
        raise AssertionError("тест не должен ходить к площадкам")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)


@pytest.fixture
async def harness(bot_settings, monkeypatch):
    # Сценарии «бот занят» описывают предел в одну нарезку: по умолчанию бот ведёт столько, сколько ядер.
    # Отдельный тест про параллельные места выставляет MAX_JOBS сам.
    monkeypatch.setenv("MAX_JOBS", "1")
    fake = Harness(bot_settings)
    yield fake
    await fake.close()


async def _open_video(harness, monkeypatch, **overrides):
    """Присылает ссылку и доходит до выбора режима."""
    video = {**VIDEO, **overrides}
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(video))
    await harness.send("/start")
    await harness.send(video["url"])
    return video


async def _open_manual(harness, monkeypatch, **overrides):
    await _open_video(harness, monkeypatch, **overrides)
    await harness.press("mode:manual")


async def _open_auto(harness, monkeypatch, **overrides):
    await _open_video(harness, monkeypatch, **overrides)
    await harness.press("mode:auto")


# ---------- главное меню ----------


async def test_start_shows_the_branded_menu_without_settings(harness):
    await harness.send("/start")  # скрытый вход: его шлёт сам Telegram по кнопке «Старт»

    text = harness.session.last_text()
    assert "TgRader Clipper" in text
    assert "ClipBanner" not in text
    assert harness.session.buttons() == MENU_BUTTONS
    assert not any("Настройки" in label for label in harness.session.button_texts())
    assert "screen:settings" not in harness.session.buttons()


async def test_menu_is_a_rich_message(harness):
    await harness.send("/start")

    assert harness.session.calls("sendRichMessage")
    assert not harness.session.calls("sendMessage")


async def test_every_menu_button_has_a_handler(harness):
    await harness.send("/start")
    for data in MENU_BUTTONS:
        await harness.press(data)
        assert texts.STALE not in harness.session.alerts(), data
        await harness.send("/start")


async def test_help_explains_modes_trends_and_limits(harness):
    await harness.press("screen:help")

    text = harness.session.last_text()
    assert "Авто" in text and "В тренде" in text and "50 МБ" in text and "20 МБ" in text
    assert "RuTube" in text and "VK Видео" in text and "4 часов" in text
    assert harness.session.buttons() == ["screen:menu"]


async def test_history_starts_empty_and_shows_the_user_id(harness):
    await harness.send("/start", user_id=777)
    await harness.press("screen:stats", user_id=777)

    text = harness.session.last_text()
    assert "История нарезок" in text and "Пока пусто" in text and "777" in text
    assert "777" in text
    assert "<code>777</code>" in harness.session.htmls()[-1]


# ---------- FSM: команда и «назад» обновляют диалог ----------


async def test_banners_back_returns_to_the_menu_not_to_the_cutting_settings(harness, monkeypatch):
    """Ошибка из жизни: меню → «Баннеры» → «Назад» уводило в настройки нарезки старого ролика."""
    await _open_manual(harness, monkeypatch)
    assert await _draft(harness) is not None

    await harness.press("screen:menu")
    await harness.press("screen:banners")
    back = harness.session.buttons()[-1]
    assert back == "screen:menu"
    await harness.press(back)

    text = harness.session.last_text()
    assert "TgRader Clipper" in text and "Ручная настройка" not in text
    assert harness.session.buttons() == MENU_BUTTONS
    assert await _draft(harness) is None


async def test_opening_banners_from_a_draft_forgets_the_draft(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("screen:banners")

    assert await _draft(harness) is None
    await harness.press("screen:menu")
    assert "Ручная настройка" not in harness.session.last_text()


@pytest.mark.parametrize("opener", ["screen:menu", "screen:trends", "screen:banners", "ask:link", "ask:query", "ask:file"])
async def test_top_level_screens_reset_the_dialog(harness, monkeypatch, opener):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")
    assert (await _fsm(harness).get_data()).get("input_field") == "num_clips"

    await harness.press(opener)

    data = await _fsm(harness).get_data()
    assert not data.get("draft") and not data.get("input_field")
    assert await _fsm(harness).get_state() in (
        Flow.waiting_link.state, Flow.waiting_query.state, Flow.waiting_file.state
    )


@pytest.mark.parametrize("command", ["/start", "/whatever", "/menu@some_bot", "/help"])
async def test_any_command_resets_the_dialog_and_shows_the_menu(harness, monkeypatch, command):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")

    await harness.send(command)

    data = await _fsm(harness).get_data()
    assert not data.get("draft") and not data.get("input_field")
    assert await _fsm(harness).get_state() == Flow.waiting_link.state
    assert "TgRader Clipper" in harness.session.last_text()
    assert harness.session.buttons() == MENU_BUTTONS


async def test_text_after_a_command_is_not_taken_for_a_number(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search(calls=calls))
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")
    await harness.send("/start")

    await harness.send("7")

    assert calls == [("7", EVERYWHERE)]  # «7» ушло в поиск, а не в число клипов


async def test_command_does_not_interrupt_a_running_job(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    monkeypatch.setattr(harness.ctx.runner, "active_for_user", lambda user_id: "job-1")

    await harness.send("/start")

    assert await _fsm(harness).get_state() == Flow.running.state
    assert "TgRader Clipper" in harness.session.last_text()


async def test_stale_button_returns_to_the_menu_and_resets(harness):
    await harness.press("pick:3")

    assert harness.session.last_alert() == texts.STALE
    assert "TgRader Clipper" in harness.session.last_text()
    assert await _fsm(harness).get_state() == Flow.waiting_link.state


async def test_noise_leads_to_the_menu(harness):
    await harness.send("   ")

    assert "ask:link" in harness.session.buttons()


async def test_text_on_the_cutting_screen_nudges_to_the_buttons(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.send("привет")

    assert harness.session.last_text() == plain_text(texts.NUDGE)
    assert "ask:link" in harness.session.buttons()


# ---------- баннеры ----------


async def test_banners_screen_lists_both_banners_and_marks_the_default(harness):
    await harness.press("screen:banners")

    text = harness.session.last_text()
    assert "Баннеры" in text and "TGrader" in text and "TgRader v2" in text
    assert "звук" in text.lower() and "7" in text
    labels = harness.session.button_texts()
    assert any(label.startswith("✅") and "TGrader" in label for label in labels)
    assert any(label.startswith("⬜") and "TgRader v2" in label for label in labels)
    assert harness.session.buttons()[:2] == ["banner:pick:TGrader-banner-7s.mp4", "banner:pick:TgRader-v2-16x9-vo.mp4"]
    assert harness.session.buttons()[-1] == "screen:menu"
    assert harness.ctx.banner().id == "TGrader-banner-7s.mp4"  # главный — первый в каталоге
    assert_valid_rich(harness.session.htmls()[-1])


async def test_files_outside_the_catalog_are_never_offered_as_banners(harness, bot_settings):
    from PIL import Image

    Image.new("RGBA", (920, 360), (255, 0, 0, 255)).save(bot_settings.banners_dir / "old_preset.png")

    await harness.press("screen:banners")

    assert harness.ctx.banner().id == "TGrader-banner-7s.mp4"
    assert "old_preset" not in " ".join(harness.session.button_texts() + harness.session.texts())


async def test_the_chosen_banner_goes_into_the_next_video(harness, monkeypatch):
    await harness.press("screen:banners")
    await harness.press("banner:pick:TgRader-v2-16x9-vo.mp4")

    assert "TgRader v2" in harness.session.last_alert()
    labels = harness.session.button_texts()
    assert any(label.startswith("✅") and "TgRader v2" in label for label in labels)
    assert any(label.startswith("⬜") and "TGrader" in label for label in labels)

    await _open_manual(harness, monkeypatch)

    draft = await _draft(harness)
    assert draft.settings.banner_id == "TgRader-v2-16x9-vo.mp4"
    text = harness.session.last_text()
    assert "Баннер TgRader v2" in text and "10 с" in text  # длительность второго баннера в итоге клипа
    assert "Баннер TgRader v2:" in " | ".join(harness.session.button_texts())


async def test_a_picked_banner_survives_a_restart(harness, bot_settings):
    from app.services.user_store import UserStore

    await harness.press("screen:banners")
    await harness.press("banner:pick:TgRader-v2-16x9-vo.mp4")

    assert UserStore(bot_settings.workspace_dir).settings(1).banner_id == "TgRader-v2-16x9-vo.mp4"


async def test_banner_select_button_only_confirms(harness):
    await harness.press("banner:select")  # кнопка из старых сообщений — выбирает главный баннер

    assert "TGrader" in harness.session.last_alert()
    assert harness.ctx.store.settings(1).banner_id == "TGrader-banner-7s.mp4"


async def test_banner_preview_sends_the_video_once_then_reuses_the_file_id(harness):
    def with_video(method, bot):
        return _message(
            bot,
            getattr(method, "chat_id", 1),
            None,
            video={"file_id": "FILE-ID-1", "file_unique_id": "u", "width": 720, "height": 1280, "duration": 7},
        )

    harness.session.handlers["sendVideo"] = with_video
    await harness.press("banner:preview")
    await harness.press("banner:preview")

    sent = harness.session.calls("sendVideo")
    assert len(sent) == 2
    assert sent[1].video == "FILE-ID-1"  # второй раз файл не грузим
    assert "TGrader" in sent[0].caption


async def test_banner_preview_sends_the_banner_from_the_button(harness):
    def with_video(method, bot):
        return _message(
            bot,
            getattr(method, "chat_id", 1),
            None,
            video={"file_id": "FILE-ID-2", "file_unique_id": "u", "width": 1920, "height": 1080, "duration": 10},
        )

    harness.session.handlers["sendVideo"] = with_video
    await harness.press("banner:preview:TgRader-v2-16x9-vo.mp4")

    sent = harness.session.calls("sendVideo")
    assert len(sent) == 1
    assert sent[0].caption.startswith("<b>TgRader v2</b>")
    assert sent[0].width == 1920 and sent[0].height == 1080


# ---------- приём источника ----------


async def test_link_opens_the_mode_picker(harness, monkeypatch):
    await _open_video(harness, monkeypatch)

    text = harness.session.last_text()
    assert "Тестовый ролик" in text and "Автор" in text and "2:00" in text
    assert "Авто" in text and "Вручную" in text
    assert harness.session.buttons() == [
        "mode:quick", "mode:auto", "mode:presets", "mode:manual", "screen:menu"
    ]  # fmt: skip
    draft = await _draft(harness)
    assert draft is not None and draft.title == "Тестовый ролик" and draft.mode is None
    assert await _fsm(harness).get_state() == Flow.editing.state


async def test_search_result_opens_the_mode_picker(harness, monkeypatch):
    results = [dict(VIDEO, video_id=f"abcdefghij{n}", url=f"https://www.youtube.com/watch?v=abcdefghij{n}") for n in "12"]
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search(results))

    await harness.send("интервью дурова")
    assert harness.session.buttons()[:2] == ["pick:0", "pick:1"]
    assert await _fsm(harness).get_state() == Flow.choosing.state

    await harness.press("pick:1")

    assert harness.session.buttons() == [
        "mode:quick", "mode:auto", "mode:presets", "mode:manual", "screen:menu"
    ]  # fmt: skip
    assert (await _draft(harness)).url.endswith("abcdefghij2")


async def test_picking_an_outdated_search_result_is_reported(harness, monkeypatch):
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([VIDEO]))
    await harness.send("что-нибудь")

    await harness.press("pick:7")

    assert "устарел" in harness.session.last_alert()


async def test_short_search_query_is_reported_in_russian(harness):
    await harness.send("a")

    assert "Слишком короткое" in harness.session.last_text()
    assert harness.session.buttons() == [*PROMPT_CHIPS, "screen:menu"]  # можно сразу написать запрос заново или сменить площадку
    assert await _fsm(harness).get_state() == Flow.waiting_query.state


async def test_search_failure_is_reported_with_a_way_back(harness, monkeypatch):
    def boom(query, limit=8, *, platform=EVERYWHERE):
        raise RuntimeError("сеть упала")

    monkeypatch.setattr(source_handlers, "search_videos", boom)
    await harness.send("интервью")

    assert "Поиск не удался" in harness.session.last_text()
    assert harness.session.buttons() == [*RETRY_CHIPS, "qp:all", "screen:menu"]  # тот же запрос — на другой площадке одной кнопкой
    assert (await _fsm(harness).get_data())[QUERY_KEY] == "интервью"


async def test_bad_link_says_what_is_wrong(harness):
    await harness.send("https://example.com/video")

    text = harness.session.last_text()
    assert "Эту ссылку я не узнаю" in text
    assert all(name in text for name in ("YouTube", "RuTube", "VK Видео"))
    assert harness.session.buttons() == ["screen:menu"]


@pytest.mark.parametrize(
    "link",
    ["https://www.youtube.com/@somechannel", "https://rutube.ru/", "https://vk.com", "vk.com.evil.com/video1_2"],
)
async def test_a_page_that_is_not_a_video_is_not_searched_as_a_title(harness, monkeypatch, link):
    def forbidden(query, limit=8, *, platform=EVERYWHERE):
        raise AssertionError("ссылку нельзя искать как название")

    monkeypatch.setattr(source_handlers, "search_videos", forbidden)

    await harness.send(link)

    assert "Эту ссылку я не узнаю" in harness.session.last_text()


@pytest.mark.parametrize(
    ("link", "platform"),
    [
        ("https://rutube.ru/video/0123456789abcdef0123456789abcdef/", "rutube"),
        ("https://vk.com/video-22822305_456239018", "vk"),
        ("https://vkvideo.ru/video-1_2", "vk"),
        ("https://dzen.ru/video/watch/0123456789abcdef01234567", "dzen"),
        ("https://ok.ru/video/123456", "ok"),
        ("https://www.twitch.tv/videos/123456789", "twitch"),
    ],
)
async def test_links_of_other_platforms_open_the_mode_picker_and_name_the_platform(harness, monkeypatch, link, platform):
    asked = []
    monkeypatch.setattr(
        source_handlers, "fetch_video_info", lambda url: asked.append(url) or dict(VIDEO, platform=platform, url=url)
    )

    await harness.send(link)

    text = harness.session.last_text()
    assert asked == [link] and "Тестовый ролик" in text and texts.platform_tag(platform) in text
    assert harness.session.buttons() == [
        "mode:quick", "mode:auto", "mode:presets", "mode:manual", "screen:menu"
    ]  # fmt: skip
    assert (await _draft(harness)).platform == platform


async def test_a_link_is_found_inside_a_longer_message(harness, monkeypatch):
    asked = []
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: asked.append(url) or dict(VIDEO))

    await harness.send("Смотри, это огонь: https://rutube.ru/video/0123456789abcdef0123456789abcdef/ !!!")

    assert asked == ["https://rutube.ru/video/0123456789abcdef0123456789abcdef/"]


async def test_search_results_name_the_platform_of_every_video(harness, monkeypatch):
    results = [
        dict(VIDEO, title="Первый на ютубе"),
        dict(VIDEO, platform="rutube", title="Второй на рутубе", url="https://rutube.ru/video/" + "b" * 32 + "/"),
    ]
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search(results))

    await harness.send("интервью")

    text = harness.session.last_text()
    assert "YouTube" in text and "RuTube" in text
    labels = harness.session.button_texts()
    assert labels[0].startswith("1. ▶️") and labels[1].startswith("2. 📺")

    await harness.press("pick:1")

    draft = await _draft(harness)
    assert draft.platform == "rutube" and draft.url.startswith("https://rutube.ru/video/")
    assert "RuTube" in harness.session.last_text()


async def test_link_failure_is_reported_with_a_way_back(harness, monkeypatch):
    from app.services.downloader import DownloadError

    def boom(url):
        raise DownloadError("Видео недоступно в вашей стране.")

    monkeypatch.setattr(source_handlers, "fetch_video_info", boom)
    await harness.send(VIDEO["url"])

    assert "недоступно" in harness.session.last_text()
    assert harness.session.buttons() == ["screen:menu"]


async def test_fresh_user_can_just_send_a_title_without_start(harness, monkeypatch):
    """После перезапуска бота состояние пустое — название всё равно уходит в поиск."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search(calls=calls))

    await harness.send("интервью дурова")

    assert calls == [("интервью дурова", EVERYWHERE)]


# ---------- выбор площадки при поиске ----------


async def test_the_search_prompt_offers_a_choice_of_platform(harness):
    await harness.press("ask:query")

    text = harness.session.last_text()
    assert "Что искать" in text and "Везде" in text and "только по ссылке" in text
    assert harness.session.buttons() == [*PROMPT_CHIPS, "screen:menu"]
    assert _styles(harness)["qp:all"] == "primary"
    assert await _fsm(harness).get_state() == Flow.waiting_query.state
    assert_valid_rich(harness.session.htmls()[-1])


async def test_the_chosen_platform_is_marked_and_the_next_title_is_searched_only_there(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([VIDEO], calls))
    await harness.press("ask:query")

    await harness.press("qp:rutube")

    assert "RuTube" in harness.session.last_text()
    styles = _styles(harness)
    assert styles["qp:rutube"] == "primary" and styles["qp:all"] is None
    assert (await _fsm(harness).get_data())[SCOPE_KEY] == "rutube"
    assert await _fsm(harness).get_state() == Flow.waiting_query.state

    await harness.send("интервью")

    assert calls == [("интервью", "rutube")]
    assert "RuTube" in harness.session.last_text()
    assert harness.session.buttons() == ["pick:0", *RETRY_CHIPS, "qp:rutube", "screen:menu"]
    assert _styles(harness)["qs:rutube"] == "primary"
    assert_valid_rich(harness.session.htmls()[-1])


async def test_the_same_query_is_repeated_on_another_platform_with_one_button(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([VIDEO], calls))
    await harness.send("интервью")

    await harness.press("qs:dailymotion")

    assert calls == [("интервью", EVERYWHERE), ("интервью", "dailymotion")]
    data = await _fsm(harness).get_data()
    assert data[QUERY_KEY] == "интервью" and data[SCOPE_KEY] == "dailymotion"
    assert "Dailymotion" in harness.session.last_text()
    assert _styles(harness)["qs:dailymotion"] == "primary"
    assert await _fsm(harness).get_state() == Flow.choosing.state
    shown = [plain_text(html) for html in harness.session.htmls()]
    assert any("Ищу" in text and "Dailymotion" in text for text in shown)  # на время поиска — экран ожидания, не пустота


async def test_a_failed_search_keeps_the_query_for_another_platform(harness, monkeypatch):
    from app.services.downloader import DownloadError

    calls: list[tuple[str, str]] = []

    def search(query, limit=8, *, platform=EVERYWHERE):
        calls.append((query, platform))
        if platform == "youtube":
            raise DownloadError("YouTube сейчас не отвечает — выберите другую площадку.")
        return [dict(VIDEO, platform="rutube")]

    monkeypatch.setattr(source_handlers, "search_videos", search)
    await harness.press("qp:youtube")
    await harness.send("кино")

    assert "YouTube сейчас не отвечает" in harness.session.last_text()
    assert harness.session.buttons() == [*RETRY_CHIPS, "qp:youtube", "screen:menu"]
    assert _styles(harness)["qs:youtube"] == "primary"
    assert await _fsm(harness).get_state() == Flow.waiting_query.state

    await harness.press("qs:rutube")

    assert calls == [("кино", "youtube"), ("кино", "rutube")]
    assert harness.session.buttons()[0] == "pick:0" and await _fsm(harness).get_state() == Flow.choosing.state


async def test_nothing_found_says_so_and_offers_the_other_platforms(harness, monkeypatch):
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([]))

    await harness.send("абракадабра")

    assert "ничего не нашлось" in harness.session.last_text()
    assert harness.session.buttons() == [*RETRY_CHIPS, "qp:all", "screen:menu"]


async def test_the_search_again_button_keeps_the_chosen_platform(harness, monkeypatch):
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([VIDEO]))
    await harness.press("qp:rutube")
    await harness.send("интервью")

    await harness.press("qp:rutube")  # «🔄 Искать другое»

    assert "Что искать" in harness.session.last_text() and _styles(harness)["qp:rutube"] == "primary"
    assert await _fsm(harness).get_state() == Flow.waiting_query.state


async def test_search_again_after_the_dialog_was_lost_asks_for_the_query(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([VIDEO], calls))

    await harness.press("qs:rutube")  # например, бот перезапускался и память диалога пуста

    assert calls == []
    assert "Что искать" in harness.session.last_text() and _styles(harness)["qp:rutube"] == "primary"
    assert await _fsm(harness).get_state() == Flow.waiting_query.state


@pytest.mark.parametrize("data", ["qp:vk", "qp:nope", "qp:", "qp:youtube:x", "qs:vk", "qs:twitch", "qs:", "qs:rutube:x"])
async def test_unknown_search_platform_is_reported(harness, data):
    await harness.press(data)

    assert harness.session.alerts() == ["Такой площадки нет — выберите из кнопок"]


async def test_a_stale_search_scope_falls_back_to_everywhere(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([VIDEO], calls))
    await harness.press("ask:query")
    await _fsm(harness).update_data({SCOPE_KEY: "dzen"})  # у этой площадки поиска нет (или данные испорчены)

    await harness.send("интервью")

    assert calls == [("интервью", EVERYWHERE)]


async def test_a_new_menu_search_forgets_the_previous_platform(harness, monkeypatch):
    await harness.press("qp:rutube")

    await harness.press("ask:query")

    assert _styles(harness)["qp:all"] == "primary" and (await _fsm(harness).get_data())[SCOPE_KEY] == EVERYWHERE


async def test_new_link_replaces_the_draft_and_forgets_pending_input(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO, title="Другой ролик"))

    await harness.send("https://youtu.be/AAAAAAAAAAA")

    assert (await _draft(harness)).title == "Другой ролик"
    assert not (await _fsm(harness).get_data()).get("input_field")
    assert harness.session.buttons() == [
        "mode:quick", "mode:auto", "mode:presets", "mode:manual", "screen:menu"
    ]  # fmt: skip


async def test_title_with_markup_characters_is_escaped(harness, monkeypatch):
    await _open_video(harness, monkeypatch, title="a < b & <i>c</i>", uploader="Автор <b>")

    html = harness.session.htmls()[-1]
    assert "a &lt; b &amp; &lt;i&gt;c&lt;/i&gt;" in html
    assert_valid_rich(html)


async def test_uploaded_file_opens_the_mode_picker(harness, tmp_path):
    source = make_video(tmp_path / "src.mp4", size="640x360", seconds=4)
    harness.session.stream_bytes = source.read_bytes()

    await harness.send_file(file_name="myvideo.mp4", file_size=source.stat().st_size)

    assert "myvideo" in harness.session.last_text()
    assert harness.session.buttons() == [
        "mode:quick", "mode:auto", "mode:presets", "mode:manual", "screen:menu"
    ]  # fmt: skip
    assert (await _draft(harness)).upload_id


async def test_non_video_file_is_rejected(harness):
    await harness.send_file(file_name="notes.pdf")

    assert "не видеофайл" in harness.session.last_text()


async def test_oversized_file_is_rejected(harness):
    await harness.send_file(file_name="big.mp4", file_size=30 * 1024 * 1024)

    assert "20 МБ" in harness.session.last_text()


# ---------- режимы: авто и вручную ----------


async def test_auto_mode_builds_a_tiktok_plan_with_goal_buttons(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=3600.0)

    draft = await _draft(harness)
    s = draft.settings
    assert draft.mode is CutMode.AUTO and draft.goal is Goal.TIKTOK
    assert (s.num_clips, s.clip_duration) == (8, 45.0)  # TikTok: не короче 45 с, баннер сверху не в счёт
    assert s.slicing_mode is SlicingMode.HIGHLIGHTS and s.auto_trim
    assert s.frame_mode is FrameMode.BLUR_PAD  # видео целиком по центру, сверху и снизу размытые поля
    assert s.banner_id == "TGrader-banner-7s.mp4" and s.banner_count == 1 and s.banner_sound

    assert harness.session.buttons() == [
        "goal:tiktok", "goal:shorts", "goal:universal", "job:start", "mode:manual", "screen:mode",
    ]  # fmt: skip
    assert "Нарезать 8 клипов" in " ".join(harness.session.button_texts())
    text = harness.session.last_text()
    assert "Авто-подбор" in text and "TikTok" in text and "Тестовый ролик" in text
    assert_valid_rich(harness.session.htmls()[-1])


async def test_the_auto_plan_says_the_video_goes_in_the_middle_with_blurred_fields(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=3600.0)

    text = harness.session.last_text()
    assert "по центру, размытый фон" in text
    assert "9:16, на весь экран" not in text and "на весь экран" not in text


async def test_the_auto_plan_tells_what_it_skips_at_the_edges_of_a_long_video(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=4014.0)  # серия на час: пропускается 5% в начале и 3% в конце

    text = harness.session.last_text()
    assert "Пропускаю" in text and "заставк" in text and "реклам" in text and "титры" in text
    assert "3:20" in text and "2:00" in text  # 200 с в начале и 120 с в конце


async def test_the_auto_plan_does_not_skip_anything_in_a_short_video(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=240.0)

    assert "Пропускаю" not in harness.session.last_text()


async def test_auto_plan_is_honest_it_is_not_a_neural_network():
    assert "не нейросеть" in plain_text(texts.HELP)


async def test_changing_the_goal_rebuilds_the_plan(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=3600.0)

    await harness.press("goal:shorts")
    draft = await _draft(harness)
    assert draft.goal is Goal.SHORTS
    assert (draft.settings.num_clips, draft.settings.clip_duration) == (6, 50.0)
    assert "YouTube Shorts" in harness.session.last_text()

    await harness.press("goal:universal")
    draft = await _draft(harness)
    assert draft.goal is Goal.UNIVERSAL
    assert (draft.settings.num_clips, draft.settings.clip_duration, draft.settings.banner_count) == (6, 30.0, 2)
    assert draft.settings.frame_mode is FrameMode.BLUR_PAD

    await harness.press("goal:nonsense")
    assert "Такой цели нет" in harness.session.last_alert()


async def test_auto_plan_keeps_every_clip_within_shorts_limits(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=3600.0)
    for goal in ("tiktok", "shorts", "universal"):
        await harness.press(f"goal:{goal}")
        s = (await _draft(harness)).settings
        total = s.clip_duration + s.banner_count * (harness.ctx.banner_seconds() or 0)
        assert total <= 59.0, goal


async def test_auto_plan_for_a_short_video_takes_it_whole(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=20.0)

    s = (await _draft(harness)).settings
    assert (s.num_clips, s.clip_duration) == (1, 20.0)
    assert "короткий" in harness.session.last_text()


async def test_auto_plan_for_a_very_long_video_downloads_less(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=2 * 3600.0)

    assert (await _draft(harness)).settings.max_height == 720


async def test_auto_screen_back_goes_to_the_mode_picker(harness, monkeypatch):
    await _open_auto(harness, monkeypatch)

    await harness.press("screen:mode")

    assert harness.session.buttons() == [
        "mode:quick", "mode:auto", "mode:presets", "mode:manual", "screen:menu"
    ]  # fmt: skip
    assert await _fsm(harness).get_state() == Flow.editing.state


async def test_tweaking_the_auto_plan_by_hand_keeps_its_values(harness, monkeypatch):
    await _open_auto(harness, monkeypatch, duration=3600.0)
    planned = (await _draft(harness)).settings

    await harness.press("mode:manual")

    draft = await _draft(harness)
    assert draft.mode is CutMode.MANUAL
    assert draft.settings == planned
    assert "Ручная настройка" in harness.session.last_text()


async def test_manual_mode_screen_explains_every_setting(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    text = harness.session.last_text()
    for part in ("Ручная настройка", "Клипов", "Откуда", "Кадр", "Края ролика", "Баннер TGrader"):
        assert part in text, part
    labels = harness.session.button_texts()
    joined = " | ".join(labels)
    for part in ("Клипов:", "Длина:", "Откуда:", "Кадр:", "Края ролика:", "Баннер TGrader:", "Дополнительно", "Начать нарезку", "Назад"):
        assert part in joined, (part, joined)
    assert_valid_rich(harness.session.htmls()[-1])


async def test_manual_screen_back_goes_to_the_mode_picker(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    assert harness.session.buttons()[-1] == "screen:mode"
    await harness.press("screen:mode")

    assert harness.session.buttons() == [
        "mode:quick", "mode:auto", "mode:presets", "mode:manual", "screen:menu"
    ]  # fmt: skip


async def test_going_back_to_auto_recomputes_the_plan(harness, monkeypatch):
    await _open_manual(harness, monkeypatch, duration=3600.0)
    await harness.press("edit:num_clips")
    await harness.press("set:num_clips:3")
    assert (await _draft(harness)).settings.num_clips == 3

    await harness.press("screen:mode")
    await harness.press("mode:auto")

    draft = await _draft(harness)
    assert draft.mode is CutMode.AUTO and draft.settings.num_clips == 8


async def test_screens_without_a_draft_ask_for_a_video(harness):
    for data in ("mode:auto", "mode:manual", "screen:mode", "screen:more", "screen:trim", "choose:frame_mode", "edit:num_clips", "job:start"):
        await harness.press(data)
        assert harness.session.last_alert() == texts.LOST_DRAFT, data
    await harness.press("screen:manual")
    assert harness.session.last_alert() == texts.NEED_VIDEO


# ---------- ручная настройка: числа ----------


async def test_number_button_opens_a_stepper_and_custom_input_works(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("edit:num_clips")
    assert "Сколько клипов" in harness.session.last_text()
    assert "set:num_clips:5" in harness.session.buttons()

    await harness.press("type:num_clips")
    assert harness.session.buttons() == ["edit:num_clips"]  # одна кнопка «Отмена»
    await harness.send("7")

    assert (await _draft(harness)).settings.num_clips == 7
    assert "set:num_clips:5" in harness.session.buttons()  # вернулись на экран выбора числа


async def test_stepper_presets_and_plus_minus_change_the_value(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("edit:clip_duration")

    await harness.press("set:clip_duration:45")
    assert (await _draft(harness)).settings.clip_duration == 45
    await harness.press("step:clip_duration:5")
    assert (await _draft(harness)).settings.clip_duration == 50
    await harness.press("step:clip_duration:-5")
    await harness.press("step:clip_duration:-5")
    assert (await _draft(harness)).settings.clip_duration == 40


async def test_stepper_never_leaves_its_range(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("edit:num_clips")

    await harness.press("set:num_clips:50")
    await harness.press("step:num_clips:1")
    assert (await _draft(harness)).settings.num_clips == 50
    await harness.press("set:num_clips:1")
    await harness.press("step:num_clips:-1")
    assert (await _draft(harness)).settings.num_clips == 1


@pytest.mark.parametrize("value", ["0", "51", "-3", "1000"])
async def test_clip_count_out_of_range_is_rejected(harness, monkeypatch, value):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")

    await harness.send(value)

    assert "от 1 до 50" in harness.session.last_text()
    assert (await _draft(harness)).settings.num_clips == 5
    assert (await _fsm(harness).get_data()).get("input_field") == "num_clips"  # ждём число дальше


async def test_fractional_clip_count_is_rejected(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")

    await harness.send("2.5")

    assert "целое" in harness.session.last_text()
    assert (await _draft(harness)).settings.num_clips == 5


async def test_garbage_instead_of_a_number_asks_for_a_number(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:clip_duration")

    await harness.send("тридцать")

    assert "Нужно число" in harness.session.last_text()
    assert (await _draft(harness)).settings.clip_duration == 30


async def test_cancelling_number_input_stops_waiting_for_a_number(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")
    await harness.press("edit:num_clips")  # «Отмена»

    assert not (await _fsm(harness).get_data()).get("input_field")
    await harness.send("9")
    assert (await _draft(harness)).settings.num_clips == 5
    assert harness.session.last_text() == plain_text(texts.NUDGE)


async def test_fractional_seconds_are_accepted_for_offsets_and_commas_work(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("screen:trim")
    await harness.press("type:start_offset")

    await harness.send("12,5")

    assert (await _draft(harness)).settings.start_offset == 12.5


async def test_banner_steppers_exist_and_are_bounded(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("edit:banner_count")
    await harness.press("set:banner_count:3")
    await harness.press("edit:banner_scale")
    await harness.press("set:banner_scale:70")
    await harness.press("step:banner_scale:100")

    s = (await _draft(harness)).settings
    assert s.banner_count == 3 and s.banner_scale == 95
    assert "95%" in harness.session.last_text()


# ---------- ручная настройка: варианты и переключатели ----------


@pytest.mark.parametrize(
    ("field", "value", "check"),
    [
        ("frame_mode", "center_crop", lambda s: s.frame_mode is FrameMode.CENTER_CROP),
        ("frame_mode", "original", lambda s: s.frame_mode is FrameMode.ORIGINAL),
        ("slicing_mode", "highlights", lambda s: s.slicing_mode is SlicingMode.HIGHLIGHTS),
        ("slicing_mode", "sequential", lambda s: s.slicing_mode is SlicingMode.SEQUENTIAL),
        ("banner_mode", "overlay", lambda s: s.banner_mode.value == "overlay"),
        ("banner_position", "top", lambda s: s.banner_position.value == "top"),
    ],
)
async def test_choice_screens_apply_the_option_and_return_to_the_settings(harness, monkeypatch, field, value, check):
    await _open_manual(harness, monkeypatch)

    await harness.press(f"choose:{field}")
    assert f"opt:{field}:{value}" in harness.session.buttons()
    assert harness.session.buttons()[-1] == "screen:manual"
    assert_valid_rich(harness.session.htmls()[-1])

    await harness.press(f"opt:{field}:{value}")

    assert check((await _draft(harness)).settings)
    assert "Ручная настройка" in harness.session.last_text()


async def test_choice_screen_explains_every_option_and_marks_the_current_one(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("choose:frame_mode")

    text = harness.session.last_text()
    for part in ("По центру, размытый фон", "На весь экран", "16:9, как есть", "слежу за лицом", "из 720p выходит мягче"):
        assert part in text, part
    assert harness.session.button_texts()[0].startswith("✅")  # по умолчанию размытый фон


async def test_quality_choice_returns_to_the_extra_screen(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("screen:more")
    assert "choose:max_height" in harness.session.buttons()

    await harness.press("choose:max_height")
    await harness.press("opt:max_height:720")

    assert (await _draft(harness)).settings.max_height == 720
    assert "Дополнительно" in harness.session.last_text()
    assert harness.session.buttons()[-1] == "screen:manual"


async def test_unknown_option_is_reported(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("opt:frame_mode:sideways")
    assert "больше нет" in harness.session.last_alert()
    await harness.press("choose:nothing")
    assert "нельзя изменить" in harness.session.last_alert()


async def test_banner_can_be_switched_off_and_on(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    assert (await _draft(harness)).settings.banner_count == 2
    assert "edit:banner_count" in harness.session.buttons()

    await harness.press("toggle:banner")
    assert (await _draft(harness)).settings.banner_count == 0
    assert "edit:banner_count" not in harness.session.buttons()  # настройки баннера спрятаны
    assert "Выключен" in harness.session.last_text()

    await harness.press("toggle:banner")
    assert (await _draft(harness)).settings.banner_count == 1
    assert "edit:banner_count" in harness.session.buttons()


async def test_sound_and_trim_and_sfx_toggles(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("toggle:banner_sound")
    assert not (await _draft(harness)).settings.banner_sound

    await harness.press("screen:trim")
    await harness.press("toggle:auto_trim:trim")
    assert (await _draft(harness)).settings.auto_trim
    assert "Автообрезка" in harness.session.last_text()

    await harness.press("screen:manual")
    await harness.press("screen:more")
    await harness.press("toggle:enable_sfx:more")
    assert (await _draft(harness)).settings.enable_sfx
    assert "Дополнительно" in harness.session.last_text()


async def test_trim_screen_buttons_say_what_they_do(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("screen:trim")

    joined = " | ".join(harness.session.button_texts())
    assert "Убрать заставку и титры" in joined and "Пропустить начало" in joined and "Пропустить конец" in joined


async def test_settings_survive_a_new_draft_for_the_same_user(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("choose:frame_mode")
    await harness.press("opt:frame_mode:original")
    harness.ctx.store.remember_settings(1, (await _draft(harness)).to_request())

    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO, title="Второй"))
    await harness.send("https://youtu.be/AAAAAAAAAAA")
    await harness.press("mode:manual")

    assert (await _draft(harness)).settings.frame_mode is FrameMode.ORIGINAL


async def test_stored_settings_with_an_old_banner_are_replaced_by_tgrader(harness, monkeypatch):
    from app.bot.context import update_settings

    stored = update_settings(harness.ctx.store.settings(1), banner_id="preset_gone.png", banner_count=3)
    harness.ctx.store.settings = lambda user_id: stored  # старый сохранённый выбор из прошлой версии бота
    await _open_manual(harness, monkeypatch)

    s = (await _draft(harness)).settings
    assert s.banner_id == "TGrader-banner-7s.mp4" and s.banner_count == 3


async def test_without_banner_files_clips_go_without_a_banner(harness, monkeypatch, bot_settings):
    for spec in BANNERS:
        (bot_settings.banners_dir / spec.file).unlink()
    await _open_manual(harness, monkeypatch)

    s = (await _draft(harness)).settings
    assert s.banner_count == 0 and s.banner_id is None
    assert "toggle:banner" not in harness.session.buttons()
    await harness.press("mode:auto")
    assert (await _draft(harness)).settings.banner_count == 0
    await harness.press("screen:banners")
    assert "Баннеров пока нет" in harness.session.last_text()


# ---------- тренды ----------


async def test_trends_home_offers_a_choice_of_platform_and_genres_and_goes_back(harness):
    await harness.press("screen:trends")

    text = harness.session.last_text()
    assert "В тренде" in text and "TikTok" in text and "Везде" in text
    buttons = harness.session.buttons()
    assert buttons[:3] == ["trends:all", "trends:youtube", "trends:rutube"]
    assert "trend:all:podcast" in buttons and buttons[-1] == "screen:menu"
    assert _styles(harness)["trends:all"] == "primary"  # по умолчанию тренды сразу со всех площадок
    assert_valid_rich(harness.session.htmls()[-1])


async def test_the_platform_switch_marks_the_current_one_and_shows_its_genres(harness):
    await harness.press("trends:rutube")

    assert "RuTube" in harness.session.last_text()
    buttons = harness.session.buttons()
    assert "trend:rutube:podcast" in buttons and "trend:youtube:podcast" not in buttons and "trend:all:podcast" not in buttons
    styles = _styles(harness)
    assert styles["trends:rutube"] == "primary" and styles["trends:youtube"] is None and styles["trends:all"] is None
    assert_valid_rich(harness.session.htmls()[-1])


def _fake_fetch(calls: list | None = None, videos: list | None = None):
    """Подмена `fetch_category`: запоминает (площадка, жанр) и отдаёт заданные ролики."""

    def fetch(category, platform="youtube", limit=8):
        if calls is not None:
            calls.append((platform, category.id))
        return [dict(video) for video in (TRENDING if videos is None else videos)]

    return fetch


async def test_trend_genre_lists_videos_and_is_cached(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch(calls))

    await harness.press("trend:youtube:podcast")

    text = harness.session.last_text()
    assert "Подкаст номер 1" in text and "1,5 млн" in text and "Канал 2" in text
    assert harness.session.buttons() == [
        "pick:0", "pick:1", "pick:2", "trend:all:podcast", "trend:youtube:podcast", "trend:rutube:podcast",
        "trend:dailymotion:podcast", "trends:youtube", "screen:menu",
    ]  # fmt: skip
    assert _styles(harness)["trend:youtube:podcast"] == "primary"  # тот же жанр на другой площадке — одной кнопкой
    assert_valid_rich(harness.session.htmls()[-1])
    assert await _fsm(harness).get_state() == Flow.choosing.state

    await harness.press("screen:trends")
    await harness.press("trend:youtube:podcast")
    assert calls == [("youtube", "podcast")]  # второй раз — из кеша


async def test_each_platform_has_its_own_cache_and_its_back_button_stays_on_it(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch(calls))

    await harness.press("trend:youtube:humor")
    await harness.press("trend:rutube:humor")
    assert calls == [("youtube", "humor"), ("rutube", "humor")]
    assert "RuTube" in harness.session.last_text()
    assert harness.session.buttons()[-2:] == ["trends:rutube", "screen:menu"]  # «К жанрам» остаётся на той же площадке

    await harness.press("trend:rutube:humor")
    await harness.press("trend:youtube:humor")
    assert len(calls) == 2  # оба раза из кеша


async def test_old_genre_buttons_without_a_platform_still_work_as_youtube(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch(calls))

    await harness.press("trend:podcast")  # кнопка из сообщения, отправленного до появления RuTube

    assert calls == [("youtube", "podcast")] and "Подкаст номер 1" in harness.session.last_text()


async def test_cold_trend_request_shows_a_loading_screen_first(harness, monkeypatch):
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch())

    await harness.press("trend:rutube:humor")

    shown = [plain_text(html) for html in harness.session.htmls()]
    assert any("Смотрю, что сейчас в тренде" in text and "RuTube" in text for text in shown)
    assert "Подкаст номер 1" in shown[-1]


async def test_picking_a_trending_video_goes_straight_to_the_tiktok_plan(harness, monkeypatch):
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch())
    await harness.press("trend:youtube:podcast")

    await harness.press("pick:1")

    draft = await _draft(harness)
    assert draft.title == "Подкаст номер 2" and draft.mode is CutMode.AUTO and draft.goal is Goal.TIKTOK
    assert draft.platform == "youtube"
    assert "job:start" in harness.session.buttons() and "goal:shorts" in harness.session.buttons()
    assert "Авто-подбор" in harness.session.last_text()


async def test_picking_a_rutube_trend_remembers_the_platform(harness, monkeypatch):
    videos = [dict(video, platform="rutube", url=f"https://rutube.ru/video/{'a' * 31}{index}/") for index, video in enumerate(TRENDING)]
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch(videos=videos))
    await harness.press("trend:rutube:podcast")

    await harness.press("pick:0")

    draft = await _draft(harness)
    assert draft.platform == "rutube" and draft.url == videos[0]["url"]
    assert "RuTube" in harness.session.last_text()


async def test_trend_error_offers_a_retry(harness, monkeypatch):
    from app.services.trends import TrendsError

    attempts = []

    def flaky(category, platform="youtube", limit=8):
        attempts.append(1)
        if len(attempts) == 1:
            raise TrendsError("RuTube сейчас не отвечает — попробуйте через пару минут.")
        return [dict(v) for v in TRENDING]

    monkeypatch.setattr(trend_handlers, "fetch_category", flaky)
    await harness.press("trend:rutube:games")

    assert "не отвечает" in harness.session.last_text()
    assert harness.session.buttons() == ["trend:rutube:games", "trends:rutube"]


async def test_unexpected_trend_failure_is_reported_politely(harness, monkeypatch):
    def broken(category, platform="youtube", limit=8):
        raise ValueError("внезапно")

    monkeypatch.setattr(trend_handlers, "fetch_category", broken)
    await harness.press("trend:youtube:money")

    assert "Попробуйте ещё раз" in harness.session.last_text()
    assert "внезапно" not in harness.session.last_text()
    assert harness.session.buttons() == ["trend:youtube:money", "trends:youtube"]


async def test_empty_trend_list_is_not_cached(harness, monkeypatch):
    calls = []

    def empty_then_full(category, platform="youtube", limit=8):
        calls.append(1)
        return [] if len(calls) == 1 else [dict(v) for v in TRENDING]

    monkeypatch.setattr(trend_handlers, "fetch_category", empty_then_full)
    await harness.press("trend:youtube:sport")
    assert "ничего подходящего" in harness.session.last_text() and "YouTube" in harness.session.last_text()

    await harness.press("trend:youtube:sport")
    assert "Подкаст номер 1" in harness.session.last_text()


def _trend_videos(platform: str, count: int = 3) -> list[dict]:
    """Подборка одной площадки: у роликов её значок, название и адрес."""
    host = "https://rutube.ru/video/" if platform == "rutube" else "https://www.youtube.com/watch?v="
    return [
        dict(video, platform=platform, title=f"{platform} {video['title']}", url=f"{host}{platform[0] * 31}{index}{'/' * (platform == 'rutube')}")
        for index, video in enumerate(TRENDING[:count])
    ]


def _platform_fetch(per_platform: dict, calls: list | None = None):
    """Подмена `fetch_category` с разным ответом у каждой площадки: список роликов или исключение (читается в момент вызова)."""

    def fetch(category, platform="youtube", limit=8):
        if calls is not None:
            calls.append((platform, category.id))
        answer = per_platform.get(platform, [])
        if isinstance(answer, Exception):
            raise answer
        return [dict(item) for item in answer]

    return fetch


async def test_everywhere_trends_mix_both_platforms_and_share_the_cache(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    answers = {
        "youtube": _trend_videos("youtube"),
        "rutube": _trend_videos("rutube"),
        "dailymotion": _trend_videos("dailymotion"),
    }
    monkeypatch.setattr(trend_handlers, "fetch_category", _platform_fetch(answers, calls))

    await harness.press("trend:all:podcast")

    assert sorted(calls) == [("dailymotion", "podcast"), ("rutube", "podcast"), ("youtube", "podcast")]
    text = harness.session.last_text()
    assert "youtube Подкаст номер 1" in text and "rutube Подкаст номер 1" in text and "Везде" in text
    labels = harness.session.button_texts()
    assert labels[0].startswith("1. ▶️") and labels[1].startswith("2. 📺")  # вперемешку: первый с одной, первый с другой…
    assert harness.session.buttons()[:6] == [f"pick:{index}" for index in range(6)]
    assert _styles(harness)["trend:all:podcast"] == "primary"
    assert await _fsm(harness).get_state() == Flow.choosing.state
    assert_valid_rich(harness.session.htmls()[-1])

    await harness.press("trend:rutube:podcast")  # кеш общий с «Везде»: площадки заново не спрашиваем
    await harness.press("trend:youtube:podcast")
    await harness.press("trend:all:podcast")
    assert len(calls) == 3  # по разу на площадку: дальше всё из общего кеша


async def test_everywhere_trends_show_the_survivor_when_one_platform_is_down(harness, monkeypatch):
    from app.services.trends import TrendsError

    answers = {
        "youtube": TrendsError("YouTube сейчас не отвечает — попробуйте через пару минут."),
        "rutube": _trend_videos("rutube"),
        "dailymotion": _trend_videos("dailymotion"),
    }
    monkeypatch.setattr(trend_handlers, "fetch_category", _platform_fetch(answers))

    await harness.press("trend:all:humor")

    text = harness.session.last_text()
    assert "rutube Подкаст номер 1" in text and "YouTube сейчас не ответил" in text
    assert harness.session.buttons()[:3] == ["pick:0", "pick:1", "pick:2"]
    assert await _fsm(harness).get_state() == Flow.choosing.state
    assert_valid_rich(harness.session.htmls()[-1])


async def test_everywhere_trends_report_an_error_only_when_nobody_answers(harness, monkeypatch):
    from app.services.trends import TrendsError

    answers = {
        "youtube": TrendsError("YouTube сейчас не отвечает."),
        "rutube": ValueError("внезапно"),
        "dailymotion": TrendsError("Dailymotion сейчас не отвечает."),
    }
    monkeypatch.setattr(trend_handlers, "fetch_category", _platform_fetch(answers))

    await harness.press("trend:all:games")

    text = harness.session.last_text()
    assert "Ни одна площадка сейчас не отвечает" in text and "внезапно" not in text
    assert harness.session.buttons() == ["trend:all:games", "trends:all"]
    assert await _fsm(harness).get_state() != Flow.choosing.state


async def test_everywhere_trends_do_not_cache_an_empty_platform(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    answers = {"youtube": [], "rutube": _trend_videos("rutube"), "dailymotion": _trend_videos("dailymotion")}
    monkeypatch.setattr(trend_handlers, "fetch_category", _platform_fetch(answers, calls))
    await harness.press("trend:all:sport")
    assert harness.session.buttons()[:3] == ["pick:0", "pick:1", "pick:2"]  # пока RuTube и Dailymotion

    answers["youtube"] = _trend_videos("youtube")  # YouTube наконец ответил
    await harness.press("trend:all:sport")

    # RuTube и Dailymotion берутся из кеша, заново спрашиваем только YouTube
    assert sorted(calls) == [
        ("dailymotion", "sport"),
        ("rutube", "sport"),
        ("youtube", "sport"),
        ("youtube", "sport"),
    ]
    assert "youtube Подкаст номер 1" in harness.session.last_text()


async def test_the_loading_screen_for_everywhere_names_no_single_platform(harness, monkeypatch):
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch())

    await harness.press("trend:all:humor")

    shown = [plain_text(html) for html in harness.session.htmls()]
    assert any("Смотрю, что сейчас в тренде" in text and "на всех площадках" in text for text in shown)


async def test_picking_a_video_from_everywhere_trends_remembers_its_platform(harness, monkeypatch):
    answers = {"youtube": _trend_videos("youtube"), "rutube": _trend_videos("rutube")}
    monkeypatch.setattr(trend_handlers, "fetch_category", _platform_fetch(answers))
    await harness.press("trend:all:podcast")

    await harness.press("pick:1")  # вторым в списке идёт ролик RuTube

    draft = await _draft(harness)
    assert draft.platform == "rutube" and draft.url.startswith("https://rutube.ru/video/")
    assert draft.mode is CutMode.AUTO and draft.goal is Goal.TIKTOK


async def test_the_movies_genre_asks_each_platform_its_own_queries(harness, monkeypatch):
    seen: dict[str, tuple[str, ...]] = {}

    def fetch(category, platform="youtube", limit=8):
        seen[platform] = category.queries_for(platform)
        return _trend_videos(platform)

    monkeypatch.setattr(trend_handlers, "fetch_category", fetch)

    await harness.press("trend:all:movies")

    assert set(seen) == {"youtube", "rutube", "dailymotion"}
    assert seen["youtube"] != seen["rutube"]  # на RuTube — целые фильмы и сериалы, а на YouTube — разборы
    assert "VK Видео" in harness.session.last_text()  # и честная подсказка, где их больше всего


@pytest.mark.parametrize(
    ("data", "alert"),
    [
        ("trend:youtube:nothing", "Такого раздела нет"),
        ("trend:all:nothing", "Такого раздела нет"),
        ("trend:nothing", "Такого раздела нет"),
        ("trend:nope:podcast", "Такого раздела нет"),
        ("trend:vk:podcast", "Такого раздела нет"),  # у VK нет ни поиска, ни трендов
        ("trend:a:b:c", "Такого раздела нет"),
        ("trend:", "Такого раздела нет"),
        ("trends:nope", "Такой площадки нет"),
        ("trends:all:x", "Такой площадки нет"),
        ("trends:vk", "Такой площадки нет"),  # у VK Видео нет ни поиска, ни трендов
        ("trend:vk:podcast", "Такого раздела нет"),
        ("trends:", "Такой площадки нет"),
    ],
)
async def test_unknown_trend_section_is_reported(harness, data, alert):
    await harness.press(data)

    assert harness.session.alerts() == [alert + " — выберите из кнопок"]


# ---------- запуск задачи и доставка ----------


async def _prepare_job(harness, tmp_path, *, clips=2, seconds=3) -> None:
    source = make_video(tmp_path / "src.mp4", size="640x360", seconds=8)
    harness.session.stream_bytes = source.read_bytes()
    await harness.send("/start")
    await harness.send_file(file_name="myvideo.mp4", file_size=Path(source).stat().st_size)
    await harness.press("mode:manual")
    await harness.press("toggle:banner")  # без баннера: задача считается быстро
    await harness.press("type:num_clips")
    await harness.send(str(clips))
    await harness.press("type:clip_duration")
    await harness.send(str(seconds))


async def _wait_finished(harness, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        if not harness.ctx.runner._jobs:  # noqa: SLF001
            return
    raise AssertionError("задача не завершилась за отведённое время")


async def test_manual_job_runs_and_sends_clips(harness, tmp_path):
    await _prepare_job(harness, tmp_path, clips=2, seconds=3)
    await harness.press("job:start")
    await _wait_finished(harness)

    videos = harness.session.calls("sendVideo")
    assert len(videos) == 2, [request.__api_method__ for request in harness.session.requests]
    assert "Клип 1 из 2" in videos[0].caption
    final = harness.session.last_text()
    assert "Готово" in final and "Клипов — 2" in final
    assert harness.session.calls("sendDocument")  # ZIP
    assert harness.session.buttons() == ["ask:link", "screen:mode", "screen:trends", "screen:menu"]
    for html in harness.session.htmls():
        if html.startswith("<"):
            assert_valid_rich(html)


async def test_job_start_requires_a_draft(harness):
    await harness.press("job:start")

    assert harness.session.last_alert() == texts.LOST_DRAFT


async def test_second_start_is_refused_while_a_job_runs(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    monkeypatch.setattr(harness.ctx.runner, "active_for_user", lambda user_id: "job-1")

    await harness.press("job:start")

    assert "ещё идёт" in harness.session.last_alert()


async def test_messages_during_my_own_cut_show_the_progress_and_a_stop_button(harness):
    job = _occupy(harness, user_id=1, progress=33, stage="Рендер клипа 2 из 6")
    await _fsm(harness).set_state(Flow.running)

    await harness.send("привет")

    text = harness.session.last_text()
    assert "Идёт ваша нарезка" in text and "33" in text and "Рендер клипа 2 из 6" in text
    assert f"job:cancel:{job}" in harness.session.buttons()


async def test_auto_plan_converts_into_a_valid_job_request(harness, monkeypatch):
    started = []

    async def capture(chat_id, user_id, request, message):
        started.append(request)
        return "job-1"

    monkeypatch.setattr(harness.ctx.runner, "start", capture)
    await _open_auto(harness, monkeypatch, duration=1800.0)

    await harness.press("job:start")

    assert len(started) == 1
    request = started[0]
    assert request.banner_id == "TGrader-banner-7s.mp4" and request.banner_count == 1
    assert request.slicing_mode is SlicingMode.HIGHLIGHTS and request.auto_trim
    assert request.url == VIDEO["url"]
    assert await _fsm(harness).get_state() == Flow.running.state


async def test_start_is_refused_when_the_disk_is_almost_full(harness, monkeypatch):
    started = []

    async def start(chat_id, user_id, request, message):
        started.append(request)
        return "job-1"

    monkeypatch.setattr(harness.ctx.runner, "start", start)
    await _open_auto(harness, monkeypatch)
    monkeypatch.setattr(capacity, "free_bytes", lambda path: 1 * 1024**3)  # место кончилось, пока человек выбирал

    await harness.press("job:start")

    assert "места" in harness.session.last_alert() and not started
    assert "job:start" in harness.session.buttons()  # план на экране остался


async def test_the_limits_are_off_when_the_environment_says_zero(harness, monkeypatch):
    started = []

    async def start(chat_id, user_id, request, message):
        started.append(request)
        return "job-1"

    monkeypatch.setenv("MAX_JOBS", "0")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    _occupy(harness, user_id=2)
    _occupy(harness, user_id=3)
    monkeypatch.setattr(capacity, "free_bytes", lambda path: 0)
    monkeypatch.setattr(harness.ctx.runner, "start", start)
    await _open_auto(harness, monkeypatch)

    await harness.press("job:start")

    assert len(started) == 1


async def test_a_waiting_job_shows_its_place_in_the_queue_and_can_be_stopped(harness, monkeypatch):
    from app.bot import runner as runner_module
    from app.bot.runner import RunningJob

    shown = []

    async def edit(message, html, markup=None):
        shown.append((html, markup))

    monkeypatch.setattr(runner_module, "edit_html", edit)
    manager = harness.ctx.manager
    first, second = manager._create(), manager._create()  # noqa: SLF001 — обе стоят в очереди

    async def progress(job_id):
        running = RunningJob(job_id=job_id, chat_id=1, user_id=1, message=None, state=manager.get(job_id))
        await harness.ctx.runner._edit_progress(running)  # noqa: SLF001
        return shown[-1]

    html, markup = await progress(first)
    assert "Перед вами" not in plain_text(html)  # он следующий — ждать некого
    assert [button.callback_data for row in markup.inline_keyboard for button in row] == [f"job:cancel:{first}"]

    html, markup = await progress(second)
    assert "Перед вами в очереди: 1" in plain_text(html)
    assert [button.callback_data for row in markup.inline_keyboard for button in row] == [f"job:cancel:{second}"]


async def test_start_failure_is_reported_and_the_dialog_stays_editable(harness, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("очередь сломалась")

    monkeypatch.setattr(harness.ctx.runner, "start", broken)
    await _open_auto(harness, monkeypatch)

    await harness.press("job:start")

    assert "Не получилось запустить" in harness.session.last_text()
    assert await _fsm(harness).get_state() == Flow.editing.state


async def test_job_can_be_cancelled(harness, tmp_path):
    await _prepare_job(harness, tmp_path, clips=5, seconds=6)
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
    assert "остановил" in final.lower() or "Отмен" in final


async def test_link_is_accepted_right_after_a_finished_job(harness, monkeypatch):
    """Состояние «идёт нарезка» не сбрасывалось само — следующая ссылка терялась без ответа."""
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    await _fsm(harness).set_state(Flow.running)

    await harness.send(VIDEO["url"])

    assert "Тестовый ролик" in harness.session.last_text()


# ---------- один ролик за раз ----------


async def test_the_menu_is_live_while_nobody_is_cutting(harness):
    await harness.send("/start")

    styles = _styles(harness)
    assert all(styles[data] != keyboards.DANGER for data in MENU_BUTTONS)
    assert "menu:refresh" not in styles
    assert "занят" not in harness.session.last_text()


async def test_the_menu_turns_red_while_another_person_is_cutting(harness):
    _occupy(harness, user_id=2, progress=42)

    await harness.send("/start")

    styles = _styles(harness)
    assert harness.session.buttons()[:1] == ["menu:refresh"] or "menu:refresh" in styles
    assert [styles[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4
    assert all(styles[data] != keyboards.DANGER for data in INFO_BUTTONS)
    assert not any(data.startswith("job:cancel:") for data in styles)  # чужую нарезку не остановить
    text = harness.session.last_text()
    assert "Бот сейчас занят" in text and "другого человека" in text and "42" in text
    assert "ClipBanner" not in text
    assert_valid_rich(harness.session.htmls()[-1])


async def test_the_menu_shows_my_own_cut_with_a_stop_button(harness):
    job = _occupy(harness, user_id=1, progress=17, stage="Скачивание видео")

    await harness.send("/start")

    styles = _styles(harness)
    assert [styles[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4
    assert styles[f"job:cancel:{job}"] == keyboards.DANGER
    text = harness.session.last_text()
    assert "Идёт ваша нарезка" in text and "17" in text and "Скачивание видео" in text
    assert_valid_rich(harness.session.htmls()[-1])


@pytest.mark.parametrize("button", SOURCE_BUTTONS)
async def test_red_buttons_do_nothing_but_explain(harness, button):
    await harness.send("/start")  # меню было живым...
    _occupy(harness, user_id=2)  # ...а потом бот занялся чужим роликом

    await harness.press(button)

    assert "нарезает другой ролик" in harness.session.last_alert()
    assert "Бот сейчас занят" in harness.session.last_text()  # на экране — актуальное красное меню
    assert [_styles(harness)[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4
    assert await _fsm(harness).get_state() == Flow.waiting_link.state  # ни ссылку, ни название, ни файл не ждём


async def test_a_link_is_not_looked_up_while_the_bot_is_busy(harness, monkeypatch):
    def forbidden(url):
        raise AssertionError("пока бот занят, ролик не запрашивается")

    monkeypatch.setattr(source_handlers, "fetch_video_info", forbidden)
    _occupy(harness, user_id=2)

    await harness.send(VIDEO["url"])

    assert "Бот сейчас занят" in harness.session.last_text()
    assert "mode:auto" not in harness.session.buttons()


async def test_a_title_is_not_searched_while_the_bot_is_busy(harness, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search(calls=calls))
    _occupy(harness, user_id=2)

    await harness.send("дуров интервью")

    assert calls == [] and "Бот сейчас занят" in harness.session.last_text()


async def test_a_file_is_not_downloaded_while_the_bot_is_busy(harness):
    _occupy(harness, user_id=2)

    await harness.send_file()

    assert "Бот сейчас занят" in harness.session.last_text()
    assert not harness.session.calls("getFile")


async def test_numbers_typed_into_the_settings_are_not_swallowed_by_the_busy_notice(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")
    _occupy(harness, user_id=2)

    await harness.send("7")

    assert (await _draft(harness)).settings.num_clips == 7


async def test_start_is_refused_while_another_person_is_cutting_and_the_plan_stays(harness, monkeypatch):
    started = []

    async def start(chat_id, user_id, request, message):
        started.append(request)
        return "job-1"

    monkeypatch.setattr(harness.ctx.runner, "start", start)
    await _open_auto(harness, monkeypatch)
    job = _occupy(harness, user_id=2)

    await harness.press("job:start")

    assert "нарезает другой ролик" in harness.session.last_alert() and not started
    assert await _fsm(harness).get_state() == Flow.editing.state  # план остался на экране
    assert "job:start" in harness.session.buttons()

    _release(harness, job)  # чужая нарезка закончилась
    await harness.press("job:start")
    assert len(started) == 1


async def test_two_people_pressing_start_at_the_same_moment_get_one_cut(harness, monkeypatch):
    started = []

    async def start(chat_id, user_id, request, message):
        started.append(user_id)
        await asyncio.sleep(0.05)  # пока первый запускается, второй уже нажал
        return f"job-{user_id}"

    monkeypatch.setattr(harness.ctx.runner, "start", start)
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO))
    for user in (1, 2):
        await harness.send("/start", user_id=user)
        await harness.send(VIDEO["url"], user_id=user)
        await harness.press("mode:auto", user_id=user)

    await asyncio.gather(harness.press("job:start", user_id=1), harness.press("job:start", user_id=2))

    assert len(started) == 1
    assert any("нарезает другой ролик" in alert for alert in harness.session.alerts())
    assert harness.ctx.runner.in_flight() == 0  # место возвращено, когда запуск закончился


async def test_the_place_is_returned_when_the_launch_fails(harness, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("не запустилось")

    monkeypatch.setattr(harness.ctx.runner, "start", broken)
    await _open_auto(harness, monkeypatch)

    await harness.press("job:start")

    assert harness.ctx.runner.in_flight() == 0 and harness.ctx.busy_for(1) is None


async def test_a_second_tap_on_start_does_not_launch_twice(harness, monkeypatch):
    started = []

    async def start(chat_id, user_id, request, message):
        started.append(user_id)
        await asyncio.sleep(0.05)
        return "job-1"

    monkeypatch.setattr(harness.ctx.runner, "start", start)
    await _open_auto(harness, monkeypatch)

    await asyncio.gather(harness.press("job:start"), harness.press("job:start"))

    assert started == [1]
    assert any("уже запускается" in alert for alert in harness.session.alerts())


async def test_refresh_tells_whether_the_bot_is_free_yet(harness):
    job = _occupy(harness, user_id=2, progress=60)
    await harness.send("/start")

    await harness.press("menu:refresh")

    assert "60" in harness.session.last_alert()
    assert [_styles(harness)[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4

    _release(harness, job)
    await harness.press("menu:refresh")

    assert "свободен" in harness.session.last_alert()
    assert all(_styles(harness)[data] != keyboards.DANGER for data in MENU_BUTTONS)
    assert "Бот сейчас занят" not in harness.session.last_text()


async def test_a_bigger_limit_lets_a_second_person_work(harness, monkeypatch):
    monkeypatch.setenv("MAX_JOBS", "2")
    _occupy(harness, user_id=2)

    await harness.send("/start")  # пользователь 1: места ещё есть
    assert all(_styles(harness)[data] != keyboards.DANGER for data in MENU_BUTTONS)

    _occupy(harness, user_id=1)  # но своя нарезка у него всё равно одна
    await harness.send("/start")
    assert [_styles(harness)[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4


async def test_the_bot_stays_busy_while_the_results_are_being_delivered(harness):
    _occupy(harness, user_id=2, delivering=True)
    mine = _occupy(harness, user_id=1, delivering=True)

    await harness.send("/start")

    text = harness.session.last_text()
    assert "Идёт ваша нарезка" in text  # свою досылку не принимаем за чужую
    assert f"job:cancel:{mine}" not in harness.session.buttons()  # готовую задачу уже не остановить
    assert [_styles(harness)[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4


async def test_little_free_space_turns_the_menu_red_too(harness, monkeypatch):
    monkeypatch.setattr(capacity, "free_bytes", lambda path: 1 * 1024**3)

    await harness.send("/start")
    assert [_styles(harness)[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4
    assert "мало места" in harness.session.last_text()

    await harness.press("ask:link")
    assert "места" in harness.session.last_alert()


async def test_noise_during_a_busy_spell_shows_the_red_menu(harness):
    _occupy(harness, user_id=2)

    await harness.send("   ")

    assert [_styles(harness)[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4
    assert "Бот сейчас занят" in harness.session.last_text()


async def test_a_stale_button_during_a_busy_spell_returns_to_the_red_menu(harness):
    _occupy(harness, user_id=2)

    await harness.press("pick:3")

    assert harness.session.last_alert() == texts.STALE
    assert [_styles(harness)[data] for data in SOURCE_BUTTONS] == [keyboards.DANGER] * 4


def test_busy_notices_and_alerts_are_valid_and_short():
    from tests.rich_check import assert_valid_rich

    for busy in (
        Busy(Cause.MINE, progress=55.5, stage="Рендер клипа 3 из 8", job_id="abc"),
        Busy(Cause.MINE, progress=100, stage="Готово"),
        Busy(Cause.OTHERS, progress=0),
        Busy(Cause.OTHERS, progress=99.6, stage="Скачивание видео"),
        Busy(Cause.DISK),
    ):
        assert_valid_rich(texts.start(busy))
        assert len(texts.busy_alert(busy)) <= 200  # помещается в окошко Telegram
        assert "<" not in texts.busy_alert(busy)


# ---------- обычный HTML вместо rich ----------


async def test_everything_works_when_telegram_refuses_rich_messages(harness, monkeypatch):
    """Старый сервер или неподдержанный тег: тот же экран уходит классическим HTML, кнопки остаются."""
    harness.session.reject_rich = True

    await harness.send("/start")

    sent = harness.session.calls("sendMessage")
    assert sent and "TgRader Clipper" in sent[-1].text
    assert "<h1>" not in sent[-1].text and "<blockquote>" in sent[-1].text
    assert harness.session.buttons() == MENU_BUTTONS

    await _open_manual(harness, monkeypatch)
    assert "Ручная настройка" in harness.session.last_text()
    assert "edit:num_clips" in harness.session.buttons()
    await harness.press("edit:num_clips")  # правка тоже идёт классическим editMessageText
    edits = harness.session.calls("editMessageText")
    assert edits and edits[-1].text and edits[-1].rich_message is None


async def test_unchanged_screen_is_not_sent_twice(harness, monkeypatch):
    """«message is not modified» — не повод слать копию экрана новым сообщением."""
    await _open_manual(harness, monkeypatch)
    await harness.press("edit:num_clips")
    sent_before = len(harness.session.calls("sendRichMessage")) + len(harness.session.calls("sendMessage"))

    def not_modified(method, bot):
        raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")

    harness.session.handlers["editMessageText"] = not_modified
    await harness.press("set:num_clips:5")

    assert len(harness.session.calls("sendRichMessage")) + len(harness.session.calls("sendMessage")) == sent_before


async def test_edit_failure_sends_a_fresh_screen(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    def gone(method, bot):
        raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")

    harness.session.handlers["editMessageText"] = gone
    before = len(harness.session.calls("sendRichMessage"))
    await harness.press("edit:num_clips")

    assert len(harness.session.calls("sendRichMessage")) > before
    assert "Сколько клипов" in harness.session.last_text()


async def test_a_full_tour_only_uses_valid_rich_markup(harness, monkeypatch):
    """Все основные экраны подряд: ни один не нарушает правил rich-разметки."""
    monkeypatch.setattr(trend_handlers, "fetch_category", _fake_fetch())
    monkeypatch.setattr(source_handlers, "search_videos", _fake_search([VIDEO, dict(VIDEO, platform="rutube")]))
    await harness.send("/start")
    for data in ("ask:query", "qp:rutube"):
        await harness.press(data)
    await harness.send("интервью")
    await harness.press("qs:youtube")
    for data in (
        "screen:help", "screen:stats", "screen:banners", "screen:trends", "trends:rutube", "trend:rutube:podcast",
        "trend:all:podcast", "screen:trends", "trend:youtube:podcast", "pick:0",
    ):  # fmt: skip
        await harness.press(data)
    await harness.press("mode:manual")
    for data in (
        "screen:more", "choose:max_height", "screen:more", "screen:manual", "screen:trim", "edit:start_offset",
        "type:start_offset", "screen:manual", "choose:slicing_mode", "choose:frame_mode", "choose:banner_mode",
        "choose:banner_position", "edit:banner_count", "edit:banner_scale", "edit:clip_duration", "screen:mode",
        "mode:auto", "goal:shorts", "goal:universal",
    ):  # fmt: skip
        await harness.press(data)

    rich = [request.rich_message.html for request in harness.session.rich_requests()]
    assert len(rich) > 20
    for html in rich:
        assert_valid_rich(html)


# ---------- только кнопки: команд в боте нет ----------


def _walk_routers(router):
    yield router
    for child in router.sub_routers:
        yield from _walk_routers(child)


def test_no_command_filters_at_all(bot_settings):
    """Команд нет совсем: `/start` и любое «/…» обрабатывает один хэндлер по началу текста."""
    from app.bot.app import build_app

    _, _, dispatcher = build_app(bot_settings)
    found = []
    for router in _walk_routers(dispatcher):
        for handler in router.message.handlers:
            for flt in handler.filters:
                if isinstance(flt.callback, Command):
                    found.append(flt.callback)
    assert found == []


def test_bot_texts_never_point_to_commands():
    command = re.compile(r"(?<![\w/:.…])/[a-z]{3,}(?![\w/])")
    screens = [
        texts.start(), texts.HELP, texts.NUDGE, texts.REQUEST_LINK, texts.request_query(), texts.REQUEST_FILE,
        texts.BAD_LINK, texts.STALE, texts.NOT_ALLOWED, texts.stats_text(1, 2, 3), texts.trends_home(),
        texts.CANCELLED, texts.FILE_TOO_BIG, texts.NOT_A_VIDEO,
        texts.start(Busy(Cause.OTHERS, progress=40)), texts.start(Busy(Cause.MINE, progress=40, job_id="x")),
        texts.start(Busy(Cause.DISK)),
    ]  # fmt: skip
    for screen in screens:
        plain = re.sub(r"<[^>]+>", "", screen)  # теги вроде </blockquote> — не команды
        assert not command.search(plain), screen


def test_main_menu_texts_use_only_documented_rich_tags():
    for screen in (texts.start(), texts.HELP, texts.REQUEST_LINK, texts.request_query(), texts.REQUEST_FILE, texts.BAD_LINK,
                   texts.QUEUED_TEXT, texts.NUDGE, texts.CANCELLED, texts.stats_text(1, 2, 3), texts.trends_home()):  # fmt: skip
        assert_valid_rich(screen)


def test_the_brand_is_renamed_everywhere_in_the_user_interface():
    assert texts.NAME == "TgRader Clipper"
    for screen in (texts.start(), texts.HELP):
        assert "ClipBanner" not in screen


# ---------- цвета и размеры кнопок ----------


def _all_keyboards(harness):
    from app.bot.steppers import STEPPERS
    from app.models import Draft

    draft = Draft(title="Тест", duration=60)
    banner = harness.ctx.banner()
    yield keyboards.main_menu()
    yield keyboards.back_to_menu()
    yield keyboards.cancel_only()
    yield keyboards.search_results([])
    yield keyboards.search_results([dict(VIDEO), dict(VIDEO)])
    yield keyboards.search_results([dict(VIDEO), dict(VIDEO, platform="rutube")], "rutube")
    for choice in (EVERYWHERE, *(platform.id for platform in SEARCHABLE)):
        yield keyboards.query_prompt(choice)
        yield keyboards.search_results([dict(VIDEO)], choice)
    yield keyboards.mode_picker()
    for goal in Goal:
        yield keyboards.auto_screen(goal, 7)
    yield keyboards.manual_screen(draft, banner_available=True, banner_name=banner.name)
    yield keyboards.manual_screen(draft, banner_available=False)
    yield keyboards.more_settings(draft.settings)
    yield keyboards.trim_screen(draft.settings)
    yield keyboards.banners_screen(harness.ctx.banners(), harness.ctx.banner().id)
    yield keyboards.banners_screen([], None)
    for choice in (EVERYWHERE, *(platform.id for platform in TREND_PLATFORMS)):
        yield keyboards.trends_home(choice)
        yield keyboards.trend_results(TRENDING, choice, "podcast")
        yield keyboards.trends_retry(choice, "podcast")
    yield keyboards.running("abc123")
    yield keyboards.done()
    for stepper in STEPPERS.values():
        yield keyboards.stepper_screen(stepper, stepper.presets[0])
    from app.bot.options import CHOICES

    for field in CHOICES:
        yield keyboards.choice_screen(field, getattr(draft.settings, field))


def test_button_styles_and_callback_sizes_are_valid(harness):
    for markup in _all_keyboards(harness):
        for row in markup.inline_keyboard:
            assert 1 <= len(row) <= 8
            for button in row:
                assert button.style in (None, "primary", "success", "danger"), button
                assert len(button.callback_data.encode()) <= 64, button.callback_data
                assert 0 < len(button.text) <= 64, button.text


def test_button_colors_follow_their_meaning(harness):
    from app.models import Draft

    def styles(markup):
        return {button.callback_data: button.style for row in markup.inline_keyboard for button in row}

    manual = styles(keyboards.manual_screen(Draft(title="t", duration=60), banner_available=True))
    assert manual["job:start"] == "success"
    assert styles(keyboards.auto_screen(Goal.TIKTOK, 3))["job:start"] == "success"
    assert styles(keyboards.auto_screen(Goal.TIKTOK, 3))["goal:tiktok"] == "primary"
    assert styles(keyboards.running("x"))["job:cancel:x"] == "danger"
    assert styles(keyboards.main_menu())["ask:link"] == "primary"
    assert styles(keyboards.mode_picker())["mode:auto"] == "primary"
    chips = styles(keyboards.trends_home("rutube"))
    assert chips["trends:rutube"] == "primary" and chips["trends:youtube"] is None and chips["trends:all"] is None
    assert styles(keyboards.trends_home())["trends:all"] == "primary"  # без выбора — «Везде»
    prompt = styles(keyboards.query_prompt("dailymotion"))
    assert prompt["qp:dailymotion"] == "primary" and prompt["qp:all"] is None and prompt["qp:rutube"] is None
    again = styles(keyboards.search_results([dict(VIDEO)], "youtube"))
    assert again["qs:youtube"] == "primary" and again["qs:rutube"] is None and again["qp:youtube"] is None


def test_search_buttons_show_platform_badges_only_when_platforms_are_mixed():
    def labels(entries):
        return [button.text for row in keyboards.search_results(entries).inline_keyboard for button in row][: len(entries)]

    assert all("▶️" not in label and "📺" not in label for label in labels([dict(VIDEO), dict(VIDEO)]))
    mixed = labels([dict(VIDEO), dict(VIDEO, platform="rutube")])
    assert mixed[0].startswith("1. ▶️ ") and mixed[1].startswith("2. 📺 ")
    assert labels([dict(VIDEO, platform=None), dict(VIDEO, platform="rutube")])[0].startswith("1. Тестовый")  # неизвестная — без значка


def test_clip_counts_are_declined_in_russian():
    words = {n: keyboards._clips_word(n) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25, 101, 111)}  # noqa: SLF001
    assert words == {1: "клип", 2: "клипа", 4: "клипа", 5: "клипов", 11: "клипов", 12: "клипов", 21: "клип",
                     22: "клипа", 25: "клипов", 101: "клип", 111: "клипов"}  # fmt: skip


async def test_every_manual_button_has_a_handler(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    for data in list(harness.session.buttons()):
        if data in ("job:start", "screen:menu"):
            continue  # запуск настоящей нарезки проверяют отдельные тесты
        await harness.press(data)
        assert texts.STALE not in harness.session.alerts(), data
        await harness.press("screen:manual")


async def test_every_auto_button_has_a_handler(harness, monkeypatch):
    await _open_auto(harness, monkeypatch)
    for data in list(harness.session.buttons()):
        if data == "job:start":
            continue
        await harness.press(data)
        assert texts.STALE not in harness.session.alerts(), data
        await harness.press("mode:auto")


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
        assert "TgRader Clipper" in fake.session.last_text()
    finally:
        await fake.close()


async def test_prepare_bot_removes_the_command_menu_and_names_the_bot(harness):
    from app.bot.app import prepare_bot

    await prepare_bot(harness.bot)

    assert harness.session.calls("deleteMyCommands")
    assert not harness.session.calls("setMyCommands")
    description = harness.session.calls("setMyDescription")[-1].description
    assert "TgRader Clipper" in description and "ClipBanner" not in description


async def test_bot_is_renamed_once_and_left_alone_when_the_name_is_right(harness):
    from app.bot.app import prepare_bot

    await prepare_bot(harness.bot)
    assert harness.session.bot_name == "TgRader Clipper"
    assert len(harness.session.calls("setMyName")) == 1

    await prepare_bot(harness.bot)  # имя уже верное — Telegram лишний раз не беспокоим
    assert len(harness.session.calls("setMyName")) == 1


async def test_a_failed_rename_does_not_stop_the_rest_of_the_profile_setup(harness):
    from app.bot.app import prepare_bot

    def refuse(method, bot):
        raise TelegramBadRequest(method=method, message="Bad Request: too many name changes")

    harness.session.handlers["setMyName"] = refuse
    await prepare_bot(harness.bot)

    assert harness.session.calls("setMyDescription")


# ---------- прогресс: стадия видна сразу, время идёт, долгое молчание объяснено ----------


async def _wait_until(condition, *, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.1)
    return condition()


async def test_progress_shows_the_stage_the_job_is_in_right_now(harness, monkeypatch):
    """Разбор ролика идёт минутами и ничего не присылает боту: сообщение всё равно должно сменить стадию.

    Раньше оно висело на последнем присланном снимке — «Скачивание видео 20%» — всё время анализа.
    """
    import threading

    from app.bot import runner as runner_module
    from app.models import JobRequest
    from app.services import jobs as jobs_module

    shown: list[str] = []
    finish = threading.Event()

    async def edit(message, html, markup=None):
        shown.append(plain_text(html))

    def long_analysis(self, request, job_id, notifier, on_clip):
        self._update(job_id, status=JobStatus.RUNNING, stage="Скачивание видео", progress=20.0)
        notifier.touch()  # последнее, что бот получает сам
        self._update(job_id, stage="Анализ видео")  # дальше долго и молча — как в настоящей задаче
        finish.wait(30)
        raise jobs_module.JobCancelled

    monkeypatch.setattr(runner_module, "edit_html", edit)
    monkeypatch.setattr(jobs_module.JobManager, "_run", long_analysis)
    request = JobRequest(source_type="upload", upload_id="demo", banner_count=0)

    await harness.ctx.runner.start(1, 1, request, None)
    try:
        assert await _wait_until(lambda: any("Анализ видео" in text for text in shown)), shown
    finally:
        finish.set()
        await _wait_finished(harness)


def test_progress_text_counts_the_time_in_steps_that_do_not_flood_telegram():
    state = JobState(id="j", status=JobStatus.RUNNING, stage="Рендер клипа 1 из 8", progress=21.0)

    shown = plain_text(texts.progress_text(state, elapsed=5 * 60 + 17))
    assert "Рендер клипа 1 из 8" in shown and "21%" in shown and "5 мин" in shown
    assert "40 с" in plain_text(texts.progress_text(state, elapsed=47))
    assert "1 ч 2 мин" in plain_text(texts.progress_text(state, elapsed=3725))

    # Сообщение правится только когда текст изменился: после двух минут это раз в минуту, а не каждую секунду.
    same_minute = (texts.progress_text(state, elapsed=5 * 60 + 5), texts.progress_text(state, elapsed=5 * 60 + 55))
    assert same_minute[0] == same_minute[1]
    assert texts.progress_text(state, elapsed=5 * 60 + 5) != texts.progress_text(state, elapsed=6 * 60 + 5)


def test_progress_text_explains_a_long_silence_and_points_to_the_stop_button():
    state = JobState(id="j", status=JobStatus.RUNNING, stage="Ищу яркие моменты", progress=20.0)

    calm = plain_text(texts.progress_text(state, elapsed=9 * 60, quiet=2 * 60))
    worried = plain_text(texts.progress_text(state, elapsed=14 * 60, quiet=11 * 60))

    assert "Остановить" not in calm
    assert "Ищу яркие моменты" in worried and "11 мин" in worried and "Остановить" in worried


async def test_the_progress_message_counts_time_and_warns_only_after_a_long_silence(harness, monkeypatch):
    from app.bot import runner as runner_module

    shown: list[str] = []

    async def edit(message, html, markup=None):
        shown.append(plain_text(html))

    monkeypatch.setattr(runner_module, "edit_html", edit)
    runner, manager = harness.ctx.runner, harness.ctx.manager
    job_id = _occupy(harness, user_id=1, progress=20.0, stage="Ищу яркие моменты")
    running = runner._jobs[job_id]  # noqa: SLF001

    await runner._edit_progress(running)  # noqa: SLF001
    assert "⏱" in shown[-1] and "Остановить" not in shown[-1]

    running.started_at -= 15 * 60  # пятнадцать минут задаче, из них двенадцать — без единой перемены
    running.moved_at -= 12 * 60
    await runner._edit_progress(running)  # noqa: SLF001
    assert "15 мин" in shown[-1] and "12 мин" in shown[-1] and "Остановить" in shown[-1]

    manager._update(job_id, stage="Рендер клипа 1 из 8")  # noqa: SLF001 — перемена: молчание отсчитывается заново
    running.state = manager.get(job_id)
    await runner._edit_progress(running)  # noqa: SLF001
    assert "Рендер клипа 1 из 8" in shown[-1] and "Остановить" not in shown[-1]


# ---------- готовый клип: подпись к публикации ----------


def test_the_finished_clip_offers_a_copy_button_with_its_description():
    """Кнопка «скопировать описание»: название ролика, таймкод окна и хештеги — без ссылки на источник."""
    from app.models import ClipResult, JobRequest, JobState

    request = JobRequest(source_type="upload", upload_id="demo", banner_count=0, title="  Тестовый   ролик ")
    state = JobState(id="job", total_clips=1)
    state.clips.append(
        ClipResult(index=1, filename="clip_01.mp4", source_start=61.0, source_end=106.0, duration=45.0)
    )

    description = texts.clip_description(request, state)
    assert "Тестовый ролик" in description  # лишние пробелы схлопнуты
    assert "1:01" in description and "#shorts" in description
    assert "http" not in description  # ссылку на чужой ролик не вставляем

    copies = [
        button.copy_text.text
        for row in keyboards.done(description).inline_keyboard
        for button in row
        if button.copy_text is not None
    ]
    assert copies == [description]
    assert not [
        button
        for row in keyboards.done().inline_keyboard
        for button in row
        if button.copy_text is not None
    ], "без описания кнопки копирования быть не должно"


# ---------- «как в прошлый раз» ----------


async def test_repeat_offered_only_after_a_finished_cut(harness, monkeypatch):
    await _open_video(harness, monkeypatch)

    assert "Как в прошлый раз" not in harness.session.last_text()
    assert "mode:repeat" not in harness.session.buttons()


async def test_repeat_takes_the_numbers_from_the_last_cut(harness, monkeypatch):
    from app.services.history import Record

    harness.ctx.history.append(
        Record(
            ts="2026-11-07T20:00", user_id=1, job_id="prev", index=1, of=3, platform="youtube",
            source="dQw4w9WgXcQ", title="Прошлый ролик", mode="auto", start=10.0, end=85.0,
            clip_len=75.0, banner="TGrader-banner-7s.mp4", banner_count=1, render_s=30.0, size_mb=5.0,
        )
    )  # fmt: skip
    await _open_video(harness, monkeypatch)
    assert "Как в прошлый раз" in harness.session.last_text()

    await harness.press("mode:repeat")

    text = harness.session.last_text()
    assert "Ручная настройка" in text
    assert "3" in text  # столько клипов было в прошлый раз
    assert "67" in text  # 75 с с баннером минус 7,5 с паузы — уже без баннера

    settings = (await _fsm(harness).get_data())["draft"]["settings"]
    assert settings["num_clips"] == 3 and settings["banner_count"] == 1
    assert 66.0 < settings["clip_duration"] < 68.0


# ---------- быстрый старт и пресеты ----------


async def test_presets_apply_numbers_and_open_the_manual_screen(harness, monkeypatch):
    await _open_video(harness, monkeypatch)

    await harness.press("mode:presets")
    assert "Пресеты" in harness.session.last_text() and "Хук для TikTok" in harness.session.last_text()

    await harness.press("preset:hook")

    text = harness.session.last_text()
    assert "Ручная настройка" in text and "3" in text  # пресет «хук» — три клипа
    settings = (await _fsm(harness).get_data())["draft"]["settings"]
    assert settings["num_clips"] == 3 and settings["clip_duration"] == 25.0


async def test_unknown_preset_is_reported(harness, monkeypatch):
    await _open_video(harness, monkeypatch)

    await harness.press("preset:nope")

    assert harness.session.alerts() == ["Такого набора нет — выберите из кнопок"]


async def test_quick_start_goes_from_the_link_to_rendering_in_one_tap(harness, monkeypatch, tmp_path):
    """«🚀 Нарезать сразу»: один тап — и задача уже идёт, без экранов «Авто/Вручную»."""
    source = make_video(tmp_path / "src.mp4", size="640x360", seconds=8)
    harness.session.stream_bytes = source.read_bytes()
    await harness.send("/start")
    await harness.send_file(file_name="myvideo.mp4", file_size=Path(source).stat().st_size)

    await harness.press("mode:quick")

    # Экран «Как будем нарезать?» пройден одним нажатием: сразу пошло сообщение о задаче.
    shown = " ".join(plain_text(html) for html in harness.session.htmls())
    assert "Задача в очереди" in shown or "Готово" in shown or "%" in shown
    await _wait_finished(harness)  # задача настоящая: дожидаемся, чтобы она не жила после теста
    assert harness.session.calls("sendVideo"), "клип должен был дойти до чата"


# ---------- черновик 540p ----------


async def test_preview_toggle_lives_in_the_extra_settings(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("screen:more")
    assert "Черновик 540p" in harness.session.last_text()

    await harness.press("toggle:preview:more")

    settings = (await _fsm(harness).get_data())["draft"]["settings"]
    assert settings["preview"] is True and "Черновик" in harness.session.last_alert()


async def test_subtitles_toggle_lives_in_the_extra_settings(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)

    await harness.press("screen:more")
    assert "Субтитры" in harness.session.last_text()

    await harness.press("toggle:subtitles:more")

    settings = (await _fsm(harness).get_data())["draft"]["settings"]
    assert settings["subtitles"] is True and "Субтитры включены" in harness.session.last_alert()


async def test_after_a_draft_the_final_button_rerenders_without_preview(harness, monkeypatch, tmp_path):
    """Черновик показывают, чтобы проверить моменты; дальше одним нажатием — полное качество."""
    source = make_video(tmp_path / "src.mp4", size="640x360", seconds=8)
    harness.session.stream_bytes = source.read_bytes()
    await harness.send("/start")
    await harness.send_file(file_name="myvideo.mp4", file_size=Path(source).stat().st_size)
    await harness.press("mode:manual")
    await harness.press("screen:more")
    await harness.press("toggle:preview:more")  # включаем черновик
    await harness.press("screen:manual")

    await harness.press("job:start")
    await _wait_finished(harness)
    assert "job:final" in harness.session.buttons(), "после черновика должна быть кнопка полного рендера"

    await harness.press("job:final")
    settings = (await _fsm(harness).get_data())["draft"]["settings"]
    assert settings["preview"] is False
    await _wait_finished(harness)
