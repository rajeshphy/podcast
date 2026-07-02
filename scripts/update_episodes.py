#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
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
        data = response.read(500000)
    root = ET.fromstring(data)
    entries = root.findall(f"{ATOM}entry")
    if not entries:
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


def feed_items(feed: dict, config: dict, cutoff_time, timezone: str, timeout: int) -> list[dict]:
    root = fetch_feed(feed, timeout)
    channel = clean_text(root.findtext(f"{ATOM}title")) or clean_text(feed.get("label"))
    items = []
    for rank, entry in enumerate(root.findall(f"{ATOM}entry"), start=1):
        title = clean_text(entry.findtext(f"{ATOM}title"))
        video_id = clean_text(entry.findtext(f"{YT}videoId"))
        url = feed_link(entry)
        published_at = parse_feed_datetime(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated"), timezone)
        if not title or not url or not published_at or published_at < cutoff_time:
            continue

        match_terms = [normalize(term) for term in feed.get("match_terms", [])]
        normalized_title = normalize(title)
        if match_terms and not any(term in normalized_title for term in match_terms):
            continue

        raw = {
            "title": title,
            "channel": channel,
            "duration": None,
        }
        text = normalize(f"{title} {channel}")
        if term_score(text, config.get("negative_terms", []), 1) > 0:
            continue

        score = score_entry(raw, {"id": feed.get("category"), "weight": feed.get("weight", 1.0)}, config, rank)
        item_id = video_id or re.sub(r"[^a-zA-Z0-9_-]+", "-", url)[-80:]
        items.append({
            "id": item_id,
            "title": title,
            "channel": channel,
            "url": url,
            "embed_url": f"https://www.youtube.com/embed/{video_id}" if video_id else url,
            "thumbnail": YOUTUBE_THUMB.format(video_id=video_id) if video_id else "",
            "duration": None,
            "duration_text": "",
            "published": published_at.strftime("%Y%m%d"),
            "published_at": published_at.isoformat(),
            "published_ts": int(published_at.timestamp()),
            "latest_rank": rank,
            "category": feed.get("category"),
            "category_label": feed.get("category_label") or feed.get("label"),
            "query": feed.get("label") or channel,
            "score": score + 3.0,
            "method": "rss",
            "feed_id": feed.get("id"),
            "feed_url": feed.get("url"),
        })
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
    if total < 420:
        return -1.5
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


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    portal = config.get("portal", {})
    timezone = portal.get("timezone", "Asia/Kolkata")
    max_results = int(os.environ.get("PODCAST_MAX_RESULTS_PER_QUERY") or portal.get("max_results_per_query") or 10)
    max_episodes = int(os.environ.get("PODCAST_MAX_EPISODES") or portal.get("max_episodes") or 72)
    timeout = int(os.environ.get("PODCAST_SEARCH_TIMEOUT") or 90)
    sort_mode = str(os.environ.get("PODCAST_SEARCH_SORT") or portal.get("search_sort") or "date").lower()
    recent_hours = int(os.environ.get("PODCAST_RECENT_HOURS") or portal.get("recent_hours") or 24)
    youtube_search_enabled = str(
        os.environ.get("PODCAST_YOUTUBE_SEARCH_ENABLED", portal.get("youtube_search_enabled", False))
    ).lower() in {"1", "true", "yes", "on"}
    now = datetime.now(ZoneInfo(timezone))
    cutoff_time = now - timedelta(hours=recent_hours)

    episodes_by_id: dict[str, dict] = {}
    errors = []

    for feed in config.get("rss_feeds", []):
        try:
            for episode in feed_items(feed, config, cutoff_time, timezone, timeout):
                existing = episodes_by_id.get(episode["id"])
                episodes_by_id[episode["id"]] = merge_episode(existing, episode) if existing else {
                    **episode,
                    "categories": [episode["category"]],
                    "category_labels": [episode["category_label"]],
                    "queries": [episode["query"]],
                }
        except Exception as exc:
            errors.append(f"{feed.get('id') or feed.get('url')}: {type(exc).__name__}: {exc}")

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
                existing = episodes_by_id.get(episode["id"])
                episodes_by_id[episode["id"]] = merge_episode(existing, episode) if existing else {
                    **episode,
                    "categories": [episode["category"]],
                    "category_labels": [episode["category_label"]],
                    "queries": [episode["query"]],
                }

    episodes = sorted(
        episodes_by_id.values(),
        key=lambda item: (item.get("published_ts", 0), item.get("score", 0)),
        reverse=True,
    )[:max_episodes]

    output = {
        "generated_at": now.isoformat(),
        "mode": "latest",
        "search_sort": sort_mode,
        "recent_hours": recent_hours,
        "youtube_search_enabled": youtube_search_enabled,
        "cutoff_time": cutoff_time.isoformat(),
        "episodes": episodes,
        "searches": config.get("searches", []),
        "rss_feeds": config.get("rss_feeds", []),
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
