"""Площадки, с которых бот берёт видео: узнаёт ссылку, называет площадку и считает ключ кеша.

Ссылка принимается, только если её адрес принадлежит известной площадке — по домену целиком, а не по
подстроке: `youtube.com.evil.com` не подойдёт. Заодно это значит, что yt-dlp ходит лишь туда, куда мы
разрешили, а не по любому адресу, который пришлют.

Поиск по названию (`Platform.search`) и раздел «В тренде» (`Platform.trends`) есть не у всех площадок: им нужен
открытый API без входа в аккаунт. У VK Видео, Дзена и OK результаты видны только после входа, у Twitch и Kick
поиск идёт по каналам, а не по словам — для них работают ссылки, скачивание берёт на себя yt-dlp.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import ParseResult, parse_qs, urlparse

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_PATHS = frozenset({"shorts", "embed", "live", "v"})

EVERYWHERE = "all"  # в кнопках выбора площадки: «везде» (это не площадка, из id площадок его не выбрать)


@dataclass(frozen=True)
class Platform:
    id: str  # короткий латинский id: он едет в callback_data и в имя файла кеша
    title: str  # как называть площадку по-русски
    emoji: str
    hosts: tuple[str, ...]  # домены; подходят и поддомены (`m.vk.com`, `clips.twitch.tv`)
    search: bool = False  # есть поиск по названию
    trends: bool = False  # есть раздел «В тренде» (нужно много свежих роликов на русском)
    id_patterns: tuple[str, ...] = ()  # как достать id ролика из адреса: именованная группа `id`


YOUTUBE = Platform(
    "youtube", "YouTube", "▶️", ("youtube.com", "youtu.be", "youtube-nocookie.com"), search=True, trends=True
)
RUTUBE = Platform(
    "rutube", "RuTube", "📺", ("rutube.ru",), search=True, trends=True,
    id_patterns=(r"(?:video(?:/private)?|embed)/(?P<id>[0-9a-z]{32})",),
)  # fmt: skip
# Адрес VK Play `live.vkvideo.ru` — поддомен `vkvideo.ru`: площадку выбирает самый длинный подошедший домен.
VKPLAY = Platform(
    "vkplay", "VK Play", "🎮", ("vkplay.live", "live.vkplay.ru", "live.vkvideo.ru"),
    id_patterns=(r"/record/(?P<id>[0-9a-f-]{36})",),
)  # fmt: skip
VK = Platform("vk", "VK Видео", "💙", ("vkvideo.ru", "vk.com", "vk.ru"), id_patterns=(r"(?:video|clip)(?P<id>-?\d+_\d+)",))
DZEN = Platform("dzen", "Дзен", "✨", ("dzen.ru", "zen.yandex.ru"), id_patterns=(r"(?P<id>[0-9a-f]{24})",))
OK = Platform("ok", "OK", "🧡", ("ok.ru", "odnoklassniki.ru"), id_patterns=(r"/video(?:embed)?/(?P<id>\d+)",))
TWITCH = Platform(
    "twitch", "Twitch", "🟣", ("twitch.tv",),
    id_patterns=(r"/(?:videos|v)/(?P<id>\d+)", r"(?:/clip/|clips\.twitch\.tv/)(?P<id>[\w-]+)"),
)  # fmt: skip
KICK = Platform(
    "kick", "Kick", "🟢", ("kick.com",),
    id_patterns=(r"(?:clip=|/clips/)(?P<id>clip_[\w-]+)", r"/videos/(?P<id>[0-9a-f-]{36})"),
)  # fmt: skip
DAILYMOTION = Platform(
    "dailymotion", "Dailymotion", "🔵", ("dailymotion.com", "dai.ly"), search=True,
    id_patterns=(r"(?:/video/|dai\.ly/)(?P<id>[a-z0-9]+)",),
)  # fmt: skip

PLATFORMS: tuple[Platform, ...] = (YOUTUBE, RUTUBE, VK, DZEN, OK, VKPLAY, TWITCH, KICK, DAILYMOTION)
PLATFORM_BY_ID = {platform.id: platform for platform in PLATFORMS}
SEARCHABLE: tuple[Platform, ...] = tuple(platform for platform in PLATFORMS if platform.search)
TRENDING: tuple[Platform, ...] = tuple(platform for platform in PLATFORMS if platform.trends)

_ID_PATTERNS = {platform.id: tuple(re.compile(pattern) for pattern in platform.id_patterns) for platform in PLATFORMS}


@dataclass(frozen=True)
class Source:
    """Ссылка на ролик известной площадки."""

    platform: Platform
    url: str  # адрес для yt-dlp: со схемой, без пробелов

    @property
    def video_id(self) -> str | None:
        """id ролика на площадке или `None`, если из адреса его не достать (например, ссылка на канал)."""
        if self.platform is YOUTUBE:
            return _youtube_id(self.url)
        return _site_id(self.platform, self.url)

    @property
    def key(self) -> str:
        """Ключ кеша, безопасный для имени файла. У YouTube это сам 11-символьный id, у остальных `<площадка>-<id>`."""
        if self.platform is YOUTUBE:
            return self.video_id or ""
        token = self.video_id or hashlib.sha1(self.url.split("#", 1)[0].encode(), usedforsecurity=False).hexdigest()[:16]
        return f"{self.platform.id}-{re.sub(r'[^A-Za-z0-9_-]', '_', token)[:64]}"


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _youtube_id(url: str) -> str | None:
    """11-символьный id ролика из ссылки YouTube (watch, youtu.be, shorts, embed, live) или `None`."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    candidate = None
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif _host_matches(host, "youtube.com") or _host_matches(host, "youtube-nocookie.com"):
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in _YOUTUBE_PATHS:
                candidate = parts[1]
    return candidate if candidate and _YOUTUBE_ID.match(candidate) else None


def _site_id(platform: Platform, url: str) -> str | None:
    parsed = urlparse(url)
    haystack = f"{parsed.hostname or ''}{parsed.path}?{parsed.query}"
    for pattern in _ID_PATTERNS[platform.id]:
        found = pattern.search(haystack)
        if found:
            return found.group("id")
    return None


def _parse_token(token: str) -> tuple[str, ParseResult, str] | None:
    """Слово сообщения → (адрес со схемой, его разбор, домен), если это похоже на http(s)-адрес с точкой в домене."""
    text = token.strip().strip("<>()[]\"'«».,;!")
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or "." not in host:
        return None
    return text, parsed, host


def _platform_of(host: str) -> Platform | None:
    """Площадка по домену. Если подходят несколько (`live.vkvideo.ru` — и VK, и VK Play), выигрывает самый длинный домен."""
    matches = [(len(domain), platform) for platform in PLATFORMS for domain in platform.hosts if _host_matches(host, domain)]
    return max(matches, key=lambda match: match[0])[1] if matches else None


def _identify_token(token: str) -> Source | None:
    parsed_token = _parse_token(token)
    if parsed_token is None:
        return None
    text, parsed, host = parsed_token
    platform = _platform_of(host)
    if platform is None:
        return None
    if platform is YOUTUBE:
        return Source(platform, text) if _youtube_id(text) else None
    if not (parsed.path.strip("/") or parsed.query):
        return None  # просто главная страница площадки — не ролик
    return Source(platform, text)


def identify(text: str) -> Source | None:
    """Ссылка на ролик известной площадки из текста сообщения или `None`.

    Ссылку находим и среди слов: из приложений ссылки приходят как «Название https://…».
    """
    for token in (text or "").split():
        source = _identify_token(token)
        if source is not None:
            return source
    return None


def mentions_known_site(text: str) -> bool:
    """В тексте есть адрес известной площадки (пусть и не ролика): человек прислал ссылку, а не название."""
    for token in (text or "").split():
        parsed_token = _parse_token(token)
        if parsed_token is not None and _platform_of(parsed_token[2]) is not None:
            return True
    return False


def scope(choice: str | None, among: tuple[Platform, ...]) -> tuple[Platform, ...] | None:
    """Где искать: `EVERYWHERE` — на всех площадках списка `among`, id площадки — только на ней. `None` — такого выбора нет."""
    if choice == EVERYWHERE:
        return among
    chosen = tuple(platform for platform in among if platform.id == choice)
    return chosen or None


def titles() -> str:
    """Все площадки через запятую — для текстов бота."""
    return ", ".join(platform.title for platform in PLATFORMS)
