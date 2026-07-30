#!/usr/bin/env python3
"""Deterministic, transactional news-digest collector.

The collector is deliberately independent of OpenClaw and Slack.  It fetches
and normalizes sources, records discovered items in SQLite, and selects a
small, diverse set of pending items.  Delivery code marks items emitted only
after the destination confirms each section.

Python 3.9+ and the standard library are sufficient.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import hmac
import html
import json
import math
import os
import posixpath
import random
import re
import sqlite3
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


USER_AGENT = "sparks-news-digest/2.0 (+https://github.com/catid/sparks)"
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_STATE_DB = os.path.join(os.path.dirname(__file__), ".news_digest.sqlite3")
X_CREDS_PATH = os.environ.get(
    "NEWS_DIGEST_X_CREDS_PATH",
    os.path.join(os.path.dirname(__file__), ".x_creds.json"),
)


@dataclass(frozen=True)
class FeedSpec:
    key: str
    name: str
    url: str
    categories: Tuple[str, ...]


@dataclass
class Item:
    uid: str
    source: str
    source_key: str
    categories: Tuple[str, ...]
    title: str
    url: str
    summary: str
    published_at: float
    raw_id: str = ""
    authors: Tuple[str, ...] = ()
    institutes: Tuple[str, ...] = ()
    affiliation: str = ""
    engagement: int = 0
    tags: Tuple[str, ...] = ()
    date_inferred: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "source": self.source,
            "source_key": self.source_key,
            "categories": list(self.categories),
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "published_at": self.published_at,
            "published": format_timestamp(self.published_at),
            "raw_id": self.raw_id,
            "authors": list(self.authors),
            "institutes": list(self.institutes),
            "affiliation": self.affiliation,
            "engagement": self.engagement,
            "tags": list(self.tags),
            "date_inferred": self.date_inferred,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Item":
        return cls(
            uid=str(data.get("uid") or ""),
            source=str(data.get("source") or ""),
            source_key=str(data.get("source_key") or ""),
            categories=tuple(str(value) for value in (data.get("categories") or [])),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            summary=str(data.get("summary") or ""),
            published_at=float(data.get("published_at") or 0),
            raw_id=str(data.get("raw_id") or ""),
            authors=tuple(str(value) for value in (data.get("authors") or [])),
            institutes=tuple(str(value) for value in (data.get("institutes") or [])),
            affiliation=str(data.get("affiliation") or ""),
            engagement=int(data.get("engagement") or 0),
            tags=tuple(str(value) for value in (data.get("tags") or [])),
            date_inferred=bool(data.get("date_inferred")),
        )


@dataclass
class SourceStatus:
    source: str
    ok: bool
    item_count: int
    elapsed_ms: int
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "item_count": self.item_count,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


@dataclass
class Digest:
    sections: Dict[str, List[Item]]
    statuses: List[SourceStatus]
    discovered_count: int
    eligible_count: int
    selected_count: int
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "generated": format_timestamp(self.generated_at),
            "discovered_count": self.discovered_count,
            "eligible_count": self.eligible_count,
            "selected_count": self.selected_count,
            "sections": {
                category: [item.to_dict() for item in items]
                for category, items in self.sections.items()
            },
            "statuses": [status.to_dict() for status in self.statuses],
        }


CATEGORY_ORDER = (
    "AI / LLMs",
    "Language Models / NLP",
    "Reinforcement Learning",
    "Generative Models",
    "Robotics",
    "Science",
    "Space",
    "Math",
    "Startups / Tech",
    "🤗 HF Models",
    "📦 ML Infra Releases",
    "🐦 X Lab Accounts",
    "📄 HF Daily Papers",
    "💭 Following Feed",
    "🔥 Key People",
    "📐 JMLR / Learning Theory",
    "🔧 NVIDIA Technical Blog",
)


RSS_SOURCES = (
    FeedSpec("arxiv-cs-ai", "ArXiv cs.AI", "https://arxiv.org/rss/cs.AI",
             ("AI / LLMs", "Reinforcement Learning")),
    FeedSpec("arxiv-cs-cl", "ArXiv cs.CL", "https://arxiv.org/rss/cs.CL",
             ("AI / LLMs", "Language Models / NLP")),
    FeedSpec("hf-blog", "Hugging Face Blog", "https://huggingface.co/blog/feed.xml",
             ("AI / LLMs",)),
    FeedSpec("anthropic-news", "Anthropic News mirror",
             "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
             ("Language Models / NLP",)),
    FeedSpec("arxiv-cs-lg", "ArXiv cs.LG", "https://arxiv.org/rss/cs.LG",
             ("Reinforcement Learning", "Generative Models")),
    FeedSpec("arxiv-stat-ml", "ArXiv stat.ML", "https://arxiv.org/rss/stat.ML",
             ("Reinforcement Learning", "Generative Models")),
    FeedSpec("deepmind-blog", "Google DeepMind Blog", "https://deepmind.google/blog/rss.xml",
             ("Reinforcement Learning",)),
    FeedSpec("arxiv-cs-cv", "ArXiv cs.CV", "https://arxiv.org/rss/cs.CV",
             ("Generative Models",)),
    FeedSpec("openai-news", "OpenAI News", "https://openai.com/news/rss.xml",
             ("Generative Models", "AI / LLMs")),
    FeedSpec("arxiv-cs-ro", "ArXiv cs.RO", "https://arxiv.org/rss/cs.RO",
             ("Robotics",)),
    FeedSpec("ieee-robotics", "IEEE Spectrum Robotics",
             "https://spectrum.ieee.org/feeds/topic/robotics.rss", ("Robotics",)),
    FeedSpec("arxiv-pop-physics", "ArXiv Popular Physics",
             "https://arxiv.org/rss/physics.pop-ph", ("Science",)),
    FeedSpec("nature", "Nature", "https://www.nature.com/nature.rss", ("Science",)),
    FeedSpec("science-daily", "ScienceDaily", "https://www.sciencedaily.com/rss/all.xml",
             ("Science",)),
    FeedSpec("nasa", "NASA", "https://www.nasa.gov/rss/dyn/breaking_news.rss", ("Space",)),
    FeedSpec("arxiv-astro", "ArXiv Astrophysics", "https://arxiv.org/rss/astro-ph",
             ("Space",)),
    FeedSpec("arxiv-math", "ArXiv Mathematics", "https://arxiv.org/rss/math", ("Math",)),
    FeedSpec("quanta", "Quanta Magazine", "https://www.quantamagazine.org/feed/", ("Math",)),
    FeedSpec("hacker-news", "Hacker News", "https://hnrss.org/frontpage",
             ("Startups / Tech",)),
    FeedSpec("techcrunch", "TechCrunch", "https://techcrunch.com/feed/",
             ("Startups / Tech",)),
    FeedSpec("ars-technica", "Ars Technica", "https://arstechnica.com/feed/",
             ("Startups / Tech",)),
    FeedSpec("vllm-releases", "vLLM releases",
             "https://github.com/vllm-project/vllm/releases.atom", ("📦 ML Infra Releases",)),
    FeedSpec("sglang-releases", "SGLang releases",
             "https://github.com/sgl-project/sglang/releases.atom", ("📦 ML Infra Releases",)),
    FeedSpec("transformers-releases", "Transformers releases",
             "https://github.com/huggingface/transformers/releases.atom",
             ("📦 ML Infra Releases",)),
    FeedSpec("triton-releases", "Triton releases",
             "https://github.com/triton-lang/triton/releases.atom", ("📦 ML Infra Releases",)),
    FeedSpec("flash-attention-releases", "FlashAttention releases",
             "https://github.com/Dao-AILab/flash-attention/releases.atom",
             ("📦 ML Infra Releases",)),
    FeedSpec("mamba-releases", "Mamba releases",
             "https://github.com/state-spaces/mamba/releases.atom", ("📦 ML Infra Releases",)),
    FeedSpec("jmlr", "JMLR", "https://www.jmlr.org/jmlr.xml",
             ("📐 JMLR / Learning Theory",)),
    FeedSpec("nvidia-blog", "NVIDIA Technical Blog", "https://developer.nvidia.com/blog/feed/",
             ("🔧 NVIDIA Technical Blog",)),
)


X_LAB_ACCOUNTS = {
    "DeepSeek": "1714580962569588736",
    "OpenAI": "4398626122",
    "Anthropic": "1353836358901501952",
    "Google DeepMind": "4783690002",
    "Mistral AI": "1667249535519805451",
    "xAI": "2074186776130859008",
    "Meta Research": "88237382",
    "Baidu Research": "2502019975",
    "ZhipuAI": "1716260935805841408",
}


X_KEY_PEOPLE = {
    "karpathy": "33836629",
    "ilyasut": "1720046887",
    "sama": "1605",
    "gdb": "162124540",
    "demishassabis": "1482581556",
    "fchollet": "68746721",
    "ylecun": "48008938",
    "geoffreyhinton": "1084212657761148928",
    "ID_AA_Carmack": "175624200",
    "PalmerLuckey": "294306372",
    "VitalikButerin": "295218901",
    "pmarca": "5943622",
    "natfriedman": "13235832",
    "rasbt": "865622395",
    "jeremyphoward": "175282603",
    "giffmana": "2236047510",
    "svlevine": "990433714948661250",
    "tri_dao": "568879807",
    "hardmaru": "2895499182",
    "danijarh": "1658829246",
    "natolambert": "2939913921",
    "tomgoldsteincs": "1086729872305766401",
    "denny_zhou": "1651242848",
    "stratechery": "1233663476",
    "ggerganov": "3300401027",
    "antirez": "5813712",
    "huggingface": "778764142412984320",
}


INTEREST_TAGS = (
    ("🔥 Optimizers/Training", (
        "optimizer", "muon", "adam", "adamw", "ademamix", "schedule-free",
        "learning rate", "weight decay", "loss landscape", "gradient descent",
        "second-order", "unit scaling", "maximal update", "training dynamics",
        "pretraining", "pre-training", "newton", "pion",
    )),
    ("🏗️ Efficient Architectures", (
        "subquadratic", "linear attention", "state space", "mamba", "griffin",
        "gated delta", "lightning attention", "sparse attention",
        "mixture-of-experts", "mixture of experts", "moe", "kv cache",
        "speculative decoding", "efficient transformer", "attention sink",
    )),
    ("🎮 RL / Agents", (
        "reinforcement learning", "deep rl", "rl", "dapo", "grpo", "ppo",
        "policy optimization", "reward", "credit assignment", "agentic",
        "llm agent", "coding agent", "tool use", "dpo", "model-based rl",
        "offline rl", "language agent", "tree search",
    )),
    ("🧠 World Models / Repr.", (
        "world model", "jepa", "joint embedding", "latent prediction",
        "state prediction", "representation learning", "disentangle", "attractor",
    )),
    ("✂️ Pruning / Sparsity", (
        "pruning", "prune", "lottery ticket", "sparse", "sparsity",
        "structured pruning", "compression", "sparsegpt",
    )),
    ("🔢 Quantization / Compression", (
        "quantization", "quantize", "bitnet", "binary neural", "int8", "int4",
        "nvfp4", "fp8", "low-precision", "smoothquant", "bfloat16",
    )),
    ("🎨 Generative Models", (
        "diffusion model", "flow matching", "diffusion policy",
        "autoregressive image", "latent diffusion", "generative", "image gen",
    )),
)


TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "ref_url",
}


class FetchError(RuntimeError):
    pass


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in ("br", "p", "div", "li"):
            self.parts.append(" ")

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def clean_text(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    raw = html.unescape(str(value))
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def parse_timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def format_timestamp(timestamp: float) -> str:
    if not timestamp:
        return ""
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonicalize_url(url: str) -> str:
    raw = html.unescape(str(url or "")).strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = "%s:%d" % (host, port)
    else:
        netloc = host
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    trailing_slash = path.endswith("/")
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = "/" + path
    if trailing_slash and path != "/":
        path += "/"

    arxiv_match = re.search(r"/(?:abs|pdf)/([a-z-]+/\d+|\d{4}\.\d+)(?:v\d+)?(?:\.pdf)?$", path, re.I)
    if host in ("arxiv.org", "www.arxiv.org", "export.arxiv.org") and arxiv_match:
        return "https://arxiv.org/abs/" + arxiv_match.group(1)

    tweet_match = re.search(r"/status/(\d+)", path)
    if host in ("twitter.com", "www.twitter.com", "x.com", "www.x.com") and tweet_match:
        return "https://x.com/i/web/status/" + tweet_match.group(1)

    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query.append((key, value))
    query.sort()
    encoded_query = urllib.parse.urlencode(query, doseq=True)
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urllib.parse.urlunsplit((scheme, netloc, path, encoded_query, ""))


def item_id(source: str, raw_id: str, url: str, title: str, published_at: float) -> str:
    canonical = canonicalize_url(url)
    raw = str(raw_id or "").strip()
    canonical_raw = canonicalize_url(raw)
    if canonical_raw:
        identity = "url\0" + canonical_raw
    elif raw:
        identity = "raw\0%s\0%s" % (source, raw)
    elif canonical:
        identity = "url\0" + canonical
    else:
        identity = "fallback\0%s\0%s\0%d" % (
            source, clean_text(title, 500), int(published_at or 0),
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _direct_child(element: ET.Element, *names: str) -> Optional[ET.Element]:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return child
    return None


def _child_text(element: ET.Element, *names: str) -> str:
    child = _direct_child(element, *names)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _all_child_text(element: ET.Element, *names: str) -> Tuple[str, ...]:
    wanted = {name.lower() for name in names}
    values = []
    for child in list(element):
        if _local_name(child.tag) in wanted:
            value = clean_text("".join(child.itertext()), 500)
            if value:
                values.append(value)
    return tuple(dict.fromkeys(values))


def _make_feed_item(
    spec: FeedSpec,
    raw_id: str,
    title: str,
    url: str,
    summary: str,
    published_at: float,
    authors: Sequence[str],
) -> Optional[Item]:
    clean_title = clean_text(title, 500)
    clean_summary = clean_text(summary, 2400)
    canonical = canonicalize_url(url)
    if not clean_title:
        clean_title = clean_text(clean_summary, 180)
    if not clean_title:
        return None
    uid = item_id(spec.key, raw_id, canonical, clean_title, published_at)
    return Item(
        uid=uid,
        source=spec.name,
        source_key=spec.key,
        categories=spec.categories,
        title=clean_title,
        url=canonical,
        summary=clean_summary,
        published_at=published_at,
        raw_id=str(raw_id or "").strip(),
        authors=tuple(authors),
        tags=match_interest_tags(clean_title, clean_summary),
    )


def parse_feed(xml_text: Any, spec: FeedSpec) -> List[Item]:
    """Parse RSS 2.0, RSS 1.0/RDF, or Atom into normalized items."""
    if isinstance(xml_text, bytes):
        payload = xml_text
    else:
        payload = str(xml_text or "").encode("utf-8")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError) as exc:
        raise ValueError("invalid XML: %s" % exc) from exc

    root_name = _local_name(root.tag)
    entries: List[Tuple[ET.Element, bool]] = []
    if root_name == "feed":
        entries = [(entry, True) for entry in root.iter() if _local_name(entry.tag) == "entry"]
    elif root_name == "rdf":
        entries = [(entry, False) for entry in list(root) if _local_name(entry.tag) == "item"]
    elif root_name == "rss":
        channel = _direct_child(root, "channel")
        if channel is not None:
            entries = [(entry, False) for entry in list(channel) if _local_name(entry.tag) == "item"]
    else:
        entries = [(entry, False) for entry in root.iter() if _local_name(entry.tag) == "item"]

    items: List[Item] = []
    for entry, is_atom in entries:
        title = _child_text(entry, "title")
        if re.match(r"^(trunk|ciflow|gh-readonly-queue)", title or "", re.I):
            continue
        raw_id = _child_text(entry, "id", "guid")
        url = ""
        if is_atom:
            fallback = ""
            for link in list(entry):
                if _local_name(link.tag) != "link":
                    continue
                href = str(link.attrib.get("href", "")).strip()
                if not href:
                    continue
                if not fallback:
                    fallback = href
                if link.attrib.get("rel", "alternate") == "alternate":
                    url = href
                    break
            url = url or fallback
        else:
            url = _child_text(entry, "link")
        if not raw_id:
            raw_id = url
        summary = ""
        for field_group in (("encoded", "content"), ("summary", "description")):
            candidates = [
                _child_text(entry, field_name)
                for field_name in field_group
            ]
            candidates = [candidate for candidate in candidates if candidate.strip()]
            if candidates:
                summary = max(candidates, key=lambda value: len(clean_text(value)))
                break
        date_value = ""
        for field_name in ("published", "pubdate", "updated", "date"):
            date_value = _child_text(entry, field_name)
            if date_value:
                break
        if is_atom:
            authors = []
            for author in list(entry):
                if _local_name(author.tag) != "author":
                    continue
                name = _child_text(author, "name") or clean_text("".join(author.itertext()), 200)
                if name:
                    authors.append(name)
        else:
            authors = list(_all_child_text(entry, "creator", "author"))
        item = _make_feed_item(
            spec, raw_id, title, url, summary, parse_timestamp(date_value), authors,
        )
        if item is not None:
            items.append(item)
    return items


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %d" % exc.code
    if isinstance(exc, urllib.error.URLError):
        return "network error: %s" % clean_text(exc.reason, 120)
    if isinstance(exc, FetchError):
        return clean_text(str(exc), 160)
    return "%s: %s" % (exc.__class__.__name__, clean_text(str(exc), 120))


def fetch_url(
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    retries: int = 1,
) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, application/json, */*",
    }
    if headers:
        request_headers.update(dict(headers))
    last_error: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise FetchError("response exceeds %d bytes" % max_bytes)
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise FetchError("response exceeds %d bytes" % max_bytes)
                if not body.strip():
                    raise FetchError("empty response")
                return body
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 or 400 <= exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError, FetchError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.4 * (2 ** attempt) + random.random() * 0.2)
    assert last_error is not None
    raise FetchError(_safe_error(last_error))


def _fetch_rss(spec: FeedSpec) -> Tuple[List[Item], SourceStatus]:
    started = time.monotonic()
    try:
        body = fetch_url(spec.url)
        items = parse_feed(body, spec)
        elapsed = int((time.monotonic() - started) * 1000)
        return items, SourceStatus(spec.name, True, len(items), elapsed)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return [], SourceStatus(spec.name, False, 0, elapsed, _safe_error(exc))


def format_params(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return "?"
    if number >= 1_000_000_000:
        return "%.1fB" % (number / 1_000_000_000)
    if number >= 1_000_000:
        return "%.0fM" % (number / 1_000_000)
    if number > 0:
        return str(number)
    return "?"


def parse_hf_trending(data: Any) -> List[Item]:
    entries = data.get("recentlyTrending", []) if isinstance(data, dict) else []
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        repo = entry.get("repoData") or {}
        repo_type = entry.get("repoType") or repo.get("repoType")
        if repo_type != "model":
            continue
        model_id = str(repo.get("id") or "").strip()
        if not model_id:
            continue
        author = str(repo.get("author") or model_id.split("/", 1)[0]).strip()
        author_data = repo.get("authorData") or {}
        organization = clean_text(
            repo.get("organization")
            or (author_data.get("fullname") if author_data.get("type") == "org" else ""),
            200,
        )
        likes = int(repo.get("likes") or 0)
        downloads = int(repo.get("downloads") or 0)
        parameters = repo.get("numParameters")
        pipeline = str(repo.get("pipeline_tag") or "N/A")
        updated = parse_timestamp(repo.get("lastModified"))
        summary = (
            "Pipeline: %s · Parameters: %s · Likes: %s · Total downloads: %s"
            % (pipeline, format_params(parameters), format(likes, ","), format(downloads, ","))
        )
        url = canonicalize_url("https://huggingface.co/" + model_id)
        items.append(Item(
            uid=item_id("hf-model", model_id, url, model_id, updated),
            source="HF / " + author,
            source_key="hf-model:" + author.lower(),
            categories=("🤗 HF Models",),
            title=model_id,
            url=url,
            summary=summary,
            published_at=updated,
            raw_id=model_id,
            authors=(author,) if author else (),
            institutes=(organization,) if organization else (),
            affiliation=organization,
            engagement=likes,
            tags=match_interest_tags(model_id, summary),
        ))
    return items


def _normalize_hf_authors(value: Any) -> Tuple[str, ...]:
    authors = []
    if isinstance(value, list):
        for author in value:
            if isinstance(author, dict):
                name = author.get("name") or author.get("user") or author.get("username")
            else:
                name = author
            cleaned = clean_text(name, 200)
            if cleaned:
                authors.append(cleaned)
    return tuple(dict.fromkeys(authors))


def _normalize_organization(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("name")
            or value.get("fullname")
            or value.get("displayName")
            or value.get("user")
        )
    return clean_text(value, 200)


def parse_hf_daily_papers(data: Any) -> List[Item]:
    if not isinstance(data, list):
        return []
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        paper = entry.get("paper") or {}
        paper_id = str(paper.get("id") or "").strip()
        title = clean_text(paper.get("title") or entry.get("title"), 500)
        if not paper_id or not title:
            continue
        summary_text = clean_text(paper.get("summary") or entry.get("summary"), 2000)
        upvotes = int(paper.get("upvotes") or 0)
        authors = _normalize_hf_authors(paper.get("authors"))
        organization = _normalize_organization(
            paper.get("organization") or entry.get("organization")
        )
        published = parse_timestamp(
            paper.get("publishedAt") or entry.get("publishedAt") or paper.get("submittedOnDailyAt")
        )
        summary = ("%d upvotes · " % upvotes if upvotes else "") + summary_text
        url = canonicalize_url("https://huggingface.co/papers/" + paper_id)
        items.append(Item(
            uid=item_id("hf-paper", paper_id, url, title, published),
            source="Hugging Face Daily Papers",
            source_key="hf-daily-papers",
            categories=("📄 HF Daily Papers",),
            title=title,
            url=url,
            summary=summary,
            published_at=published,
            raw_id=paper_id,
            authors=authors,
            affiliation=organization,
            engagement=upvotes,
            tags=match_interest_tags(title, summary_text),
        ))
    return items


def parse_pytorch_releases(data: Any) -> List[Item]:
    """Normalize official GitHub Release API objects for PyTorch."""
    if not isinstance(data, list):
        return []
    items = []
    for release in data:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = clean_text(release.get("tag_name") or release.get("name"), 200)
        url = canonicalize_url(str(release.get("html_url") or ""))
        if not tag or not url:
            continue
        published = parse_timestamp(
            release.get("published_at") or release.get("created_at")
        )
        body = clean_text(release.get("body"), 1600)
        author_data = release.get("author") or {}
        author = clean_text(
            author_data.get("login") if isinstance(author_data, dict) else "",
            100,
        )
        prerelease = bool(release.get("prerelease"))
        summary = ("Pre-release · " if prerelease else "") + body
        items.append(Item(
            uid=item_id("pytorch-release", tag, url, tag, published),
            source="PyTorch releases",
            source_key="pytorch-releases",
            categories=("📦 ML Infra Releases",),
            title=tag,
            url=url,
            summary=summary,
            published_at=published,
            raw_id=tag,
            authors=(author,) if author else (),
            tags=match_interest_tags(tag, summary),
        ))
    return items


def _fetch_json_adapter(
    source: str,
    url: str,
    parser: Any,
) -> Tuple[List[Item], SourceStatus]:
    started = time.monotonic()
    try:
        body = fetch_url(url, headers={"Accept": "application/json"})
        data = json.loads(body.decode("utf-8"))
        items = parser(data)
        elapsed = int((time.monotonic() - started) * 1000)
        return items, SourceStatus(source, True, len(items), elapsed)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return [], SourceStatus(source, False, 0, elapsed, _safe_error(exc))


def load_x_credentials(path: str = X_CREDS_PATH) -> Dict[str, Any]:
    try:
        stat_result = os.stat(path)
        if stat_result.st_mode & 0o077:
            raise PermissionError("X credential file must be mode 0600")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def expand_x_urls(text: str, entities: Any) -> str:
    expanded = html.unescape(str(text or ""))
    if isinstance(entities, dict):
        for entity in entities.get("urls") or []:
            if not isinstance(entity, dict):
                continue
            short = str(entity.get("url") or "")
            target = str(
                entity.get("unwound_url") or entity.get("expanded_url") or entity.get("display_url") or ""
            )
            if short and target:
                expanded = expanded.replace(short, target)
    return clean_text(expanded, 2000)


def is_x_media_only(text: str) -> bool:
    """Return true when a post contains only an X-hosted photo/video URL."""
    value = clean_text(text, 2000)
    urls = re.findall(r"https?://[^\s]+", value)
    if not urls or re.sub(r"https?://[^\s]+", "", value).strip():
        return False
    for url in urls:
        try:
            parsed = urllib.parse.urlsplit(url.rstrip(".,;:!?"))
        except ValueError:
            return False
        if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            return False
        if not re.search(r"/(?:photo|video)/\d+", parsed.path):
            return False
    return True


def extract_x_affiliation(bio: str) -> str:
    text = clean_text(bio, 500)
    if not text:
        return ""
    patterns = (
        r"\b(?:ceo|cto|chief|founder|co-founder|president|vp|researcher|scientist|engineer|professor|lead)"
        r"\b.{0,45}?\b(?:at|of)\s+([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,3})",
        r"\b(?:ceo|cto|chief|founder|co-founder|president|vp|researcher|scientist|engineer|professor|lead)"
        r"\b.{0,45}?@([A-Za-z0-9_]{2,30})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        value = match.group(1).strip(" .,-")
        if value.lower() not in {"the", "this", "that", "me", "my", "here"}:
            return ("@" + value) if "@" in pattern else value
    return ""


def _tweet_to_item(
    tweet: Mapping[str, Any],
    users: Mapping[str, Mapping[str, Any]],
    category: str,
    fallback_handle: str,
    lab_affiliation: str = "",
) -> Optional[Item]:
    tweet_id = str(tweet.get("id") or "").strip()
    if not tweet_id:
        return None
    references = tweet.get("referenced_tweets") or []
    if any(isinstance(ref, dict) and ref.get("type") == "replied_to" for ref in references):
        return None
    user = users.get(str(tweet.get("author_id") or ""), {})
    handle = clean_text(user.get("username") or fallback_handle, 80).lstrip("@")
    name = clean_text(user.get("name") or handle, 120)
    affiliation = lab_affiliation or extract_x_affiliation(str(user.get("description") or ""))
    text = expand_x_urls(str(tweet.get("text") or ""), tweet.get("entities"))
    if not text or is_x_media_only(text):
        return None
    published = parse_timestamp(tweet.get("created_at"))
    url = canonicalize_url("https://x.com/i/web/status/" + tweet_id)
    title = "@%s: %s" % (handle or "unknown", clean_text(text, 180))
    summary_prefix = name
    if affiliation and affiliation.lower() != name.lower():
        summary_prefix += " · " + affiliation
    summary = summary_prefix + ": " + text
    return Item(
        uid=item_id("x", tweet_id, url, title, published),
        source="X @%s" % (handle or fallback_handle),
        source_key="x:" + (handle or fallback_handle).lower(),
        categories=(category,),
        title=title,
        url=url,
        summary=summary,
        published_at=published,
        raw_id=tweet_id,
        authors=(name,) if name else (),
        affiliation=affiliation,
        tags=match_interest_tags(title, text),
    )


def _fetch_x_account(
    display_name: str,
    user_id: str,
    category: str,
    bearer_token: str,
) -> Tuple[List[Item], str]:
    query = urllib.parse.urlencode({
        "max_results": "5",
        "exclude": "replies,retweets",
        "tweet.fields": "created_at,text,author_id,entities,referenced_tweets",
        "expansions": "author_id",
        "user.fields": "username,name,description",
    })
    url = "https://api.twitter.com/2/users/%s/tweets?%s" % (user_id, query)
    body = fetch_url(url, headers={"Authorization": "Bearer " + bearer_token}, retries=0)
    data = json.loads(body.decode("utf-8"))
    users = {
        str(user.get("id")): user
        for user in (data.get("includes", {}).get("users", []) or [])
        if isinstance(user, dict)
    }
    items = []
    for tweet in data.get("data", []) or []:
        item = _tweet_to_item(
            tweet, users, category, display_name,
            lab_affiliation=display_name if category == "🐦 X Lab Accounts" else "",
        )
        if item is not None:
            items.append(item)
    return items, ""


def _fetch_x_accounts(
    source_name: str,
    accounts: Mapping[str, str],
    category: str,
    credentials: Mapping[str, Any],
) -> Tuple[List[Item], SourceStatus]:
    started = time.monotonic()
    token = str(credentials.get("x_bearer_token") or "").strip()
    if not token:
        return [], SourceStatus(source_name, False, 0, 0, "missing X bearer token")
    items: List[Item] = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(_fetch_x_account, name, user_id, category, token): name
            for name, user_id in accounts.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            name = future_map[future]
            try:
                account_items, _ = future.result()
                items.extend(account_items)
            except Exception as exc:
                errors.append("%s: %s" % (name, _safe_error(exc)))
    elapsed = int((time.monotonic() - started) * 1000)
    ok = not errors
    error = "; ".join(errors[:4])
    if len(errors) > 4:
        error += "; +%d more" % (len(errors) - 4)
    return items, SourceStatus(source_name, ok, len(items), elapsed, error)


def _oauth_signature(
    method: str,
    url: str,
    params: Mapping[str, str],
    consumer_secret: str,
    token_secret: str,
) -> str:
    encoded = [
        (urllib.parse.quote(str(key), safe=""), urllib.parse.quote(str(value), safe=""))
        for key, value in params.items()
    ]
    encoded.sort()
    parameter_string = "&".join("%s=%s" % pair for pair in encoded)
    base = "&".join((
        method.upper(),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote(parameter_string, safe=""),
    ))
    key = "%s&%s" % (
        urllib.parse.quote(consumer_secret, safe=""),
        urllib.parse.quote(token_secret, safe=""),
    )
    digest = hmac.new(key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _fetch_x_home(credentials: Mapping[str, Any]) -> Tuple[List[Item], SourceStatus]:
    started = time.monotonic()
    required = (
        "x_consumer_key", "x_consumer_secret", "x_access_token",
        "x_access_secret", "x_user_id",
    )
    if not all(credentials.get(key) for key in required):
        return [], SourceStatus("X Following Feed", False, 0, 0, "missing X OAuth credentials")
    endpoint = "https://api.twitter.com/2/users/%s/timelines/reverse_chronological" % credentials["x_user_id"]
    query_params = {
        "max_results": "50",
        "tweet.fields": "created_at,text,author_id,entities,referenced_tweets",
        "expansions": "author_id",
        "user.fields": "username,name,description",
    }
    oauth_params = {
        "oauth_consumer_key": str(credentials["x_consumer_key"]),
        "oauth_nonce": "".join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(32)),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": str(credentials["x_access_token"]),
        "oauth_version": "1.0",
    }
    signature_params = dict(query_params)
    signature_params.update(oauth_params)
    oauth_params["oauth_signature"] = _oauth_signature(
        "GET", endpoint, signature_params,
        str(credentials["x_consumer_secret"]), str(credentials["x_access_secret"]),
    )
    auth = "OAuth " + ", ".join(
        '%s="%s"' % (key, urllib.parse.quote(value, safe=""))
        for key, value in sorted(oauth_params.items())
    )
    url = endpoint + "?" + urllib.parse.urlencode(query_params)
    try:
        body = fetch_url(url, headers={"Authorization": auth}, retries=0)
        data = json.loads(body.decode("utf-8"))
        users = {
            str(user.get("id")): user
            for user in (data.get("includes", {}).get("users", []) or [])
            if isinstance(user, dict)
        }
        items = []
        for tweet in data.get("data", []) or []:
            item = _tweet_to_item(tweet, users, "💭 Following Feed", "unknown")
            if item is not None:
                items.append(item)
        elapsed = int((time.monotonic() - started) * 1000)
        return items, SourceStatus("X Following Feed", True, len(items), elapsed)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return [], SourceStatus("X Following Feed", False, 0, elapsed, _safe_error(exc))


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    if re.fullmatch(r"[a-z0-9+-]+", keyword, re.I):
        return re.compile(r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])", re.I)
    return re.compile(escaped, re.I)


_COMPILED_TAGS = tuple(
    (tag, tuple(_keyword_pattern(keyword) for keyword in keywords))
    for tag, keywords in INTEREST_TAGS
)


def match_interest_tags(title: str, summary: str) -> Tuple[str, ...]:
    text = "%s %s" % (title or "", summary or "")
    matched = []
    for tag, patterns in _COMPILED_TAGS:
        if any(pattern.search(text) for pattern in patterns):
            matched.append(tag)
    return tuple(matched)


class StateDB:
    def __init__(self, path: str = DEFAULT_STATE_DB) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if path != ":memory:":
            os.makedirs(parent, mode=0o700, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                uid TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at REAL NOT NULL,
                date_inferred INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                emitted_at REAL
            );
            CREATE INDEX IF NOT EXISTS items_emitted_idx ON items(emitted_at);
            CREATE INDEX IF NOT EXISTS items_last_seen_idx ON items(last_seen);
            CREATE TABLE IF NOT EXISTS arxiv_cache (
                arxiv_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_key TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                manifest TEXT NOT NULL,
                integrity TEXT NOT NULL,
                root_ts TEXT,
                completed_sections TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                completed_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS deliveries_active_channel_idx
                ON deliveries(channel) WHERE completed_at IS NULL;
            CREATE INDEX IF NOT EXISTS deliveries_completed_idx
                ON deliveries(completed_at);
            """
        )
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(items)")
        }
        if "date_inferred" not in columns:
            self.connection.execute(
                "ALTER TABLE items ADD COLUMN date_inferred INTEGER NOT NULL DEFAULT 0"
            )
        if "payload" not in columns:
            self.connection.execute(
                "ALTER TABLE items ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'"
            )
        self.connection.commit()
        if path != ":memory:":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()

    def upsert_discovered(self, items: Iterable[Item], now: Optional[float] = None) -> None:
        timestamp = now if now is not None else time.time()
        rows = [
            (
                item.uid, item.source, item.url, item.title,
                float(item.published_at or 0), int(item.date_inferred),
                json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")),
                timestamp, timestamp,
            )
            for item in items
        ]
        self.connection.executemany(
            """
            INSERT INTO items(
                uid, source, url, title, published_at, date_inferred, payload,
                first_seen, last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                source=excluded.source,
                url=excluded.url,
                title=excluded.title,
                published_at=excluded.published_at,
                date_inferred=excluded.date_inferred,
                payload=excluded.payload,
                last_seen=excluded.last_seen
            """,
            rows,
        )
        self.connection.commit()

    def stabilize_inferred_dates(self, items: Iterable[Item]) -> None:
        inferred = {item.uid: item for item in items if item.date_inferred}
        if not inferred:
            return
        uids = list(inferred)
        for offset in range(0, len(uids), 500):
            batch = uids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.connection.execute(
                "SELECT uid, published_at FROM items "
                "WHERE date_inferred=1 AND uid IN (%s)" % placeholders,
                batch,
            )
            for uid, published_at in rows:
                inferred[uid].published_at = float(published_at)

    def last_completed_at(self) -> float:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='last_completed_at'"
        ).fetchone()
        if not row:
            return 0.0
        try:
            return max(0.0, float(row[0]))
        except (TypeError, ValueError):
            return 0.0

    def mark_collection_complete(self, completed_through: float) -> None:
        """Acknowledge every candidate discovered through a successful run."""

        timestamp = max(self.last_completed_at(), float(completed_through))
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES ('last_completed_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (repr(timestamp),),
        )
        self.connection.commit()

    @staticmethod
    def _delivery_integrity(manifest_text: str) -> str:
        return hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_delivery_row(row: Sequence[Any]) -> Dict[str, Any]:
        (
            delivery_key,
            channel,
            manifest_text,
            integrity,
            root_ts,
            completed_text,
            created_at,
        ) = row
        expected = StateDB._delivery_integrity(str(manifest_text))
        if not hmac.compare_digest(str(integrity), expected):
            raise ValueError("delivery manifest integrity check failed")
        try:
            manifest = json.loads(str(manifest_text))
            completed_raw = json.loads(str(completed_text))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("delivery manifest is invalid") from exc
        if not isinstance(manifest, dict) or not isinstance(completed_raw, list):
            raise ValueError("delivery manifest is invalid")
        completed = []
        for value in completed_raw:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("delivery progress is invalid")
            if value not in completed:
                completed.append(value)
        return {
            "delivery_key": str(delivery_key),
            "channel": str(channel),
            "manifest": manifest,
            "root_ts": str(root_ts or ""),
            "completed_sections": completed,
            "created_at": float(created_at),
        }

    def save_delivery_manifest(
        self,
        delivery_key: str,
        channel: str,
        manifest: Mapping[str, Any],
        created_at: Optional[float] = None,
    ) -> None:
        """Persist an immutable rendered delivery before its first API call."""

        if not delivery_key or not channel or not isinstance(manifest, Mapping):
            raise ValueError("delivery manifest identity is invalid")
        manifest_text = json.dumps(
            dict(manifest),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        integrity = self._delivery_integrity(manifest_text)
        timestamp = created_at if created_at is not None else time.time()
        try:
            self.connection.execute(
                """
                INSERT INTO deliveries(
                    delivery_key, channel, manifest, integrity,
                    completed_sections, created_at
                )
                VALUES (?, ?, ?, ?, '[]', ?)
                """,
                (
                    str(delivery_key),
                    str(channel),
                    manifest_text,
                    integrity,
                    float(timestamp),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            existing = self.connection.execute(
                """
                SELECT delivery_key, channel, manifest, integrity, root_ts,
                       completed_sections, created_at
                FROM deliveries WHERE delivery_key=?
                """,
                (str(delivery_key),),
            ).fetchone()
            if existing is not None:
                decoded = self._decode_delivery_row(existing)
                if (
                    decoded["channel"] == str(channel)
                    and decoded["manifest"] == dict(manifest)
                ):
                    return
            raise ValueError(
                "another delivery is already active for this channel"
            ) from exc

    def load_active_delivery(self, channel: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT delivery_key, channel, manifest, integrity, root_ts,
                   completed_sections, created_at
            FROM deliveries
            WHERE channel=? AND completed_at IS NULL
            ORDER BY created_at ASC LIMIT 1
            """,
            (str(channel),),
        ).fetchone()
        return self._decode_delivery_row(row) if row is not None else None

    def set_delivery_root_ts(self, delivery_key: str, root_ts: str) -> None:
        if not root_ts:
            raise ValueError("delivery root timestamp is empty")
        self.connection.execute(
            """
            UPDATE deliveries SET root_ts=?
            WHERE delivery_key=? AND completed_at IS NULL AND root_ts IS NULL
            """,
            (str(root_ts), str(delivery_key)),
        )
        row = self.connection.execute(
            "SELECT root_ts FROM deliveries WHERE delivery_key=?",
            (str(delivery_key),),
        ).fetchone()
        if row is None or str(row[0] or "") != str(root_ts):
            self.connection.rollback()
            raise ValueError("delivery root timestamp conflicts with state")
        self.connection.commit()

    def mark_delivery_section(
        self,
        delivery_key: str,
        section_index: int,
        emitted_at: Optional[float] = None,
    ) -> None:
        if (
            isinstance(section_index, bool)
            or not isinstance(section_index, int)
            or section_index < 0
        ):
            raise ValueError("delivery section index is invalid")
        timestamp = emitted_at if emitted_at is not None else time.time()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT delivery_key, channel, manifest, integrity, root_ts,
                       completed_sections, created_at
                FROM deliveries
                WHERE delivery_key=? AND completed_at IS NULL
                """,
                (str(delivery_key),),
            ).fetchone()
            if row is None:
                raise ValueError("active delivery was not found")
            decoded = self._decode_delivery_row(row)
            sections = decoded["manifest"].get("sections")
            if not isinstance(sections, list) or section_index >= len(sections):
                raise ValueError("delivery section index is invalid")
            section = sections[section_index]
            if not isinstance(section, dict) or not isinstance(
                section.get("uids"), list
            ):
                raise ValueError("delivery section is invalid")
            completed = decoded["completed_sections"]
            if section_index in completed:
                self.connection.commit()
                return
            uids = [
                str(uid) for uid in section["uids"]
                if isinstance(uid, str) and uid
            ]
            if len(uids) != len(section["uids"]) or len(uids) != len(set(uids)):
                raise ValueError("delivery section item IDs are invalid")
            self.connection.executemany(
                "UPDATE items SET emitted_at=? WHERE uid=?",
                [(float(timestamp), uid) for uid in uids],
            )
            completed.append(section_index)
            completed.sort()
            self.connection.execute(
                """
                UPDATE deliveries SET completed_sections=?
                WHERE delivery_key=? AND completed_at IS NULL
                """,
                (
                    json.dumps(completed, separators=(",", ":")),
                    str(delivery_key),
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def complete_delivery(
        self,
        delivery_key: str,
        completed_through: float,
        completed_at: Optional[float] = None,
    ) -> None:
        timestamp = completed_at if completed_at is not None else time.time()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT delivery_key, channel, manifest, integrity, root_ts,
                       completed_sections, created_at
                FROM deliveries
                WHERE delivery_key=? AND completed_at IS NULL
                """,
                (str(delivery_key),),
            ).fetchone()
            if row is None:
                raise ValueError("active delivery was not found")
            decoded = self._decode_delivery_row(row)
            sections = decoded["manifest"].get("sections")
            if not isinstance(sections, list):
                raise ValueError("delivery manifest sections are invalid")
            if set(decoded["completed_sections"]) != set(range(len(sections))):
                raise ValueError("delivery has incomplete sections")
            watermark = max(self.last_completed_at(), float(completed_through))
            self.connection.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES ('last_completed_at', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (repr(watermark),),
            )
            self.connection.execute(
                """
                UPDATE deliveries SET completed_at=?
                WHERE delivery_key=? AND completed_at IS NULL
                """,
                (float(timestamp), str(delivery_key)),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def pending_items(self, cutoff: float) -> List[Item]:
        completed_through = self.last_completed_at()
        items = []
        rows = self.connection.execute(
            "SELECT payload FROM items "
            "WHERE emitted_at IS NULL AND published_at >= ? AND first_seen > ? "
            "ORDER BY published_at DESC",
            (float(cutoff), completed_through),
        )
        for (payload,) in rows:
            try:
                data = json.loads(payload)
                item = Item.from_dict(data)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if item.uid and item.title:
                items.append(item)
        return items

    def emitted_uids(self) -> Set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT uid FROM items WHERE emitted_at IS NOT NULL"
            )
        }

    def mark_emitted(self, uids: Iterable[str], emitted_at: Optional[float] = None) -> None:
        timestamp = emitted_at if emitted_at is not None else time.time()
        rows = [(timestamp, uid) for uid in dict.fromkeys(uids)]
        self.connection.executemany(
            "UPDATE items SET emitted_at=? WHERE uid=?",
            rows,
        )
        self.connection.commit()

    def is_emitted(self, uid: str) -> bool:
        row = self.connection.execute(
            "SELECT emitted_at FROM items WHERE uid=?", (uid,)
        ).fetchone()
        return bool(row and row[0] is not None)

    def purge(self, retention_days: int = 120, now: Optional[float] = None) -> int:
        timestamp = now if now is not None else time.time()
        cutoff = timestamp - retention_days * 86400
        cursor = self.connection.execute(
            "DELETE FROM items WHERE last_seen < ?", (cutoff,)
        )
        self.connection.execute(
            "DELETE FROM arxiv_cache WHERE fetched_at < ?", (cutoff,)
        )
        self.connection.execute(
            """
            DELETE FROM deliveries
            WHERE completed_at IS NOT NULL AND completed_at < ?
            """,
            (cutoff,),
        )
        self.connection.commit()
        return int(cursor.rowcount or 0)

    def get_arxiv(self, arxiv_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        result = {}
        for arxiv_id in arxiv_ids:
            row = self.connection.execute(
                "SELECT payload FROM arxiv_cache WHERE arxiv_id=?", (arxiv_id,)
            ).fetchone()
            if not row:
                continue
            try:
                value = json.loads(row[0])
                if isinstance(value, dict):
                    result[arxiv_id] = value
            except json.JSONDecodeError:
                continue
        return result

    def put_arxiv(self, values: Mapping[str, Mapping[str, Any]]) -> None:
        timestamp = time.time()
        self.connection.executemany(
            """
            INSERT INTO arxiv_cache(arxiv_id, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(arxiv_id) DO UPDATE SET
                payload=excluded.payload, fetched_at=excluded.fetched_at
            """,
            [
                (arxiv_id, json.dumps(payload, separators=(",", ":")), timestamp)
                for arxiv_id, payload in values.items()
            ],
        )
        self.connection.commit()


def _merge_duplicate(existing: Item, incoming: Item) -> Item:
    categories = tuple(dict.fromkeys(existing.categories + incoming.categories))
    summary = existing.summary if len(existing.summary) >= len(incoming.summary) else incoming.summary
    authors = tuple(dict.fromkeys(existing.authors + incoming.authors))
    institutes = tuple(dict.fromkeys(existing.institutes + incoming.institutes))
    if existing.date_inferred and not incoming.date_inferred:
        published_at = incoming.published_at
        date_inferred = False
    elif incoming.date_inferred and not existing.date_inferred:
        published_at = existing.published_at
        date_inferred = False
    else:
        published_at = max(existing.published_at, incoming.published_at)
        date_inferred = existing.date_inferred and incoming.date_inferred
    return replace(
        existing,
        categories=categories,
        summary=summary,
        published_at=published_at,
        authors=authors,
        institutes=institutes,
        affiliation=existing.affiliation or incoming.affiliation,
        engagement=max(existing.engagement, incoming.engagement),
        tags=match_interest_tags(existing.title, summary),
        date_inferred=date_inferred,
    )


def _rank_value(item: Item, now: float) -> float:
    age_hours = max(0.0, (now - item.published_at) / 3600.0)
    interest_bonus = 6.0 * len(item.tags)
    engagement_bonus = min(10.0, math.log1p(max(0, item.engagement)))
    return -age_hours + interest_bonus + engagement_bonus


def select_diverse(
    items: Sequence[Item],
    emitted_uids: Optional[Set[str]] = None,
    max_per_category: int = 8,
    max_per_source: int = 2,
    x_max_per_source: int = 1,
    now: Optional[float] = None,
) -> Dict[str, List[Item]]:
    """Select recent items while giving every available source a first pass."""
    timestamp = now if now is not None else time.time()
    already_emitted = emitted_uids or set()
    globally_selected: Set[str] = set()
    sections: Dict[str, List[Item]] = {}
    discovered_categories = {
        category for item in items for category in item.categories
    }
    category_order = list(CATEGORY_ORDER) + sorted(discovered_categories.difference(CATEGORY_ORDER))
    for category in category_order:
        candidates = [
            item for item in items
            if category in item.categories
            and item.uid not in already_emitted
            and item.uid not in globally_selected
        ]
        if category == "💭 Following Feed":
            relevant = [item for item in candidates if item.tags]
            if relevant:
                candidates = relevant
        candidates.sort(
            key=lambda item: (_rank_value(item, timestamp), item.published_at, item.uid),
            reverse=True,
        )
        if not candidates:
            continue
        source_cap = x_max_per_source if category in {
            "🐦 X Lab Accounts", "💭 Following Feed", "🔥 Key People",
        } else max_per_source
        source_groups: Dict[str, List[Item]] = {}
        for item in candidates:
            source_groups.setdefault(item.source_key, []).append(item)
        selected = []
        for source_round in range(source_cap):
            round_items = [
                group[source_round]
                for group in source_groups.values()
                if len(group) > source_round
            ]
            round_items.sort(
                key=lambda item: (_rank_value(item, timestamp), item.published_at, item.uid),
                reverse=True,
            )
            for item in round_items:
                selected.append(item)
                if len(selected) >= max_per_category:
                    break
            if len(selected) >= max_per_category:
                break
        if selected:
            sections[category] = selected
            globally_selected.update(item.uid for item in selected)
    return sections


def _arxiv_id(item: Item) -> str:
    match = re.search(r"arxiv\.org/abs/([a-z-]+/\d+|\d{4}\.\d+)", item.url, re.I)
    return match.group(1) if match else ""


def _parse_arxiv_api(body: bytes) -> Dict[str, Dict[str, Any]]:
    root = ET.fromstring(body)
    values = {}
    for entry in root.iter():
        if _local_name(entry.tag) != "entry":
            continue
        raw_id = _child_text(entry, "id")
        match = re.search(r"/([a-z-]+/\d+|\d{4}\.\d+)(?:v\d+)?$", raw_id)
        if not match:
            continue
        arxiv_id = match.group(1)
        authors = []
        institutes = []
        for author in list(entry):
            if _local_name(author.tag) != "author":
                continue
            name = _child_text(author, "name")
            if name:
                authors.append(clean_text(name, 200))
            for child in list(author):
                if _local_name(child.tag) == "affiliation":
                    affiliation = clean_text("".join(child.itertext()), 300)
                    if affiliation:
                        institutes.append(affiliation)
        values[arxiv_id] = {
            "authors": list(dict.fromkeys(authors)),
            "institutes": list(dict.fromkeys(institutes))[:5],
            "summary": clean_text(_child_text(entry, "summary"), 2400),
        }
    return values


def enrich_selected_arxiv(
    sections: Mapping[str, Sequence[Item]],
    state: StateDB,
) -> SourceStatus:
    started = time.monotonic()
    item_map: Dict[str, List[Item]] = {}
    for items in sections.values():
        for item in items:
            arxiv_id = _arxiv_id(item)
            if arxiv_id:
                item_map.setdefault(arxiv_id, []).append(item)
    if not item_map:
        return SourceStatus("ArXiv metadata", True, 0, 0)
    cached = state.get_arxiv(item_map)
    missing = [arxiv_id for arxiv_id in item_map if arxiv_id not in cached]
    fetched: Dict[str, Dict[str, Any]] = {}
    errors = []
    for offset in range(0, len(missing), 50):
        batch = missing[offset:offset + 50]
        query = urllib.parse.urlencode({
            "id_list": ",".join(batch),
            "max_results": str(len(batch)),
        })
        try:
            body = fetch_url(
                "https://export.arxiv.org/api/query?" + query,
                timeout=30,
                retries=1,
            )
            fetched.update(_parse_arxiv_api(body))
        except Exception as exc:
            errors.append(_safe_error(exc))
        if offset + 50 < len(missing):
            time.sleep(3)
    if fetched:
        state.put_arxiv(fetched)
    cached.update(fetched)
    enriched = 0
    for arxiv_id, target_items in item_map.items():
        metadata = cached.get(arxiv_id)
        if not metadata:
            continue
        for item in target_items:
            item.authors = tuple(metadata.get("authors") or item.authors)
            item.institutes = tuple(metadata.get("institutes") or item.institutes)
            if metadata.get("summary"):
                item.summary = str(metadata["summary"])
            enriched += 1
    elapsed = int((time.monotonic() - started) * 1000)
    return SourceStatus(
        "ArXiv metadata", not errors, enriched, elapsed, "; ".join(errors[:2]),
    )


def _apply_missing_dates(items: Sequence[Item], fetched_at: float) -> List[Item]:
    result = []
    for index, item in enumerate(items):
        if item.published_at:
            result.append(item)
        else:
            result.append(replace(
                item, published_at=fetched_at - index, date_inferred=True,
            ))
    return result


def collect_digest(
    state_db_path: str = DEFAULT_STATE_DB,
    fresh_hours: int = 72,
    max_per_category: int = 8,
    max_per_source: int = 2,
    x_max_per_source: int = 1,
    persist_discovery: bool = True,
) -> Digest:
    now = time.time()
    all_items: List[Item] = []
    statuses: List[SourceStatus] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_rss, spec): spec for spec in RSS_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            items, status = future.result()
            all_items.extend(_apply_missing_dates(items, now))
            statuses.append(status)

    special_jobs = (
        ("HF Trending", "https://huggingface.co/api/trending", parse_hf_trending),
        ("HF Daily Papers", "https://huggingface.co/api/daily_papers", parse_hf_daily_papers),
        (
            "PyTorch releases",
            "https://api.github.com/repos/pytorch/pytorch/releases?per_page=10",
            parse_pytorch_releases,
        ),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_fetch_json_adapter, name, url, parser): name
            for name, url, parser in special_jobs
        }
        for future in concurrent.futures.as_completed(futures):
            items, status = future.result()
            all_items.extend(_apply_missing_dates(items, now))
            statuses.append(status)

    try:
        credentials = load_x_credentials()
    except Exception as exc:
        credentials = {}
        statuses.append(SourceStatus("X credentials", False, 0, 0, _safe_error(exc)))
    if credentials:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            x_futures = (
                executor.submit(
                    _fetch_x_accounts, "X Lab Accounts", X_LAB_ACCOUNTS,
                    "🐦 X Lab Accounts", credentials,
                ),
                executor.submit(
                    _fetch_x_accounts, "X Key People", X_KEY_PEOPLE,
                    "🔥 Key People", credentials,
                ),
                executor.submit(_fetch_x_home, credentials),
            )
            for future in concurrent.futures.as_completed(x_futures):
                items, status = future.result()
                all_items.extend(_apply_missing_dates(items, now))
                statuses.append(status)
    else:
        statuses.append(SourceStatus("X", False, 0, 0, "credentials unavailable"))

    merged: Dict[str, Item] = {}
    for item in all_items:
        existing = merged.get(item.uid)
        merged[item.uid] = _merge_duplicate(existing, item) if existing else item
    discovered = list(merged.values())
    cutoff = now - max(1, fresh_hours) * 3600

    with StateDB(state_db_path) as state:
        state.stabilize_inferred_dates(discovered)
        if persist_discovery:
            state.upsert_discovered(discovered, now=now)
        pending = {item.uid: item for item in state.pending_items(cutoff)}
        emitted = state.emitted_uids()
        if not persist_discovery:
            for item in discovered:
                if item.published_at < cutoff or item.uid in emitted:
                    continue
                existing = pending.get(item.uid)
                pending[item.uid] = (
                    _merge_duplicate(existing, item) if existing else item
                )
        eligible = list(pending.values())
        sections = select_diverse(
            eligible,
            emitted_uids=set(),
            max_per_category=max_per_category,
            max_per_source=max_per_source,
            x_max_per_source=x_max_per_source,
            now=now,
        )
        statuses.append(enrich_selected_arxiv(sections, state))
        if persist_discovery:
            state.upsert_discovered(
                [item for section in sections.values() for item in section],
                now=now,
            )
        state.purge(now=now)

    statuses.sort(key=lambda status: status.source.lower())
    selected_count = sum(len(items) for items in sections.values())
    return Digest(
        sections=sections,
        statuses=statuses,
        discovered_count=len(discovered),
        eligible_count=len(eligible),
        selected_count=selected_count,
        generated_at=now,
    )


def _markdown_escape(text: str) -> str:
    value = clean_text(text, 1000)
    return re.sub(r"([\\`*_[\]<>])", r"\\\1", value)


def _safe_display_url(url: str) -> str:
    return canonicalize_url(url).replace(")", "%29")


def _metadata_line(item: Item) -> str:
    values = [item.source]
    if item.published_at:
        values.append(dt.datetime.fromtimestamp(
            item.published_at, tz=dt.timezone.utc,
        ).strftime("%Y-%m-%d %H:%M UTC"))
    if item.authors:
        values.append(
            ", ".join(item.authors[:3])
            + (" +%d" % (len(item.authors) - 3) if len(item.authors) > 3 else "")
        )
    if item.affiliation:
        author_names = {author.casefold() for author in item.authors}
        if item.affiliation.casefold() not in author_names:
            values.append(item.affiliation)
    elif item.institutes:
        values.append(", ".join(item.institutes[:2]))
    return " · ".join(value for value in values if value)


def render_item_markdown(item: Item) -> str:
    title = _markdown_escape(item.title)
    url = _safe_display_url(item.url)
    if url:
        heading = "- **[%s](%s)**" % (title, url)
    else:
        heading = "- **%s**" % title
    lines = [heading, "  " + _markdown_escape(_metadata_line(item))]
    if item.tags:
        lines.append("  " + " ".join(item.tags))
    summary = clean_text(item.summary, 600)
    if summary:
        lines.append("  " + _markdown_escape(summary))
    return "\n".join(lines)


def render_markdown(digest: Digest) -> str:
    generated = dt.datetime.fromtimestamp(
        digest.generated_at, tz=dt.timezone.utc,
    ).strftime("%A, %B %d, %Y %H:%M UTC")
    lines = [
        "# News Digest — " + generated,
        "",
        "_%d selected from %d discovered; %d pending and fresh._"
        % (digest.selected_count, digest.discovered_count, digest.eligible_count),
    ]
    failures = [status for status in digest.statuses if not status.ok]
    if failures:
        lines.extend(("", "⚠️ Sources with errors: " + ", ".join(
            "%s (%s)" % (status.source, status.error or "unknown")
            for status in failures
        )))
    for category, items in digest.sections.items():
        lines.extend(("", "## " + category, ""))
        lines.extend(render_item_markdown(item) for item in items)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def slack_escape(text: str) -> str:
    return clean_text(text, 4000).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_slack_overview(digest: Digest) -> str:
    generated = dt.datetime.fromtimestamp(
        digest.generated_at, tz=dt.timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")
    failures = [status for status in digest.statuses if not status.ok]
    lines = [
        "🗞️ *News Digest — %s*" % generated,
        "%d selected from %d discovered across %d sections."
        % (digest.selected_count, digest.discovered_count, len(digest.sections)),
    ]
    if failures:
        lines.append("⚠️ Partial source failures: " + ", ".join(
            "%s (%s)" % (slack_escape(status.source), slack_escape(status.error or "unknown"))
            for status in failures[:6]
        ))
    lines.append("Details are in this thread.")
    return "\n".join(lines)


def _render_slack_item(item: Item) -> str:
    url = canonicalize_url(item.url).replace("|", "%7C")
    title = slack_escape(item.title)
    heading = "• <%s|%s>" % (url, title) if url else "• *%s*" % title
    metadata = slack_escape(_metadata_line(item))
    tags = " ".join(item.tags)
    summary = slack_escape(clean_text(item.summary, 420))
    lines = [heading, "  _%s_" % metadata]
    if tags:
        lines.append("  " + tags)
    if summary:
        lines.append("  " + summary)
    return "\n".join(lines)


def render_slack_sections(digest: Digest) -> List[Tuple[str, str, List[str]]]:
    sections = []
    for category, items in digest.sections.items():
        text = "*%s*\n\n%s" % (
            slack_escape(category),
            "\n\n".join(_render_slack_item(item) for item in items),
        )
        sections.append((category, text, [item.uid for item in items]))
    return sections


def _write_output(path: str, content: str) -> None:
    if path == "-":
        print(content, end="")
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect and rank a diverse news digest")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--fresh-hours", type=int, default=72)
    parser.add_argument("--max-per-category", type=int, default=8)
    parser.add_argument("--max-per-source", type=int, default=2)
    parser.add_argument("--x-max-per-source", type=int, default=1)
    parser.add_argument("--no-persist-discovery", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", "-o", default="-")
    parser.add_argument(
        "--state", action="store_true",
        help="compatibility flag; SQLite state is enabled by default",
    )
    args = parser.parse_args(argv)
    digest = collect_digest(
        state_db_path=args.state_db,
        fresh_hours=args.fresh_hours,
        max_per_category=args.max_per_category,
        max_per_source=args.max_per_source,
        x_max_per_source=args.x_max_per_source,
        persist_discovery=not args.no_persist_discovery,
    )
    if args.json:
        output = json.dumps(digest.to_dict(), indent=2, ensure_ascii=False) + "\n"
    else:
        output = render_markdown(digest)
    _write_output(args.output, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
