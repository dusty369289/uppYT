"""OPML, the file format RSS readers use to exchange lists of feeds."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import Element, ParseError, SubElement, indent, tostring

import defusedxml.ElementTree as SafeXML
from defusedxml import DefusedXmlException

from ..domain.feeds import FEED_ROOT, channel_feed_url, channel_id_from_playlist
from ..domain.models import CHANNEL_ID, Priority


class OpmlError(ValueError):
    pass


@dataclass(frozen=True)
class OpmlChannel:
    channel_id: str
    title: str
    tags: tuple[str, ...] = ()
    priority: Priority | None = None


def channel_id_from_feed_url(url: str) -> str | None:
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}{parts.path}".replace("http://", "https://") != FEED_ROOT:
        return None
    query = parse_qs(parts.query)
    if "channel_id" in query and CHANNEL_ID.match(query["channel_id"][0]):
        return query["channel_id"][0]
    if "playlist_id" in query:
        return channel_id_from_playlist(query["playlist_id"][0])
    return None


def parse_opml(data: str | bytes) -> tuple[list[OpmlChannel], int]:
    """The YouTube channels in an OPML file, and how many other feeds were skipped."""
    try:
        root = SafeXML.fromstring(data)
    except (ParseError, DefusedXmlException, ValueError) as error:
        raise OpmlError("That file isn't readable OPML.") from error
    if root.tag != "opml":
        raise OpmlError("That file isn't OPML.")
    found: dict[str, OpmlChannel] = {}
    skipped = 0
    for outline in root.iter("outline"):
        url = outline.get("xmlUrl")
        if not url:
            continue
        channel_id = channel_id_from_feed_url(url)
        if not channel_id:
            skipped += 1
            continue
        categories = [c.strip() for c in outline.get("category", "").replace("/", ",").split(",")]
        priority = None
        if outline.get("uppytPriority"):
            try:
                priority = Priority.parse(outline.get("uppytPriority", ""))
            except ValueError:
                priority = None
        found.setdefault(
            channel_id,
            OpmlChannel(
                channel_id,
                outline.get("title") or outline.get("text") or channel_id,
                tuple(c for c in categories if c),
                priority,
            ),
        )
    return list(found.values()), skipped


def write_opml(channels: list[OpmlChannel], title: str = "uppYT channels") -> str:
    root = Element("opml", version="2.0")
    SubElement(SubElement(root, "head"), "title").text = title
    body = SubElement(root, "body")
    for channel in channels:
        attributes = {
            "type": "rss",
            "text": channel.title,
            "title": channel.title,
            "xmlUrl": channel_feed_url(channel.channel_id),
        }
        if channel.tags:
            attributes["category"] = ",".join(channel.tags)
        if channel.priority is not None:
            attributes["uppytPriority"] = channel.priority.key
        SubElement(body, "outline", attributes)
    indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode") + "\n"
