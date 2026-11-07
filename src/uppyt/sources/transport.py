"""The only code that talks HTTP. Everything else asks a Transport, so tests can replay recordings."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from .. import __version__

ALLOWED_HOSTS = frozenset(
    {
        "www.youtube.com",
        "youtube.com",
        "m.youtube.com",
        "consent.youtube.com",
        "yt3.googleusercontent.com",
        "yt3.ggpht.com",
        "i.ytimg.com",
    }
)
IMAGE_HOSTS = frozenset({"yt3.googleusercontent.com", "yt3.ggpht.com", "i.ytimg.com"})
MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5


class TransportError(Exception):
    """The request didn't get an answer, or wasn't allowed. The message is written for people."""


@dataclass(frozen=True)
class Response:
    status: int
    content: bytes = b""
    content_type: str = ""
    url: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Transport(Protocol):
    def get(self, url: str, *, stop_after: int | None = None) -> Response: ...


def is_allowed(url: str, hosts: frozenset[str] = ALLOWED_HOSTS) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname in hosts and parts.port in (None, 443)
    except ValueError:
        return False


class HttpTransport:
    """Only YouTube's own hosts, over HTTPS, checked again at every redirect. Paced so polling stays polite."""

    def __init__(self, *, min_interval: float = 0.1, timeout: float = 20.0, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": f"uppYT/{__version__}"})
        # No cookies, ever. Besides being polite, keeping YouTube's cookie is what lands UK and EU visitors on its
        # consent page. Without one, the same redirect comes straight back to the page asked for.
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def _pace(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._min_interval
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, *, stop_after: int | None = None) -> Response:
        """With stop_after, stops reading once it has that many bytes, for when only the start of a page matters."""
        for _ in range(MAX_REDIRECTS + 1):
            if not is_allowed(url):
                raise TransportError(f"uppYT only fetches from YouTube, so it won't open {url[:80]}.")
            self._pace()
            try:
                request = self._client.build_request("GET", url)
                reply = self._client.send(request, stream=True)
                try:
                    if reply.is_redirect and "location" in reply.headers:
                        url = urljoin(url, reply.headers["location"])
                        continue
                    content = self._read_limited(reply, stop_after)
                finally:
                    reply.close()
            except httpx.HTTPError as error:
                raise TransportError(f"Couldn't reach YouTube: {error}") from error
            return Response(reply.status_code, content, reply.headers.get("content-type", ""), url)
        raise TransportError("YouTube redirected too many times.")

    @staticmethod
    def _read_limited(reply: httpx.Response, stop_after: int | None = None) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in reply.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise TransportError("YouTube sent more data than expected.")
            chunks.append(chunk)
            if stop_after is not None and size >= stop_after:
                break
        return b"".join(chunks)

    def close(self) -> None:
        self._client.close()
