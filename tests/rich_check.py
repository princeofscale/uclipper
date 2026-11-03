"""Проверка rich-разметки в тестах: только документированные теги, всё закрыто, сущности — из разрешённых."""
from __future__ import annotations

import re
from html.parser import HTMLParser

# Теги, которыми написаны экраны бота. Расширять список можно только по документации Bot API 10.1+.
ALLOWED_TAGS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "b", "i", "u", "s", "code", "mark", "hr", "br",
    "ul", "ol", "li", "blockquote", "table", "tr", "td", "th", "details", "summary", "footer", "pre",
})  # fmt: skip
VOID = frozenset({"br", "hr"})
ALLOWED_ENTITIES = frozenset({"lt", "gt", "amp", "quot", "apos", "nbsp", "hellip", "mdash", "ndash", "lsquo", "rsquo", "ldquo", "rdquo"})
LIMIT_CHARS = 32768
LIMIT_BLOCKS = 500


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.blocks = 0

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"недокументированный тег <{tag}>")
        self.blocks += 1
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"недокументированный тег <{tag}/>")
        self.blocks += 1

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> закрывает не то (открыто: {self.stack[-1] if self.stack else 'ничего'})")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()

    def handle_entityref(self, name):
        if name not in ALLOWED_ENTITIES:
            self.problems.append(f"сущность &{name}; не разрешена")

    def handle_charref(self, name):
        self.problems.append(f"числовая сущность &#{name}; не нужна — пишите символ как есть")


def rich_problems(html: str) -> list[str]:
    """Список нарушений; пустой — разметка годится для rich-сообщения."""
    checker = _Checker()
    checker.feed(html)
    checker.close()
    problems = list(checker.problems)
    if checker.stack:
        problems.append(f"не закрыто: {checker.stack}")
    if len(re.sub(r"<[^>]+>", "", html)) > LIMIT_CHARS:
        problems.append("текст длиннее 32768 символов")
    if checker.blocks > LIMIT_BLOCKS:
        problems.append("больше 500 блоков")
    return problems


def assert_valid_rich(html: str) -> None:
    problems = rich_problems(html)
    assert not problems, f"{problems}\n{html}"
