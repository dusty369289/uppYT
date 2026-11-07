"""The terminal app, driven by real key presses through Textual's headless test driver."""

from __future__ import annotations

import pytest
from textual.widgets import Checkbox, DataTable, Input, Select, Static

from tests.support import MKBHD, NOW, SECOND, FixedClock, entries, feed_xml, no_sleep, world
from uppyt.domain.feeds import FeedKind
from uppyt.domain.models import Priority
from uppyt.services.app import UppYT
from uppyt.services.runtime import Paths, Runtime
from uppyt.tui.app import (
    AddChannelScreen,
    ChannelEditScreen,
    ChannelsScreen,
    FilterScreen,
    HelpScreen,
    ProfileScreen,
    SeenScreen,
    SettingsScreen,
    TransferScreen,
    UppytApp,
)


class Scene:
    def __init__(self, tmp_path):
        self.clock = FixedClock()
        self.transport = world()
        self.runtime = Runtime(Paths(tmp_path), clock=self.clock, transport=self.transport, sleep=no_sleep)
        self.opened: list[str] = []

    def app(self, *, profile: str | None = "dusty") -> UppytApp:
        store = None
        if profile:
            if profile not in self.runtime.profile_names():
                self.runtime.create_profile(profile)
            store = self.runtime.profile(profile)
        return UppytApp(self.runtime, store, opener=self.opened.append, background_refresh=False)

    def services(self, profile: str = "dusty") -> UppYT:
        if profile not in self.runtime.profile_names():
            self.runtime.create_profile(profile)
        return UppYT(self.runtime, self.runtime.profile(profile))


@pytest.fixture
def scene(tmp_path):
    made = Scene(tmp_path)
    yield made
    made.runtime.close()


def rows(app, table="#board") -> list[list[str]]:
    data = app.screen.query_one(table, DataTable)
    return [[str(cell) for cell in data.get_row_at(i)] for i in range(data.row_count)]


def status(app) -> str:
    return str(app.query_one("#status", Static).render())


async def settle(app, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_a_blank_slate_says_how_to_begin(scene):
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "No channels yet. Press a" in status(app)
        assert rows(app) == []


async def test_no_profiles_asks_for_one_then_opens_it(scene):
    app = scene.app(profile=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        await pilot.click("#new-profile")
        await pilot.press(*"dusty", "enter")
        await settle(app, pilot)
        assert not isinstance(app.screen, ProfileScreen)
        assert scene.runtime.profile_names() == ["dusty"]
        assert "dusty" in status(app)


async def test_a_bad_profile_name_says_why(scene):
    app = scene.app(profile=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#new-profile")
        await pilot.press("full_stop", "full_stop", "enter")
        await pilot.pause()
        assert "letters, numbers" in str(app.screen.query_one("#problem", Static).render())


async def test_choosing_an_existing_profile(scene):
    scene.services("nat").channels.add("@veritasium")
    scene.services("dusty")
    app = scene.app(profile=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.screen.query_one("#profiles", DataTable)
        table.focus()
        await pilot.press("down", "enter") if rows(app, "#profiles")[0][0] == "dusty" else await pilot.press("enter")
        await settle(app, pilot)
        assert "nat" in status(app)
        assert len(rows(app)) == 4


async def test_the_board_shows_dense_ranked_rows(scene):
    services = scene.services()
    services.channels.add("@veritasium", priority=Priority.MUST_SEE)
    services.channels.add("@mkbhd", priority=Priority.LOW)
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        first = rows(app)[0]
        assert first[0] == "Must-see"
        assert first[1] == "Veritasium"
        assert first[4] == "Veritasium video 0"
        assert "unseen" in status(app)


async def test_marking_seen_and_undoing_with_keys(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(rows(app)) == 4
        await pilot.press("s")
        await pilot.pause()
        assert len(rows(app)) == 3
        assert "Veritasium video 0" not in [r[4] for r in rows(app)]
        await pilot.press("u")
        await pilot.pause()
        assert len(rows(app)) == 4


async def test_the_cursor_stays_put_after_marking_seen(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("j", "j", "s")
        await pilot.pause()
        assert app.query_one("#board", DataTable).cursor_row == 2


async def test_opening_a_video_leaves_it_on_the_board(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert scene.opened == ["https://www.youtube.com/watch?v=veri0000xxx"]
        assert len(rows(app)) == 4
        await pilot.press("enter")
        await pilot.pause()
        assert len(scene.opened) == 2


async def test_adding_a_channel_from_a_link(scene):
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, AddChannelScreen)
        await pilot.click("#link")
        for key in "@mkbhd":
            await pilot.press(key)
        await pilot.press("enter")
        await settle(app, pilot)
        assert "Marques Brownlee" in str(app.screen.query_one("#found", Static).render())
        app.screen.query_one("#priority", Select).value = Priority.HIGH
        app.screen.query_one("#feed-shorts", Checkbox).value = True
        app.screen.query_one("#tags", Input).value = "tech, gadgets"
        await pilot.pause()
        await pilot.click("#follow")
        await settle(app, pilot)
        assert not isinstance(app.screen, AddChannelScreen)
        subscription = scene.services().store.subscriptions()[MKBHD]
        assert subscription.priority is Priority.HIGH
        assert FeedKind.SHORTS in subscription.feeds
        assert subscription.tags == ("tech", "gadgets")
        assert rows(app)


async def test_a_bad_link_says_why_and_stays_open(scene):
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#link", Input).value = "https://evil.example/x"
        await pilot.click("#link")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "isn't a YouTube link" in str(app.screen.query_one("#problem", Static).render())
        assert isinstance(app.screen, AddChannelScreen)


async def test_a_channel_already_followed_says_so(scene):
    scene.services().channels.add("@mkbhd")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#link", Input).value = "@mkbhd"
        await pilot.click("#link")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "already follow" in str(app.screen.query_one("#problem", Static).render())


async def test_feeds_a_channel_doesnt_offer_are_labelled(scene):
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#link", Input).value = "@veritasium"
        await pilot.click("#link")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "not found" in str(app.screen.query_one("#feed-shorts", Checkbox).label)
        assert not app.screen.query_one("#feed-live", Checkbox).value


async def test_changing_a_channel(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ChannelsScreen)
        assert rows(app, "#channels")[0][3] == "Veritasium"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChannelEditScreen)
        app.screen.query_one("#priority", Select).value = Priority.MUST_SEE
        app.screen.query_one("#tags", Input).value = "science"
        app.screen.query_one("#window", Input).value = "9"
        await pilot.pause()
        await pilot.click("#save")
        await settle(app, pilot)
        subscription = scene.services().store.subscriptions()[SECOND]
        assert subscription.priority is Priority.MUST_SEE
        assert subscription.tags == ("science",)
        assert subscription.window_days == 9
        assert rows(app, "#channels")[0][0] == "Must-see"


async def test_a_bad_window_says_why(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#window", Input).value = "soon"
        await pilot.click("#save")
        await pilot.pause()
        assert "whole number" in str(app.screen.query_one("#problem", Static).render())


async def test_stopping_following_asks_first(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#remove")
        await pilot.pause()
        assert scene.services().store.subscriptions()
        await pilot.click("#yes")
        await settle(app, pilot)
        assert scene.services().store.subscriptions() == {}


async def test_filtering_the_board(scene):
    services = scene.services()
    services.channels.add("@veritasium", tags=["science"])
    services.channels.add("@mkbhd", tags=["tech"])
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert isinstance(app.screen, FilterScreen)
        app.screen.query_one("#tag", Select).value = "science"
        app.screen.query_one("#window", Select).value = 14
        await pilot.pause()
        await pilot.click("#apply")
        await pilot.pause()
        assert {r[1] for r in rows(app)} == {"Veritasium"}
        assert len(rows(app)) == 3
        assert "tag science" in status(app)


async def test_kind_filter(scene):
    scene.services().channels.add("@mkbhd", feeds=[FeedKind.VIDEOS, FeedKind.SHORTS])
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        app.screen.query_one("#window", Select).value = 3650
        app.screen.query_one("#kind-videos", Checkbox).value = False
        await pilot.pause()
        await pilot.click("#apply")
        await pilot.pause()
        assert {r[3] for r in rows(app)} == {"Short"}


async def test_the_seen_list_can_put_videos_back(scene):
    services = scene.services()
    services.channels.add("@veritasium")
    services.seen.mark("veri0000xxx")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(rows(app)) == 3
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, SeenScreen)
        assert rows(app, "#seen")[0][2] == "Veritasium video 0"
        await pilot.press("u")
        await pilot.pause()
        assert rows(app, "#seen") == []
        await pilot.press("escape")
        await pilot.pause()
        assert len(rows(app)) == 4


async def test_settings_change_the_board(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        app.screen.query_one("#set-repeat_penalty_days", Input).value = "3"
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause()
        assert scene.services().settings.get().repeat_penalty_days == 3


async def test_bad_settings_say_why(scene):
    app = scene.app()
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.pause()
        app.screen.query_one("#set-window_days", Input).value = "-5"
        await pilot.click("#save")
        await pilot.pause()
        assert "window_days" in str(app.screen.query_one("#problem", Static).render())


async def test_import_export_and_backup(scene, tmp_path):
    services = scene.services()
    services.channels.add("@veritasium")
    out = tmp_path / "list.opml"
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, TransferScreen)
        app.screen.query_one("#export-path", Input).value = str(out)
        await pilot.click("#export-path")
        await pilot.press("enter")
        await pilot.pause()
        assert "Veritasium" in out.read_text()
        await pilot.click("#backup")
        await pilot.pause()
        assert "Backed up" in str(app.screen.query_one("#result", Static).render())

    nat = scene.app(profile="nat")
    async with nat.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        nat.screen.query_one("#import-path", Input).value = str(out)
        await pilot.click("#import-path")
        await pilot.press("enter")
        await settle(nat, pilot)
        assert "Added 1 channel" in str(nat.screen.query_one("#result", Static).render())
        assert scene.services("nat").store.subscriptions()


async def test_a_missing_import_file_says_so(scene, tmp_path):
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one("#import-path", Input).value = str(tmp_path / "nope")
        await pilot.click("#import-path")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "Couldn't read" in str(app.screen.query_one("#result", Static).render())


async def test_refresh_key_finds_new_videos(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scene.clock.advance(hours=2)
        scene.transport.serve_feed(
            SECOND, FeedKind.VIDEOS, feed_xml(SECOND, "Veritasium", [("fresh000xxx", "Fresh", NOW)])
        )
        scene.runtime.feeds._cache.clear()
        await pilot.press("r")
        await settle(app, pilot)
        assert "Fresh" in [r[4] for r in rows(app)]


async def test_status_and_help_open(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert any("feeds working" in str(n.message) for n in app._notifications)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_a_title_with_markup_stays_literal(scene):
    """Feed text comes from strangers. It must not be able to style or break the app."""
    scene.transport.serve_feed(
        SECOND,
        FeedKind.VIDEOS,
        feed_xml(SECOND, "Veritasium", [("markup00xxx", "[bold red]Not bold[/] [link=x]", NOW)]),
    )
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert rows(app)[0][4] == "[bold red]Not bold[/] [link=x]"


async def test_keys_dont_reach_the_board_from_a_dialog(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.click("#link")
        await pilot.press("s")  # typing, not marking seen
        await pilot.pause()
        assert scene.services().store.seen() == {}
        assert app.screen.query_one("#link", Input).value == "s"


async def test_the_board_notes_thin_history(scene):
    scene.services().channels.add("@mkbhd")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.window_days = 3650
        app.reload_board()
        await pilot.pause()
        assert "15 videos" in str(app.query_one("#notes", Static).render())


# Found by an independent review. Each failed before its fix.


async def test_a_tag_from_another_profile_cant_crash_the_filter(scene):
    services = scene.services()
    services.channels.add("@veritasium", tags=["science"])
    scene.services("nat")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        app.tag = "science"
        app.reload_board()
        await pilot.press("p")
        await pilot.pause()
        app.screen.query_one("#profiles", DataTable).focus()
        names = rows(app, "#profiles")
        await pilot.press(*(["down"] * [n[0] for n in names].index("nat")), "enter")
        await settle(app, pilot)
        assert "nat" in status(app)
        assert app.tag is None
        await pilot.press("slash")
        await pilot.pause()
        assert isinstance(app.screen, FilterScreen)


async def test_a_tag_nobody_uses_any_more_cant_crash_the_filter(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        app.tag = "gone"
        await pilot.press("slash")
        await pilot.pause()
        assert isinstance(app.screen, FilterScreen)


async def test_changing_the_default_window_applies_straight_away(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        assert len(rows(app)) == 4
        await pilot.press("comma")
        await pilot.pause()
        app.screen.query_one("#set-window_days", Input).value = "10"
        await pilot.click("#save")
        await pilot.pause()
        assert len(rows(app)) == 2


async def test_a_new_lookup_clears_the_last_ones_answers(scene):
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#link", Input).value = "@veritasium"
        await pilot.click("#link")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "not found" in str(app.screen.query_one("#feed-live", Checkbox).label)
        app.screen.query_one("#link", Input).value = "@mkbhd"
        await pilot.click("#link")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "not found" not in str(app.screen.query_one("#feed-live", Checkbox).label)
        assert app.screen.query_one("#feed-live", Checkbox).value


async def test_a_failed_lookup_cant_follow_the_previous_channel(scene):
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        app.screen.query_one("#link", Input).value = "@veritasium"
        await pilot.click("#link")
        await pilot.press("enter")
        await settle(app, pilot)
        app.screen.query_one("#link", Input).value = "@nobody"
        await pilot.click("#link")
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.screen.preview is None
        assert not app.screen.query_one("#choices").display


async def test_the_edit_screen_shows_a_problem_on_an_enabled_feed(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "Not found" in str(app.screen.query_one("#feed-live", Checkbox).label)


async def test_the_seen_list_isnt_cut_off(scene):
    services = scene.services()
    services.channels.add("@veritasium")
    feed = entries(SECOND, 600)
    scene.runtime.catalogue.record_entries(SECOND, FeedKind.VIDEOS, feed.entries, NOW)
    services.store._write_seen({entry.video_id: NOW for entry in feed.entries})
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert len(rows(app, "#seen")) == 600
