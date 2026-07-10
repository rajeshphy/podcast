#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "searches.json"
OUTPUT_PATH = ROOT / "data" / "episodes.json"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_THUMB = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def clock(seconds: object) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def seconds_value(seconds: object) -> int | None:
    try:
        return int(float(seconds))
    except (TypeError, ValueError):
        return None


def parse_duration(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return seconds_value(text)
    parts = text.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return None
    if len(values) == 2:
        minutes, seconds = values
        return minutes * 60 + seconds
    hours, minutes, seconds = values
    return hours * 3600 + minutes * 60 + seconds


def parse_timestamp(value: object, timezone: str):
    text = clean_text(value)
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        return datetime.fromtimestamp(float(text), tz=ZoneInfo(timezone))
    except (OverflowError, OSError, ValueError):
        return None


def parse_feed_datetime(value: object, timezone: str):
    text = clean_text(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone))
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        return parsed.astimezone(ZoneInfo(timezone))
    except ValueError:
        try:
            return parsedate_to_datetime(text).astimezone(ZoneInfo(timezone))
        except (TypeError, ValueError, AttributeError):
            return None


def run_search(query: str, max_results: int, timeout: int, sort_mode: str) -> list[dict]:
    prefix = "ytsearchdate" if sort_mode == "date" else "ytsearch"
    target = f"{prefix}{max_results}:{query}"
    fields = "%(id)s\t%(title)s\t%(channel)s\t%(duration)s\t%(upload_date)s\t%(timestamp)s\t%(webpage_url)s"
    command = [
        "yt-dlp",
        "--skip-download",
        "--ignore-errors",
        "--no-warnings",
        "--quiet",
        "--playlist-end", str(max_results),
        "--print", fields,
        target,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    entries = []
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 6)
        if len(parts) != 7:
            continue
        video_id, title, channel, duration, upload_date, timestamp, webpage_url = parts
        entries.append({
            "id": clean_text(video_id),
            "title": clean_text(title),
            "channel": clean_text(channel),
            "duration": None if duration == "NA" else duration,
            "upload_date": "" if upload_date == "NA" else clean_text(upload_date),
            "timestamp": "" if timestamp == "NA" else clean_text(timestamp),
            "webpage_url": clean_text(webpage_url),
        })

    if not entries and completed.returncode:
        raise RuntimeError((completed.stderr or "yt-dlp returned no entries").strip())
    return entries


def fetch_feed(feed: dict, timeout: int) -> ET.Element:
    req = Request(str(feed["url"]), headers={"User-Agent": "podcast-radar/1.0"})
    with urlopen(req, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        data = response.read(20000000)
    root = ET.fromstring(data)
    entries = root.findall(f"{ATOM}entry")
    items = root.findall("./channel/item")
    if not entries and not items:
        raise RuntimeError("RSS feed has no entries")
    return root


def feed_link(entry: ET.Element) -> str:
    for link in entry.findall(f"{ATOM}link"):
        rel = link.attrib.get("rel", "alternate")
        href = link.attrib.get("href", "")
        if rel == "alternate" and href:
            return href
    video_id = clean_text(entry.findtext(f"{YT}videoId"))
    return YOUTUBE_WATCH.format(video_id=video_id) if video_id else ""


def rss_text(item: ET.Element, name: str) -> str:
    return clean_text(item.findtext(name))


def rss_image(item: ET.Element, root: ET.Element, feed: dict) -> str:
    image = item.find(f"{ITUNES}image")
    if image is not None and image.attrib.get("href"):
        return clean_text(image.attrib.get("href"))
    image = item.find(f"{MEDIA}thumbnail")
    if image is not None and image.attrib.get("url"):
        return clean_text(image.attrib.get("url"))
    channel_image = root.find("./channel/image/url")
    if channel_image is not None and channel_image.text:
        return clean_text(channel_image.text)
    return clean_text(feed.get("thumbnail"))


def rss_enclosure(item: ET.Element) -> tuple[str, str]:
    for enclosure in item.findall("enclosure"):
        url = clean_text(enclosure.attrib.get("url"))
        media_type = clean_text(enclosure.attrib.get("type"))
        if url and (not media_type or media_type.startswith("audio/") or url.lower().split("?")[0].endswith((".mp3", ".m4a", ".aac", ".ogg"))):
            return url, media_type
    return "", ""


def rss_link(item: ET.Element, audio_url: str) -> str:
    link = rss_text(item, "link")
    return link or audio_url


def stable_audio_id(feed: dict, item: ET.Element, audio_url: str) -> str:
    guid = rss_text(item, "guid")
    raw = f"{feed.get('id')}:{guid or audio_url or rss_text(item, 'title')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fetch_video_duration(video_id: str, timeout: int, cache: dict[str, int | None]) -> int | None:
    if video_id in cache:
        return cache[video_id]
    if str(os.environ.get("PODCAST_SKIP_DURATION_FETCH", "")).lower() in {"1", "true", "yes"}:
        cache[video_id] = None
        return None
    url = f"{YOUTUBE_WATCH.format(video_id=video_id)}&bpctr=9999999999&has_verified=1"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urlopen(req, timeout=timeout) as response:
            html = response.read(2500000).decode("utf-8", "ignore")
    except Exception:
        cache[video_id] = None
        return None

    start = html.find("ytInitialPlayerResponse")
    if start == -1:
        cache[video_id] = None
        return None
    match = re.search(r'"lengthSeconds"\s*:\s*"(\d+)"', html[start:start + 1000000])
    cache[video_id] = int(match.group(1)) if match else None
    return cache[video_id]


def is_blocked_text(text: str, config: dict) -> bool:
    return term_score(text, config.get("negative_terms", []), 1) > 0


def feed_items(
    feed: dict,
    config: dict,
    cutoff_time,
    timezone: str,
    timeout: int,
    duration_cache: dict[str, int | None],
    duration_timeout: int,
) -> list[dict]:
    root = fetch_feed(feed, timeout)
    max_per_feed = int(feed.get("max_items") or config.get("portal", {}).get("max_items_per_feed") or 5)
    rss_entries = root.findall("./channel/item")
    if rss_entries:
        return podcast_feed_items(root, rss_entries, feed, config, cutoff_time, timezone, max_per_feed)

    channel = clean_text(root.findtext(f"{ATOM}title")) or clean_text(feed.get("label"))
    items = []
    for rank, entry in enumerate(root.findall(f"{ATOM}entry"), start=1):
        title = clean_text(entry.findtext(f"{ATOM}title"))
        video_id = clean_text(entry.findtext(f"{YT}videoId"))
        url = feed_link(entry)
        published_at = parse_feed_datetime(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated"), timezone)
        if not title or not url or not published_at or published_at < cutoff_time:
            continue
        if "/shorts/" in url:
            continue

        match_terms = [normalize(term) for term in feed.get("match_terms", [])]
        normalized_title = normalize(title)
        if match_terms and not any(term in normalized_title for term in match_terms):
            continue
        if normalized_title == normalize(channel):
            continue

        raw = {
            "title": title,
            "channel": channel,
            "duration": None,
        }
        text = normalize(f"{title} {channel}")
        if is_blocked_text(text, config):
            continue

        duration = fetch_video_duration(video_id, duration_timeout, duration_cache) if video_id else None
        duration_source = "youtube"
        if duration is None and feed.get("fallback_duration"):
            duration = seconds_value(feed.get("fallback_duration"))
            duration_source = "feed_fallback"
        min_duration = int(config.get("portal", {}).get("min_duration_seconds") or 600)
        if duration is None or duration < min_duration:
            continue
        raw["duration"] = duration

        score = score_entry(raw, {"id": feed.get("category"), "weight": feed.get("weight", 1.0)}, config, rank)
        item_id = video_id or re.sub(r"[^a-zA-Z0-9_-]+", "-", url)[-80:]
        items.append({
            "id": item_id,
            "title": title,
            "channel": channel,
            "url": url,
            "embed_url": f"https://www.youtube.com/embed/{video_id}" if video_id else url,
            "thumbnail": YOUTUBE_THUMB.format(video_id=video_id) if video_id else "",
            "duration": duration,
            "duration_text": clock(duration),
            "published": published_at.strftime("%Y%m%d"),
            "published_at": published_at.isoformat(),
            "published_ts": int(published_at.timestamp()),
            "latest_rank": rank,
            "category": feed.get("category"),
            "category_label": feed.get("category_label") or feed.get("label"),
            "query": feed.get("label") or channel,
            "score": score + 3.0,
            "method": "rss",
            "duration_source": duration_source,
            "feed_id": feed.get("id"),
            "feed_url": feed.get("url"),
        })
        if len(items) >= max_per_feed:
            break
    return items


def podcast_feed_items(
    root: ET.Element,
    entries: list[ET.Element],
    feed: dict,
    config: dict,
    cutoff_time,
    timezone: str,
    max_per_feed: int,
) -> list[dict]:
    channel = clean_text(root.findtext("./channel/title")) or clean_text(feed.get("label"))
    items = []
    min_duration = int(config.get("portal", {}).get("min_duration_seconds") or 600)
    for rank, entry in enumerate(entries, start=1):
        title = rss_text(entry, "title")
        audio_url, media_type = rss_enclosure(entry)
        published_at = parse_feed_datetime(rss_text(entry, "pubDate") or rss_text(entry, f"{ATOM}published"), timezone)
        if not title or not audio_url or not published_at or published_at < cutoff_time:
            continue

        duration = parse_duration(entry.findtext(f"{ITUNES}duration"))
        if duration is None and feed.get("fallback_duration"):
            duration = seconds_value(feed.get("fallback_duration"))
        if duration is not None and duration < min_duration:
            continue

        text = normalize(f"{title} {channel}")
        if is_blocked_text(text, config):
            continue

        score = score_entry({"title": title, "channel": channel, "duration": duration}, {
            "id": feed.get("category"),
            "weight": feed.get("weight", 1.0),
        }, config, rank) + 4.0

        item_id = stable_audio_id(feed, entry, audio_url)
        image = rss_image(entry, root, feed)
        items.append({
            "id": item_id,
            "title": title,
            "channel": clean_text(feed.get("label")) or channel,
            "url": rss_link(entry, audio_url),
            "audio_url": audio_url,
            "audio_type": media_type,
            "thumbnail": image,
            "duration": duration,
            "duration_text": clock(duration) if duration is not None else "",
            "published": published_at.strftime("%Y%m%d"),
            "published_at": published_at.isoformat(),
            "published_ts": int(published_at.timestamp()),
            "latest_rank": rank,
            "category": feed.get("category"),
            "category_label": feed.get("category_label") or feed.get("label"),
            "query": feed.get("label") or channel,
            "score": round(score, 3),
            "method": "podcast_rss",
            "background_audio": True,
            "feed_id": feed.get("id"),
            "feed_url": feed.get("url"),
        })
        if len(items) >= max_per_feed:
            break
    return items


def term_score(text: str, terms: list[str], value: float) -> float:
    score = 0.0
    padded = f" {text} "
    for term in terms:
        normalized = normalize(term)
        if normalized and normalized in padded:
            score += value
    return score


def duration_score(seconds: object, category: str) -> float:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return 0.0

    if total < 180:
        return -5.0
    if total < 600:
        return -4.0
    if category in {"air"} and total <= 1800:
        return 1.2
    if 600 <= total <= 5400:
        return 2.2
    if 5400 < total <= 9000:
        return 0.6
    if total > 10800:
        return -2.0
    return 0.0


def trusted_score(channel: str, trusted: list[str]) -> float:
    normalized_channel = normalize(channel)
    for item in trusted:
        trusted_name = normalize(item)
        if trusted_name and trusted_name in normalized_channel:
            return 2.5
    return 0.0


def canonical_title(title: str) -> str:
    text = normalize(title)
    for marker in [
        " what in the world podcast",
        " the climate question podcast",
        " bbc world service",
    ]:
        text = text.replace(marker, " ")
    text = re.split(r" bbc world service| podcast| ft | feat ", text, maxsplit=1)[0]
    return " ".join(text.split())


def score_entry(entry: dict, search: dict, config: dict, rank: int) -> float:
    title = clean_text(entry.get("title"))
    channel = clean_text(entry.get("channel") or entry.get("uploader"))
    text = normalize(f"{title} {channel}")
    category = str(search.get("id", ""))

    score = float(search.get("weight", 1.0)) * 8.0
    score += max(0, 20 - rank) * 0.8
    score += term_score(text, config.get("positive_terms", []), 1.05)
    score += term_score(text, config.get("negative_terms", []), -4.5)
    score += duration_score(entry.get("duration"), category)
    score += trusted_score(channel, config.get("trusted_channels", []))

    return round(score, 3)


def video_id_from(entry: dict) -> str:
    video_id = clean_text(entry.get("id"))
    if video_id:
        return video_id
    url = clean_text(entry.get("url") or entry.get("webpage_url"))
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{8,})", url)
    return match.group(1) if match else ""


def episode_from_entry(entry: dict, search: dict, config: dict, rank: int, cutoff_time, timezone: str) -> dict | None:
    video_id = video_id_from(entry)
    title = clean_text(entry.get("title"))
    if not video_id or not title:
        return None

    published_at = parse_timestamp(entry.get("timestamp"), timezone)
    if not published_at or published_at < cutoff_time:
        return None

    channel = clean_text(entry.get("channel") or entry.get("uploader"))
    score = score_entry(entry, search, config, rank)
    if score < 8:
        return None

    return {
        "id": video_id,
        "title": title,
        "channel": channel,
        "url": YOUTUBE_WATCH.format(video_id=video_id),
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
        "thumbnail": YOUTUBE_THUMB.format(video_id=video_id),
        "duration": entry.get("duration"),
        "duration_text": clock(entry.get("duration")),
        "published": clean_text(entry.get("upload_date")),
        "published_at": published_at.isoformat(),
        "published_ts": int(published_at.timestamp()),
        "latest_rank": rank,
        "category": search.get("id"),
        "category_label": search.get("label"),
        "query": search.get("query"),
        "score": score,
    }


def episode_from_evergreen(entry: dict, config: dict, rank: int) -> dict | None:
    video_id = clean_text(entry.get("id"))
    title = clean_text(entry.get("title"))
    channel = clean_text(entry.get("channel"))
    category = clean_text(entry.get("category"))
    category_label = clean_text(entry.get("category_label"))
    duration = seconds_value(entry.get("duration"))
    min_duration = int(config.get("portal", {}).get("min_duration_seconds") or 600)
    if not video_id or not title or not category or duration is None or duration < min_duration:
        return None

    text = normalize(f"{title} {channel}")
    if is_blocked_text(text, config):
        return None

    score = score_entry({"title": title, "channel": channel, "duration": duration}, {
        "id": category,
        "weight": float(entry.get("weight", 1.0)),
    }, config, rank) + 4.0

    return {
        "id": video_id,
        "title": title,
        "channel": channel,
        "url": entry.get("url") or YOUTUBE_WATCH.format(video_id=video_id),
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
        "thumbnail": YOUTUBE_THUMB.format(video_id=video_id),
        "duration": duration,
        "duration_text": clock(duration),
        "published": "evergreen",
        "published_at": None,
        "published_ts": 0,
        "latest_rank": rank,
        "category": category,
        "category_label": category_label,
        "query": "Evergreen",
        "score": round(score, 3),
        "method": "evergreen",
        "evergreen": True,
    }


def merge_episode(existing: dict, incoming: dict) -> dict:
    merged = {**existing, **incoming} if incoming["score"] > existing["score"] else dict(existing)
    categories = set(existing.get("categories") or [existing.get("category")])
    categories.add(incoming.get("category"))
    labels = set(existing.get("category_labels") or [existing.get("category_label")])
    labels.add(incoming.get("category_label"))
    queries = set(existing.get("queries") or [existing.get("query")])
    queries.add(incoming.get("query"))
    merged["categories"] = sorted(item for item in categories if item)
    merged["category_labels"] = sorted(item for item in labels if item)
    merged["queries"] = sorted(item for item in queries if item)
    return merged


def store_episode(episodes_by_id: dict[str, dict], episode: dict) -> None:
    existing = episodes_by_id.get(episode["id"])
    episodes_by_id[episode["id"]] = merge_episode(existing, episode) if existing else {
        **episode,
        "categories": [episode["category"]],
        "category_labels": [episode["category_label"]],
        "queries": [episode["query"]],
    }


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    portal = config.get("portal", {})
    timezone = portal.get("timezone", "Asia/Kolkata")
    max_results = int(os.environ.get("PODCAST_MAX_RESULTS_PER_QUERY") or portal.get("max_results_per_query") or 10)
    max_episodes = int(os.environ.get("PODCAST_MAX_EPISODES") or portal.get("max_episodes") or 72)
    timeout = int(os.environ.get("PODCAST_SEARCH_TIMEOUT") or 90)
    duration_timeout = int(os.environ.get("PODCAST_DURATION_TIMEOUT") or 15)
    sort_mode = str(os.environ.get("PODCAST_SEARCH_SORT") or portal.get("search_sort") or "date").lower()
    recent_hours = int(os.environ.get("PODCAST_RECENT_HOURS") or portal.get("recent_hours") or 24)
    category_backfill_hours = int(portal.get("category_backfill_hours") or recent_hours)
    backfill_items_per_category = int(portal.get("backfill_items_per_category") or 1)
    youtube_search_enabled = str(
        os.environ.get("PODCAST_YOUTUBE_SEARCH_ENABLED", portal.get("youtube_search_enabled", False))
    ).lower() in {"1", "true", "yes", "on"}
    audio_only = str(os.environ.get("PODCAST_AUDIO_ONLY", portal.get("audio_only", True))).lower() in {"1", "true", "yes", "on"}
    now = datetime.now(ZoneInfo(timezone))
    cutoff_time = now - timedelta(hours=recent_hours)

    episodes_by_id: dict[str, dict] = {}
    duration_cache: dict[str, int | None] = {}
    errors = []

    for feed in config.get("rss_feeds", []):
        if audio_only and "youtube.com/feeds/videos.xml" in clean_text(feed.get("url")):
            continue
        try:
            for episode in feed_items(feed, config, cutoff_time, timezone, timeout, duration_cache, duration_timeout):
                store_episode(episodes_by_id, episode)
        except Exception as exc:
            errors.append(f"{feed.get('id') or feed.get('url')}: {type(exc).__name__}: {exc}")

    if backfill_items_per_category > 0 and category_backfill_hours > recent_hours:
        direct_feed_categories = {
            clean_text(feed.get("category"))
            for feed in config.get("rss_feeds", [])
            if clean_text(feed.get("category")) and "youtube.com/feeds/videos.xml" not in clean_text(feed.get("url"))
        }
        desired_categories = [
            clean_text(search.get("id"))
            for search in config.get("searches", [])
            if clean_text(search.get("id")) in direct_feed_categories
        ]
        filled_categories = {
            category
            for episode in episodes_by_id.values()
            for category in (episode.get("categories") or [episode.get("category")])
            if category
        }
        missing_categories = [category for category in desired_categories if category not in filled_categories]
        backfill_counts: dict[str, int] = {category: 0 for category in missing_categories}
        backfill_cutoff = now - timedelta(hours=category_backfill_hours)
        for feed in config.get("rss_feeds", []):
            category = clean_text(feed.get("category"))
            if category not in missing_categories:
                continue
            if audio_only and "youtube.com/feeds/videos.xml" in clean_text(feed.get("url")):
                continue
            if backfill_counts.get(category, 0) >= backfill_items_per_category:
                continue
            try:
                for episode in feed_items(feed, config, backfill_cutoff, timezone, timeout, duration_cache, duration_timeout):
                    category = clean_text(episode.get("category"))
                    if category not in missing_categories or backfill_counts.get(category, 0) >= backfill_items_per_category:
                        continue
                    episode["backfill"] = True
                    episode["query"] = "Latest in category"
                    episode["score"] = round(float(episode.get("score", 0)) - 0.5, 3)
                    store_episode(episodes_by_id, episode)
                    backfill_counts[category] = backfill_counts.get(category, 0) + 1
            except Exception as exc:
                errors.append(f"{feed.get('id') or feed.get('url')} backfill: {type(exc).__name__}: {exc}")

    if youtube_search_enabled:
        for search in config.get("searches", []):
            query = clean_text(search.get("query"))
            if not query:
                continue
            try:
                entries = run_search(query, max_results, timeout, sort_mode)
            except Exception as exc:
                errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue

            for rank, entry in enumerate(entries, start=1):
                episode = episode_from_entry(entry, search, config, rank, cutoff_time, timezone)
                if not episode:
                    continue
                store_episode(episodes_by_id, episode)

    evergreen_limit = 0 if audio_only else int(portal.get("evergreen_items_per_category", 2))
    evergreen_counts: dict[str, int] = {}
    for rank, entry in enumerate(config.get("evergreen_episodes", []), start=1):
        category = clean_text(entry.get("category"))
        if not category or evergreen_counts.get(category, 0) >= evergreen_limit:
            continue
        episode = episode_from_evergreen(entry, config, rank)
        if not episode:
            continue
        evergreen_counts[category] = evergreen_counts.get(category, 0) + 1
        store_episode(episodes_by_id, episode)

    sorted_episodes = sorted(
        episodes_by_id.values(),
        key=lambda item: (item.get("published_ts", 0), item.get("score", 0)),
        reverse=True,
    )
    episodes = []
    seen_titles = set()
    for item in sorted_episodes:
        key = canonical_title(item.get("title", ""))
        if key and key in seen_titles:
            continue
        seen_titles.add(key)
        episodes.append(item)
        if len(episodes) >= max_episodes:
            break

    output = {
        "generated_at": now.isoformat(),
        "mode": "latest",
        "search_sort": sort_mode,
        "recent_hours": recent_hours,
        "category_backfill_hours": category_backfill_hours,
        "backfill_items_per_category": backfill_items_per_category,
        "min_duration_seconds": int(portal.get("min_duration_seconds") or 600),
        "evergreen_items_per_category": evergreen_limit,
        "youtube_search_enabled": youtube_search_enabled,
        "cutoff_time": cutoff_time.isoformat(),
        "episodes": episodes,
        "searches": config.get("searches", []),
        "rss_feeds": config.get("rss_feeds", []),
        "evergreen_episodes": config.get("evergreen_episodes", []),
        "error": "; ".join(errors) if errors else None,
        "stale": False,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(output.get('episodes') or [])} episodes to {OUTPUT_PATH.relative_to(ROOT)}")
    if errors:
        print("Warnings:")
        for error in errors:
            print(f"- {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
