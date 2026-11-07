"""`uppyt --cli`: dense text rows, driven by the keyboard. Holds no logic: every action is an operation.

Titles come from strangers, so they're always wrapped in Text. A title like "[bold]" has to stay literal.
"""

from __future__ import annotations

import logging
import webbrowser
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Select, Static

from ..domain.errors import UserError, message_for_people
from ..domain.feeds import SPECS, TOGGLES, FeedKind
from ..domain.models import Priority
from ..domain.settings import WINDOW_CHOICES, Settings, window_label
from ..ops import implements
from ..services.app import UppYT
from ..services.refresh import RefreshLoop
from ..services.runtime import Runtime
from ..services.views import ChannelDetail, ChannelPreview
from ..store.profiles import ProfileStore

log = logging.getLogger("uppyt.tui")

PRIORITY_OPTIONS = [(p.label, p) for p in reversed(Priority)]
WINDOW_OPTIONS = [(label, days) for days, label in WINDOW_CHOICES]

CSS = """
Screen { background: $background; }
#status { height: 1; padding: 0 1; color: $text-muted; }
#notes { height: auto; padding: 0 1; color: $warning; }
#notes.-empty { display: none; }
DataTable { height: 1fr; }
.dialog {
    width: 80%; max-width: 90; height: auto; max-height: 90%; padding: 1 2;
    border: solid $primary; background: $surface;
}
ModalScreen { align: center middle; }
.heading { text-style: bold; margin-bottom: 1; }
.field-label { margin-top: 1; color: $text-muted; }
.buttons { height: auto; margin-top: 1; }
.buttons Button { margin-right: 2; }
.problem { color: $error; height: auto; }
.note { color: $text-muted; height: auto; margin-top: 1; }
Checkbox { height: auto; }
"""


def plain(text: object) -> Text:
    return Text(str(text), no_wrap=True, overflow="ellipsis")


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]
    KEYS = (
        ("j / k or arrows", "Move"),
        ("s", "Mark as seen"),
        ("u", "Undo the last seen"),
        ("o or enter", "Open on YouTube"),
        ("a", "Add a channel"),
        ("c", "Channels"),
        ("v", "Seen videos"),
        ("/", "Filter the board"),
        ("r", "Check for new videos"),
        ("t", "Refresh status"),
        (",", "Settings"),
        ("i", "Import, export and back up"),
        ("p", "Switch profile"),
        ("q", "Quit"),
    )

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("Keys", classes="heading")
            for key, what in self.KEYS:
                yield Static(Text(f"{key:<18}{what}"))


class ConfirmScreen(ModalScreen[bool]):
    AUTO_FOCUS = "#no"  # a stray Enter cancels
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, question: str, action: str):
        super().__init__()
        self.question = question
        self.action = action

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static(Text(self.question))
            with Horizontal(classes="buttons"):
                yield Button(self.action, id="yes", variant="error")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


@implements("tui", "profiles.list", "profiles.create")
class ProfileScreen(ModalScreen[str]):
    """Choosing who's using uppYT, or creating the first profile."""

    AUTO_FOCUS = "#profiles, #new-profile"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        if self.app.services:  # on first run there's no board to go back to
            self.dismiss(None)

    def compose(self) -> ComposeResult:
        names = self.app.runtime.profile_names()
        with VerticalScroll(classes="dialog"):
            yield Label("Who's watching?" if names else "Welcome to uppYT", classes="heading")
            if names:
                table = DataTable(id="profiles", cursor_type="row")
                table.add_column("Profile")
                for name in names:
                    table.add_row(plain(name), key=name)
                yield table
            yield Label("Create a profile", classes="field-label")
            yield Input(placeholder="Your name", id="new-profile")
            yield Static("", id="problem", classes="problem")

    @on(DataTable.RowSelected, "#profiles")
    def picked(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))

    @on(Input.Submitted, "#new-profile")
    def create(self, event: Input.Submitted) -> None:
        try:
            self.dismiss(self.app.runtime.create_profile(event.value))
        except UserError as error:
            self.query_one("#problem", Static).update(Text(str(error)))


class FilterScreen(ModalScreen[dict | None]):
    AUTO_FOCUS = "#window"
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, tags: list[str]):
        super().__init__()
        self.tags = tags

    def compose(self) -> ComposeResult:
        app = self.app
        tags = self.tags
        options = list(WINDOW_OPTIONS)
        if app.window_days not in dict(WINDOW_CHOICES):
            options.append((window_label(app.window_days), app.window_days))
        with VerticalScroll(classes="dialog"):
            yield Label("Filter the board", classes="heading")
            yield Label("How far back", classes="field-label")
            yield Select(options, value=app.window_days, allow_blank=False, id="window")
            yield Label("Show", classes="field-label")
            for kind in TOGGLES:
                yield Checkbox(SPECS[kind].label, value=kind in app.kinds, id=f"kind-{kind.value}")
            yield Label("Tag", classes="field-label")
            yield Select(
                [("All tags", ""), *[(Content(t), t) for t in tags]],  # tags are typed by people: no markup
                value=app.tag if app.tag in tags else "",
                allow_blank=False,
                id="tag",
            )
            with Horizontal(classes="buttons"):
                yield Button("Apply", id="apply", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#apply")
    def apply(self) -> None:
        self.dismiss(
            {
                "window_days": self.query_one("#window", Select).value,
                "kinds": frozenset(k for k in TOGGLES if self.query_one(f"#kind-{k.value}", Checkbox).value),
                "tag": self.query_one("#tag", Select).value or None,
            }
        )


@implements("tui", "channels.lookup", "channels.add")
class AddChannelScreen(ModalScreen[bool]):
    """Paste a link, see what the channel offers, choose, follow."""

    AUTO_FOCUS = "#link"
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self.preview: ChannelPreview | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="add-dialog"):
            yield Label("Add a channel", classes="heading")
            yield Label("Channel link, or an @name", classes="field-label")
            yield Input(placeholder="https://www.youtube.com/@name", id="link")
            yield Static("", id="problem", classes="problem")
            yield Static("", id="found")
            with VerticalScroll(id="choices"):
                yield Label("Priority", classes="field-label")
                yield Select(PRIORITY_OPTIONS, value=Priority.NORMAL, allow_blank=False, id="priority")
                yield Label("Feeds", classes="field-label")
                for kind in TOGGLES:
                    yield Checkbox(SPECS[kind].label, value=SPECS[kind].default_on, id=f"feed-{kind.value}")
                yield Label("Tags, separated by commas", classes="field-label")
                yield Input(id="tags")
                with Horizontal(classes="buttons"):
                    yield Button("Follow", id="follow", variant="primary")
                    yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#choices").display = False

    @on(Input.Submitted, "#link")
    def find(self, event: Input.Submitted) -> None:
        self.preview = None  # whatever the last lookup found no longer applies
        self.query_one("#choices").display = False
        self.query_one("#problem", Static).update("")
        self.query_one("#found", Static).update("Looking...")
        link = event.value

        def work() -> None:
            try:
                preview = self.app.services.channels.lookup(link)
            except UserError as error:
                message = str(error)
                self.app.call_from_thread(self._show_problem, message)
            else:
                self.app.call_from_thread(self._show_preview, preview)

        self.run_worker(work, thread=True, exclusive=True)

    def _show_problem(self, message: str) -> None:
        self.query_one("#found", Static).update("")
        self.query_one("#problem", Static).update(Text(message))

    def _show_preview(self, preview: ChannelPreview) -> None:
        self.preview = preview
        handle = f" {preview.channel.handle}" if preview.channel.handle else ""
        self.query_one("#found", Static).update(Text(f"{preview.channel.name}{handle}"))
        if preview.following:
            self._show_problem(f"You already follow {preview.channel.name}.")
            return
        for kind in TOGGLES:
            box = self.query_one(f"#feed-{kind.value}", Checkbox)
            found = preview.available.get(kind) is not False
            box.value = SPECS[kind].default_on and found
            box.label = SPECS[kind].label if found else f"{SPECS[kind].label} (not found)"
        self.query_one("#choices").display = True
        self.query_one("#priority", Select).focus()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#follow")
    def follow(self) -> None:
        preview = self.preview
        if preview is None:
            return
        feeds = [k for k in TOGGLES if self.query_one(f"#feed-{k.value}", Checkbox).value]
        priority = self.query_one("#priority", Select).value
        tags = self.query_one("#tags", Input).value.split(",")

        def work() -> None:
            try:
                self.app.services.channels.add(preview.channel.id, priority=priority, feeds=feeds, tags=tags)
            except UserError as error:
                message = str(error)
                self.app.call_from_thread(self._show_problem, message)
            else:
                self.app.call_from_thread(self.dismiss, True)

        self.run_worker(work, thread=True, exclusive=True)


@implements("tui", "channels.get", "channels.set", "channels.remove")
class ChannelEditScreen(ModalScreen[bool]):
    AUTO_FOCUS = "#priority"
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, detail: ChannelDetail):
        super().__init__()
        self.detail = detail
        self.channel_id = detail.channel.id

    def compose(self) -> ComposeResult:
        detail = self.detail
        subscription = detail.subscription
        with VerticalScroll(classes="dialog"):
            yield Label(Text(detail.channel.name), classes="heading")
            yield Static(Text(f"{detail.channel.handle or detail.channel.id}, {detail.unseen} unseen"))
            yield Label("Priority", classes="field-label")
            yield Select(PRIORITY_OPTIONS, value=subscription.priority, allow_blank=False, id="priority")
            yield Static(Text(f"Counts as {detail.head_start_days:g} days newer on your board."), classes="note")
            yield Label("Feeds", classes="field-label")
            for feed in detail.feeds:
                fine = feed.state_text in ("Up to date", "Off", "Not fetched yet")
                suffix = "" if fine else f" ({feed.state_text})"
                yield Checkbox(f"{feed.label}{suffix}", value=feed.enabled, id=f"feed-{feed.kind.value}")
            yield Label("Tags, separated by commas", classes="field-label")
            yield Input(", ".join(subscription.tags), id="tags")
            yield Label("This channel's window in days. Empty follows the board", classes="field-label")
            yield Input(str(subscription.window_days or ""), id="window")
            yield Static("", id="problem", classes="problem")
            for note in detail.history_notes:
                yield Static(Text(note), classes="note")
            if detail.also_offers:
                yield Static(Text(f"Also offers, not used: {', '.join(detail.also_offers)}."), classes="note")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Stop following", id="remove", variant="error")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        window_text = self.query_one("#window", Input).value.strip()
        try:
            window_days = int(window_text) if window_text else None
        except ValueError:
            self.query_one("#problem", Static).update("The window has to be a whole number of days.")
            return
        feeds = [k for k in TOGGLES if self.query_one(f"#feed-{k.value}", Checkbox).value]
        priority = self.query_one("#priority", Select).value
        tags = self.query_one("#tags", Input).value.split(",")

        def work() -> None:
            try:
                self.app.services.channels.set(
                    self.channel_id, priority=priority, feeds=feeds, tags=tags, window_days=window_days
                )
            except UserError as error:
                message = str(error)
                self.app.call_from_thread(self.query_one("#problem", Static).update, Text(message))
            else:
                self.app.call_from_thread(self.dismiss, True)

        self.run_worker(work, thread=True, exclusive=True)

    @on(Button.Pressed, "#remove")
    def stop_following(self) -> None:
        name = self.detail.channel.name

        def decided(yes: bool | None) -> None:
            if yes:
                try:
                    self.app.services.channels.remove(self.channel_id)
                except UserError as error:
                    self.app.report(error)
                self.dismiss(True)

        self.app.push_screen(ConfirmScreen(f"Stop following {name}? Its history is kept.", "Stop following"), decided)


@implements("tui", "channels.list")
class ChannelsScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("a", "add", "Add"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Channels. Enter to change one.", id="status")
        table = DataTable(id="channels", cursor_type="row")
        for column in ("Priority", "Unseen", "Feeds", "Channel"):
            table.add_column(column)
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        table = self.query_one("#channels", DataTable)
        table.clear()
        try:
            summaries = self.app.services.channels.list()
        except UserError as error:
            self.app.report(error)
            summaries = []
        for summary in summaries:
            feeds = ",".join(SPECS[k].label for k in TOGGLES if k in summary.subscription.feeds)
            name = summary.channel.name + (f" ({summary.problem})" if summary.problem else "")
            table.add_row(
                plain(summary.subscription.priority.label),
                plain(summary.unseen),
                plain(feeds),
                plain(name),
                key=summary.channel.id,
            )
        table.focus()

    def _changed(self, changed: bool | None) -> None:
        if changed:
            self.reload()
            self.app.reload_board()

    @on(DataTable.RowSelected, "#channels")
    def edit(self, event: DataTable.RowSelected) -> None:
        try:
            detail = self.app.services.channels.get(str(event.row_key.value))
        except UserError as error:  # stopped following elsewhere, say from the web app
            self.app.report(error)
            self._changed(True)
            return
        self.app.push_screen(ChannelEditScreen(detail), self._changed)

    def action_add(self) -> None:
        self.app.push_screen(AddChannelScreen(), self._changed)

    def action_cursor_down(self) -> None:
        self.query_one("#channels", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#channels", DataTable).action_cursor_up()


@implements("tui", "seen.list")
class SeenScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("u", "restore", "Put back"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Seen videos, most recent first", id="status")
        table = DataTable(id="seen", cursor_type="row")
        for column in ("Channel", "Age", "Title"):
            table.add_column(column)
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        table = self.query_one("#seen", DataTable)
        table.clear()
        try:
            items = self.app.services.seen.list(limit=1_000_000).items
        except UserError as error:
            self.app.report(error)
            items = []
        for item in items:
            name = item.channel.name if item.channel else ""
            table.add_row(plain(name), plain(item.age), plain(item.video.title), key=item.video.id)
        table.focus()

    def action_restore(self) -> None:
        table = self.query_one("#seen", DataTable)
        if not table.row_count:
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        try:
            if self.app.services.seen.undo(str(key)) is None:
                self.app.notify("That video wasn't marked seen any more.", markup=False)
        except UserError as error:
            self.app.report(error)
        self.reload()
        self.app.reload_board()

    def action_cursor_down(self) -> None:
        self.query_one("#seen", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#seen", DataTable).action_cursor_up()


@implements("tui", "settings.get", "settings.update")
class SettingsScreen(ModalScreen[bool]):
    AUTO_FOCUS = "Input"
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]
    NUMBERS = (
        ("window_days", "Default window, in days"),
        ("repeat_penalty_days", "Repeat penalty, in days. 0 turns it off"),
        ("head_starts.must_see", "Must-see head start, in days"),
        ("head_starts.high", "High head start, in days"),
        ("head_starts.normal", "Normal head start, in days"),
        ("head_starts.low", "Low head start, in days"),
        ("page_size", "Cards per page"),
    )

    @staticmethod
    def _input_id(name: str) -> str:
        return "set-" + name.replace(".", "-")

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        data = self.settings.model_dump()
        with VerticalScroll(classes="dialog"):
            yield Label("Settings", classes="heading")
            for name, label in self.NUMBERS:
                value = data
                for part in name.split("."):
                    value = value[part]
                yield Label(label, classes="field-label")
                yield Input(f"{value:g}", id=self._input_id(name))
            yield Static("", id="problem", classes="problem")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        changes = {name: self.query_one(f"#{self._input_id(name)}", Input).value for name, _ in self.NUMBERS}
        try:
            self.app.services.settings.update(changes)
        except UserError as error:
            self.query_one("#problem", Static).update(Text(message_for_people(error)))
        else:
            self.dismiss(True)


@implements("tui", "transfer.import", "transfer.export", "backup.create")
class TransferScreen(ModalScreen[bool]):
    AUTO_FOCUS = "#import-path"
    BINDINGS = [Binding("escape", "dismiss(False)", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("Import, export and back up", classes="heading")
            yield Label("Import channels from an OPML file", classes="field-label")
            yield Input(placeholder="Path to the file. Enter to import", id="import-path")
            yield Label("Export channels to an OPML file", classes="field-label")
            yield Input("uppyt-channels.opml", id="export-path")
            with Horizontal(classes="buttons"):
                yield Button("Back up everything", id="backup")
                yield Button("Close", id="close")
            yield Static("", id="result", classes="note")

    def _say(self, message: str) -> None:
        self.query_one("#result", Static).update(Text(message))

    @on(Input.Submitted, "#import-path")
    def import_file(self, event: Input.Submitted) -> None:
        path = Path(event.value).expanduser()
        self._say("Importing...")

        def work() -> None:
            try:
                data = path.read_bytes()
                report = self.app.services.transfer.import_opml(
                    data,
                    progress=lambda done, total: self.app.call_from_thread(
                        self._say, f"Importing {done} of {total}..."
                    ),
                )
                message = report.summary
            except OSError as error:
                message = f"Couldn't read that file: {error.strerror}."
            except UserError as error:
                message = str(error)
            self.app.call_from_thread(self._say, message)
            self.app.call_from_thread(self.app.reload_board)

        self.run_worker(work, thread=True, exclusive=True)

    @on(Input.Submitted, "#export-path")
    def export_file(self, event: Input.Submitted) -> None:
        path = Path(event.value).expanduser()
        try:
            path.write_text(self.app.services.transfer.export_opml(), encoding="utf-8")
        except OSError as error:
            self._say(f"Couldn't write that file: {error.strerror}.")
        except UserError as error:
            self._say(str(error))
        else:
            self._say(f"Wrote {path}.")

    @on(Button.Pressed, "#backup")
    def backup(self) -> None:
        try:
            folder = self.app.services.backup.create().folder
        except OSError as error:
            self._say(f"Couldn't back up: {error.strerror or error}.")
        except UserError as error:
            self._say(str(error))
        else:
            self._say(f"Backed up to {folder}.")

    @on(Button.Pressed, "#close")
    def close_screen(self) -> None:
        self.dismiss(False)


@implements("tui", "board.view", "seen.mark", "seen.undo", "refresh.run", "refresh.status")
class UppytApp(App):
    TITLE = "uppYT"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "mark_seen", "Seen"),
        Binding("u", "undo", "Undo"),
        Binding("o", "open_video", "Open"),
        Binding("a", "add_channel", "Add"),
        Binding("c", "channels", "Channels"),
        Binding("slash", "filter", "Filter"),
        Binding("r", "refresh", "Refresh"),
        Binding("v", "seen_list", "Seen list"),
        Binding("question_mark", "help", "Help"),
        Binding("t", "status", "Status", show=False),
        Binding("comma", "settings", "Settings", show=False),
        Binding("i", "transfer", "Import/export", show=False),
        Binding("p", "profiles", "Profile", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(
        self,
        runtime: Runtime,
        store: ProfileStore | None = None,
        *,
        opener: Callable[[str], object] = webbrowser.open,
        background_refresh: bool = True,
    ):
        super().__init__()
        self.runtime = runtime
        self.services: UppYT | None = UppYT(runtime, store) if store else None
        self.opener = opener
        self.background_refresh = background_refresh
        self.window_days: int = 60
        self.kinds: frozenset[FeedKind] = frozenset(TOGGLES)
        self.tag: str | None = None
        self._refresh_loop: RefreshLoop | None = None
        self.profile_problem: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        yield Static("", id="notes", classes="-empty")
        table = DataTable(id="board", cursor_type="row", zebra_stripes=True)
        table.add_column("Priority", key="priority")
        table.add_column("Channel", key="channel", width=24)
        table.add_column("Age", key="age")
        table.add_column("Kind", key="kind")
        table.add_column("Title", key="title")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        if self.services is None:
            self.action_profiles()
        else:
            self._begin()

    def _begin(self) -> None:
        try:
            self.window_days = self.services.settings.get().window_days
        except UserError as error:
            self.report(error)
        self.reload_board()
        self.query_one("#board", DataTable).focus()
        if self.background_refresh and self._refresh_loop is None:
            self._refresh_loop = RefreshLoop(
                self.services.refresh, on_change=lambda: self.call_from_thread(self.reload_board)
            )
            self._refresh_loop.start()

    def on_unmount(self) -> None:
        if self._refresh_loop:
            self._refresh_loop.stop()

    def report(self, error: Exception) -> None:
        """Errors meant for people (a broken file, a channel removed elsewhere) are shown, not crashes."""
        self.notify(str(error), severity="error", markup=False, timeout=8)

    def reload_board(self) -> None:
        if self.services is None:
            return
        table = self.query_one("#board", DataTable)
        row = table.cursor_row
        notes = self.query_one("#notes", Static)
        try:
            view = self.services.board.view(window_days=self.window_days, kinds=self.kinds, tag=self.tag, limit=2000)
        except UserError as error:
            # A hand edit uppYT can't read: say what's wrong, and leave the file for the person to fix.
            self.profile_problem = str(error)
            table.clear()
            self.query_one("#status", Static).update(Text(f"{self.services.store.name}  This profile can't be shown."))
            notes.update(Text(self.profile_problem))
            notes.set_class(False, "-empty")
            return
        self.profile_problem = None
        table.clear()
        for card in view.cards:
            label = "" if card.priority is Priority.NORMAL else card.priority.label
            table.add_row(
                plain(label),
                plain(card.channel.name),
                plain(card.age),
                plain(card.kind_label),
                plain(card.video.title),
                key=card.video.id,
            )
        if view.cards:
            table.move_cursor(row=min(row, len(view.cards) - 1))
        name = self.services.store.name
        filter_text = f", tag {view.tag}" if view.tag else ""
        status = f"{name}  {window_label(view.window_days)}{filter_text}  {view.total} unseen"
        if not view.channel_count:
            status = f"{name}  No channels yet. Press a to add one."
        self.query_one("#status", Static).update(Text(status))
        notes.update(Text("\n".join(view.notes)))
        notes.set_class(not view.notes, "-empty")

    def _selected_video(self) -> str | None:
        table = self.query_one("#board", DataTable)
        if not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def _on_board(self) -> bool:
        return self.services is not None and len(self.screen_stack) == 1

    def action_mark_seen(self) -> None:
        video = self._selected_video()
        if video and self._on_board():
            try:
                self.services.seen.mark(video)
            except UserError as error:
                self.report(error)
                return
            self.reload_board()
            self.notify("Marked as seen. Press u to undo.", markup=False, timeout=4)

    def action_undo(self) -> None:
        if self._on_board():
            try:
                restored = self.services.seen.undo()
            except UserError as error:
                self.report(error)
                return
            self.reload_board()
            self.notify("Put back on the board." if restored else "Nothing to undo.", markup=False, timeout=3)

    @on(DataTable.RowSelected, "#board")
    def _row_selected(self) -> None:
        self.action_open_video()

    def action_open_video(self) -> None:
        video = self._selected_video()
        if video and self._on_board():
            found = self.runtime.catalogue.videos_by_id([video]).get(video)
            # webbrowser.open says False when no browser can start, as over SSH
            if found and self.opener(found.url) is False:
                self.notify(f"Couldn't open a browser. The video is at {found.url}", markup=False, timeout=10)

    def action_cursor_down(self) -> None:
        self.query_one("#board", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#board", DataTable).action_cursor_up()

    def _reload_if(self, changed: bool | None) -> None:
        if changed:
            self.reload_board()

    def _usable(self) -> bool:
        """A profile whose channels can't be read can't offer screens built on them. Say why instead."""
        if self.services is None:
            return False
        if self.profile_problem:
            self.reload_board()  # the file may have been fixed by hand since
        if self.profile_problem:
            self.notify(self.profile_problem, severity="error", markup=False, timeout=8)
            return False
        return True

    def action_add_channel(self) -> None:
        if self._usable():
            self.push_screen(AddChannelScreen(), self._reload_if)

    def _show_list(self, screen_type: type[Screen]) -> None:
        """Channels and Seen swap in place, so one Escape always gets back to the board."""
        if not self.services or isinstance(self.screen, (screen_type, ModalScreen)):
            return  # a dialog on top is finished first, so the stack can't grow under it
        if isinstance(self.screen, (ChannelsScreen, SeenScreen)):
            self.switch_screen(screen_type())
        else:
            self.push_screen(screen_type())

    def action_channels(self) -> None:
        if self._usable():
            self._show_list(ChannelsScreen)

    def action_seen_list(self) -> None:
        self._show_list(SeenScreen)

    def _settings_closed(self, saved: bool | None) -> None:
        if saved:
            try:
                self.window_days = self.services.settings.get().window_days
            except UserError as error:
                self.report(error)
            self.reload_board()

    def action_settings(self) -> None:
        if not self.services:
            return
        try:
            settings = self.services.settings.get()
        except UserError as error:
            self.report(error)
            return
        self.push_screen(SettingsScreen(settings), self._settings_closed)

    def action_transfer(self) -> None:
        if self.services:
            self.push_screen(TransferScreen(), self._reload_if)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_filter(self) -> None:
        if not self._usable():
            return

        def apply(choice: dict | None) -> None:
            if choice:
                self.window_days = choice["window_days"]
                self.kinds = choice["kinds"]
                self.tag = choice["tag"]
                self.reload_board()

        try:
            tags = self.services.board.view(limit=1).tags
        except UserError as error:
            self.report(error)
            return
        self.push_screen(FilterScreen(tags), apply)

    def action_profiles(self) -> None:
        def chosen(name: str | None) -> None:
            if name:
                self.services = UppYT(self.runtime, self.runtime.profile(name))
                self.tag = None  # another profile's tags mean nothing here
                self.call_later(self._switched)

        self.push_screen(ProfileScreen(), chosen)

    def _switched(self) -> None:
        """Back to the board once the dialog has gone: a list left open would show the old profile."""
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self._begin()

    def action_refresh(self) -> None:
        if not self.services:
            return
        self.notify("Checking for new videos...", markup=False, timeout=2)
        refresh = self.services.refresh

        def work() -> None:
            try:
                summary = refresh.run(force=True).summary
            except Exception as error:  # the hourly refresh survives this too
                log.exception("Refresh failed.")
                self.call_from_thread(
                    self.notify, f"Refresh failed: {error}. Trying again later.", severity="error", markup=False
                )
                return
            self.call_from_thread(self.reload_board)
            self.call_from_thread(self.notify, summary, markup=False, timeout=5)

        self.run_worker(work, thread=True, exclusive=True, group="refresh")

    def action_status(self) -> None:
        if not self.services:
            return
        state = self.services.refresh.status()
        last = f"{state.last_refresh:%-d %b %H:%M}" if state.last_refresh else "never"
        feeds = f"{state.active_feeds} feed{'s' if state.active_feeds != 1 else ''}"
        text = f"Last refresh {last}. {feeds} working, {state.failing_feeds} failing."
        if state.possible_gaps:
            text += f" {state.possible_gaps} possible gap{'s' if state.possible_gaps != 1 else ''}."
        self.notify(text, markup=False, timeout=6)
