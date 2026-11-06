"""The rules in CLAUDE.md that a machine can check, so they hold even when a session forgets them."""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.responses import HTMLResponse

from tests.support import FixedClock, no_sleep, world
from uppyt import ops
from uppyt.cli import main as _cli  # noqa: F401  registers its operations
from uppyt.domain.feeds import CATALOGUE, TOGGLES
from uppyt.domain.settings import THEMES
from uppyt.services import app as services_app
from uppyt.services import refresh as services_refresh
from uppyt.services import runtime as services_runtime
from uppyt.services.runtime import Paths, Runtime
from uppyt.tui import app as _tui  # noqa: F401
from uppyt.web import app as web_app
from uppyt.web.pages import PAGES

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "src" / "uppyt"
STATIC = SOURCE / "web" / "static"


def imports(path: Path) -> set[str]:
    package = "uppyt." + ".".join(path.relative_to(SOURCE).parent.parts)
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rstrip(".").split(".")
                base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base)
                found.add(f"{prefix}.{node.module}" if node.module else prefix)
                if not node.module:
                    found.update(f"{prefix}.{alias.name}" for alias in node.names)
            else:
                found.add(node.module or "")
    return found


LAYERS = {
    "domain": {"uppyt.sources", "uppyt.store", "uppyt.services", "uppyt.web", "uppyt.tui", "uppyt.cli", "uppyt.ops"},
    "sources": {"uppyt.store", "uppyt.services", "uppyt.web", "uppyt.tui", "uppyt.cli"},
    "store": {"uppyt.sources", "uppyt.services", "uppyt.web", "uppyt.tui", "uppyt.cli"},
    "services": {"uppyt.web", "uppyt.tui", "uppyt.cli"},
    "web": {"uppyt.sources", "uppyt.store.catalogue", "uppyt.tui", "uppyt.cli"},
    "tui": {"uppyt.sources", "uppyt.store.catalogue", "uppyt.web", "uppyt.cli"},
    "cli": {"uppyt.sources", "uppyt.store"},
}


def python_files(layer: str) -> list[Path]:
    return sorted((SOURCE / layer).rglob("*.py"))


@pytest.mark.parametrize("layer", LAYERS)
def test_dependencies_only_point_inward(layer):
    for path in python_files(layer):
        for name in imports(path):
            for banned in LAYERS[layer]:
                assert not (name == banned or name.startswith(banned + ".")), f"{path} imports {name}"


def test_only_the_transport_talks_http():
    for path in SOURCE.rglob("*.py"):
        if path.name != "transport.py":
            names = imports(path)
            assert not names & {"httpx", "requests", "urllib.request", "http.client", "socket"}, path


def test_only_the_catalogue_talks_sql():
    for path in SOURCE.rglob("*.py"):
        if path.name != "catalogue.py":
            names = {n.split(".")[0] for n in imports(path)}
            assert not names & {"sqlite3", "sqlmodel", "sqlalchemy"}, path


def test_interfaces_hold_no_ranking_logic():
    for layer in ("web", "tui", "cli"):
        for path in python_files(layer):
            assert "HeadStartRanker" not in path.read_text(encoding="utf-8"), path


# Operations


@pytest.fixture(scope="module")
def web_routes(tmp_path_factory):
    """Building the app is what registers its routes, pages and operations."""
    runtime = Runtime(Paths(tmp_path_factory.mktemp("web")), clock=FixedClock(), transport=world(), sleep=no_sleep)
    app = web_app.create_app(runtime, background_refresh=False)
    yield app.routes
    runtime.close()


@pytest.mark.parametrize("surface", ops.SURFACES)
def test_every_operation_is_offered_on_every_interface(surface, web_routes):
    missing = set(ops.OPERATIONS) - ops.offered(surface)
    assert not missing, f"{surface} doesn't offer {sorted(missing)}. A feature isn't done until it's in all three."


@pytest.mark.parametrize("surface", ops.SURFACES)
def test_interfaces_only_claim_real_operations(surface, web_routes):
    assert not ops.offered(surface) - set(ops.OPERATIONS)


SERVICE_CLASSES = [
    services_app.SettingsService,
    services_app.BoardService,
    services_app.ChannelService,
    services_app.SeenService,
    services_app.TransferService,
    services_app.BackupService,
    services_refresh.RefreshService,
]
INTERNAL = {("ChannelService", "find"), ("RefreshService", "poll")}  # used by other services


@pytest.mark.parametrize("cls", SERVICE_CLASSES, ids=lambda c: c.__name__)
def test_every_public_service_method_is_an_operation(cls):
    for name, member in inspect.getmembers(cls, inspect.isfunction):
        if not name.startswith("_") and (cls.__name__, name) not in INTERNAL:
            assert hasattr(member, "__operation__"), f"{cls.__name__}.{name} isn't a registered operation"


def test_runtime_operations_are_registered():
    assert services_runtime.Runtime.profile_names.__operation__ == "profiles.list"


# Pages


def test_every_page_has_scenarios(web_routes):
    """So a new page can't escape the browser checks."""
    html_routes = {
        route.path
        for route in web_routes
        if "GET" in getattr(route, "methods", set()) and getattr(route, "response_class", None) is HTMLResponse
    }
    assert html_routes
    assert html_routes == set(PAGES), f"pages not declared: {html_routes ^ set(PAGES)}"
    assert all(page.scenarios for page in PAGES.values())


# Feeds and settings


def test_the_catalogue_is_the_only_place_prefixes_appear():
    prefixes = {spec.prefix for spec in CATALOGUE if len(spec.prefix) > 2}
    for path in SOURCE.rglob("*.py"):
        if path.name != "feeds.py" or "domain" not in path.parts:
            text = path.read_text(encoding="utf-8")
            for prefix in prefixes:
                assert not re.search(rf'["\']{prefix}', text), f"{path} hard-codes {prefix}"


def test_toggles_are_shown_from_the_catalogue():
    assert len(TOGGLES) == 4


# CSS


def css_files() -> list[Path]:
    return sorted((STATIC / "css").rglob("*.css"))


COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def test_no_raw_colours_outside_themes():
    for path in css_files():
        if "themes" not in path.parts:
            text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            assert not COLOUR.search(text), f"{path} uses a raw colour. Use a token."


def test_every_token_used_is_defined():
    defined: set[str] = set()
    used: set[str] = set()
    for path in css_files():
        text = path.read_text(encoding="utf-8")
        defined |= set(re.findall(r"(--[\w-]+)\s*:", text))
        used |= set(re.findall(r"var\((--[\w-]+)", text))
    assert not {u for u in used - defined if u not in {"--stack-gap", "--cluster-gap"}}


def test_themes_only_set_tokens():
    for path in (STATIC / "css" / "themes").glob("*.css"):
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        for declaration in re.findall(r"[{;]\s*([\w-]+)\s*:", text):
            assert declaration == "color-scheme" or declaration.startswith("--"), f"{path} sets {declaration}"


def test_every_theme_has_a_file_and_is_in_the_bundle():
    for key, _label in THEMES:
        assert (STATIC / "css" / "themes" / f"{key}.css").exists()
    every_file = {str(p.relative_to(STATIC / "css").with_suffix("")) for p in css_files()}
    assert every_file == set(web_app.CSS_ORDER), "a stylesheet exists that the bundle doesn't include, or the reverse"


def test_no_pills_or_glow():
    """The owner rejected these as stereotypically AI. Near-square corners and no glow."""
    for path in css_files():
        text = path.read_text(encoding="utf-8")
        for value in re.findall(r"border-radius:\s*([^;]+);", text):
            assert value.strip() in ("var(--radius)", "50%"), f"{path}: border-radius {value}"
        assert "box-shadow" not in text, path


def test_templates_have_no_inline_styles_or_scripts():
    """The content security policy would block them anyway. This says so before a browser does."""
    for path in (SOURCE / "web" / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert " style=" not in text, path
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path
        assert not re.search(r"\bon[a-z]+=", text), path


# A blank slate


def test_personal_folders_are_ignored_and_untracked():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/config/" in ignored
    assert "/data/" in ignored
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert not [f for f in tracked if f.startswith(("config/", "data/"))]


def test_no_commit_carries_an_ai_trailer_in_this_file_set():
    log = subprocess.run(["git", "log", "--format=%B"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert "Co-Authored-By" not in log
    assert "Generated with" not in log
