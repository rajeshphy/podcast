(() => {
  "use strict";

  const title = document.querySelector("#title");
  const subtitle = document.querySelector("#subtitle");
  const updated = document.querySelector("#updated");
  const tabs = document.querySelector("#tabs");
  const filter = document.querySelector("#filter");
  const episodesElement = document.querySelector("#episodes");
  const player = document.querySelector("#player");
  const emptyPlayer = document.querySelector("#empty-player");
  const youtubeSearch = document.querySelector("#youtube-search");

  let episodes = [];
  let searches = [];
  let activeCategory = "all";

  function formatGenerated(value) {
    if (!value) return "Not updated";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Updated";
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      hour: "numeric",
      minute: "2-digit",
      hour12: true
    }).format(date);
  }

  function visibleEpisodes() {
    const text = filter.value.trim().toLowerCase();
    return episodes.filter(episode => {
      const inCategory = activeCategory === "all" || (episode.categories || []).includes(activeCategory);
      if (!inCategory) return false;
      if (!text) return true;
      const haystack = [
        episode.title,
        episode.channel,
        ...(episode.category_labels || []),
        ...(episode.queries || [])
      ].join(" ").toLowerCase();
      return haystack.includes(text);
    });
  }

  function tabButton(id, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.category = id;
    button.textContent = label;
    button.classList.toggle("active", id === activeCategory);
    return button;
  }

  function buildTabs() {
    tabs.replaceChildren();
    tabs.append(tabButton("all", "All"));
    const activeCategories = new Set(episodes.flatMap(episode => episode.categories || []));
    for (const search of searches) {
      if (!activeCategories.has(search.id)) continue;
      tabs.append(tabButton(search.id, search.label));
    }
    if (activeCategory !== "all" && !activeCategories.has(activeCategory)) {
      activeCategory = "all";
    }
  }

  function play(episode) {
    player.src = `${episode.embed_url}?autoplay=1&rel=0`;
    emptyPlayer.classList.add("hidden");
    youtubeSearch.href = episode.url;
  }

  function episodeCard(episode) {
    const article = document.createElement("article");
    article.className = "episode";

    const playButton = document.createElement("button");
    playButton.type = "button";
    playButton.setAttribute("aria-label", `Play ${episode.title}`);
    playButton.addEventListener("click", () => play(episode));

    const thumb = document.createElement("div");
    thumb.className = "thumb";
    const img = document.createElement("img");
    img.src = episode.thumbnail;
    img.alt = "";
    img.loading = "lazy";
    thumb.append(img);
    if (episode.duration_text) {
      const duration = document.createElement("span");
      duration.className = "duration";
      duration.textContent = episode.duration_text;
      thumb.append(duration);
    }
    playButton.append(thumb);

    const body = document.createElement("div");
    body.className = "episode-body";

    const heading = document.createElement("h2");
    heading.textContent = episode.title;

    const meta = document.createElement("div");
    meta.className = "meta";
    const channel = document.createElement("span");
    channel.textContent = episode.channel || "YouTube";
    const open = document.createElement("a");
    open.className = "open";
    open.href = episode.url;
    open.target = "_blank";
    open.rel = "noopener";
    open.textContent = "Open";
    meta.append(channel, open);

    const chips = document.createElement("div");
    chips.className = "chips";
    if (episode.evergreen) {
      const evergreen = document.createElement("span");
      evergreen.className = "chip";
      evergreen.textContent = "Evergreen";
      chips.append(evergreen);
    }
    for (const label of episode.category_labels || []) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = label;
      chips.append(chip);
    }

    body.append(heading, meta, chips);
    article.append(playButton, body);
    return article;
  }

  function render() {
    tabs.querySelectorAll("button").forEach(button => {
      button.classList.toggle("active", button.dataset.category === activeCategory);
    });

    const visible = visibleEpisodes();
    episodesElement.replaceChildren();

    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "empty-list";
      empty.textContent = "No episodes found";
      episodesElement.append(empty);
      return;
    }

    for (const episode of visible) {
      episodesElement.append(episodeCard(episode));
    }
  }

  async function load() {
    const stamp = Date.now();
    const [configResponse, episodesResponse] = await Promise.all([
      fetch(`data/searches.json?v=${stamp}`, { cache: "no-store" }),
      fetch(`data/episodes.json?v=${stamp}`, { cache: "no-store" })
    ]);
    const config = await configResponse.json();
    const data = await episodesResponse.json();

    title.textContent = config.portal?.title || "Podcast Radar";
    subtitle.textContent = config.portal?.subtitle || "";
    updated.textContent = formatGenerated(data.generated_at);
    searches = config.searches || data.searches || [];
    episodes = data.episodes || [];

    buildTabs();
    render();
  }

  tabs.addEventListener("click", event => {
    const button = event.target.closest("button[data-category]");
    if (!button) return;
    activeCategory = button.dataset.category;
    render();
  });

  filter.addEventListener("input", render);

  load().catch(error => {
    console.error(error);
    updated.textContent = "Unavailable";
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "Podcast data could not be loaded";
    episodesElement.replaceChildren(empty);
  });
})();
