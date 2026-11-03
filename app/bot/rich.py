"""Rich-сообщения Telegram (Bot API 10.1+) и запасной классический HTML.

Каждый экран бота пишется один раз — на rich-HTML: заголовки, списки, таблицы, цитаты, раскрывающиеся
блоки. Если Telegram не принял такое сообщение (старый сервер, неподдержанный тег), тот же экран уходит
классическим HTML: `to_classic` переводит rich-разметку в теги, которые понимает `parse_mode=HTML`.

В rich-HTML пробелы и переводы строк — это просто пробелы (как в браузере): строку обрывают `<br/>`,
`<p>` и блочные теги. Только то, что Telegram документирует: `h1–h6`, `p`, `b`, `i`, `u`, `s`, `code`,
`mark`, `ul/ol/li`, `table`, `blockquote`, `aside`, `details`, `hr`, `tg-time`.
"""
from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser

from aiogram.types import InputRichMessage

CLASSIC_LIMIT = 4096  # символов в обычном сообщении
DIVIDER = "──────────"
INDENT = "\u2002\u2002"  # отступ вложенных списков: обычные пробелы после перевода строки сгладились бы

_BREAK = "\x01"  # внутренний маркер «здесь пустая строка»: подряд идущие маркеры схлопываются в один


def esc(text: object) -> str:
    """Экранирует текст для вставки в HTML: названия роликов часто содержат `<`, `&` и кавычки."""
    return str(text if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(text: object) -> str:
    return esc(text).replace('"', "&quot;")


def rich_input(html: str) -> InputRichMessage:
    """Тело rich-сообщения. Автоопознавание ссылок, #тегов и @имён выключено: оформление задаём сами."""
    return InputRichMessage(html=html, skip_entity_detection=True)


# ---------- rich-HTML → классический HTML ----------

_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_VOID = frozenset({"br", "hr", "input", "img", "meta", "link"})
_INLINE = {
    "b": "b", "strong": "b", "mark": "b",
    "i": "i", "em": "i",
    "u": "u", "ins": "u",
    "s": "s", "strike": "s", "del": "s",
    "tg-spoiler": "tg-spoiler",
}  # fmt: skip


class _Converter(HTMLParser):
    """Читает rich-HTML и пишет тот же смысл в тегах классического `parse_mode=HTML`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._closers: dict[str, list[str]] = {}
        self._lists: list[list] = []  # [вид, следующий номер]
        self._bullet: str | None = None  # маркер пункта, который ждёт первого содержимого
        self._pre = 0
        self._capture: list[list[str]] = []  # куда вернуться после ячейки таблицы
        self._rows: list[list[tuple[str, bool]]] | None = None
        self._row: list[tuple[str, bool]] = []
        self._cell_is_header = False

    # ---------- события парсера ----------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value for name, value in attrs}
        if tag == "input":
            if attr.get("type") == "checkbox":
                self._bullet = None
                self.parts.append("☑ " if "checked" in attr else "☐ ")
            return
        if tag == "br":
            self.parts.append("\n")
            return
        if tag == "hr":
            self.parts.append(f"{_BREAK}{DIVIDER}{_BREAK}")
            return
        if tag in _VOID:
            return
        if tag in ("td", "th"):
            self._capture.append(self.parts)
            self.parts = []
            self._cell_is_header = tag == "th"
            return
        if tag == "tr":
            self._row = []
            return
        if tag == "table":
            self.parts.append(_BREAK)
            self._rows = []
            return
        self._flush_bullet()
        opening, closing = self._tag_pair(tag, attr)
        self.parts.append(opening)
        self._closers.setdefault(tag, []).append(closing)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if tag in ("td", "th"):
            cell = re.sub(r"\s+", " ", "".join(self.parts)).strip()
            self.parts = self._capture.pop() if self._capture else []
            self._row.append((cell, self._cell_is_header))
            return
        if tag == "tr":
            if self._rows is not None:
                self._rows.append(self._row)
            return
        if tag == "table":
            if self._rows is not None:
                self.parts.append(_render_table(self._rows) + _BREAK)
            self._rows = None
            return
        stack = self._closers.get(tag)
        if stack:
            self.parts.append(stack.pop())
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
        elif tag in ("ul", "ol") and self._lists:
            self._lists.pop()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._pre:
            self._flush_bullet()
            self.parts.append(esc(data))
            return
        collapsed = re.sub(r"[ \t\r\n\f\v]+", " ", data)  # \xa0 (&nbsp;) намеренно остаётся
        if not collapsed.strip(" "):
            if self.parts and not self.parts[-1].endswith((" ", "\n", _BREAK)):
                self.parts.append(" ")
            return
        self._flush_bullet()
        if self.parts and self.parts[-1].endswith((" ", "\n", _BREAK)):
            collapsed = collapsed.lstrip(" ")  # «☑ » + « готово» — один пробел, а не два
        self.parts.append(esc(collapsed))

    # ---------- детали ----------

    def _flush_bullet(self) -> None:
        if self._bullet is not None:
            self.parts.append(self._bullet)
            self._bullet = None

    def _tag_pair(self, tag: str, attr: dict[str, str | None]) -> tuple[str, str]:
        if tag in _INLINE:
            name = _INLINE[tag]
            return f"<{name}>", f"</{name}>"
        if tag == "code":
            css = attr.get("class")
            return (f'<code class="{esc_attr(css)}">' if css else "<code>"), "</code>"
        if tag == "a":
            href = attr.get("href") or ""
            if href and not href.startswith("#"):
                return f'<a href="{esc_attr(href)}">', "</a>"
            return "", ""
        if tag == "tg-time":
            unix = attr.get("unix") or ""
            fmt = attr.get("format")
            pieces = f' unix="{esc_attr(unix)}"' + (f' format="{esc_attr(fmt)}"' if fmt else "")
            return f"<tg-time{pieces}>", "</tg-time>"
        if tag in _HEADINGS:
            return f"{_BREAK}<b>", "</b>\n"
        if tag in ("p", "div", "figure"):
            return _BREAK, _BREAK
        if tag in ("ul", "ol"):
            start = attr.get("start")
            first = int(start) if start and start.lstrip("-").isdigit() else 1
            nested = bool(self._lists)
            self._lists.append([tag, first])
            return ("" if nested else _BREAK), ("" if nested else _BREAK)
        if tag == "li":
            kind, number = self._lists[-1] if self._lists else ("ul", 1)
            indent = INDENT * max(0, len(self._lists) - 1)
            if self._lists:
                self._lists[-1][1] = number + 1
            self._bullet = "• " if kind == "ul" else f"{number}. "
            return f"\n{indent}", ""
        if tag == "blockquote":
            return (
                f"{_BREAK}<blockquote expandable>" if "expandable" in attr else f"{_BREAK}<blockquote>",
                f"</blockquote>{_BREAK}",
            )
        if tag == "aside":
            return f"{_BREAK}<blockquote>", f"</blockquote>{_BREAK}"
        if tag == "details":
            return f"{_BREAK}<blockquote expandable>", f"</blockquote>{_BREAK}"
        if tag == "summary":
            return "<b>", "</b>\n"
        if tag == "cite":
            return "\n<i>— ", "</i>"
        if tag == "footer":
            return f"{_BREAK}<i>", f"</i>{_BREAK}"
        if tag == "caption":
            return "<b>", "</b>\n"
        if tag == "pre":
            self._pre += 1
            return f"{_BREAK}<pre>", f"</pre>{_BREAK}"
        return "", ""  # sub, sup, span, figcaption… — берём только их текст

    def finish(self) -> str:
        self.close()
        text = "".join(self.parts)
        # Содержимое <pre> трогать нельзя: в нём важны и пробелы, и переводы строк. Прячем его до уборки.
        blocks: list[str] = []

        def hide(match: re.Match) -> str:
            blocks.append(match.group(0))
            return f"\x02{len(blocks) - 1}\x02"

        text = re.sub(r"<pre>.*?</pre>", hide, text, flags=re.DOTALL)
        text = re.sub(r"[\s\x01]+(?=</blockquote>)", "", text)  # внутри цитаты пустых строк по краям не бывает
        text = re.sub(r"(<blockquote(?: expandable)?>)[\s\x01]+", r"\1", text)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        text = re.sub(r"[ \t]*\x01[ \t]*", "\x01", text)
        text = re.sub(r"\n*\x01[\n\x01]*", "\n\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"\x02(\d+)\x02", lambda match: blocks[int(match.group(1))], text)
        return text.strip()


def _render_table(rows: list[list[tuple[str, bool]]]) -> str:
    """Таблица без таблиц: «заголовок — значение» построчно, строки заголовков выделены жирным."""
    lines = []
    for row in rows:
        cells = [(cell, header) for cell, header in row if cell]
        if not cells:
            continue
        if all(header for _, header in cells):
            lines.append("<b>" + " · ".join(cell for cell, _ in cells) + "</b>")
        elif len(cells) == 2:
            key, value = cells[0][0], cells[1][0]
            key = key if "<b>" in key else f"<b>{key}</b>"
            lines.append(f"{key} — {value}")
        else:
            lines.append(" · ".join(cell for cell, _ in cells))
    return "\n".join(lines)


def to_classic(html: str) -> str:
    """Rich-HTML → классический HTML Telegram (то, что понимает `parse_mode=HTML`)."""
    converter = _Converter()
    converter.feed(html)
    text = converter.finish()
    if len(text) > CLASSIC_LIMIT:  # резать посреди тега нельзя — отдаём голый текст
        text = esc(plain_text(html)[: CLASSIC_LIMIT - 1] + "…")
    return text


def plain_text(html: str) -> str:
    """Текст экрана без разметки: для логов и проверок в тестах."""
    classic = _Converter()
    classic.feed(html)
    text = classic.finish()
    return html_lib.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))
