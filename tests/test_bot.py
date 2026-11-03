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
from app.bot.context import get_draft
from app.bot.handlers import modes as mode_handlers
from app.bot.handlers import source as source_handlers
from app.bot.handlers import trends as trend_handlers
from app.bot.rich import plain_text
from app.bot.states import Flow
from app.config import Settings
from app.models import CutMode, FrameMode, Goal, SlicingMode
from tests.bot_fakes import Harness, _message
from tests.media_utils import make_video
from tests.rich_check import assert_valid_rich

VIDEO = {
    "video_id": "dQw4w9WgXcQ",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "title": "Тестовый ролик",
    "duration": 120.0,
    "uploader": "Автор",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    "resolutions": [1080, 720],
}

TRENDING = [
    {
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


def _fsm(harness, user_id: int = 1) -> FSMContext:
    """Состояние диалога пользователя — чтобы проверять черновик и подкладывать состояния."""
    key = StorageKey(bot_id=harness.bot.id, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=harness.dispatcher.storage, key=key)


async def _draft(harness):
    return await get_draft(_fsm(harness))


# ---------- фикстуры ----------


@pytest.fixture
def bot_settings(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    return settings


@pytest.fixture(autouse=True)
def no_youtube(monkeypatch):
    """Любой выход в YouTube в этих тестах — ошибка, а не сюрприз: нужное подменяется точечно."""
    import yt_dlp

    class Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError("тест не должен ходить в YouTube")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Forbidden)


@pytest.fixture
async def harness(bot_settings):
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
    assert harness.session.buttons() == ["screen:menu"]


async def test_stats_start_at_zero_and_show_the_user_id(harness):
    await harness.send("/start", user_id=777)
    await harness.press("screen:stats", user_id=777)

    text = harness.session.last_text()
    assert "Роликов нарезано" in text and "0" in text
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
    calls: list[str] = []
    monkeypatch.setattr(source_handlers, "search_videos", lambda query, limit=5: calls.append(query) or [])
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")
    await harness.send("/start")

    await harness.send("7")

    assert calls == ["7"]  # «7» ушло в поиск, а не в число клипов


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


async def test_banners_screen_lists_only_the_tgrader_banner(harness):
    await harness.press("screen:banners")

    text = harness.session.last_text()
    assert "Баннеры" in text and "TGrader" in text
    assert "7" in text and "звук" in text.lower()
    assert harness.session.buttons() == ["banner:select", "banner:preview", "screen:menu"]
    labels = harness.session.button_texts()
    assert any("TGrader" in label for label in labels) and len(labels) == 3
    assert [banner.id for banner in harness.ctx.banners()] == ["TGrader-banner-7s.mp4"]
    assert_valid_rich(harness.session.htmls()[-1])


async def test_only_one_banner_is_ever_offered_even_if_other_files_lie_around(harness, bot_settings):
    from PIL import Image

    Image.new("RGBA", (920, 360), (255, 0, 0, 255)).save(bot_settings.banners_dir / "old_preset.png")

    await harness.press("screen:banners")

    assert [banner.id for banner in harness.ctx.banners()] == ["TGrader-banner-7s.mp4"]
    assert "old_preset" not in " ".join(harness.session.button_texts() + harness.session.texts())


async def test_banner_select_button_only_confirms(harness):
    await harness.press("banner:select")

    assert "TGrader" in harness.session.last_alert()


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


# ---------- приём источника ----------


async def test_link_opens_the_mode_picker(harness, monkeypatch):
    await _open_video(harness, monkeypatch)

    text = harness.session.last_text()
    assert "Тестовый ролик" in text and "Автор" in text and "2:00" in text
    assert "Авто" in text and "Вручную" in text
    assert harness.session.buttons() == ["mode:auto", "mode:manual", "screen:menu"]
    draft = await _draft(harness)
    assert draft is not None and draft.title == "Тестовый ролик" and draft.mode is None
    assert await _fsm(harness).get_state() == Flow.editing.state


async def test_search_result_opens_the_mode_picker(harness, monkeypatch):
    results = [dict(VIDEO, video_id=f"abcdefghij{n}", url=f"https://www.youtube.com/watch?v=abcdefghij{n}") for n in "12"]
    monkeypatch.setattr(source_handlers, "search_videos", lambda query, limit=5: results)

    await harness.send("интервью дурова")
    assert harness.session.buttons()[:2] == ["pick:0", "pick:1"]
    assert await _fsm(harness).get_state() == Flow.choosing.state

    await harness.press("pick:1")

    assert harness.session.buttons() == ["mode:auto", "mode:manual", "screen:menu"]
    assert (await _draft(harness)).url.endswith("abcdefghij2")


async def test_picking_an_outdated_search_result_is_reported(harness, monkeypatch):
    results = [dict(VIDEO)]
    monkeypatch.setattr(source_handlers, "search_videos", lambda query, limit=5: results)
    await harness.send("что-нибудь")

    await harness.press("pick:7")

    assert "устарел" in harness.session.last_alert()


async def test_short_search_query_is_reported_in_russian(harness):
    await harness.send("a")

    assert "Слишком короткое" in harness.session.last_text()
    assert harness.session.buttons() == ["screen:menu"]


async def test_search_failure_is_reported_with_a_way_back(harness, monkeypatch):
    def boom(query, limit=5):
        raise RuntimeError("сеть упала")

    monkeypatch.setattr(source_handlers, "search_videos", boom)
    await harness.send("интервью")

    assert "Поиск не удался" in harness.session.last_text()
    assert harness.session.buttons() == ["screen:menu"]


async def test_bad_link_says_what_is_wrong(harness):
    await harness.send("https://example.com/video")

    text = harness.session.last_text()
    assert "не похоже на ссылку на YouTube" in text and "youtu.be" in text


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
    calls: list[str] = []
    monkeypatch.setattr(source_handlers, "search_videos", lambda query, limit=5: calls.append(query) or [])

    await harness.send("интервью дурова")

    assert calls == ["интервью дурова"]


async def test_new_link_replaces_the_draft_and_forgets_pending_input(harness, monkeypatch):
    await _open_manual(harness, monkeypatch)
    await harness.press("type:num_clips")
    monkeypatch.setattr(source_handlers, "fetch_video_info", lambda url: dict(VIDEO, title="Другой ролик"))

    await harness.send("https://youtu.be/AAAAAAAAAAA")

    assert (await _draft(harness)).title == "Другой ролик"
    assert not (await _fsm(harness).get_data()).get("input_field")
    assert harness.session.buttons() == ["mode:auto", "mode:manual", "screen:menu"]


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
    assert harness.session.buttons() == ["mode:auto", "mode:manual", "screen:menu"]
    assert (await _draft(harness)).upload_id


async def test_non_video_file_is_rejected(harness):
    await harness.send_file(file_name="notes.pdf")

    assert "не видеофайл" in harness.session.last_text()


async def test_oversized_file_is_rejected(harness):
    await harness.send_file(file_name="big.mp4", file_size=30 * 1024 * 1024)

    assert "20 МБ" in harness.session.last_text()


# ---------- режимы: авто и вручную ----------


async def test_auto_mode_builds_a_tiktok_plan_with_goal_buttons(harness, monkeypatch):
    monkeypatch.setattr(mode_handlers, "_can_follow_faces", lambda: True)
    await _open_auto(harness, monkeypatch, duration=3600.0)

    draft = await _draft(harness)
    s = draft.settings
    assert draft.mode is CutMode.AUTO and draft.goal is Goal.TIKTOK
    assert (s.num_clips, s.clip_duration) == (10, 25.0)
    assert s.slicing_mode is SlicingMode.HIGHLIGHTS and s.auto_trim
    assert s.frame_mode is FrameMode.CENTER_CROP
    assert s.banner_id == "TGrader-banner-7s.mp4" and s.banner_count == 1 and s.banner_sound

    assert harness.session.buttons() == [
        "goal:tiktok", "goal:shorts", "goal:universal", "job:start", "mode:manual", "screen:mode",
    ]  # fmt: skip
    assert "Нарезать 10 клипов" in " ".join(harness.session.button_texts())
    text = harness.session.last_text()
    assert "Авто-подбор" in text and "TikTok" in text and "Тестовый ролик" in text
    assert_valid_rich(harness.session.htmls()[-1])


async def test_auto_plan_is_honest_it_is_not_a_neural_network():
    assert "не нейросеть" in plain_text(texts.HELP)


async def test_changing_the_goal_rebuilds_the_plan(harness, monkeypatch):
    monkeypatch.setattr(mode_handlers, "_can_follow_faces", lambda: False)
    await _open_auto(harness, monkeypatch, duration=3600.0)

    await harness.press("goal:shorts")
    draft = await _draft(harness)
    assert draft.goal is Goal.SHORTS
    assert (draft.settings.num_clips, draft.settings.clip_duration) == (8, 40.0)
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

    assert harness.session.buttons() == ["mode:auto", "mode:manual", "screen:menu"]
    assert await _fsm(harness).get_state() == Flow.editing.state


async def test_tweaking_the_auto_plan_by_hand_keeps_its_values(harness, monkeypatch):
    monkeypatch.setattr(mode_handlers, "_can_follow_faces", lambda: False)
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

    assert harness.session.buttons() == ["mode:auto", "mode:manual", "screen:menu"]


async def test_going_back_to_auto_recomputes_the_plan(harness, monkeypatch):
    monkeypatch.setattr(mode_handlers, "_can_follow_faces", lambda: False)
    await _open_manual(harness, monkeypatch, duration=3600.0)
    await harness.press("edit:num_clips")
    await harness.press("set:num_clips:3")
    assert (await _draft(harness)).settings.num_clips == 3

    await harness.press("screen:mode")
    await harness.press("mode:auto")

    draft = await _draft(harness)
    assert draft.mode is CutMode.AUTO and draft.settings.num_clips == 10


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
    for part in ("9:16, размытый фон", "9:16, на весь экран", "16:9, как есть", "слежу за лицом"):
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

    stored = update_settings(
        harness.ctx.store.settings(1), banner_id="preset_gone.png", static_banner_id="strip_gone.png", banner_count=3
    )
    harness.ctx.store.settings = lambda user_id: stored  # старый сохранённый выбор из прошлой версии бота
    await _open_manual(harness, monkeypatch)

    s = (await _draft(harness)).settings
    assert s.banner_id == "TGrader-banner-7s.mp4" and s.static_banner_id is None and s.banner_count == 3


async def test_without_the_banner_file_clips_go_without_a_banner(harness, monkeypatch, bot_settings):
    (bot_settings.banners_dir / "TGrader-banner-7s.mp4").unlink()
    await _open_manual(harness, monkeypatch)

    s = (await _draft(harness)).settings
    assert s.banner_count == 0 and s.banner_id is None
    assert "toggle:banner" not in harness.session.buttons()
    await harness.press("mode:auto")
    assert (await _draft(harness)).settings.banner_count == 0
    await harness.press("screen:banners")
    assert "Баннера пока нет" in harness.session.last_text()


# ---------- тренды ----------


async def test_trends_home_lists_genres_and_goes_back(harness):
    await harness.press("screen:trends")

    text = harness.session.last_text()
    assert "В тренде" in text and "TikTok" in text
    buttons = harness.session.buttons()
    assert buttons[-1] == "screen:menu" and "trend:podcast" in buttons
    assert_valid_rich(harness.session.htmls()[-1])


async def test_trend_genre_lists_videos_and_is_cached(harness, monkeypatch):
    calls: list[str] = []

    def fake(category, limit=8):
        calls.append(category.id)
        return [dict(video) for video in TRENDING]

    monkeypatch.setattr(trend_handlers, "fetch_category", fake)

    await harness.press("trend:podcast")

    text = harness.session.last_text()
    assert "Подкаст номер 1" in text and "1,5 млн" in text and "Канал 2" in text
    assert harness.session.buttons() == ["pick:0", "pick:1", "pick:2", "screen:trends", "screen:menu"]
    assert any("Загляните" not in line for line in harness.session.texts())
    assert_valid_rich(harness.session.htmls()[-1])
    assert await _fsm(harness).get_state() == Flow.choosing.state

    await harness.press("screen:trends")
    await harness.press("trend:podcast")
    assert calls == ["podcast"]  # второй раз — из кеша


async def test_cold_trend_request_shows_a_loading_screen_first(harness, monkeypatch):
    monkeypatch.setattr(trend_handlers, "fetch_category", lambda category, limit=8: [dict(v) for v in TRENDING])

    await harness.press("trend:humor")

    shown = [plain_text(html) for html in harness.session.htmls()]
    assert any("Смотрю, что сейчас в тренде" in text for text in shown)
    assert "Подкаст номер 1" in shown[-1]


async def test_picking_a_trending_video_goes_straight_to_the_tiktok_plan(harness, monkeypatch):
    monkeypatch.setattr(mode_handlers, "_can_follow_faces", lambda: False)
    monkeypatch.setattr(trend_handlers, "fetch_category", lambda category, limit=8: [dict(v) for v in TRENDING])
    await harness.press("trend:podcast")

    await harness.press("pick:1")

    draft = await _draft(harness)
    assert draft.title == "Подкаст номер 2" and draft.mode is CutMode.AUTO and draft.goal is Goal.TIKTOK
    assert "job:start" in harness.session.buttons() and "goal:shorts" in harness.session.buttons()
    assert "Авто-подбор" in harness.session.last_text()


async def test_trend_error_offers_a_retry(harness, monkeypatch):
    from app.services.trends import TrendsError

    attempts = []

    def flaky(category, limit=8):
        attempts.append(1)
        if len(attempts) == 1:
            raise TrendsError("YouTube сейчас не отвечает — попробуйте через пару минут.")
        return [dict(v) for v in TRENDING]

    monkeypatch.setattr(trend_handlers, "fetch_category", flaky)
    await harness.press("trend:games")

    assert "не отвечает" in harness.session.last_text()
    assert harness.session.buttons() == ["trend:games", "screen:trends"]


async def test_unexpected_trend_failure_is_reported_politely(harness, monkeypatch):
    def broken(category, limit=8):
        raise ValueError("внезапно")

    monkeypatch.setattr(trend_handlers, "fetch_category", broken)
    await harness.press("trend:money")

    assert "Попробуйте ещё раз" in harness.session.last_text()
    assert "внезапно" not in harness.session.last_text()
    assert harness.session.buttons()[0] == "trend:money"


async def test_empty_trend_list_is_not_cached(harness, monkeypatch):
    calls = []

    def empty_then_full(category, limit=8):
        calls.append(1)
        return [] if len(calls) == 1 else [dict(v) for v in TRENDING]

    monkeypatch.setattr(trend_handlers, "fetch_category", empty_then_full)
    await harness.press("trend:sport")
    assert "ничего подходящего" in harness.session.last_text()

    await harness.press("trend:sport")
    assert "Подкаст номер 1" in harness.session.last_text()


async def test_unknown_trend_genre_is_reported(harness):
    await harness.press("trend:nothing")

    assert "Такого жанра нет" in harness.session.last_alert()


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


async def test_messages_during_a_job_get_a_hint(harness, monkeypatch):
    monkeypatch.setattr(harness.ctx.runner, "active_for_user", lambda user_id: "job-1")
    await _fsm(harness).set_state(Flow.running)

    await harness.send("привет")

    assert "Сейчас идёт нарезка" in harness.session.last_text()


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
    monkeypatch.setattr(mode_handlers, "_can_follow_faces", lambda: True)
    monkeypatch.setattr(trend_handlers, "fetch_category", lambda category, limit=8: [dict(v) for v in TRENDING])
    await harness.send("/start")
    for data in ("screen:help", "screen:stats", "screen:banners", "screen:trends", "trend:podcast", "pick:0"):
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
        texts.START, texts.HELP, texts.NUDGE, texts.REQUEST_LINK, texts.REQUEST_QUERY, texts.REQUEST_FILE,
        texts.BAD_LINK, texts.STALE, texts.NOT_ALLOWED, texts.stats_text(1, 2, 3), texts.trends_home(),
        texts.BUSY, texts.CANCELLED, texts.FILE_TOO_BIG, texts.NOT_A_VIDEO,
    ]  # fmt: skip
    for screen in screens:
        plain = re.sub(r"<[^>]+>", "", screen)  # теги вроде </blockquote> — не команды
        assert not command.search(plain), screen


def test_main_menu_texts_use_only_documented_rich_tags():
    for screen in (texts.START, texts.HELP, texts.REQUEST_LINK, texts.REQUEST_QUERY, texts.REQUEST_FILE, texts.BAD_LINK,
                   texts.QUEUED_TEXT, texts.NUDGE, texts.BUSY, texts.CANCELLED, texts.stats_text(1, 2, 3), texts.trends_home()):  # fmt: skip
        assert_valid_rich(screen)


def test_the_brand_is_renamed_everywhere_in_the_user_interface():
    assert texts.NAME == "TgRader Clipper"
    for screen in (texts.START, texts.HELP):
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
    yield keyboards.search_results([dict(VIDEO), dict(VIDEO)])
    yield keyboards.mode_picker()
    for goal in Goal:
        yield keyboards.auto_screen(goal, 7)
    yield keyboards.manual_screen(draft, banner_available=True)
    yield keyboards.manual_screen(draft, banner_available=False)
    yield keyboards.more_settings(draft.settings)
    yield keyboards.trim_screen(draft.settings)
    yield keyboards.banners_screen(banner.name)
    yield keyboards.banners_screen(None)
    yield keyboards.trends_home()
    yield keyboards.trend_results(TRENDING)
    yield keyboards.trends_retry("podcast")
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
