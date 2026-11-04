"""Площадки: какие ссылки бот принимает, как определяет площадку и какой ключ кеша им выдаёт."""
import re

import pytest

from app.services.platforms import (
    DAILYMOTION,
    DZEN,
    EVERYWHERE,
    FIRSTTV,
    KICK,
    OK,
    PLATFORM_BY_ID,
    PLATFORMS,
    RUTUBE,
    SEARCHABLE,
    TRENDING,
    TWITCH,
    VK,
    VKPLAY,
    YOUTUBE,
    identify,
    mentions_known_site,
    scope,
    titles,
)

YOUTUBE_ID = "dQw4w9WgXcQ"
RUTUBE_ID = "0123456789abcdef0123456789abcdef"
UUID = "01234567-89ab-cdef-0123-456789abcdef"


# ---------- YouTube ----------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={YOUTUBE_ID}&list=PL123&t=10s",
        f"https://youtu.be/{YOUTUBE_ID}?t=10",
        f"https://www.youtube.com/shorts/{YOUTUBE_ID}",
        f"https://www.youtube.com/embed/{YOUTUBE_ID}",
        f"https://www.youtube.com/live/{YOUTUBE_ID}?feature=share",
        f"https://m.youtube.com/watch?v={YOUTUBE_ID}",
        f"youtube.com/watch?v={YOUTUBE_ID}",
        f"  https://music.youtube.com/watch?v={YOUTUBE_ID}  ",
    ],
)
def test_every_common_youtube_link_is_recognised_and_keyed_by_the_bare_id(url):
    source = identify(url)
    assert source is not None and source.platform is YOUTUBE
    assert source.key == YOUTUBE_ID  # так называется уже скачанное в кеше — ключ менять нельзя


@pytest.mark.parametrize(
    "url",
    [
        f"https://example.com/watch?v={YOUTUBE_ID}",
        f"https://youtube.com.evil.com/watch?v={YOUTUBE_ID}",
        f"https://evil.com/?u=youtube.com/watch?v={YOUTUBE_ID}",
        f"https://notyoutube.com/watch?v={YOUTUBE_ID}",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@somechannel",
        "not a url",
        "",
    ],
)
def test_everything_else_is_refused(url):
    assert identify(url) is None


# ---------- остальные площадки ----------


@pytest.mark.parametrize(
    ("url", "platform", "key"),
    [
        (f"https://rutube.ru/video/{RUTUBE_ID}/", RUTUBE, f"rutube-{RUTUBE_ID}"),
        (f"https://rutube.ru/video/private/{RUTUBE_ID}/?p=secret", RUTUBE, f"rutube-{RUTUBE_ID}"),
        (f"https://rutube.ru/play/embed/{RUTUBE_ID}", RUTUBE, f"rutube-{RUTUBE_ID}"),
        ("https://vk.com/video-22822305_456239018", VK, "vk--22822305_456239018"),
        ("https://vkvideo.ru/video-1_2", VK, "vk--1_2"),
        ("https://m.vk.com/video1_2?list=abc", VK, "vk-1_2"),
        ("https://vk.com/clip-1_2", VK, "vk--1_2"),
        (f"https://live.vkvideo.ru/someone/record/{UUID}", VKPLAY, f"vkplay-{UUID}"),
        (f"https://vkplay.live/someone/record/{UUID}", VKPLAY, f"vkplay-{UUID}"),
        ("https://dzen.ru/video/watch/0123456789abcdef01234567", DZEN, "dzen-0123456789abcdef01234567"),
        ("https://ok.ru/video/123456", OK, "ok-123456"),
        ("https://www.twitch.tv/videos/123456789", TWITCH, "twitch-123456789"),
        ("https://clips.twitch.tv/SomeSlug-abc", TWITCH, "twitch-SomeSlug-abc"),
        ("https://www.twitch.tv/chan/clip/Slug-1", TWITCH, "twitch-Slug-1"),
        ("https://kick.com/chan/clips/clip_01ABC", KICK, "kick-clip_01ABC"),
        ("https://kick.com/chan?clip=clip_01ABC", KICK, "kick-clip_01ABC"),
        ("https://www.dailymotion.com/video/x8abcd", DAILYMOTION, "dailymotion-x8abcd"),
        ("https://dai.ly/x8abcd", DAILYMOTION, "dailymotion-x8abcd"),
        ("https://www.1tv.ru/shows/kvn/vypuski/vypusk-ot-01-11-2026", FIRSTTV, "firsttv-shows_kvn_vypuski_vypusk-ot-01-11-2026"),
        ("https://1tv.ru/news/issue/2016-12-01/14:00", FIRSTTV, "firsttv-news_issue_2016-12-01_14_00"),
    ],
)
def test_links_of_other_platforms_are_recognised_with_a_per_platform_key(url, platform, key):
    source = identify(url)
    assert source is not None and source.platform is platform
    assert source.key == key


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        (f"https://youtu.be/{YOUTUBE_ID}?t=5", YOUTUBE_ID),
        (f"https://rutube.ru/video/{RUTUBE_ID}/", RUTUBE_ID),
        (f"https://rutube.ru/video/private/{RUTUBE_ID}/?p=secret", RUTUBE_ID),
        ("https://vk.com/video-22822305_456239018", "-22822305_456239018"),
        ("https://www.dailymotion.com/video/x8abcd", "x8abcd"),
        ("https://rutube.ru/channel/123/", None),  # id из адреса не достать
    ],
)
def test_the_video_id_is_taken_from_the_link(url, video_id):
    assert identify(url).video_id == video_id


def test_first_channel_key_comes_from_the_whole_path_not_from_tracking_marks():
    # ссылки с сайта приходят с utm-метками и косой чертой на конце — это тот же выпуск, и в кеше он один
    plain = identify("https://www.1tv.ru/shows/kvn/vypuski/vypusk-ot-01-11-2026")
    marked = identify("https://www.1tv.ru/shows/kvn/vypuski/vypusk-ot-01-11-2026/?utm_source=1tv&utm_medium=promo#t=5")
    assert plain.key == marked.key
    # у выпусков новостей последний кусок адреса — просто «14:00»: без остального пути разные дни склеились бы
    assert identify("https://www.1tv.ru/news/issue/2016-12-01/14:00").key != identify("https://www.1tv.ru/news/issue/2016-12-02/14:00").key


def test_a_long_path_is_folded_into_a_hash_instead_of_being_cut():
    # адреса выпусков бывают длинными, а различаются только датой в конце: обрезка склеила бы выпуски
    head = "https://www.1tv.ru/shows/pust-govoryat/vypuski-i-dramatichnye-momenty/" + "ochen-dlinnoe-nazvanie-vypuska-" * 3
    first, second = identify(head + "ot-03-11-2026"), identify(head + "ot-03-11-2025")
    assert first.key != second.key
    assert first.key.startswith("firsttv-") and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", first.key)
    assert first.key == identify(head + "ot-03-11-2026").key  # и ключ при этом стабилен


def test_vk_play_is_not_mistaken_for_vk():
    # live.vkvideo.ru — поддомен vkvideo.ru, но это другая площадка: выигрывает самый длинный домен
    assert identify(f"https://live.vkvideo.ru/someone/record/{UUID}").platform is VKPLAY
    assert identify("https://vkvideo.ru/video-1_2").platform is VK


def test_a_link_without_a_known_id_still_gets_a_stable_safe_key():
    first = identify("https://rutube.ru/channel/123/")
    second = identify("https://rutube.ru/channel/123/")
    other = identify("https://rutube.ru/channel/124/")
    assert first.key == second.key != other.key
    assert first.key.startswith("rutube-") and re.fullmatch(r"[A-Za-z0-9_-]+", first.key)


def test_the_fragment_does_not_change_the_fallback_key():
    assert identify("https://rutube.ru/channel/1/#a").key == identify("https://rutube.ru/channel/1/#b").key


@pytest.mark.parametrize(
    "url",
    [
        "https://rutube.ru/",
        "https://vk.com",
        "https://vk.com.evil.com/video1_2",
        "https://notvk.com/video1_2",
        f"https://evilrutube.ru/video/{RUTUBE_ID}/",
        f"https://rutube.ru.evil.com/video/{RUTUBE_ID}/",
        "https://evil.com/?u=https://vk.com/video1_2",
        "ftp://vk.com/video1_2",
    ],
)
def test_lookalikes_and_bare_home_pages_are_refused(url):
    assert identify(url) is None


def test_keys_are_filename_safe_even_for_hostile_links():
    hostile = [
        "https://twitch.tv/videos/../../etc/passwd",
        "https://kick.com/x/clips/clip_../../x",
        "https://vk.com/video-1_2/../../x",
        "https://www.1tv.ru/shows/../../etc/" + "x" * 300,
        "https://dzen.ru/video/watch/" + "a" * 500,
        "https://rutube.ru/" + "я" * 300,
    ]
    for url in hostile:
        source = identify(url)
        assert source is not None, url
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,100}", source.key), (url, source.key)


# ---------- поиск ссылки в тексте ----------


def test_a_link_is_found_among_other_words():
    source = identify(f"Смотри https://rutube.ru/video/{RUTUBE_ID}/ классное")
    assert source is not None and source.platform is RUTUBE
    assert identify(f"https://youtu.be/{YOUTUBE_ID}, круто").key == YOUTUBE_ID
    assert identify(f"«https://youtu.be/{YOUTUBE_ID}»").key == YOUTUBE_ID


def test_the_first_recognised_link_wins():
    text = f"https://example.com/x https://youtu.be/{YOUTUBE_ID} https://ok.ru/video/1"
    assert identify(text).platform is YOUTUBE


def test_a_known_site_is_noticed_even_when_it_is_not_a_video():
    assert mentions_known_site("https://youtube.com/@chan")
    assert mentions_known_site("глянь vk.com/someone")
    assert not mentions_known_site("интервью дурова")
    assert not mentions_known_site("https://evil.com/x")
    assert not mentions_known_site("")


# ---------- реестр ----------


def test_the_registry_is_consistent():
    ids = [platform.id for platform in PLATFORMS]
    assert len(ids) == len(set(ids)) and set(PLATFORM_BY_ID) == set(ids)
    for platform in PLATFORMS:
        assert platform.title and platform.emoji and platform.hosts
        assert all(host == host.lower() and "/" not in host for host in platform.hosts)
        assert re.fullmatch(r"[a-z]+", platform.id)  # едет в callback_data и в имя файла кеша


def test_search_is_offered_where_an_open_api_exists():
    # VK Видео, Дзен и OK не отдают результаты без входа, Twitch и Kick ищут только по каналам — у них работают ссылки
    assert [platform.id for platform in SEARCHABLE] == ["youtube", "rutube", "dailymotion"]


def test_trends_need_enough_russian_videos_so_dailymotion_has_none():
    assert [platform.id for platform in TRENDING] == ["youtube", "rutube"]
    assert set(TRENDING) <= set(SEARCHABLE)


# ---------- выбор площадки ----------


def test_scope_everywhere_means_every_platform_of_the_list():
    assert scope(EVERYWHERE, SEARCHABLE) == SEARCHABLE
    assert scope(EVERYWHERE, TRENDING) == TRENDING


def test_scope_of_one_platform_is_just_that_platform():
    assert scope("rutube", SEARCHABLE) == (RUTUBE,)
    assert scope("dailymotion", SEARCHABLE) == (DAILYMOTION,)


@pytest.mark.parametrize("choice", ["", None, "vk", "nope", "ALL", "youtube,rutube"])
def test_scope_refuses_what_the_list_does_not_offer(choice):
    assert scope(choice, SEARCHABLE) is None


def test_a_platform_without_trends_cannot_be_chosen_for_trends():
    assert scope("dailymotion", TRENDING) is None


def test_the_everywhere_id_cannot_clash_with_a_platform():
    assert EVERYWHERE not in PLATFORM_BY_ID


def test_titles_lists_every_platform_for_error_messages():
    text = titles()
    assert all(platform.title in text for platform in PLATFORMS)


def test_every_platform_explains_itself_for_the_help():
    """Справка собирается из реестра: какие ссылки подходят и почему искать (или смотреть тренды) нельзя."""
    for platform in PLATFORMS:
        assert platform.links, platform.id
        if platform.search:
            assert not platform.why_not_search, platform.id
        else:
            assert platform.why_not_search, platform.id
        if platform.search and not platform.trends:
            assert platform.why_not_trends, platform.id
        else:
            assert not platform.why_not_trends, platform.id
