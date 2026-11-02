from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
import calendar
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests
from loguru import logger
from omegaconf import ListConfig

from .base import BaseRetriever, register_retriever
from ..protocol import Paper
from ..url_utils import safe_http_url


RSS_REQUEST_TIMEOUT = 20
RSS_HISTORY_FILE = Path(".cache/rss_seen.json")
RSS_HISTORY_LIMIT = 10000
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"</?[a-z][^>]*>", re.IGNORECASE | re.DOTALL)
HTML_SCRIPT_PATTERN = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
TRACKING_QUERY_KEYS = {"fbclid", "gclid"}
NULL_VALUES = {"null", "none", "n/a", "na", "not available", "undefined"}
KNOWN_JOURNAL_FEED_SOURCES = {
    ("rss.sciencedirect.com", "/publication/science/09203796"): "Fusion Engineering and Design",
    ("iopscience.iop.org", "/journal/rss/0029-5515"): "Nuclear Fusion",
    ("iopscience.iop.org", "/journal/rss/0741-3335"): "Plasma Physics and Controlled Fusion",
    ("ieeexplore.ieee.org", "/rss/toc23.xml"): "IEEE Transactions on Nuclear Science",
    ("ieeexplore.ieee.org", "/rss/toc27.xml"): "IEEE Transactions on Plasma Science",
}
FEED_HOST_DISPLAY_NAMES = {
    "rss.sciencedirect.com": "ScienceDirect",
    "iopscience.iop.org": "IOPscience",
    "ieeexplore.ieee.org": "IEEE Xplore",
}
METADATA_LABELS = (
    "author", "authors", "author(s)", "publication date", "date", "journal", "source",
    "volume", "issue", "pages", "article number", "doi", "keywords", "abstract",
)


@dataclass
class RSSRawPaper:
    title: str
    authors: list[str]
    abstract: str
    url: str
    pdf_url: str | None
    doi: str | None
    published: datetime | None
    source_display: str | None = None


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    text = HTML_SCRIPT_PATTERN.sub(" ", text)
    return " ".join(HTML_TAG_PATTERN.sub(" ", text).split())


def _meaningful_text(value: Any) -> str:
    text = _plain_text(value)
    return "" if text.casefold() in NULL_VALUES else text


def _resolve_http_url(value: Any, base_url: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = unescape(value).strip()
    if not value:
        return None
    try:
        return safe_http_url(urljoin(base_url, value))
    except ValueError:
        return None


def _article_url(entry: Any, base_url: str) -> str | None:
    links = entry.get("links", []) or []
    for link in links:
        if not isinstance(link, dict):
            continue
        rel = (link.get("rel") or "alternate").lower()
        media_type = (link.get("type") or "").lower()
        if rel == "alternate" and "pdf" not in media_type:
            if url := _resolve_http_url(link.get("href"), base_url):
                return url

    if url := _resolve_http_url(entry.get("link"), base_url):
        return url
    if url := safe_http_url(entry.get("id")):
        return url
    return None


def _pdf_url(entry: Any, base_url: str) -> str | None:
    links = entry.get("links", []) or []
    for link in links:
        if not isinstance(link, dict):
            continue
        media_type = (link.get("type") or "").lower()
        rel = (link.get("rel") or "").lower()
        title = (link.get("title") or "").lower()
        href = link.get("href")
        safe_href = _resolve_http_url(href, base_url)
        if safe_href is None:
            continue
        path = urlsplit(safe_href).path.lower()
        if "pdf" in media_type or "pdf" in title or path.endswith(".pdf"):
            return safe_href
        if rel == "enclosure" and media_type in {"application/pdf", "application/x-pdf"}:
            return safe_href
    return None


def _parse_entry_date(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _author_names(entry: Any) -> list[str]:
    raw_authors = entry.get("authors") or entry.get("dc_creator") or []
    if isinstance(raw_authors, (str, dict)):
        raw_authors = [raw_authors]
    names = []
    for author in raw_authors:
        if isinstance(author, dict):
            name = _meaningful_text(author.get("name", ""))
        else:
            name = _meaningful_text(author)
        if ";" in name:
            names.extend(part.strip() for part in name.split(";") if part.strip())
        elif name:
            names.append(name)
    if not names:
        author_detail = entry.get("author_detail") or {}
        author = entry.get("author") or (author_detail.get("name") if isinstance(author_detail, dict) else "")
        name = _meaningful_text(author)
        if ";" in name:
            names = [part.strip() for part in name.split(";") if part.strip()]
        elif name:
            names = [name]
    return names or ["Unknown"]


def _feed_kind(feed_url: str) -> str:
    host = (urlsplit(feed_url).hostname or "").lower()
    if host == "ieeexplore.ieee.org" or host.endswith(".ieee.org"):
        return "ieee"
    if host == "rss.sciencedirect.com" or host.endswith(".sciencedirect.com"):
        return "sciencedirect"
    if host == "iopscience.iop.org":
        return "iop"
    return "generic"


def _known_journal_feed_source(feed_url: str) -> str | None:
    parsed = urlsplit(feed_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").casefold()
    return KNOWN_JOURNAL_FEED_SOURCES.get((host, path))


def _feed_source_display(feed_url: str) -> str:
    if known_source := _known_journal_feed_source(feed_url):
        return known_source
    host = (urlsplit(feed_url).hostname or "").lower()
    if host in FEED_HOST_DISPLAY_NAMES:
        return FEED_HOST_DISPLAY_NAMES[host]
    return host.removeprefix("www.") or "Unknown source"


def _item_source_display(entry: Any) -> str:
    publication_name = _meaningful_text(entry.get("prism_publicationname"))
    if publication_name:
        return publication_name
    item_source = entry.get("source")
    if isinstance(item_source, dict):
        item_source = item_source.get("title")
    return _meaningful_text(item_source)


def _display_source(entry: Any, feed: Any, feed_url: str) -> str:
    # Configured journal feeds have stable names that are more useful than
    # inconsistent feed and item metadata. Other feeds use their own metadata.
    if known_source := _known_journal_feed_source(feed_url):
        return known_source
    if item_source := _item_source_display(entry):
        return item_source
    feed_metadata = getattr(feed, "feed", {}) or {}
    feed_title = _meaningful_text(feed_metadata.get("title", "")) if hasattr(feed_metadata, "get") else ""
    return feed_title or _feed_source_display(feed_url)


def _extract_labeled_field(value: Any, label: str) -> str:
    text = _plain_text(value)
    labels = "|".join(re.escape(item) for item in METADATA_LABELS)
    match = re.search(
        rf"(?is)\b{re.escape(label)}\s*:\s*(.*?)(?=\s*(?:{labels})\s*:|$)",
        text,
    )
    return _meaningful_text(match.group(1)) if match else ""


def _metadata_authors_from_description(value: Any) -> list[str]:
    names = (
        _extract_labeled_field(value, "author(s)")
        or _extract_labeled_field(value, "authors")
        or _extract_labeled_field(value, "author")
    )
    if not names:
        return []
    return [part.strip() for part in re.split(r"\s*[;,]\s*", names) if part.strip()]


def _is_ieee_nonpaper_title(title: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    exact = {
        "table of contents",
        "blank page",
        "cover",
        "front cover",
        "back cover",
        "inside front cover",
        "inside back cover",
        "transactions on plasma science",
        "introducing ieee collabratec",
        "affiliate plan of the ieee nuclear and plasma sciences society",
    }
    return (
        not normalized
        or normalized in NULL_VALUES
        or normalized in exact
        or normalized.startswith("guest editorial")
        or normalized.endswith(" table of contents")
        or normalized.endswith(" publication information")
        or normalized.endswith(" information for authors")
        or normalized.endswith(" cover")
    )


def _abstract(entry: Any, feed_kind: str) -> str:
    content = entry.get("content") or []
    content_value = content[0].get("value") if content and isinstance(content[0], dict) else ""
    values = [entry.get("summary"), entry.get("description"), content_value]
    values = [value for value in values if _meaningful_text(value)]

    if feed_kind == "sciencedirect":
        for value in values:
            if abstract := _extract_labeled_field(value, "abstract"):
                return abstract
        for value in values:
            text = _meaningful_text(value)
            if not re.search(rf"(?i)\b(?:{'|'.join(re.escape(item) for item in METADATA_LABELS)})\s*:", text):
                return text
        return ""

    return _meaningful_text(values[0]) if values else ""


def _iop_pdf_url(entry: Any) -> str | None:
    value = entry.get("iop_pdf")
    if isinstance(value, dict):
        value = value.get("href") or value.get("url")
    return safe_http_url(_meaningful_text(value))


def _doi(entry: Any, article_url: str) -> str | None:
    candidates = [entry.get("doi"), entry.get("prism_doi"), entry.get("dc_identifier"), entry.get("id"), urlsplit(article_url).path]
    for candidate in candidates:
        if not candidate:
            continue
        match = DOI_PATTERN.search(unescape(str(candidate)))
        if match:
            return match.group(0).rstrip(".,;:").lower()
    return None


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        host = f"{host}:{port}"
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(query), ""))


def _entry_identifiers(entry: Any, article_url: str, doi: str | None) -> set[str]:
    identifiers = {f"url:{_canonical_url(article_url)}"}
    if doi:
        identifiers.add(f"doi:{doi.casefold()}")
    guid = _meaningful_text(entry.get("id") or entry.get("guid"))
    if guid:
        guid_url = safe_http_url(guid)
        if guid_url:
            identifiers.add(f"guid-url:{_canonical_url(guid_url)}")
        else:
            identifiers.add(f"guid:{' '.join(guid.split()).casefold()}")
    return identifiers


def _merge_identifier_group(groups: list[set[str]], identifiers: set[str]) -> bool:
    matches = [index for index, group in enumerate(groups) if group.intersection(identifiers)]
    if not matches:
        groups.append(set(identifiers))
        return False
    target = groups[matches[0]]
    target.update(identifiers)
    for index in reversed(matches[1:]):
        target.update(groups.pop(index))
    return True


@register_retriever("rss")
class RSSRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        self.updated_journal_feeds: set[str] = set()
        self._staged_history: dict[str, list[set[str]]] = {}

    def _load_history(self) -> dict[str, list[set[str]]] | None:
        history_path = RSS_HISTORY_FILE
        try:
            with history_path.open("r", encoding="utf-8") as history_file:
                state = json.load(history_file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"Cannot read RSS seen history at {history_path}: {exc}; journal feeds will be skipped")
            return None

        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("feeds"), dict):
            logger.error(f"Invalid RSS seen history at {history_path}; journal feeds will be skipped")
            return None
        history: dict[str, list[set[str]]] = {}
        try:
            for feed_key, groups in state["feeds"].items():
                if not isinstance(feed_key, str) or not isinstance(groups, list):
                    raise ValueError("invalid feed history record")
                history[feed_key] = []
                for group in groups:
                    if not isinstance(group, list) or any(not isinstance(item, str) for item in group):
                        raise ValueError("invalid item identity group")
                    history[feed_key].append(set(group))
        except ValueError as exc:
            logger.error(f"Invalid RSS seen history at {history_path}: {exc}; journal feeds will be skipped")
            return None
        return history

    def commit_history(self) -> None:
        if self.config.executor.debug or not self._staged_history:
            return
        history_path = RSS_HISTORY_FILE
        history = self._load_history()
        if history is None:
            raise RuntimeError("RSS seen history was not committed because the existing file could not be read")
        for feed_key, groups in self._staged_history.items():
            history[feed_key] = groups[-RSS_HISTORY_LIMIT:]
        state = {
            "version": 1,
            "feeds": {
                feed_key: [sorted(group) for group in groups]
                for feed_key, groups in history.items()
            },
        }
        temp_path = None
        try:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=history_path.parent,
                prefix=f".{history_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as history_file:
                temp_path = Path(history_file.name)
                json.dump(state, history_file, ensure_ascii=False, separators=(",", ":"))
                history_file.write("\n")
                history_file.flush()
                os.fsync(history_file.fileno())
            os.replace(temp_path, history_path)
        except OSError as exc:
            logger.error(f"Failed to atomically write RSS seen history at {history_path}: {exc}")
            if temp_path:
                temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to persist RSS seen history at {history_path}") from exc

    def _retrieve_raw_papers(self) -> list[RSSRawPaper]:
        feeds = self.retriever_config.get("feeds", []) or []
        if not isinstance(feeds, (list, tuple, ListConfig)):
            raise TypeError("source.rss.feeds must be a list of feed URLs")
        journal_feeds = self.retriever_config.get("journal_feeds", []) or []
        if not isinstance(journal_feeds, (list, tuple, ListConfig)):
            raise TypeError("source.rss.journal_feeds must be a list of feed URLs")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        seen_keys: set[str] = set()
        papers: list[RSSRawPaper] = []
        self.updated_journal_feeds = set()
        self._staged_history = {}
        history = None
        if journal_feeds and not self.config.executor.debug:
            history = self._load_history()

        source_by_key = {}
        for feed_url in feeds:
            safe_feed_url = safe_http_url(feed_url)
            key = _canonical_url(safe_feed_url) if safe_feed_url else str(feed_url)
            source_by_key[key] = (feed_url, False)
        if self.config.executor.debug and journal_feeds:
            logger.info("Skipping persistent journal RSS feeds in debug mode")
        for feed_url in journal_feeds:
            safe_feed_url = safe_http_url(feed_url)
            key = _canonical_url(safe_feed_url) if safe_feed_url else str(feed_url)
            source_by_key[key] = (feed_url, True)

        sources = list(source_by_key.values())

        for feed_url, is_journal in sources:
            if is_journal and (self.config.executor.debug or history is None):
                continue
            safe_feed_url = safe_http_url(feed_url)
            if safe_feed_url is None:
                logger.warning(f"Skipping invalid RSS feed URL: {feed_url!r}")
                continue
            feed_key = _canonical_url(safe_feed_url)
            try:
                response = requests.get(safe_feed_url, timeout=RSS_REQUEST_TIMEOUT)
                response.raise_for_status()
                feed = feedparser.parse(
                    response.content,
                    response_headers={"content-location": response.url},
                )
            except Exception as exc:
                logger.warning(f"Failed to fetch RSS feed {safe_feed_url}: {exc}")
                continue

            if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
                logger.warning(f"Failed to parse RSS feed {safe_feed_url}: {getattr(feed, 'bozo_exception', 'invalid feed')}")
                continue
            if not getattr(feed, "entries", None) and not getattr(feed, "version", ""):
                logger.warning(f"Response from {safe_feed_url} is not a recognized RSS/Atom feed; refusing an empty baseline")
                continue

            feed_kind = _feed_kind(response.url)
            initialized = is_journal and feed_key in history
            stored_groups = [set(group) for group in history.get(feed_key, [])] if initialized else []
            staged_groups = [set(group) for group in stored_groups]
            if is_journal:
                # Even a valid, empty first response is a completed baseline.
                self._staged_history[feed_key] = staged_groups

            for entry in getattr(feed, "entries", []):
                published = _parse_entry_date(entry)
                if not is_journal:
                    if published is None:
                        logger.warning(f"Skipping RSS item without a published or updated date from {safe_feed_url}")
                        continue
                    if published < cutoff or published > now:
                        continue
                title = _meaningful_text(entry.get("title"))
                if not title:
                    logger.warning(f"Skipping RSS item without a title from {safe_feed_url}")
                    continue
                if feed_kind == "ieee" and _is_ieee_nonpaper_title(title):
                    continue
                article_url = _article_url(entry, response.url)
                pdf_url = _pdf_url(entry, response.url)
                if article_url is None and pdf_url:
                    article_url = pdf_url
                if article_url is None:
                    logger.warning(f"Skipping RSS item without a safe HTTP(S) article URL: {title}")
                    continue
                doi = _doi(entry, article_url)
                if pdf_url is None and feed_kind == "iop":
                    pdf_url = _iop_pdf_url(entry)
                abstract = _abstract(entry, feed_kind)
                authors = _author_names(entry)
                if authors == ["Unknown"] and feed_kind == "sciencedirect":
                    metadata_values = [entry.get("description"), entry.get("summary")]
                    for value in metadata_values:
                        if metadata_authors := _metadata_authors_from_description(value):
                            authors = metadata_authors
                            break

                identifiers = _entry_identifiers(entry, article_url, doi)
                candidate = True
                if is_journal:
                    was_stored = any(identifiers.intersection(group) for group in stored_groups)
                    was_current = _merge_identifier_group(staged_groups, identifiers)
                    self._staged_history[feed_key] = staged_groups
                    candidate = initialized and not was_stored and not was_current
                    if candidate:
                        self.updated_journal_feeds.add(feed_key)

                if not candidate:
                    continue
                duplicate = bool(seen_keys.intersection(identifiers))
                seen_keys.update(identifiers)
                if duplicate:
                    continue
                paper = RSSRawPaper(
                    title=title,
                    authors=authors,
                    abstract=abstract,
                    url=article_url,
                    pdf_url=pdf_url,
                    doi=doi,
                    published=published,
                    source_display=_display_source(entry, feed, safe_feed_url),
                )
                papers.append(paper)

        if self.config.executor.debug:
            papers = papers[:10]
        return papers

    def convert_to_paper(self, raw_paper: RSSRawPaper) -> Paper:
        return Paper(
            source=self.name,
            title=raw_paper.title,
            authors=raw_paper.authors,
            abstract=raw_paper.abstract,
            url=raw_paper.url,
            pdf_url=raw_paper.pdf_url,
            source_display=raw_paper.source_display,
        )
