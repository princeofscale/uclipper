"""Rich-разметка: перевод в классический HTML (запасной путь) и проверка текстов бота."""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from app.bot import texts
from app.bot.rich import (
    CLASSIC_LIMIT,
    DIVIDER,
    esc,
    esc_attr,
    plain_text,
    rich_input,
    to_classic,
)
from app.models import ClipSettings, Draft, Goal, JobState, JobStatus
from app.services.autoplan import GOALS, plan_auto
from app.services.platforms import PLATFORMS, SEARCHABLE, YOUTUBE
from app.services.trends import CATEGORIES
from tests.rich_check import assert_valid_rich, rich_problems


class _Balanced(HTMLParser):
    """Классический HTML Telegram: допустимы только эти теги, и все они закрыты в правильном порядке."""

    ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler", "tg-time"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED:
            self.problems.append(f"тег <{tag}> классический HTML не знает")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.problems.append(f"</{tag}> закрывает не то")


def classic_problems(html: str) -> list[str]:
    checker = _Balanced()
    checker.feed(html)
    checker.close()
    return checker.problems + ([f"не закрыто: {checker.stack}"] if checker.stack else [])


# ---------- экранирование ----------


def test_esc_escapes_the_three_html_characters():
    assert esc("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert esc(None) == "" and esc(5) == "5"
    assert esc_attr('say "hi"') == "say &quot;hi&quot;"


def test_rich_input_turns_off_entity_detection():
    message = rich_input("<p>x</p>")
    assert message.html == "<p>x</p>" and message.skip_entity_detection is True


# ---------- rich → классический ----------


def test_headings_become_bold_lines_separated_from_the_next_block():
    assert to_classic("<h1>Заголовок</h1><p>Текст</p>") == "<b>Заголовок</b>\n\nТекст"


def test_whitespace_collapses_like_in_a_browser_and_br_breaks_lines():
    assert to_classic("<p>раз   два\n   три<br/>четыре</p>") == "раз два три\nчетыре"


def test_paragraphs_are_separated_by_one_blank_line():
    assert to_classic("<p>раз</p><p>два</p><p>три</p>") == "раз\n\nдва\n\nтри"


def test_unordered_and_ordered_lists():
    assert to_classic("<ul><li>один</li><li>два</li></ul>") == "• один\n• два"
    assert to_classic("<ol><li>один</li><li>два</li></ol>") == "1. один\n2. два"
    assert to_classic('<ol start="3"><li>а</li><li>б</li></ol>') == "3. а\n4. б"


def test_nested_lists_keep_their_indent():
    html = "<ul><li>верх<ul><li>вложенный</li></ul></li><li>второй</li></ul>"
    lines = to_classic(html).split("\n")
    assert lines[0] == "• верх"
    assert lines[1].endswith("• вложенный") and lines[1].startswith(" ")
    assert lines[2] == "• второй"


def test_table_becomes_key_value_lines():
    html = "<table bordered><tr><td>🎞 Клипов</td><td><b>5</b></td></tr><tr><td>Длина</td><td>30 с</td></tr></table>"
    assert to_classic(html) == "<b>🎞 Клипов</b> — <b>5</b>\n<b>Длина</b> — 30 с"


def test_table_header_row_is_bold():
    html = "<table><tr><th>Имя</th><th>Значение</th></tr><tr><td>а</td><td>б</td></tr></table>"
    assert to_classic(html) == "<b>Имя · Значение</b>\n<b>а</b> — б"


def test_blockquote_details_and_footer():
    assert to_classic("<blockquote>цитата</blockquote>") == "<blockquote>цитата</blockquote>"
    assert to_classic("<details><summary>Тема</summary><p>скрыто</p></details>") == (
        "<blockquote expandable><b>Тема</b>\n\nскрыто</blockquote>"
    )
    assert to_classic("<footer>мелко</footer>") == "<i>мелко</i>"


def test_divider_and_inline_tags_and_code():
    assert to_classic("<p>до</p><hr/><p>после</p>") == f"до\n\n{DIVIDER}\n\nпосле"
    assert to_classic("<p><b>ж</b> <i>к</i> <u>п</u> <s>з</s> <mark>м</mark> <code>c</code></p>") == (
        "<b>ж</b> <i>к</i> <u>п</u> <s>з</s> <b>м</b> <code>c</code>"
    )


def test_text_inside_cells_and_entities_survive():
    assert to_classic("<p>a &lt; b &amp; c</p>") == "a &lt; b &amp; c"
    assert to_classic("<p>x&nbsp;y</p>") == "x\xa0y"


def test_checkboxes_become_ballots():
    html = '<ul><li><input type="checkbox" checked/> готово</li><li><input type="checkbox"/> нет</li></ul>'
    assert to_classic(html) == "☑ готово\n☐ нет"


def test_pre_keeps_its_text_verbatim():
    assert to_classic("<pre>a  b\n c</pre>") == "<pre>a  b\n c</pre>"


def test_overlong_screens_degrade_to_safe_plain_text():
    html = "<p>" + "слово " * 2000 + "</p>"
    classic = to_classic(html)
    assert len(classic) <= CLASSIC_LIMIT and classic.endswith("…")
    assert "<" not in classic.replace("&lt;", "")


def test_plain_text_strips_markup_and_unescapes():
    assert plain_text("<h2>Итог</h2><p>1 &lt; 2 &amp; <b>3</b></p>") == "Итог\n\n1 < 2 & 3"


# ---------- тексты бота ----------


def _all_static_screens() -> list[str]:
    draft = Draft(title="Тест <b>", uploader="Автор & Ко", duration=3725)
    plan = plan_auto(3725, Goal.TIKTOK, ClipSettings(), banner_seconds=7.5, face_crop=True)

    class Banner:
        duration, has_audio, width, height = 7.5, True, 720, 1280

    done = JobState(id="x", status=JobStatus.DONE, archive_name="clips.zip")
    running = JobState(id="y", status=JobStatus.RUNNING, stage="Рендер <1>", progress=42, log=["a < b", "c"])
    screens = [
        texts.START, texts.HELP, texts.REQUEST_LINK, texts.REQUEST_QUERY, texts.REQUEST_FILE, texts.NUDGE,
        texts.SEARCHING.format(query=esc("x < y")), texts.SEARCH_NOTHING.format(query=esc("x")), texts.FETCHING,
        texts.BAD_LINK, texts.DOWNLOADING_FILE, texts.FILE_TOO_BIG, texts.FILE_BAD.format(error="ошибка"),
        texts.NOT_A_VIDEO, texts.BUSY, texts.QUEUED_TEXT, texts.CANCELLED, texts.BANNER_EMPTY,
        texts.error_text("плохо <b>"), texts.search_cards([{"title": "A & B", "uploader": "U", "duration": 90}]),
        texts.search_cards([
            {"platform": "youtube", "title": "Один <i>", "uploader": "U & V", "duration": 90},
            {"platform": "rutube", "title": "Два", "uploader": "", "duration": 4000},
            {"platform": "нет такой", "title": "Три", "duration": 5},
        ]),
        texts.mode_screen(Draft(title="Ролик", uploader="Автор", duration=600, platform="rutube")),
        texts.mode_screen(draft), texts.auto_screen(draft, plan, 7.5), texts.manual_screen(draft, 7.5),
        texts.manual_screen(Draft(settings=ClipSettings(banner_count=0)), None), texts.more_screen(draft.settings),
        texts.trim_screen(draft.settings), texts.banners_screen(Banner()), texts.banners_screen(None),
        texts.stats_text(3, 2, 99), texts.progress_text(running), texts.results_summary(done),
        texts.video_card("T < 1", None, 0), texts.video_card("T", "U", 60, "vk"),
    ]  # fmt: skip
    for platform in SEARCHABLE:
        screens.append(texts.trends_home(platform))
        for category in CATEGORIES:
            screens.append(texts.trends_loading(category, platform))
            screens.append(texts.trend_list(category, [], platform))
            screens.append(
                texts.trend_list(category, [{"title": "A <b>", "duration": 600, "uploader": "U", "views": 12_345}], platform)
            )
    for goal in GOALS:
        screens.append(texts.auto_screen(draft, plan_auto(900, goal, ClipSettings(), banner_seconds=None, face_crop=False), None))
    return screens


@pytest.mark.parametrize("html", _all_static_screens())
def test_every_screen_is_valid_rich_and_converts_to_valid_classic(html):
    assert_valid_rich(html)
    classic = to_classic(html)
    assert classic and len(classic) <= CLASSIC_LIMIT
    assert classic_problems(classic) == [], classic


def test_user_supplied_text_never_injects_tags():
    draft = Draft(title="<h1>взлом</h1>", uploader="<script>", duration=60)
    html = texts.mode_screen(draft)
    assert "<h1>взлом" not in html and "<script>" not in html
    assert "&lt;h1&gt;взлом" in html
    assert rich_problems(html) == []


def test_validator_catches_what_it_should():
    assert rich_problems("<p>ок</p>") == []
    assert rich_problems("<marquee>x</marquee>")
    assert rich_problems("<p>не закрыто")
    assert rich_problems("<p><b>x</p></b>")
    assert rich_problems("<p>&copy;</p>")
    assert rich_problems("<p>" + "x" * 33000 + "</p>")


def test_trend_titles_in_lists_are_escaped_and_numbers_are_humane():
    html = texts.trend_list(
        CATEGORIES[0], [{"title": "AC & DC <live>", "duration": 4000, "uploader": "Chan", "views": 1_340_000}], YOUTUBE
    )
    assert "AC &amp; DC &lt;live&gt;" in html
    assert re.search(r"1,3 млн", html)


def test_every_trend_screen_names_its_platform():
    for platform in SEARCHABLE:
        assert platform.title in texts.trends_home(platform)
        assert platform.title in texts.trends_loading(CATEGORIES[0], platform)
        assert platform.title in texts.trend_list(CATEGORIES[0], [], platform)
        assert platform.title in texts.trend_list(CATEGORIES[0], [{"title": "x", "duration": 1}], platform)


def test_platform_tag_names_the_source_and_stays_quiet_when_unknown():
    assert texts.platform_tag("rutube") == "📺 RuTube"
    assert texts.platform_tag("vk") == "💙 VK Видео"
    assert texts.platform_tag(None) == texts.platform_tag("") == texts.platform_tag("нет такой") == ""


def test_video_card_shows_the_platform_and_skips_what_is_unknown():
    card = texts.video_card("Название", "Автор", 3725, "rutube")
    assert "Название" in card and "Автор" in card and "1:02:05" in card and "RuTube" in card
    assert "RuTube" not in texts.video_card("Название", "Автор", 60)  # файл, присланный в чат, — без площадки
    assert "👤" not in texts.video_card("Название", None, 60, "vk")


def test_search_cards_mark_the_platform_of_every_result():
    html = texts.search_cards([
        {"platform": "youtube", "title": "Один", "uploader": "A", "duration": 60},
        {"platform": "rutube", "title": "Два", "uploader": "B", "duration": 60},
    ])  # fmt: skip
    assert "YouTube" in html and "RuTube" in html


def test_start_and_help_list_every_platform():
    for platform in PLATFORMS:
        assert platform.title in texts.START
        assert platform.title in texts.HELP
        assert platform.title in texts.BAD_LINK
        assert platform.title in texts.REQUEST_LINK


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(95, "1:35"), (3725, "1:02:05"), (0, "0:00"), (59.6, "1:00")],
)
def test_duration_text_is_human_readable(seconds, expected):
    assert texts.duration_text(seconds) == expected


def test_seconds_text_uses_a_decimal_comma():
    assert texts.seconds_text(7.5) == "7,5 с" and texts.seconds_text(30.0) == "30 с"


def test_progress_bar_has_a_fixed_width_and_never_overflows():
    for percent in (-10, 0, 33, 50, 100, 250):
        bar = texts.progress_bar(percent)
        assert len(bar) == texts.BAR_WIDTH
