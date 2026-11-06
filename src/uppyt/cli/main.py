"""The command line. `uppyt` opens the web app, `uppyt --cli` the terminal app, and the rest are for scripts."""

from __future__ import annotations

import functools
import os
import sys
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer

from .. import __version__
from ..domain.errors import UserError
from ..domain.feeds import TOGGLES, FeedKind
from ..domain.models import Priority
from ..domain.settings import window_label
from ..ops import implements
from ..services.app import UNSET, UppYT
from ..services.runtime import Paths, Runtime
from . import render

app = typer.Typer(add_completion=False, help="A board of recent uploads from the YouTube channels you follow.")
channels_app = typer.Typer(help="Add, change and remove channels.", no_args_is_help=True)
profiles_app = typer.Typer(help="List and create profiles.", no_args_is_help=True)
settings_app = typer.Typer(help="Show and change settings.", no_args_is_help=True)
app.add_typer(channels_app, name="channels")
app.add_typer(profiles_app, name="profiles")
app.add_typer(settings_app, name="settings")

ProfileOption = Annotated[
    str | None, typer.Option("--profile", "-p", help="Which profile to use", envvar="UPPYT_PROFILE")
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON for scripts")]
FEEDS_HELP = f"Comma-separated feeds: {', '.join(k.value for k in TOGGLES)}"
PRIORITY_HELP = "low, normal, high or must-see"


class Session:
    """The runtime and chosen profile for one command. Tests pass their own runtime as the Typer context object."""

    def __init__(self, runtime: Runtime | None = None, profile: str | None = None, *, owned: bool = True):
        self.runtime = runtime or Runtime(Paths.default())
        self.profile = profile
        self.owned = owned

    def app(self, profile: str | None = None) -> UppYT:
        return UppYT(self.runtime, self.runtime.choose_profile(profile or self.profile))


def _session(ctx: typer.Context) -> Session:
    return ctx.obj


def friendly(function):
    """Problems a person can fix are shown as a sentence, not a stack trace."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except UserError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from error

    return wrapper


def _parse_feeds(text: str | None) -> list[FeedKind] | None:
    if text is None:
        return None
    feeds = []
    for part in (p.strip().lower() for p in text.split(",") if p.strip()):
        if part not in {k.value for k in TOGGLES}:
            raise UserError(f"{part} isn't a feed. {FEEDS_HELP}.")
        feeds.append(FeedKind(part))
    return feeds


def _parse_priority(text: str | None) -> Priority | None:
    return None if text is None else Priority.parse(text)


def _parse_tags(text: str | None) -> list[str] | None:
    return None if text is None else [t for t in text.split(",") if t.strip()]


def _print_detail(detail) -> None:
    subscription = detail.subscription
    channel = detail.channel
    typer.echo(f"{channel.name}  {channel.handle or ''}  {channel.id}".rstrip())
    typer.echo(f"Priority: {subscription.priority.label}, counts as {detail.head_start_days:g} days newer")
    typer.echo(
        f"Window:   {window_label(subscription.window_days) if subscription.window_days else 'Follows the board'}"
    )
    typer.echo(f"Tags:     {', '.join(subscription.tags) or 'none'}")
    typer.echo(f"Unseen:   {detail.unseen}")
    typer.echo("Feeds:")
    for feed in detail.feeds:
        typer.echo(f"  [{'x' if feed.enabled else ' '}] {feed.label}: {feed.state_text}")
    if detail.also_offers:
        typer.echo(f"Also offers, not used: {', '.join(detail.also_offers)}")
    for note in detail.history_notes:
        typer.echo(note)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    cli: Annotated[bool, typer.Option("--cli", help="Open the terminal app instead of the web app")] = False,
    profile: ProfileOption = None,
    host: Annotated[
        str, typer.Option(help="Address for the web app. Profiles aren't private, so take care")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for the web app")] = 8770,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Don't open a browser")] = False,
    version: Annotated[bool, typer.Option("--version", help="Show the version")] = False,
) -> None:
    if version:
        typer.echo(f"uppYT {__version__}")
        raise typer.Exit
    session = ctx.obj if isinstance(ctx.obj, Session) else Session(profile=profile)
    session.profile = profile or session.profile
    ctx.obj = session
    if ctx.invoked_subcommand is not None:
        return
    if cli:
        _run_terminal_app(session)
    else:
        _run_web_app(session, host, port, not no_browser)


def _run_terminal_app(session: Session) -> None:
    from ..tui.app import UppytApp

    store = session.runtime.profile(session.profile) if session.profile else session.runtime.default_profile()
    UppytApp(session.runtime, store).run()


def _allowed_hosts(host: str) -> frozenset[str] | None:
    """Which Host headers to answer, so a web page can't reach a local uppYT by pointing a domain name at it."""
    extra = {h.strip() for h in os.environ.get("UPPYT_ALLOWED_HOSTS", "").split(",") if h.strip()}
    if host in ("0.0.0.0", "::") and not extra:
        return None  # listening everywhere: the person running it knows their own addresses
    return frozenset({"localhost", "127.0.0.1", "[::1]", host, *extra})


def _run_web_app(session: Session, host: str, port: int, open_browser: bool) -> None:
    import threading

    import uvicorn

    from ..web.app import create_app

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    typer.echo(f"uppYT is running at {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo("Warning: anyone who can reach this address can see and change every profile.", err=True)
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(
        create_app(session.runtime, allowed_hosts=_allowed_hosts(host)), host=host, port=port, log_level="warning"
    )


@implements("cli", "board.view")
@app.command("board")
@friendly
def board(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(help="How many to show")] = 20,
    offset: Annotated[int, typer.Option(help="How many to skip")] = 0,
    window: Annotated[int | None, typer.Option(help="How many days back. Defaults to your setting")] = None,
    kinds: Annotated[str | None, typer.Option(help=FEEDS_HELP)] = None,
    tag: Annotated[str | None, typer.Option(help="Only channels with this tag")] = None,
    as_json: JsonOption = False,
) -> None:
    """Show the ranked board."""
    view = (
        _session(ctx)
        .app()
        .board.view(window_days=window, kinds=_parse_feeds(kinds), tag=tag, offset=offset, limit=limit)
    )
    if as_json:
        typer.echo(render.to_json(view))
        return
    if not view.cards:
        if view.channel_count:
            typer.echo(f"Nothing unseen from your {view.channel_count} channels in this window.")
        else:
            typer.echo("No channels yet. Add one with: uppyt channels add @name")
    rows = [
        (c.video.id, "" if c.priority is Priority.NORMAL else c.priority.label, c.channel.name, c.age, c.video.title)
        for c in view.cards
    ]
    if rows:
        typer.echo(render.columns(rows))
        typer.echo(f"\n{view.total} unseen, {window_label(view.window_days).lower()}")
    for note in view.notes:
        typer.echo(note)


@implements("cli", "seen.mark", "seen.undo", "seen.list")
@app.command("seen")
@friendly
def seen(
    ctx: typer.Context,
    video_id: Annotated[str | None, typer.Argument(help="The video to mark, or to put back with --undo")] = None,
    undo: Annotated[bool, typer.Option("--undo", help="Put it back on the board. With no ID, the latest")] = False,
    list_seen: Annotated[bool, typer.Option("--list", help="List seen videos")] = False,
    limit: Annotated[int, typer.Option(help="How many to list")] = 50,
    as_json: JsonOption = False,
) -> None:
    """Mark a video as seen, put one back, or list them."""
    services = _session(ctx).app().seen
    if undo:
        restored = services.undo(video_id)
        typer.echo(f"Put {restored} back on the board." if restored else "Nothing to undo.")
    elif video_id:
        typer.echo(f"Marked {services.mark(video_id)} as seen. Undo with: uppyt seen --undo")
    elif list_seen:
        page = services.list(limit=limit)
        if as_json:
            typer.echo(render.to_json(page))
        else:
            rows = [(i.video.id, i.channel.name if i.channel else "", i.video.title) for i in page.items]
            typer.echo(render.columns(rows) if rows else "Nothing marked as seen yet.")
    else:
        raise UserError("Give a video ID, or use --list or --undo.")


@implements("cli", "refresh.run")
@app.command("refresh")
@friendly
def refresh(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option("--force", help="Check now, except anything checked in the last 15 minutes")
    ] = False,
) -> None:
    """Check feeds for new videos. Works well from cron or a timer."""
    session = _session(ctx)
    from ..services.refresh import RefreshService

    typer.echo(RefreshService(session.runtime).run(force=force).summary)


@implements("cli", "refresh.status")
@app.command("status")
@friendly
def status(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Show when feeds were last checked."""
    from ..services.refresh import RefreshService

    state = RefreshService(_session(ctx).runtime).status()
    if as_json:
        typer.echo(render.to_json(state))
        return
    typer.echo(f"Last refresh: {render.when(state.last_refresh)}")
    typer.echo(f"{state.channels} channels, {state.active_feeds} feeds working, {state.failing_feeds} failing")
    if state.possible_gaps:
        typer.echo(f"{state.possible_gaps} stretches where videos may be missing")


@implements("cli", "transfer.import")
@app.command("import")
@friendly
def import_file(
    ctx: typer.Context, file: Annotated[Path, typer.Argument(help="An OPML file", exists=True, dir_okay=False)]
) -> None:
    """Follow the YouTube channels in an OPML file."""
    services = _session(ctx).app()
    report = services.transfer.import_opml(
        file.read_bytes(), progress=lambda done, total: typer.echo(f"{done}/{total}", err=True)
    )
    typer.echo(report.summary)
    for title, reason in report.failures:
        typer.echo(f"  {title}: {reason}")


@implements("cli", "transfer.export")
@app.command("export")
@friendly
def export_file(
    ctx: typer.Context,
    file: Annotated[Path | None, typer.Argument(help="Where to write. Defaults to the screen")] = None,
) -> None:
    """Write your channels as OPML."""
    text = _session(ctx).app().transfer.export_opml()
    if file is None:
        sys.stdout.write(text)
    else:
        file.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {file}")


@implements("cli", "backup.create")
@app.command("backup")
@friendly
def backup(ctx: typer.Context) -> None:
    """Copy history and every profile to a dated backup folder."""
    from ..services.app import BackupService

    typer.echo(f"Backed up to {BackupService(_session(ctx).runtime).create().folder}")


@implements("cli", "channels.list")
@channels_app.command("ls")
@friendly
def channels_ls(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """List the channels you follow."""
    summaries = _session(ctx).app().channels.list()
    if as_json:
        typer.echo(render.to_json(summaries))
        return
    if not summaries:
        typer.echo("No channels yet. Add one with: uppyt channels add @name")
        return
    rows = [
        (
            s.subscription.priority.label,
            f"{s.unseen} unseen",
            ",".join(k.value for k in TOGGLES if k in s.subscription.feeds),
            s.channel.name + (f"  ({s.problem})" if s.problem else ""),
        )
        for s in summaries
    ]
    typer.echo(render.columns(rows))


@implements("cli", "channels.get")
@channels_app.command("show")
@friendly
def channels_show(ctx: typer.Context, channel: str, as_json: JsonOption = False) -> None:
    """Show one channel, by ID, @handle or name."""
    detail = _session(ctx).app().channels.get(channel)
    typer.echo(render.to_json(detail)) if as_json else _print_detail(detail)


@implements("cli", "channels.lookup")
@channels_app.command("lookup")
@friendly
def channels_lookup(ctx: typer.Context, link: str, as_json: JsonOption = False) -> None:
    """Find a channel from a link and show every feed it offers, without following it."""
    preview = _session(ctx).app().channels.lookup(link)
    if as_json:
        typer.echo(render.to_json(preview))
        return
    typer.echo(f"{preview.channel.name}  {preview.channel.handle or ''}  {preview.channel.id}".rstrip())
    if preview.following:
        typer.echo("You already follow this channel.")
    words = {True: "yes", False: "no", None: "couldn't check"}
    typer.echo(render.columns([(kind.value, words[exists]) for kind, exists in preview.available.items()]))


@implements("cli", "channels.add")
@channels_app.command("add")
@friendly
def channels_add(
    ctx: typer.Context,
    link: Annotated[str, typer.Argument(help="A channel link, or an @name")],
    priority: Annotated[str, typer.Option(help=PRIORITY_HELP)] = "normal",
    feeds: Annotated[str | None, typer.Option(help=FEEDS_HELP)] = None,
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags")] = None,
    window: Annotated[int | None, typer.Option(help="This channel's own window in days")] = None,
) -> None:
    """Follow a channel."""
    detail = (
        _session(ctx)
        .app()
        .channels.add(
            link,
            priority=_parse_priority(priority) or Priority.NORMAL,
            feeds=_parse_feeds(feeds),
            tags=_parse_tags(tags) or (),
            window_days=window,
        )
    )
    typer.echo(f"Following {detail.channel.name}.")
    _print_detail(detail)


@implements("cli", "channels.set")
@channels_app.command("set")
@friendly
def channels_set(
    ctx: typer.Context,
    channel: str,
    priority: Annotated[str | None, typer.Option(help=PRIORITY_HELP)] = None,
    feeds: Annotated[str | None, typer.Option(help=FEEDS_HELP)] = None,
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags. Empty clears them")] = None,
    window: Annotated[int | None, typer.Option(help="This channel's own window in days")] = None,
    follow_board: Annotated[bool, typer.Option("--follow-board-window", help="Go back to the board's window")] = False,
) -> None:
    """Change a channel's priority, feeds, tags or window."""
    window_days: Any = UNSET
    if follow_board:
        window_days = None
    elif window is not None:
        window_days = window
    detail = (
        _session(ctx)
        .app()
        .channels.set(
            channel,
            priority=_parse_priority(priority),
            feeds=_parse_feeds(feeds),
            tags=_parse_tags(tags),
            window_days=window_days,
        )
    )
    _print_detail(detail)


@implements("cli", "channels.remove")
@channels_app.command("rm")
@friendly
def channels_rm(ctx: typer.Context, channel: str) -> None:
    """Stop following a channel. Its history is kept in case you come back."""
    typer.echo(f"Stopped following {_session(ctx).app().channels.remove(channel).name}. Its history is kept.")


@implements("cli", "profiles.list")
@profiles_app.command("ls")
@friendly
def profiles_ls(ctx: typer.Context) -> None:
    """List the profiles."""
    names = _session(ctx).runtime.profile_names()
    typer.echo("\n".join(names) if names else "No profiles yet. Create one with: uppyt profiles add NAME")


@implements("cli", "profiles.create")
@profiles_app.command("add")
@friendly
def profiles_add(ctx: typer.Context, name: str) -> None:
    """Create a profile."""
    typer.echo(f"Created profile {_session(ctx).runtime.create_profile(name)}.")


@implements("cli", "settings.get")
@settings_app.command("show")
@friendly
def settings_show(ctx: typer.Context) -> None:
    """Show settings."""
    typer.echo(render.to_json(_session(ctx).app().settings.get()))


@implements("cli", "settings.update")
@settings_app.command("set")
@friendly
def settings_set(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="For example window_days or head_starts.high")],
    value: str,
) -> None:
    """Change a setting."""
    settings = _session(ctx).app().settings.update({name: value})
    typer.echo(render.to_json(settings))
