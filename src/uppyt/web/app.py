"""The web app: pages and form posts over the same operations as the terminal. No business logic lives here."""

from __future__ import annotations

import hashlib
import ipaddress
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware

from ..domain.errors import NotFound, SettingError, UserError, message_for_people
from ..domain.feeds import SPECS, TOGGLES, FeedKind
from ..domain.models import CHANNEL_ID, Priority
from ..domain.ranking import describe_age
from ..domain.settings import THEMES, WINDOW_CHOICES, window_label
from ..ops import implements
from ..services.app import UppYT
from ..services.refresh import RefreshLoop, RefreshService
from ..services.runtime import Runtime
from ..store.profiles import ProfileError, validate_name
from .jobs import ImportJobs
from .pages import page

HERE = Path(__file__).parent
STATIC = HERE / "static"
CSS_ORDER = ("themes/lights-down", "base/elements", "composition/layout", "block/chrome", "block/card", "block/forms",
             "block/lists", "block/toast", "utility/text", "exception/states")  # fmt: skip
CSS_LAYERS = {"themes": "tokens", "base": "base", "composition": "composition", "block": "block",
              "utility": "utility", "exception": "exception"}  # fmt: skip
PROFILE_COOKIE = "uppyt_profile"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' https://i.ytimg.com https://yt3.googleusercontent.com https://yt3.ggpht.com; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
MAX_UPLOAD = 2 * 1024 * 1024
MAX_CARDS = 2000


class NeedsProfile(Exception):
    def __init__(self, target: str):
        self.target = target


@dataclass(frozen=True)
class Bundle:
    url: str
    body: bytes


def _css_bundle() -> Bundle:
    """One stylesheet, in cascade layers. Named after its contents, so a browser can never keep a stale copy."""
    parts = ["@layer tokens, base, composition, block, utility, exception;"]
    for name in CSS_ORDER:
        layer = CSS_LAYERS[name.split("/")[0]]
        parts.append(f"@layer {layer} {{\n{(STATIC / 'css' / f'{name}.css').read_text(encoding='utf-8')}\n}}")
    body = "\n".join(parts).encode()
    return Bundle(f"/assets/uppyt-{hashlib.sha256(body).hexdigest()[:12]}.css", body)


def _versioned(path: str) -> str:
    digest = hashlib.sha256((STATIC / path).read_bytes()).hexdigest()[:12]
    return f"/static/{path}?v={digest}"


class StaticFilesRevalidated(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _safe_back(request: Request, fallback: str = "/") -> str:
    """Back to the page a form came from, on this site only."""
    referer = request.headers.get("referer", "")
    parts = urlsplit(referer)
    on_this_site = parts.path.startswith("/") and not parts.path.startswith("//")  # //host is another site
    if parts.netloc == request.headers.get("host") and on_this_site:
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return fallback


def _host_name(header: str) -> str:
    """The name in a Host header, without the port. IPv6 keeps its brackets, as in `[::1]`."""
    if header.startswith("["):
        end = header.find("]")
        return header[: end + 1] if end != -1 else header
    return header.partition(":")[0]


def _is_ip_literal(name: str) -> bool:
    """DNS rebinding needs a domain name, so a Host that's a plain IP address can't be one of those attacks."""
    text = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return (address.version == 6) == name.startswith("[")


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _kinds_from(values: list[str]) -> list[FeedKind]:
    allowed = {k.value for k in TOGGLES}
    return [FeedKind(v) for v in values if v in allowed]


def _optional_int(text: str, what: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise UserError(f"{what} has to be a whole number.") from None


def create_app(
    runtime: Runtime, *, background_refresh: bool = True, allowed_hosts: frozenset[str] | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop = RefreshLoop(RefreshService(runtime)) if background_refresh else None
        if loop:
            loop.start()
        yield
        if loop:
            loop.stop()

    app = FastAPI(title="uppYT", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    if allowed_hosts is not None:  # DNS names ignore case, and browsers send them lowercased
        allowed_hosts = frozenset(name.lower() for name in allowed_hosts)
    jobs = ImportJobs()
    templates = Jinja2Templates(directory=HERE / "templates")
    css = _css_bundle()
    assets = {"css": css.url, "htmx": _versioned("vendor/htmx.min.js"), "js": _versioned("js/app.js")}
    env = templates.env
    env.globals.update(
        assets=assets,
        Priority=Priority,
        toggles=[SPECS[k] for k in TOGGLES],
        window_choices=WINDOW_CHOICES,
        window_label=window_label,
        themes=THEMES,
        ago=lambda when: describe_age(when, runtime.clock()) if when else "never",
    )
    env.filters["date"] = lambda when: f"{when:%-d %b %Y}" if when else ""

    def services(request: Request) -> UppYT:
        """The chosen profile. With none chosen, sends people to make or pick one."""
        names = runtime.profile_names()
        if not names:
            raise NeedsProfile("/welcome")
        chosen = request.cookies.get(PROFILE_COOKIE)
        if chosen in names:
            return UppYT(runtime, runtime.profile(chosen))
        if len(names) == 1:
            return UppYT(runtime, runtime.profile(names[0]))
        raise NeedsProfile("/profiles")

    def render(request: Request, name: str, context: dict | None = None, *, status: int = 200) -> HTMLResponse:
        base = dict(context or {})
        base.setdefault("profile", None)
        base.setdefault("appearance", "system")
        base.setdefault("theme", "lights-down")
        if base["profile"]:
            settings = base["profile"].store.settings()
            base["appearance"] = settings.appearance
            base["theme"] = settings.theme
            base["profile_name"] = base["profile"].store.name
        base["profile_count"] = len(runtime.profile_names())
        return templates.TemplateResponse(request, name, base, status_code=status)

    def board_query(view) -> str:
        pairs: list[tuple[str, str | int]] = [("window", view.window_days), ("filtered", 1)]
        pairs += [("kind", k.value) for k in TOGGLES if k in view.kinds]
        if view.tag:
            pairs.append(("tag", view.tag))
        return urlencode(pairs)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = _host_name(request.headers.get("host") or "").lower()
        if allowed_hosts is not None and host not in allowed_hosts and not _is_ip_literal(host):
            return PlainTextResponse(
                "That address isn't one uppYT answers on. See UPPYT_ALLOWED_HOSTS.", status_code=421
            )
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("That request came from another site, so uppYT refused it.", status_code=403)
            if not origin and request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                return PlainTextResponse("That request came from another site, so uppYT refused it.", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    app.add_middleware(GZipMiddleware, minimum_size=800)

    @app.exception_handler(NeedsProfile)
    async def needs_profile(_request: Request, error: NeedsProfile):
        return _see_other(error.target)

    @app.exception_handler(ProfileError)
    @app.exception_handler(UserError)
    async def user_error(request: Request, error: UserError):
        status = 404 if isinstance(error, NotFound) else 400
        if _is_htmx(request):
            toast = templates.get_template("_toast.html").render(message=message_for_people(error))
            return HTMLResponse(toast, status_code=200, headers={"HX-Reswap": "none"})
        try:
            profile = services(request)
        except NeedsProfile:
            profile = None
        context = {"profile": profile, "message": message_for_people(error)}
        return render(request, "problem.html", context, status=status)

    @app.exception_handler(404)
    async def not_found(request: Request, _error):
        try:
            profile = services(request)
        except NeedsProfile:
            profile = None
        message = "There's nothing at that address."
        return render(request, "problem.html", {"profile": profile, "message": message}, status=404)

    app.mount("/static", StaticFilesRevalidated(directory=STATIC), name="static")

    @app.get(css.url)
    async def stylesheet() -> Response:
        return Response(
            css.body, media_type="text/css", headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    @app.get("/avatars/{channel_id}.jpg")
    def avatar(channel_id: str) -> FileResponse:
        path = runtime.paths.avatars / f"{channel_id}.jpg"
        if not CHANNEL_ID.match(channel_id) or not path.exists():
            raise NotFound("No picture for that channel.")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    # Profiles

    @page("/welcome", "First run", ("blank",))
    @app.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request):
        if runtime.profile_names():
            return _see_other("/")
        return render(request, "welcome.html", {"problem": None})

    @implements("web", "profiles.list")
    @page("/profiles", "Profiles", ("populated", "two_profiles"))
    @app.get("/profiles", response_class=HTMLResponse)
    def profiles_page(request: Request):
        try:
            profile = services(request)
        except NeedsProfile as needs:
            if needs.target == "/welcome":
                return _see_other("/welcome")
            profile = None
        return render(request, "profiles.html", {"profile": profile, "names": runtime.profile_names(), "problem": None})

    def _chosen(name: str, target: str = "/") -> RedirectResponse:
        response = _see_other(target)
        response.set_cookie(PROFILE_COOKIE, name, samesite="lax", httponly=True, max_age=60 * 60 * 24 * 400)
        return response

    @implements("web", "profiles.create")
    @app.post("/profiles")
    def create_profile(request: Request, name: str = Form("")):
        try:
            return _chosen(runtime.create_profile(name))
        except ProfileError as error:
            template = "profiles.html" if runtime.profile_names() else "welcome.html"
            context = {"names": runtime.profile_names(), "problem": message_for_people(error), "typed": name}
            return render(request, template, context, status=400)

    @app.post("/profiles/use")
    def use_profile(name: str = Form("")):
        return _chosen(runtime.profile(validate_name(name)).name)

    # The board

    def _board_context(request: Request, app_services: UppYT) -> dict:
        query = request.query_params
        window = _optional_int(query.get("window", ""), "The window")
        kinds = _kinds_from(query.getlist("kind")) if query.get("filtered") else None
        tag = query.get("tag") or None
        # Cards always come fresh from the top, up to however many are showing. Marking one seen re-scores its channel,
        # so paging by offset would repeat some cards and skip others.
        step = app_services.settings.get().page_size
        show = max(step, _optional_int(query.get("show", ""), "The number of cards") or step)
        view = app_services.board.view(window_days=window, kinds=kinds, tag=tag, limit=min(show, MAX_CARDS))
        choices = list(WINDOW_CHOICES)
        if view.window_days not in dict(WINDOW_CHOICES):
            choices.append((view.window_days, window_label(view.window_days)))
        return {
            "profile": app_services,
            "view": view,
            "window_options": choices,
            "query": board_query(view),
            "next_show": len(view.cards) + step,
            "step": step,
        }

    @implements("web", "board.view")
    @page("/", "Board", ("blank_profile", "populated", "long_titles", "failing_feed", "nothing_unseen", "just_marked"))
    @app.get("/", response_class=HTMLResponse)
    def board(request: Request):
        return render(request, "board.html", _board_context(request, services(request)))

    # Seen

    @implements("web", "seen.mark")
    @app.post("/seen/{video_id}")
    def mark_seen(request: Request, video_id: str):
        app_services = services(request)
        app_services.seen.mark(video_id)
        if _is_htmx(request):
            remaining = app_services.seen.list(limit=1).total
            html = templates.get_template("_toast_seen.html").render(video_id=video_id, seen_total=remaining)
            return HTMLResponse(html)
        return _see_other(_safe_back(request))

    @implements("web", "seen.undo")
    @app.post("/seen/{video_id}/undo")
    def undo_seen(request: Request, video_id: str):
        services(request).seen.undo(video_id)
        return _see_other(_safe_back(request))

    @implements("web", "seen.list")
    @page("/seen", "Seen", ("blank_profile", "populated", "long_titles", "just_marked"))
    @app.get("/seen", response_class=HTMLResponse)
    def seen_page(request: Request):
        app_services = services(request)
        offset = max(0, _optional_int(request.query_params.get("offset", ""), "The offset") or 0)
        seen_page = app_services.seen.list(offset=offset)
        if not seen_page.items and seen_page.total:  # the last on a later page was put back, or the address is old
            seen_page = app_services.seen.list()
        return render(request, "seen.html", {"profile": app_services, "page": seen_page})

    # Channels

    @implements("web", "channels.list")
    @page("/channels", "Channels", ("blank_profile", "populated", "long_titles", "failing_feed"))
    @app.get("/channels", response_class=HTMLResponse)
    def channels_page(request: Request):
        app_services = services(request)
        return render(request, "channels.html", {"profile": app_services, "summaries": app_services.channels.list()})

    @page("/channels/add", "Add a channel", ("blank_profile", "populated"))
    @app.get("/channels/add", response_class=HTMLResponse)
    def add_page(request: Request):
        return render(request, "add.html", {"profile": services(request), "preview": None, "problem": None, "link": ""})

    @implements("web", "channels.lookup")
    @app.post("/channels/lookup", response_class=HTMLResponse)
    def lookup(request: Request, link: str = Form("")):
        app_services = services(request)
        try:
            preview = app_services.channels.lookup(link)
        except UserError as error:
            context = {"profile": app_services, "preview": None, "problem": message_for_people(error), "link": link}
            return render(request, "add.html", context, status=400)
        return render(request, "add.html", {"profile": app_services, "preview": preview, "problem": None, "link": link})

    @implements("web", "channels.add")
    @app.post("/channels")
    def add_channel(
        request: Request,
        channel_id: str = Form(""),
        priority: str = Form("normal"),
        feeds: list[str] = Form([]),
        tags: str = Form(""),
    ):
        app_services = services(request)
        detail = app_services.channels.add(
            channel_id, priority=Priority.parse(priority), feeds=_kinds_from(feeds), tags=tags.split(",")
        )
        return _see_other(f"/channels/{detail.channel.id}?added=1")

    @implements("web", "channels.get")
    @page("/channels/{channel_id}", "Channel", ("populated", "long_titles", "failing_feed", "with_gap"))
    @app.get("/channels/{channel_id}", response_class=HTMLResponse)
    def channel_page(request: Request, channel_id: str):
        app_services = services(request)
        detail = app_services.channels.get(channel_id)
        done = "added" if "added" in request.query_params else "saved" if "saved" in request.query_params else None
        context = {"profile": app_services, "detail": detail, "problem": None, "done": done}
        return render(request, "channel.html", context)

    @implements("web", "channels.set")
    @app.post("/channels/{channel_id}")
    def save_channel(
        request: Request,
        channel_id: str,
        priority: str = Form("normal"),
        feeds: list[str] = Form([]),
        tags: str = Form(""),
        window: str = Form(""),
    ):
        app_services = services(request)
        try:
            app_services.channels.set(
                channel_id,
                priority=Priority.parse(priority),
                feeds=_kinds_from(feeds),
                tags=tags.split(","),
                window_days=_optional_int(window, "The window"),
            )
        except NotFound:
            raise
        except UserError as error:
            detail = app_services.channels.get(channel_id)
            context = {"profile": app_services, "detail": detail, "problem": message_for_people(error)}
            return render(request, "channel.html", context, status=400)
        return _see_other(f"/channels/{channel_id}?saved=1")

    @page("/channels/{channel_id}/remove", "Stop following", ("populated", "long_titles"))
    @app.get("/channels/{channel_id}/remove", response_class=HTMLResponse)
    def remove_page(request: Request, channel_id: str):
        app_services = services(request)
        return render(
            request, "remove.html", {"profile": app_services, "detail": app_services.channels.get(channel_id)}
        )

    @implements("web", "channels.remove")
    @app.post("/channels/{channel_id}/remove")
    def remove_channel(request: Request, channel_id: str):
        services(request).channels.remove(channel_id)
        return _see_other("/channels")

    # Settings, refreshing, importing and backing up

    def _settings_context(app_services: UppYT, **extra) -> dict:
        return {
            "profile": app_services,
            "settings": app_services.settings.get(),
            "status": app_services.refresh.status(),
            "problem": None,
            "saved": False,
            "message": None,
            "typed": {},
            "invalid": None,
            **extra,
        }

    @implements("web", "settings.get", "refresh.status")
    @page("/settings", "Settings", ("blank_profile", "populated", "failing_feed"))
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return render(request, "settings.html", _settings_context(services(request)))

    @implements("web", "settings.update")
    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request):
        app_services = services(request)
        form = await request.form()
        names = ("window_days", "repeat_penalty_days", "page_size", "appearance", "head_starts.must_see",
                 "head_starts.high", "head_starts.normal", "head_starts.low")  # fmt: skip
        changes = {name: form[name] for name in names if name in form}
        try:
            app_services.settings.update(changes)
        except UserError as error:  # nothing was saved, so show what was typed and point at what was wrong
            invalid = error.field if isinstance(error, SettingError) else None
            problem = message_for_people(error)
            context = _settings_context(app_services, problem=problem, typed=changes, invalid=invalid)
            return render(request, "settings.html", context, status=400)
        return render(request, "settings.html", _settings_context(app_services, saved=True))

    @implements("web", "refresh.run")
    @app.post("/refresh")
    def refresh(request: Request):
        app_services = services(request)
        report = app_services.refresh.run(force=True)
        if _is_htmx(request):
            body = templates.get_template("_toast.html").render(message=report.summary)
            if urlsplit(request.headers.get("hx-current-url", "")).path == "/settings":  # its History line too
                status = app_services.refresh.status()
                body += templates.get_template("_refresh_status.html").render(status=status, oob=True)
            response = HTMLResponse(body)
            response.headers["HX-Reswap"] = "none"
            response.headers["HX-Trigger"] = "uppyt-refreshed"
            return response
        back = _safe_back(request)
        if urlsplit(back).path == "/settings":  # say what it found, the way Back up now does
            return render(request, "settings.html", _settings_context(app_services, message=report.summary))
        return _see_other(back)

    @implements("web", "transfer.export")
    @app.get("/export.opml")
    def export(request: Request) -> Response:
        return Response(
            services(request).transfer.export_opml(),
            media_type="text/x-opml",
            headers={"Content-Disposition": 'attachment; filename="uppyt-channels.opml"'},
        )

    @implements("web", "transfer.import")
    @app.post("/import")
    async def start_import(request: Request, file: UploadFile):
        app_services = services(request)
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise UserError("That file is too big to be a list of channels.")
        if not data:
            raise UserError("Choose an OPML file to import.")
        job = jobs.start(
            app_services.store.name, lambda progress: app_services.transfer.import_opml(data, progress=progress)
        )
        return _see_other(f"/import/{job.id}")

    @page("/import/{job_id}", "Import", ("import_running",))
    @app.get("/import/{job_id}", response_class=HTMLResponse)
    def import_page(request: Request, job_id: str):
        app_services = services(request)
        job = jobs.get(job_id)
        if job is None or job.profile != app_services.store.name:
            raise NotFound("There's no import with that address.")
        return render(request, "import.html", {"profile": app_services, "job": job})

    @implements("web", "backup.create")
    @app.post("/backup", response_class=HTMLResponse)
    def backup(request: Request):
        app_services = services(request)
        result = app_services.backup.create()
        return render(
            request, "settings.html", _settings_context(app_services, message=f"Backed up to {result.folder}.")
        )

    app.state.jobs = jobs
    return app
