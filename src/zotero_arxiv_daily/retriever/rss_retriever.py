from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
import calendar
import re
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
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"</?[a-z][^>]*>", re.IGNORECASE | re.DOTALL)
HTML_SCRIPT_PATTERN = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
TRACKING_QUERY_KEYS = {"fbclid", "gclid"}


@dataclass
class RSSRawPaper:
    title: str
    authors: list[str]
    abstract: str
    url: str
    pdf_url: str | None
    doi: str | None
    published: datetime


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    text = HTML_SCRIPT_PATTERN.sub(" ", text)
    return " ".join(HTML_TAG_PATTERN.sub(" ", text).split())


def _resolve_http_url(value: Any, base_url: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return safe_http_url(urljoin(base_url, unescape(value).strip()))
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
    raw_authors = entry.get("authors") or []
    if isinstance(raw_authors, (str, dict)):
        raw_authors = [raw_authors]
    names = []
    for author in raw_authors:
        if isinstance(author, dict):
            name = _plain_text(author.get("name", ""))
        else:
            name = _plain_text(author)
        if name:
            names.append(name)
    if not names:
        author_detail = entry.get("author_detail") or {}
        author = entry.get("author") or (author_detail.get("name") if isinstance(author_detail, dict) else "")
        if name := _plain_text(author):
            names = [name]
    return names or ["Unknown"]


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


@register_retriever("rss")
class RSSRetriever(BaseRetriever):
    def _retrieve_raw_papers(self) -> list[RSSRawPaper]:
        feeds = self.retriever_config.get("feeds", []) or []
        if not isinstance(feeds, (list, tuple, ListConfig)):
            raise TypeError("source.rss.feeds must be a list of feed URLs")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        seen_keys: set[str] = set()
        papers: list[RSSRawPaper] = []
        for feed_url in feeds:
            safe_feed_url = safe_http_url(feed_url)
            if safe_feed_url is None:
                logger.warning(f"Skipping invalid RSS feed URL: {feed_url!r}")
                continue
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

            for entry in getattr(feed, "entries", []):
                published = _parse_entry_date(entry)
                if published is None:
                    logger.warning(f"Skipping RSS item without a published or updated date from {safe_feed_url}")
                    continue
                if published < cutoff or published > now:
                    continue
                title = _plain_text(entry.get("title"))
                if not title:
                    logger.warning(f"Skipping RSS item without a title from {safe_feed_url}")
                    continue
                article_url = _article_url(entry, response.url)
                pdf_url = _pdf_url(entry, response.url)
                if article_url is None and pdf_url:
                    article_url = pdf_url
                if article_url is None:
                    logger.warning(f"Skipping RSS item without a safe HTTP(S) article URL: {title}")
                    continue
                content = entry.get("content") or []
                content_value = content[0].get("value") if content and isinstance(content[0], dict) else ""
                abstract = _plain_text(entry.get("summary") or entry.get("description") or content_value)
                paper = RSSRawPaper(
                    title=title,
                    authors=_author_names(entry),
                    abstract=abstract,
                    url=article_url,
                    pdf_url=pdf_url,
                    doi=_doi(entry, article_url),
                    published=published,
                )
                keys = {f"url:{_canonical_url(paper.url)}"}
                if paper.doi:
                    keys.add(f"doi:{paper.doi.lower()}")
                duplicate = bool(seen_keys.intersection(keys))
                seen_keys.update(keys)
                if duplicate:
                    continue
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
        )
