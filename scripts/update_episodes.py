#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "searches.json"
OUTPUT_PATH = ROOT / "data" / "episodes.json"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_THUMB = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


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


def parse_upload_date(value: object):
    text = clean_text(value)
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def run_search(query: str, max_results: int, timeout: int, sort_mode: str) -> list[dict]:
    prefix = "ytsearchdate" if sort_mode == "date" else "ytsearch"
    target = f"{prefix}{max_results}:{query}"
    fields = "%(id)s\t%(title)s\t%(channel)s\t%(duration)s\t%(upload_date)s\t%(webpage_url)s"
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
        parts = line.split("\t", 5)
        if len(parts) != 6:
            continue
        video_id, title, channel, duration, upload_date, webpage_url = parts
        entries.append({
            "id": clean_text(video_id),
            "title": clean_text(title),
            "channel": clean_text(channel),
            "duration": None if duration == "NA" else duration,
            "upload_date": "" if upload_date == "NA" else clean_text(upload_date),
            "webpage_url": clean_text(webpage_url),
        })

    if not entries and completed.returncode:
        raise RuntimeError((completed.stderr or "yt-dlp returned no entries").strip())
    return entries


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


def episode_from_entry(entry: dict, search: dict, config: dict, rank: int, cutoff_date) -> dict | None:
    video_id = video_id_from(entry)
    title = clean_text(entry.get("title"))
    if not video_id or not title:
        return None

    upload_date = parse_upload_date(entry.get("upload_date"))
    if not upload_date or upload_date < cutoff_date:
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
        "published_iso": upload_date.isoformat(),
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
    recent_days = int(os.environ.get("PODCAST_RECENT_DAYS") or portal.get("recent_days") or 365)
    cutoff_date = datetime.now(ZoneInfo(timezone)).date() - timedelta(days=recent_days)
    previous = load_json(OUTPUT_PATH, {})

    episodes_by_id: dict[str, dict] = {}
    errors = []

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
            episode = episode_from_entry(entry, search, config, rank, cutoff_date)
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
        key=lambda item: (item.get("published", ""), item.get("score", 0)),
        reverse=True,
    )[:max_episodes]

    if not episodes and errors and previous.get("episodes"):
        output = dict(previous)
        output["generated_at"] = datetime.now(ZoneInfo(timezone)).isoformat()
        output["stale"] = True
        output["error"] = "Refresh failed; kept previous episodes. " + "; ".join(errors)
    else:
        output = {
            "generated_at": datetime.now(ZoneInfo(timezone)).isoformat(),
            "mode": "latest",
            "search_sort": sort_mode,
            "recent_days": recent_days,
            "cutoff_date": cutoff_date.isoformat(),
            "episodes": episodes,
            "searches": config.get("searches", []),
            "error": "; ".join(errors) if errors else None,
            "stale": False,
        }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(output.get('episodes') or [])} episodes to {OUTPUT_PATH.relative_to(ROOT)}")
    if errors:
        print("Warnings:")
        for error in errors:
            print(f"- {error}")
    return 0 if output.get("episodes") else 1


if __name__ == "__main__":
    raise SystemExit(main())
