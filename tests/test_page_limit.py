"""Only the start of a channel page gets read. These move, cut and fake its tags to see what that can get wrong.

Whatever YouTube does to its page, the answer must match a full read or say less. It must never be wrong.
"""

from __future__ import annotations

import httpx
import pytest

from tests.support import MKBHD, FakeTransport, FixedClock, no_sleep, recorded_page, world
from uppyt.services.app import UppYT
from uppyt.services.runtime import Paths, Runtime
from uppyt.sources.channels import PAGE_START, ChannelLookupError, PageResolver, ResolvedChannel
from uppyt.sources.transport import MAX_BYTES, HttpTransport, Response, TransportError

AT_LINK = "https://www.youtube.com/@mkbhd"
ID_LINK = f"https://www.youtube.com/channel/{MKBHD}"
PICTURE = "https://yt3.googleusercontent.com/picture=s900-c"
TRUTH = ResolvedChannel(MKBHD, "Marques Brownlee", "@mkbhd", "https://yt3.googleusercontent.com/picture=s176-c")
SIZE = 3_000_000
USUAL = 825_000
VANITY = '{"vanityChannelUrl":"http://www.youtube.com/@mkbhd"}'
ID_TAG = f'<link rel="canonical" href="{ID_LINK}">'
NAME_TAG = '<meta property="og:title" content="Marques Brownlee">'
PICTURE_TAG = f'<meta property="og:image" content="{PICTURE}">'
HANDLE_TAG = '<link rel="alternate" media="handheld" href="https://m.youtube.com/@mkbhd">'
TAGS = {"id": ID_TAG, "name": NAME_TAG, "picture": PICTURE_TAG, "handle": HANDLE_TAG}


def page(*, end_tag: str = VANITY, extra: tuple[tuple[int, str], ...] = (), **places: int | None) -> bytes:
    """A 3 MB page shaped like the real one, with each tag at USUAL, or where it's told to be. None leaves it out."""
    wanted = {name: places.get(name, USUAL + 200 * n) for n, name in enumerate(TAGS)}
    parts = [(at, TAGS[name]) for name, at in wanted.items() if at is not None]
    parts += extra
    if end_tag:
        parts.append((SIZE - len(end_tag), end_tag))
    built = bytearray()
    for at, text in sorted(parts):
        assert at >= len(built), f"overlapping tags at {at}"
        built += b"x" * (at - len(built)) + text.encode()
    return bytes(built) + b"x" * max(0, SIZE - len(built))


class Server(FakeTransport):
    """Serves one page and cuts it where the real transport would. Answers can be swapped for later reads."""

    def __init__(self, first: bytes, later: list | None = None):
        super().__init__()
        self.first = first
        self.later = later or []

    def get(self, url, *, stop_after=None):
        self.requests.append(url)
        self.stops.append(stop_after)
        answer = self.first if len(self.requests) == 1 or not self.later else self.later.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Response):
            return answer
        return Response(200, answer[:stop_after] if stop_after else answer, "text/html", url)


def look_up(server, text="@mkbhd", **options):
    return PageResolver(server, FixedClock()).resolve(text, **options)


# Tags where they are now, and tags that move


def test_with_everything_in_its_usual_place_one_short_read_is_enough():
    server = Server(page())
    assert look_up(server) == TRUTH
    assert server.stops == [PAGE_START]


@pytest.mark.parametrize("field", list(TAGS))
@pytest.mark.parametrize(
    ("place", "reads"),
    [
        (lambda tag: PAGE_START - len(tag), 1),  # ends exactly at the limit
        (lambda tag: PAGE_START - len(tag) + 1, 1),  # only its closing > is cut off, and that's not needed
        (lambda tag: PAGE_START - len(tag) + 2, 2),  # its closing quote is cut off too
        (lambda tag: PAGE_START - len(tag) // 2, 2),  # cut in half
        (lambda tag: PAGE_START, 2),  # starts exactly at the limit
        (lambda tag: SIZE - 1000, 2),  # moved to the end
    ],
    ids=["ends-at-limit", "bracket-cut", "quote-cut", "cut-in-half", "starts-at-limit", "near-the-end"],
)
def test_one_tag_moving_never_changes_the_answer(field, place, reads):
    server = Server(page(**{field: place(TAGS[field])}))
    assert look_up(server) == TRUTH
    assert len(server.stops) == reads


def test_all_of_them_moving_together():
    server = Server(page(id=2_000_000, name=2_000_200, picture=2_000_400, handle=2_000_600))
    assert look_up(server) == TRUTH
    assert server.stops == [PAGE_START, None]


def test_tags_moving_to_the_very_start():
    server = Server(page(id=0, name=200, picture=400, handle=600))
    assert look_up(server) == TRUTH
    assert server.stops == [PAGE_START]


def test_the_handle_moving_when_the_page_only_names_it_near_the_end():
    server = Server(page(handle=None))
    assert look_up(server) == TRUTH
    assert server.stops == [PAGE_START, None]


def test_a_page_cut_at_any_byte_gives_the_right_answer():
    """Every possible limit, across the real recorded page."""
    recording = recorded_page().encode()
    whole = look_up(Server(recording))
    for cut in range(len(recording) + 1):
        short = Server(recording[:cut], [recording])
        assert look_up(short) == whole, f"cut at {cut}"


@pytest.mark.parametrize("cut", range(30))
def test_a_cut_inside_a_character_cant_garble_a_name(cut):
    tag = '<meta property="og:title" content="Ñandú Café 日本語">'.encode()
    full = page(name=None, extra=((PAGE_START - cut, tag.decode()),)) if cut else page(name=None)
    server = Server(full)
    name = look_up(server).name
    assert "\ufffd" not in name
    assert name == ("Ñandú Café 日本語" if cut else MKBHD)


# Tags that have gone altogether


def test_with_the_id_gone_it_says_so_after_reading_everything():
    server = Server(page(id=None))
    with pytest.raises(ChannelLookupError, match="doesn't name a channel"):
        look_up(server)
    assert server.stops == [PAGE_START, None]


@pytest.mark.parametrize(
    ("gone", "expected"),
    [
        ({"name": None}, ResolvedChannel(MKBHD, MKBHD, "@mkbhd", TRUTH.avatar_url)),
        ({"picture": None}, ResolvedChannel(MKBHD, "Marques Brownlee", "@mkbhd", None)),
        ({"handle": None, "end_tag": ""}, ResolvedChannel(MKBHD, "Marques Brownlee", None, TRUTH.avatar_url)),
    ],
    ids=["name", "picture", "handle"],
)
def test_a_tag_gone_from_the_whole_page_costs_one_read_and_is_remembered(gone, expected):
    server = Server(page(**gone))
    resolver = PageResolver(server, FixedClock())
    assert resolver.resolve("@mkbhd") == expected
    assert server.stops == [PAGE_START, None]
    assert resolver.resolve("@mkbhd") == expected
    assert len(server.requests) == 2  # a whole read found all there was, so there's no point asking again


@pytest.mark.parametrize("tag", ['<meta property="og:title" content="">', '<meta property="og:title" content="  ">'])
def test_a_blank_name_is_never_used(tag):
    server = Server(page(name=None, extra=((USUAL + 1000, tag),)))
    assert look_up(server).name == MKBHD


def test_a_picture_on_the_wrong_host_is_refused_without_reading_more():
    bad = '<meta property="og:image" content="https://evil.example/x.jpg">'
    server = Server(page(picture=None, extra=((USUAL + 1000, bad),)))
    assert look_up(server).avatar_url is None
    assert server.stops == [PAGE_START]


# The second read going wrong


@pytest.mark.parametrize(
    "failure",
    [
        TransportError("Couldn't reach YouTube: connection dropped"),
        Response(500, b"oops", "text/html", AT_LINK),
        Response(404, b"gone", "text/html", AT_LINK),
        Response(200, b"", "text/html", AT_LINK),
        Response(200, b"<html>not a channel</html>", "text/html", AT_LINK),
        Response(200, b"consent", "text/html", "https://consent.youtube.com/m"),
    ],
    ids=["dropped", "500", "404", "empty", "other-page", "consent"],
)
def test_a_failed_second_read_keeps_what_the_first_found_and_tries_again_next_time(failure):
    moved = page(name=2_000_000)
    server = Server(moved, [failure, moved])
    resolver = PageResolver(server, FixedClock())
    partial = resolver.resolve("@mkbhd")
    assert partial == ResolvedChannel(MKBHD, MKBHD, "@mkbhd", TRUTH.avatar_url)
    assert resolver.resolve("@mkbhd") == TRUTH


@pytest.mark.parametrize(
    "failure",
    [TransportError("Couldn't reach YouTube: dropped"), Response(500, b"oops", "text/html", AT_LINK)],
    ids=["dropped", "500"],
)
def test_with_no_id_from_either_read_it_still_reports_an_error(failure):
    server = Server(page(id=2_000_000), [failure])
    with pytest.raises(ChannelLookupError):
        look_up(server)


# Fakes


def test_another_channels_handle_earlier_in_the_page_isnt_taken():
    """A link to another channel in the page data, with this channel's own tag now out of reach."""
    decoy = '{"url":"https://m.youtube.com/@somebodyelse"}'
    server = Server(page(handle=None, extra=((USUAL + 1000, decoy),)))
    assert look_up(server).handle == "@mkbhd"


@pytest.mark.parametrize(
    "decoy",
    [
        '\\u003cmeta property=\\"og:title\\" content=\\"Fake name\\"\\u003e',
        "&lt;meta property=&quot;og:title&quot; content=&quot;Fake name&quot;&gt;",
    ],
    ids=["json-escaped", "html-escaped"],
)
def test_a_tag_quoted_in_a_channel_description_isnt_believed(decoy):
    server = Server(page(extra=((1000, decoy),)))
    assert look_up(server) == TRUTH


def test_a_quoted_id_in_a_description_isnt_believed():
    other = "UC" + "z" * 22
    decoy = f'\\u003clink rel=\\"canonical\\" href=\\"https://www.youtube.com/channel/{other}\\"\\u003e'
    server = Server(page(extra=((1000, decoy),)))
    assert look_up(server).id == MKBHD


def test_a_name_with_html_in_it_stays_as_text():
    tag = '<meta property="og:title" content="Tom &amp; &quot;Jerry&quot; &lt;b&gt;">'
    server = Server(page(name=None, extra=((USUAL + 1000, tag),)))
    assert look_up(server).name == 'Tom & "Jerry" <b>'


# Imports, which don't care about handles


def test_an_import_doesnt_read_on_for_a_missing_handle():
    server = Server(page(handle=None, end_tag=""))
    assert look_up(server, ID_LINK, need_handle=False).name == "Marques Brownlee"
    assert server.stops == [PAGE_START]


@pytest.mark.parametrize("field", ["name", "picture"])
def test_an_import_still_reads_on_for_a_moved_name_or_picture(field):
    server = Server(page(**{field: 2_000_000}))
    assert look_up(server, ID_LINK, need_handle=False) == TRUTH
    assert server.stops == [PAGE_START, None]


def test_following_soon_after_an_import_still_gets_the_handle():
    """An import remembers a channel without its handle. That mustn't become permanent when someone adds it by hand."""
    server = Server(page(handle=None))
    resolver = PageResolver(server, FixedClock())
    assert resolver.resolve(ID_LINK, need_handle=False).handle is None
    assert resolver.resolve(ID_LINK).handle == "@mkbhd"
    assert server.stops == [PAGE_START, None]


# Links that always read the whole page


def test_channel_links_read_once_wherever_the_tags_are():
    server = Server(page(handle=None, name=2_000_000))
    assert look_up(server, ID_LINK) == TRUTH
    assert server.stops == [None]


# Through the real transport, which stops mid-stream


def streaming(body: bytes, chunk: int, sent: list[int]):
    def handler(request):
        def chunks():
            for at in range(0, len(body), chunk):
                sent.append(chunk)
                yield body[at : at + chunk]

        return httpx.Response(200, content=chunks(), headers={"content-type": "text/html"})

    return HttpTransport(min_interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("chunk", [1000, 65_536, 1_000_000])
def test_the_real_transport_with_tags_where_they_usually_are(chunk):
    sent: list[int] = []
    assert look_up(streaming(page(), chunk, sent)) == TRUTH
    assert sum(sent) < PAGE_START + chunk  # it stopped when it said it would


@pytest.mark.parametrize("chunk", [1000, 65_536, 1_000_000])
def test_the_real_transport_with_a_tag_past_the_limit(chunk):
    sent: list[int] = []
    assert look_up(streaming(page(picture=2_000_000), chunk, sent)) == TRUTH
    assert sum(sent) > PAGE_START


def test_a_page_too_big_to_read_whole_keeps_what_the_start_had():
    body = page(name=None) + b"x" * (MAX_BYTES + 1)
    assert look_up(streaming(body, 65_536, [])) == ResolvedChannel(MKBHD, MKBHD, "@mkbhd", TRUTH.avatar_url)


# All the way through adding a channel


@pytest.mark.parametrize("field", ["name", "picture", "handle"])
def test_adding_a_channel_with_a_moved_tag_saves_the_right_details(tmp_path, field):
    transport = world()
    transport.routes[AT_LINK] = Response(200, page(**{field: 2_000_000}), "text/html", AT_LINK)

    runtime = Runtime(Paths(tmp_path), clock=FixedClock(), transport=transport, sleep=no_sleep)
    runtime.create_profile("dusty")
    app = UppYT(runtime, runtime.profile("dusty"))
    app.channels.add("@mkbhd")
    detail = app.channels.get("@mkbhd")  # finding it by handle needs the handle to have been saved
    assert detail.channel.name == "Marques Brownlee"
    assert detail.channel.handle == "@mkbhd"
    assert sum(url == AT_LINK for url in transport.requests) == 2
    runtime.close()
