# Podcast Radar

Static YouTube discovery page for useful listening: All India Radio/Akashvani, health, finance, markets, policy, science, and ideas.

The updater searches YouTube with `yt-dlp`, scores results toward expert discussion and longer-form listening, and writes `data/episodes.json`. It stores only public metadata and links.

## Local update

```sh
python3 scripts/update_episodes.py
```

## Tuning

Edit `data/searches.json` to tune:

- search phrases
- category weights
- positive ranking terms
- negative ranking terms
- trusted channels
- max episodes and max results per query

