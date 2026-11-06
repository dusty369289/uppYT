from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tests.support import MKBHD, SECOND, FixedClock, world
from uppyt.cli.main import Session, app
from uppyt.domain.feeds import FeedKind
from uppyt.domain.models import Priority
from uppyt.services.runtime import Paths, Runtime


class Cli:
    def __init__(self, tmp_path):
        self.clock = FixedClock()
        self.transport = world()
        self.runtime = Runtime(Paths(tmp_path), clock=self.clock, transport=self.transport)
        self.runner = CliRunner()

    def __call__(self, *args: str, code: int = 0):
        result = self.runner.invoke(app, list(args), obj=Session(self.runtime, owned=False))
        assert result.exit_code == code, result.output
        return result.output

    def store(self, name="dusty"):
        return self.runtime.profile(name)


@pytest.fixture
def cli(tmp_path):
    made = Cli(tmp_path)
    made("profiles", "add", "dusty")
    yield made
    made.runtime.close()


def test_a_fresh_install_says_how_to_begin(tmp_path):
    cli = Cli(tmp_path)
    assert "profiles add" in cli("profiles", "ls")
    assert "profiles add" in cli("board", code=1)
    cli.runtime.close()


def test_version(cli):
    assert "uppYT 0.1.0" in cli("--version")


def test_adding_a_channel_with_every_choice(cli):
    output = cli(
        "channels",
        "add",
        "@mkbhd",
        "--priority",
        "must-see",
        "--feeds",
        "videos,shorts",
        "--tags",
        "Tech,Long",
        "--window",
        "90",
    )
    assert "Following Marques Brownlee" in output
    subscription = cli.store().subscriptions()[MKBHD]
    assert subscription.priority is Priority.MUST_SEE
    assert subscription.feeds == {FeedKind.VIDEOS, FeedKind.SHORTS}
    assert subscription.tags == ("tech", "long")
    assert subscription.window_days == 90


def test_lookup_maps_every_feed_without_following(cli):
    output = cli("channels", "lookup", "@mkbhd")
    assert "Marques Brownlee" in output
    assert "videos" in output
    assert "popular_live" in output
    assert cli.store().subscriptions() == {}


def test_lookup_as_json(cli):
    data = json.loads(cli("channels", "lookup", "@mkbhd", "--json"))
    assert data["available"]["shorts"] is True
    assert data["available"]["popular_live"] is False


def test_changing_a_channel(cli):
    cli("channels", "add", "@mkbhd")
    cli("channels", "set", "@mkbhd", "--priority", "low", "--tags", "x")
    subscription = cli.store().subscriptions()[MKBHD]
    assert subscription.priority is Priority.LOW
    assert subscription.tags == ("x",)
    cli("channels", "set", "@mkbhd", "--window", "9")
    assert cli.store().subscriptions()[MKBHD].window_days == 9
    cli("channels", "set", "@mkbhd", "--follow-board-window")
    assert cli.store().subscriptions()[MKBHD].window_days is None


def test_listing_and_showing_channels(cli):
    cli("channels", "add", "@mkbhd", "--priority", "high")
    assert "Marques Brownlee" in cli("channels", "ls")
    detail = cli("channels", "show", "@mkbhd")
    assert "Priority: High, counts as 28 days newer" in detail
    assert "[x] Videos" in detail
    assert "[ ] Shorts" in detail
    assert "history starts" in detail
    assert json.loads(cli("channels", "ls", "--json"))[0]["channel"]["id"] == MKBHD
    assert json.loads(cli("channels", "show", "@mkbhd", "--json"))["subscription"]["priority"] == "high"


def test_removing_a_channel(cli):
    cli("channels", "add", "@mkbhd")
    assert "history is kept" in cli("channels", "rm", "@mkbhd")
    assert "No channels yet" in cli("channels", "ls")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("channels", "add", "https://evil.example/x"), "isn't a YouTube link"),
        (("channels", "add", "@nobody"), "no channel at"),
        (("channels", "add", "@mkbhd", "--priority", "urgent"), "isn't a priority"),
        (("channels", "add", "@mkbhd", "--feeds", "popular"), "isn't a feed"),
        (("channels", "set", "@nobody", "--priority", "low"), "don't follow"),
        (("channels", "rm", "@nobody"), "don't follow"),
        (("seen", "nosuchvideo"), "doesn't know"),
        (("seen",), "Give a video ID"),
        (("settings", "set", "nonsense", "1"), "no setting"),
        (("settings", "set", "window_days", "0"), "window_days"),
        (("profiles", "add", "../evil"), "Use up to 40"),
    ],
)
def test_problems_are_sentences_not_stack_traces(cli, args, message):
    assert message in cli(*args, code=1)


def test_the_board_lists_and_marks_seen(cli):
    cli("channels", "add", "@veritasium", "--priority", "high")
    output = cli("board")
    assert "Veritasium video 0" in output
    assert "High" in output
    assert "4 unseen" in output
    first = json.loads(cli("board", "--json", "--limit", "1"))["cards"][0]["video"]
    assert first["url"] == f"https://www.youtube.com/watch?v={first['id']}"
    assert "Undo with" in cli("seen", first["id"])
    assert "3 unseen" in cli("board")
    assert first["title"] in cli("seen", "--list")
    assert first["id"] in cli("seen", "--undo")
    assert "4 unseen" in cli("board")
    assert "Nothing to undo" in cli("seen", "--undo")


def test_board_filters(cli):
    cli("channels", "add", "@mkbhd", "--feeds", "videos,shorts", "--tags", "tech")
    cli("channels", "add", "@veritasium", "--tags", "science")
    assert "Marques" not in cli("board", "--tag", "science", "--window", "3650")
    only_shorts = json.loads(cli("board", "--kinds", "shorts", "--window", "3650", "--json"))
    assert {c["video"]["kind"] for c in only_shorts["cards"]} == {"shorts"}
    assert "2 unseen" in cli("board", "--window", "10", "--tag", "science")


def test_an_empty_board_says_why(cli):
    assert "No channels yet" in cli("board")
    cli("channels", "add", "@veritasium")
    assert "Nothing unseen" in cli("board", "--window", "1")


def test_refresh_and_status(cli):
    cli("channels", "add", "@veritasium")
    assert "Everything was checked recently" in cli("refresh")
    cli.clock.advance(hours=2)
    assert "Checked 1 feed" in cli("refresh")
    assert "1 channels, 1 feeds working" in cli("status")
    assert json.loads(cli("status", "--json"))["active_feeds"] == 1


def test_settings(cli):
    cli("settings", "set", "window_days", "30")
    cli("settings", "set", "head_starts.high", "14")
    data = json.loads(cli("settings", "show"))
    assert data["window_days"] == 30
    assert data["head_starts"]["high"] == 14


def test_export_and_import_between_profiles(cli, tmp_path):
    cli("channels", "add", "@veritasium", "--tags", "science", "--priority", "high")
    out = tmp_path / "list.opml"
    cli("export", str(out))
    assert "Veritasium" in cli("export")
    cli("profiles", "add", "nat")
    assert "Added 1 channel" in cli("--profile", "nat", "import", str(out))
    subscription = cli.store("nat").subscriptions()[SECOND]
    assert subscription.tags == ("science",)
    assert subscription.priority is Priority.HIGH


def test_a_missing_import_file_is_refused(cli):
    cli("import", "/no/such/file.opml", code=2)


def test_choosing_between_profiles(cli):
    cli("profiles", "add", "nat")
    assert "dusty" in cli("profiles", "ls")
    assert "Choose a profile" in cli("board", code=1)
    assert "No channels" in cli("--profile", "nat", "board")
    assert "no profile called" in cli("--profile", "ghost", "board", code=1)


def test_backup(cli):
    cli("channels", "add", "@veritasium")
    assert "Backed up to" in cli("backup")
    assert list(cli.runtime.paths.backups.glob("*/catalogue.db"))


def test_a_title_prints_exactly_as_written(cli):
    from tests.support import NOW, feed_xml

    cli.transport.serve_feed(
        SECOND,
        FeedKind.VIDEOS,
        feed_xml(SECOND, "Veritasium", [("markup00xxx", "[bold]Not bold[/bold] &amp; ok", NOW)]),
    )
    cli("channels", "add", "@veritasium")
    assert "[bold]Not bold[/bold] & ok" in cli("board")
