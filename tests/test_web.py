from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from tests.support import MKBHD, NOW, SECOND, FixedClock, feed_xml, no_sleep, world
from uppyt.domain.feeds import FeedKind
from uppyt.domain.models import Priority
from uppyt.services.app import UppYT
from uppyt.services.runtime import Paths, Runtime
from uppyt.web.app import PROFILE_COOKIE, create_app


class Site:
    def __init__(self, tmp_path, **kwargs):
        self.clock = FixedClock()
        self.transport = world()
        self.runtime = Runtime(Paths(tmp_path), clock=self.clock, transport=self.transport, sleep=no_sleep)
        self.client = TestClient(create_app(self.runtime, background_refresh=False, **kwargs), follow_redirects=False)

    def services(self, name="dusty") -> UppYT:
        if name not in self.runtime.profile_names():
            self.runtime.create_profile(name)
        return UppYT(self.runtime, self.runtime.profile(name))

    def get(self, path, **kwargs):
        return self.client.get(path, **kwargs)

    def post(self, path, **kwargs):
        return self.client.post(path, **kwargs)

    def cards(self, path="/"):
        return self.get(path).text.count('class="card"')


@pytest.fixture
def site(tmp_path):
    made = Site(tmp_path)
    yield made
    made.runtime.close()


@pytest.fixture
def followed(site):
    site.services().channels.add("@veritasium", priority=Priority.HIGH, tags=["science"])
    return site


HTMX = {"HX-Request": "true"}


# First run


def test_a_fresh_install_starts_at_welcome(site):
    response = site.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/welcome"
    assert "Welcome to uppYT" in site.get("/welcome").text


def test_creating_the_first_profile_opens_the_blank_board(site):
    response = site.post("/profiles", data={"name": "dusty"})
    assert response.status_code == 303
    assert site.runtime.profile_names() == ["dusty"]
    assert "Add your first channel" in site.get("/").text


def test_a_bad_profile_name_comes_back_with_advice(site):
    response = site.post("/profiles", data={"name": "../evil"})
    assert response.status_code == 400
    assert "letters, numbers" in response.text
    assert 'value="../evil"' in response.text
    assert site.runtime.profile_names() == []


def test_welcome_is_skipped_once_there_is_a_profile(followed):
    assert followed.get("/welcome").headers["location"] == "/"


def test_with_two_profiles_people_choose(site):
    site.services("dusty")
    site.services("nat")
    assert site.get("/").headers["location"] == "/profiles"
    page = site.get("/profiles").text
    assert "dusty" in page
    assert "nat" in page
    response = site.post("/profiles/use", data={"name": "nat"})
    assert response.cookies[PROFILE_COOKIE] == "nat"
    assert "nat" in site.get("/").text


def test_a_forged_profile_cookie_is_ignored(site):
    site.services("dusty")
    site.services("nat")
    site.client.cookies.set(PROFILE_COOKIE, "../../etc")
    assert site.get("/").headers["location"] == "/profiles"


def test_switching_to_a_bad_name_is_refused(site):
    site.services()
    assert site.post("/profiles/use", data={"name": "../evil"}).status_code == 400


# The board


def test_the_board_shows_ranked_cards(followed):
    page = followed.get("/").text
    assert page.count('class="card"') == 4
    assert page.index("Veritasium video 0") < page.index("Veritasium video 1")
    assert "4 unseen" in page
    assert "card__tag--high" in page
    assert "https://i.ytimg.com/vi/veri0000xxx/mqdefault.jpg" in page


def test_normal_priority_carries_no_tag(site):
    site.services().channels.add("@veritasium")
    assert "card__tag" not in site.get("/").text


def test_opening_a_card_goes_to_youtube_in_a_new_tab(followed):
    page = followed.get("/").text
    assert 'href="https://www.youtube.com/watch?v=veri0000xxx" target="_blank"' in page
    assert "noopener" in page


def test_the_window_filter(followed):
    assert followed.cards("/?window=10") == 2
    assert 'value="14"' in followed.get("/").text
    assert "selected>Last week" in followed.get("/?window=7").text


def test_an_unusual_window_still_shows_in_the_dropdown(followed):
    assert "Last 45 days" in followed.get("/?window=45").text


def test_a_nonsense_window_is_a_message_not_a_crash(followed):
    response = followed.get("/?window=soon")
    assert response.status_code == 400
    assert "whole number" in response.text


def test_kind_and_tag_filters(site):
    services = site.services()
    services.channels.add("@mkbhd", feeds=[FeedKind.VIDEOS, FeedKind.SHORTS], tags=["tech"])
    services.channels.add("@veritasium", tags=["science"])
    only_shorts = site.get("/?window=3650&filtered=1&kind=shorts").text
    assert "Veritasium" not in only_shorts
    assert "Short" in only_shorts
    assert "Marques" not in site.get("/?window=3650&tag=science").text


def test_unchecking_everything_shows_nothing_and_says_why(followed):
    page = followed.get("/?filtered=1").text
    assert "Nothing unseen here" in page


def test_the_board_notes_thin_history(site):
    site.services().channels.add("@mkbhd")
    assert "15 videos" in site.get("/?window=3650").text


def test_show_more_asks_for_a_larger_list(site):
    services = site.services()
    services.settings.update({"page_size": 6})
    services.channels.add("@mkbhd", feeds=[FeedKind.VIDEOS, FeedKind.SHORTS])
    first = site.get("/?window=3650").text
    assert first.count('class="card"') == 6
    assert "Show more" in first
    assert "show=12" in first
    second = site.get("/?window=3650&show=12").text
    assert second.count('class="card"') == 12
    assert second.index(first.split('data-video="')[1].split('"')[0]) > 0
    assert 'id="cards"' in second


def test_a_silly_number_of_cards_is_handled(site):
    services = site.services()
    services.settings.update({"page_size": 6})
    services.channels.add("@veritasium")
    assert site.get("/?show=-4").status_code == 200
    assert site.get("/?show=lots").status_code == 400


# Seen


def test_marking_seen_without_javascript_returns_to_the_page(followed):
    response = followed.post("/seen/veri0000xxx", headers={"referer": "http://testserver/?window=30"})
    assert response.status_code == 303
    assert response.headers["location"] == "/?window=30"
    assert followed.cards() == 3


def test_a_referer_from_elsewhere_isnt_followed(followed):
    response = followed.post("/seen/veri0000xxx", headers={"referer": "https://evil.example/"})
    assert response.headers["location"] == "/"


def test_a_protocol_relative_referer_isnt_followed(followed):
    """A same-host Referer whose path starts with // would send the browser to another host."""
    response = followed.post("/seen/veri0000xxx", headers={"referer": "http://testserver//evil.example/x"})
    assert response.headers["location"] == "/"
    response = followed.post("/seen/veri0000xxx/undo", headers={"referer": "http://testserver/seen?offset=24"})
    assert response.headers["location"] == "/seen?offset=24"


def test_marking_seen_with_htmx_answers_with_an_undo(followed):
    response = followed.post("/seen/veri0000xxx", headers=HTMX)
    assert response.status_code == 200
    assert "Marked as seen" in response.text
    assert "/seen/veri0000xxx/undo" in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text  # the live region stays, so its new words are announced


def test_undo_brings_it_back(followed):
    followed.post("/seen/veri0000xxx")
    assert followed.post("/seen/veri0000xxx/undo").status_code == 303
    assert followed.cards() == 4


def test_marking_something_unknown_says_so(followed):
    response = followed.post("/seen/nope")
    assert response.status_code == 404
    assert "doesn't know" in unescape(response.text)
    toast = followed.post("/seen/nope", headers=HTMX)
    assert toast.status_code == 200  # htmx won't swap an error status, so the toast would never show
    assert toast.headers["HX-Reswap"] == "none"
    assert 'class="toast"' in toast.text
    assert "doesn't know" in unescape(toast.text)


def test_the_seen_page_lists_and_restores(followed):
    assert "Nothing marked" in followed.get("/seen").text
    followed.post("/seen/veri0000xxx")
    page = followed.get("/seen").text
    assert "Veritasium video 0" in page
    assert "Put back" in page


# Channels


def test_the_add_flow_previews_then_follows(site):
    site.services()
    assert "Find channel" in site.get("/channels/add").text
    preview = site.post("/channels/lookup", data={"link": "https://www.youtube.com/@mkbhd"})
    assert preview.status_code == 200
    assert "Marques Brownlee" in preview.text
    assert "Follow Marques Brownlee" in preview.text
    assert "(not found)" not in preview.text.split("Videos")[0]
    assert site.services().store.subscriptions() == {}
    response = site.post(
        "/channels",
        data={"channel_id": MKBHD, "priority": "must_see", "feeds": ["videos", "shorts"], "tags": "Tech, gadgets"},
    )
    assert response.headers["location"] == f"/channels/{MKBHD}?added=1"
    assert "Now following Marques Brownlee." in unescape(site.get(response.headers["location"]).text)
    assert "Now following" not in site.get(f"/channels/{MKBHD}").text
    subscription = site.services().store.subscriptions()[MKBHD]
    assert subscription.priority is Priority.MUST_SEE
    assert subscription.feeds == {FeedKind.VIDEOS, FeedKind.SHORTS}
    assert subscription.tags == ("tech", "gadgets")


def test_the_preview_marks_feeds_a_channel_doesnt_offer(site):
    site.services()
    assert "Members (not found)" in site.post("/channels/lookup", data={"link": "@veritasium"}).text


def test_a_bad_link_comes_back_with_advice_and_the_text_kept(site):
    site.services()
    response = site.post("/channels/lookup", data={"link": "https://evil.example/x"})
    assert response.status_code == 400
    assert "isn't a YouTube link" in unescape(response.text)
    assert 'value="https://evil.example/x"' in response.text
    assert site.transport.requests == []


def test_looking_up_a_followed_channel_says_so(followed):
    assert "already follow" in followed.post("/channels/lookup", data={"link": "@veritasium"}).text


def test_following_twice_is_a_message(followed):
    response = followed.post("/channels", data={"channel_id": SECOND})
    assert response.status_code == 400
    assert "already follow" in response.text


def test_a_malformed_link_is_a_message_not_a_crash(site):
    site.services()
    response = site.post("/channels/lookup", data={"link": "https://[www.youtube.com/@x"})
    assert response.status_code == 400
    assert "isn't a YouTube link" in unescape(response.text)


def test_a_seen_page_past_the_end_shows_what_is_left(followed):
    """Putting back the last item on a later page said 'nothing marked' while others remained."""
    followed.post("/seen/veri0000xxx")
    page = followed.get("/seen?offset=50").text
    assert "Nothing marked" not in page
    assert "Veritasium video 0" in page


def test_the_channel_list_and_page(followed):
    listing = followed.get("/channels").text
    assert "Veritasium" in listing
    assert "4 unseen" in listing
    page = followed.get(f"/channels/{SECOND}").text
    assert "counts as 28 days newer" in page
    assert "history" in page.lower()


def test_a_channel_you_dont_follow_is_a_404(followed):
    assert followed.get("/channels/UCxxxxxxxxxxxxxxxxxxxxxx").status_code == 404


def test_saving_a_channel(followed):
    response = followed.post(
        f"/channels/{SECOND}", data={"priority": "low", "feeds": ["videos"], "tags": "new", "window": "9"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/channels/{SECOND}?saved=1"
    assert "Changes saved." in followed.get(response.headers["location"]).text
    assert "Changes saved." not in followed.get(f"/channels/{SECOND}").text
    subscription = followed.services().store.subscriptions()[SECOND]
    assert subscription.priority is Priority.LOW
    assert subscription.tags == ("new",)
    assert subscription.window_days == 9


def test_clearing_a_channels_window(followed):
    followed.post(f"/channels/{SECOND}", data={"priority": "high", "feeds": ["videos"], "window": ""})
    assert followed.services().store.subscriptions()[SECOND].window_days is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"window": "soon"}, "whole number"),
        ({"window": "-1"}, "between 1"),
        ({"priority": "urgent"}, "isn't a priority"),
    ],
)
def test_a_bad_channel_save_says_why_and_changes_nothing(followed, data, message):
    response = followed.post(f"/channels/{SECOND}", data={"priority": "low", "feeds": ["videos"], **data})
    assert response.status_code == 400
    assert message in unescape(response.text)
    assert followed.services().store.subscriptions()[SECOND].priority is Priority.HIGH


def test_stopping_following_asks_first(followed):
    page = followed.get(f"/channels/{SECOND}/remove").text
    assert "Stop following Veritasium?" in page
    assert "history is kept" in page
    assert followed.services().store.subscriptions()
    assert followed.post(f"/channels/{SECOND}/remove").status_code == 303
    assert followed.services().store.subscriptions() == {}


# Settings and refreshing


def test_settings_save_and_apply(followed):
    page = followed.get("/settings").text
    assert 'name="head_starts.must_see"' in page
    assert 'value="56"' in page
    response = followed.post("/settings", data={"window_days": "10", "repeat_penalty_days": "0", "appearance": "light"})
    assert response.status_code == 200
    assert "Settings saved" in response.text
    assert 'data-appearance="light"' in response.text
    assert followed.cards() == 2


def test_every_settings_field_on_the_page_is_saved(followed):
    """save_settings copies a list of field names, so a name it misspells is dropped without a word."""
    page = followed.get("/settings").text
    form = page.split('action="/settings"')[1].split("</form>")[0]
    names = set(re.findall(r'name="([^"]+)"', form))
    data = {
        "window_days": "11",
        "appearance": "dark",
        "page_size": "12",
        "repeat_penalty_days": "3",
        "head_starts.must_see": "50",
        "head_starts.high": "20",
        "head_starts.normal": "5",
        "head_starts.low": "1",
    }
    assert names == set(data)
    assert followed.post("/settings", data=data).status_code == 200
    saved = followed.services().settings.get()
    assert (saved.window_days, saved.appearance, saved.page_size, saved.repeat_penalty_days) == (11, "dark", 12, 3)
    assert saved.head_starts.model_dump() == {"must_see": 50, "high": 20, "normal": 5, "low": 1}


def test_bad_settings_say_why(followed):
    response = followed.post("/settings", data={"window_days": "-1"})
    assert response.status_code == 400
    assert "window_days" in response.text
    assert followed.services().settings.get().window_days == 60


def test_a_bad_setting_keeps_what_was_typed_and_marks_the_field(followed):
    """Every value in the submit went back to what was saved, and the message used the internal name."""
    data = {"window_days": "abc", "page_size": "12", "head_starts.high": "20", "appearance": "light"}
    response = followed.post("/settings", data=data)
    assert response.status_code == 400
    assert "Default window (window_days) has to be a whole number" in unescape(response.text)
    fields = described(response.text)
    assert fields["window_days"]["value"] == "abc"
    assert fields["page_size"]["value"] == "12"
    assert fields["hs-high"]["value"] == "20"
    assert '<option value="light" selected>' in response.text
    assert fields["window_days"].get("aria-invalid") == "true"
    assert "settings-problem" in fields["window_days"]["aria-describedby"].split()
    assert "aria-invalid" not in fields["page_size"]
    saved = followed.services().settings.get()
    assert (saved.window_days, saved.page_size, saved.head_starts.high, saved.appearance) == (60, 48, 28, "system")


def test_refresh_finds_new_videos(followed):
    followed.clock.advance(hours=2)
    followed.runtime.feeds._cache.clear()
    followed.transport.serve_feed(
        SECOND, FeedKind.VIDEOS, feed_xml(SECOND, "Veritasium", [("fresh000xxx", "Fresh one", NOW)])
    )
    response = followed.post("/refresh", headers=HTMX)
    assert "Checked 1 feed, 1 new" in response.text
    assert "Fresh one" in followed.get("/").text


def test_refresh_without_javascript_returns_to_the_page(followed):
    assert followed.post("/refresh").status_code == 303


def test_checking_from_settings_says_what_it_found(followed):
    """The Settings button threw the summary away, with or without JavaScript."""
    page = followed.get("/settings").text
    button = re.search(r'<form[^>]*action="/refresh"[^>]*>', page).group()
    assert 'hx-post="/refresh"' in button
    assert 'hx-swap="none"' in button
    followed.clock.advance(hours=2)
    followed.runtime.feeds._cache.clear()
    response = followed.post("/refresh", headers={"referer": "http://testserver/settings"})
    assert response.status_code == 200
    assert "Checked 1 feed" in response.text
    assert "<h1>Settings</h1>" in response.text


def test_settings_show_refresh_status(followed):
    page = followed.get("/settings").text
    assert "1 channel across every profile" in page
    assert "1 feed working" in page


def test_backup_from_the_page(followed):
    assert "Backed up to" in followed.post("/backup").text
    assert list(followed.runtime.paths.backups.glob("*/catalogue.db"))


# OPML


def test_export_downloads_opml(followed):
    response = followed.get("/export.opml")
    assert "attachment" in response.headers["content-disposition"]
    assert "Veritasium" in response.text
    assert "science" in response.text


def wait_for(job_page):
    import time

    for _ in range(200):
        page = job_page()
        if "Go to your board" in page or "Try another file" in page:
            return page
        time.sleep(0.01)
    raise AssertionError("the import never finished")


def test_import_runs_in_the_background_and_reports(followed):
    data = followed.get("/export.opml").content
    followed.services("nat")
    followed.post("/profiles/use", data={"name": "nat"})
    response = followed.post("/import", files={"file": ("list.opml", data)})
    assert response.status_code == 303
    page = wait_for(lambda: followed.get(response.headers["location"]).text)
    assert "Added 1 channel" in page
    assert SECOND in followed.services("nat").store.subscriptions()


def test_importing_a_bad_file_says_why(followed):
    response = followed.post("/import", files={"file": ("x.opml", b"not opml")})
    page = wait_for(lambda: followed.get(response.headers["location"]).text)
    assert "isn't readable OPML" in unescape(page)


def test_an_empty_upload_is_refused(followed):
    assert followed.post("/import", files={"file": ("x.opml", b"")}).status_code == 400


def test_an_enormous_upload_is_refused(followed):
    assert followed.post("/import", files={"file": ("x.opml", b"x" * (3 * 1024 * 1024))}).status_code == 400


def test_someone_elses_import_cant_be_watched(followed):
    response = followed.post("/import", files={"file": ("x.opml", followed.get("/export.opml").content)})
    location = response.headers["location"]
    followed.services("nat")
    followed.client.post("/profiles/use", data={"name": "nat"})
    assert followed.get(location).status_code == 404


def test_an_unknown_import_is_a_404(followed):
    assert followed.get("/import/nope").status_code == 404


# Untrusted text and hostile requests


NASTY = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


def test_feed_text_cant_inject_anything(site):
    from html import escape

    site.transport.serve_feed(
        SECOND, FeedKind.VIDEOS, feed_xml(SECOND, escape(NASTY), [("nasty000xxx", escape(NASTY), NOW)])
    )
    services = site.services()
    services.channels.add("@veritasium")
    services.channels.set(SECOND, tags=['"><script>x</script>'])
    for path in ("/", "/channels", f"/channels/{SECOND}", f"/channels/{SECOND}/remove"):
        page = site.get(path).text
        assert "<script>alert" not in page, path
        assert "<img src=x" not in page, path
        assert "<script>x" not in page, path
    services.seen.mark("nasty000xxx")
    assert "<script>alert" not in site.get("/seen").text


def test_every_page_sends_a_strict_policy(followed):
    headers = followed.get("/").headers
    policy = headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy
    assert "unsafe" not in policy
    assert headers["x-content-type-options"] == "nosniff"


def test_a_form_posted_from_another_site_is_refused(followed):
    response = followed.post("/seen/veri0000xxx", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert followed.cards() == 4


def test_a_cross_site_post_with_no_origin_is_refused(followed):
    response = followed.post("/seen/veri0000xxx", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403


def test_a_form_from_this_site_is_fine(followed):
    assert followed.post("/seen/veri0000xxx", headers={"origin": "http://testserver"}).status_code == 303


def test_an_unknown_host_is_refused_when_listening_locally(tmp_path):
    site = Site(tmp_path, allowed_hosts=frozenset({"localhost"}))
    assert site.client.get("/", headers={"host": "evil.example"}).status_code == 421
    assert site.client.get("/", headers={"host": "localhost:8770"}).status_code == 303
    site.runtime.close()


def test_the_listening_address_decides_the_allowed_hosts(monkeypatch):
    from uppyt.cli.main import _allowed_hosts

    monkeypatch.delenv("UPPYT_ALLOWED_HOSTS", raising=False)
    assert "localhost" in _allowed_hosts("127.0.0.1")
    assert _allowed_hosts("0.0.0.0") is None
    monkeypatch.setenv("UPPYT_ALLOWED_HOSTS", "yt.example.com")
    assert "yt.example.com" in _allowed_hosts("0.0.0.0")


def test_avatars_cant_escape_their_folder(followed):
    assert followed.get(f"/avatars/{SECOND}.jpg").status_code == 200
    assert followed.get("/avatars/..%2f..%2fsecret.jpg").status_code == 404
    assert followed.get("/avatars/UCxxxxxxxxxxxxxxxxxxxxxx.jpg").status_code == 404


def test_a_channel_with_no_avatar_gets_its_initial(site):
    for url in list(site.transport.routes):
        if "googleusercontent" in url:
            del site.transport.routes[url]
    site.services().channels.add("@veritasium")
    assert "avatar--blank" in site.get("/").text


def test_the_stylesheet_is_named_after_its_contents(followed):
    page = followed.get("/").text
    url = page.split('rel="stylesheet" href="')[1].split('"')[0]
    response = followed.get(url)
    assert "immutable" in response.headers["cache-control"]
    assert "@layer tokens, base, composition, block, utility, exception" in response.text


def test_unknown_pages_are_friendly(followed):
    response = followed.get("/nope")
    assert response.status_code == 404
    assert "nothing at that address" in response.text


def test_pages_are_compressed(followed):
    response = followed.get("/", headers={"accept-encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"


# Accessibility of forms


class _Fields(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.ids: set[str] = set()
        self.fields: dict[str, dict[str, str | None]] = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        if found.get("id"):
            self.ids.add(found["id"])
        if tag in ("input", "select", "textarea") and found.get("id"):
            self.fields[found["id"]] = found


def described(html: str) -> dict[str, dict[str, str | None]]:
    """Each field by id, after checking every aria-describedby points at something on the page."""
    page = _Fields(html)
    for field in page.fields.values():
        for target in (field.get("aria-describedby") or "").split():
            assert target in page.ids, f"{field['id']} is described by {target}, which isn't on the page"
    return page.fields


def test_hints_are_tied_to_their_fields(followed):
    channel = described(followed.get(f"/channels/{SECOND}").text)
    for name in ("priority", "tags", "window"):
        assert channel[name].get("aria-describedby"), name
    settings = described(followed.get("/settings").text)
    for name in ("window_days", "repeat_penalty_days"):
        assert settings[name].get("aria-describedby"), name
    add = described(followed.get("/channels/add").text)
    assert add["link"].get("aria-describedby")
    assert "aria-invalid" not in add["link"]
    preview = described(followed.post("/channels/lookup", data={"link": "@mkbhd"}).text)
    assert preview["tags"].get("aria-describedby")


def test_a_single_field_problem_marks_the_field(site):
    welcome = described(site.post("/profiles", data={"name": "../evil"}).text)
    assert welcome["name"].get("aria-invalid") == "true"
    assert len(welcome["name"]["aria-describedby"].split()) == 1
    site.services()
    profiles = described(site.post("/profiles", data={"name": "../evil"}).text)
    assert profiles["name"].get("aria-invalid") == "true"
    add = described(site.post("/channels/lookup", data={"link": "https://evil.example/x"}).text)
    assert add["link"].get("aria-invalid") == "true"
    assert len(add["link"]["aria-describedby"].split()) == 2  # the hint and the problem
