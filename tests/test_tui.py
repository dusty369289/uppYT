"""The terminal app, driven by real key presses through Textual's headless test driver."""

from __future__ import annotations

import asyncio
import json

import pytest
from textual.widgets import Checkbox, DataTable, Input, Select, Static
from textual.worker import WorkerCancelled, WorkerFailed

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


async def workers_done(app) -> None:
    """A worker that closes its own screen gets cancelled as it does, which is fine. One that fails isn't."""
    answers = await asyncio.gather(*(worker.wait() for worker in app.workers), return_exceptions=True)
    for answer in answers:
        if isinstance(answer, Exception) and not isinstance(answer, WorkerCancelled):
            raise answer


async def settle(app, pilot) -> None:
    await pilot.pause()
    await workers_done(app)
    await pilot.pause()


async def test_waiting_for_workers_ignores_cancelled_ones_but_not_failed_ones(scene):
    async def forever():
        await asyncio.sleep(60)

    async def broken():
        raise ValueError("broken")

    app = scene.app()
    async with app.run_test() as pilot:
        worker = app.run_worker(forever(), exit_on_error=False)
        await pilot.pause()
        worker.cancel()
        await workers_done(app)
        app.run_worker(broken(), exit_on_error=False)
        with pytest.raises(WorkerFailed):
            await workers_done(app)


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
        assert "Default window has to be" in str(app.screen.query_one("#problem", Static).render())
        assert "(window_days)" not in str(app.screen.query_one("#problem", Static).render())


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
        assert any("1 feed working" in str(n.message) for n in app._notifications)
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
    marked = {entry.video_id: NOW.isoformat() for entry in feed.entries}
    (services.store.folder / "seen.json").write_text(json.dumps(marked))  # quicker than 600 marks
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert len(rows(app, "#seen")) == 600


# Dialogs start on their first real control, so typing works without clicking first.


async def test_typing_a_first_profile_name_works_straight_away(scene):
    app = scene.app(profile=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(*"alex", "enter")
        await settle(app, pilot)
        assert scene.runtime.profile_names() == ["alex"]
        assert "alex" in status(app)


async def test_typing_a_link_after_a_works_straight_away(scene):
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("a", *"@mkbhd", "enter")
        await settle(app, pilot)
        assert "Marques Brownlee" in str(app.screen.query_one("#found", Static).render())


async def test_switching_profile_by_keyboard_alone(scene):
    scene.services("dusty")
    scene.services("nat").channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        names = [n[0] for n in rows(app, "#profiles")]
        await pilot.press(*(["down"] * names.index("nat")), "enter")
        await settle(app, pilot)
        assert "nat" in status(app)


@pytest.mark.parametrize(
    ("key", "screen", "first"),
    [
        ("slash", FilterScreen, "window"),
        ("comma", SettingsScreen, "set-window_days"),
        ("i", TransferScreen, "import-path"),
    ],
)
async def test_each_dialog_starts_on_its_first_control(scene, key, screen, first):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, screen)
        assert app.focused is not None and app.focused.id == first


async def test_the_edit_dialog_starts_on_priority_and_confirm_on_cancel(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused.id == "priority"
        await pilot.click("#remove")
        await pilot.pause()
        assert app.focused.id == "no"
        await pilot.press("enter")  # a stray Enter cancels, it doesn't stop following
        await settle(app, pilot)
        assert scene.services().store.subscriptions()


# Errors meant for people are shown, not crashes.


def notified(app) -> list[str]:
    return [str(n.message) for n in app._notifications]


async def test_a_channel_removed_elsewhere_is_reported_not_a_crash(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        scene.services().channels.remove(SECOND)  # as the web app would
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running
        assert isinstance(app.screen, ChannelsScreen)
        assert "You don't follow Veritasium." in notified(app)  # its name, and no command to type
        assert not any("uppyt channels" in m for m in notified(app))
        assert rows(app, "#channels") == []


async def test_a_broken_seen_file_is_reported_not_a_crash(scene):
    services = scene.services()
    services.channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        (services.store.folder / "seen.json").write_text("{broken", encoding="utf-8")
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        assert app.is_running
        assert len([m for m in notified(app) if "seen.json" in m]) == 2


async def test_broken_settings_are_reported_not_a_crash(scene):
    services = scene.services()
    app = scene.app()
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        (services.store.folder / "settings.toml").write_text("window_days = [", encoding="utf-8")
        await pilot.press("comma")
        await pilot.pause()
        assert app.is_running
        assert not isinstance(app.screen, SettingsScreen)
        assert any("settings.toml" in m for m in notified(app))


async def test_broken_settings_at_launch_are_reported_not_a_crash(scene):
    services = scene.services()
    services.channels.add("@veritasium")
    (services.store.folder / "settings.toml").write_text("window_days = [", encoding="utf-8")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running
        assert any("settings.toml" in m for m in notified(app))


async def test_a_failed_manual_refresh_is_reported_not_a_crash(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()

        def broken(**_):
            raise RuntimeError("database is locked")

        app.services.refresh.run = broken
        await pilot.press("r")
        await settle(app, pilot)
        assert app.is_running
        assert any("Refresh failed" in m and "database is locked" in m for m in notified(app))


async def test_the_profile_dialog_can_be_cancelled(scene):
    scene.services().channels.add("@veritasium")
    scene.services("nat")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        services = app.services
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ProfileScreen)
        assert app.services is services
        assert len(rows(app)) == 4


async def test_the_first_profile_dialog_cant_be_cancelled(scene):
    app = scene.app(profile=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ProfileScreen)


async def test_no_browser_shows_the_link_instead(scene):
    scene.services().channels.add("@veritasium")
    app = scene.app()
    app.opener = lambda url: False
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert any("https://www.youtube.com/watch?v=veri0000xxx" in m for m in notified(app))


@pytest.mark.parametrize("tag", ["[/oops]", "[red]hi"])
async def test_a_tag_with_markup_stays_literal_in_the_filter(scene, tag):
    scene.services().channels.add("@veritasium", tags=[tag])
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        select = app.screen.query_one("#tag", Select)
        select.focus()
        await pilot.press("enter", "down", "enter")
        await pilot.pause()
        assert app.is_running
        assert select.value == tag
        assert tag in str(select.query_one("SelectCurrent Static").render())


async def test_a_bad_hand_edit_to_channels_is_a_message_not_a_crash(scene, tmp_path):
    out = tmp_path / "list.opml"
    scene.services().channels.add("@veritasium")
    path = scene.runtime.profile("dusty").folder / "channels.toml"
    path.write_text(f'[channels]\n{MKBHD} = "high"\n')
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await settle(app, pilot)
        notes = str(app.query_one("#notes", Static).render())
        assert MKBHD in notes and "channels.toml" in notes
        assert rows(app) == []
        for key in ("c", "slash", "a", "r"):
            await pilot.press(key)
            await settle(app, pilot)
            assert len(app.screen_stack) == 1, key
        assert any(MKBHD in str(n.message) for n in app._notifications)
        assert MKBHD in str(app.query_one("#notes", Static).render())
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one("#export-path", Input).value = str(out)
        await pilot.click("#export-path")
        await pilot.press("enter")
        await pilot.pause()
        assert MKBHD in str(app.screen.query_one("#result", Static).render())
        assert not out.exists()
    assert path.read_text() == f'[channels]\n{MKBHD} = "high"\n'


async def test_choosing_a_profile_with_a_bad_hand_edit_says_why(scene):
    scene.services("nat")
    (scene.runtime.profile("nat").folder / "channels.toml").write_text(f"[channels.{MKBHD}]\nwindow_days = true\n")
    app = scene.app(profile=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#profiles", DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert "nat" in status(app)
        assert f"{MKBHD}: window_days" in str(app.query_one("#notes", Static).render())


async def test_channels_and_seen_switch_in_place_so_one_escape_gets_back(scene):
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        board = app.screen
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ChannelsScreen)
        for key, screen in (("v", SeenScreen), ("c", ChannelsScreen), ("v", SeenScreen), ("v", SeenScreen)):
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, screen)
            assert len(app.screen_stack) == 2
        await pilot.press("c", "c")
        await pilot.pause()
        assert isinstance(app.screen, ChannelsScreen)
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is board


async def test_fixing_channels_by_hand_is_picked_up_without_pressing_r(scene):
    app = scene.app()
    path = scene.runtime.profile("dusty").folder / "channels.toml"
    path.write_text(f'[channels]\n{MKBHD} = "high"\n')
    async with app.run_test(size=(100, 50)) as pilot:
        await settle(app, pilot)
        await pilot.press("c")
        await settle(app, pilot)
        assert len(app.screen_stack) == 1
        path.write_text(f'[channels.{MKBHD}]\npriority = "high"\n')
        for key, screen in (("c", ChannelsScreen), ("slash", FilterScreen), ("a", AddChannelScreen)):
            await pilot.press(key)
            await settle(app, pilot)
            assert isinstance(app.screen, screen), key
            await pilot.press("escape")
            await settle(app, pilot)
            assert len(app.screen_stack) == 1
        assert app.profile_problem is None
        assert "can't be shown" not in status(app)


@pytest.mark.parametrize("under", ["board", "c", "v"])
async def test_c_and_v_do_nothing_while_a_dialog_is_open(scene, under):
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        if under != "board":
            await pilot.press(under)
            await pilot.pause()
        depth = len(app.screen_stack)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        for key in ("c", "v", "c", "v"):
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen), key
            assert len(app.screen_stack) == depth + 1, key
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == depth


async def test_the_channels_and_seen_actions_never_stack_on_a_dialog(scene):
    """Textual already keeps app keys away from a dialog. The actions refuse too, whatever calls them."""
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c", "question_mark")
        await pilot.pause()
        app.action_seen_list()
        app.action_channels()
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert len(app.screen_stack) == 3


@pytest.mark.parametrize(("key", "screen"), [("c", ChannelsScreen), ("v", SeenScreen)])
async def test_switching_profile_from_a_list_goes_back_to_the_board(scene, key, screen):
    """A list left open would still show the old profile while acting on the new one."""
    scene.services("dusty").channels.add("@veritasium")
    scene.services("nat")
    app = scene.app()
    async with app.run_test(size=(100, 50)) as pilot:
        await settle(app, pilot)
        board = app.screen
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, screen)
        await pilot.press("p")
        await pilot.pause()
        names = [n[0] for n in rows(app, "#profiles")]
        await pilot.press(*(["down"] * names.index("nat")), "enter")
        await settle(app, pilot)
        assert app.screen is board
        assert len(app.screen_stack) == 1
        assert "nat" in status(app)


async def test_putting_back_a_video_that_is_no_longer_seen_says_so(scene):
    services = scene.services()
    services.channels.add("@veritasium")
    services.seen.mark("veri0000xxx")
    app = scene.app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        services.seen.undo("veri0000xxx")  # put back elsewhere, say from the web app
        await pilot.press("u")
        await pilot.pause()
        assert any("wasn't marked seen" in m for m in notified(app))
        assert rows(app, "#seen") == []
