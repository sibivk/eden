# Eden

> Search · Grab · Watch — automated OTT movie discovery, NZB queuing, and library delivery.

Eden watches [ottmovierelease.com](https://www.ottmovierelease.com) daily, finds new Malayalam / Hindi / Tamil OTT releases, searches nzbs.in for a 1080p grab in the 1–10 GB sweet spot, queues it to NZBGet, and when the download finishes, moves the file into the right language library and triggers a Plex scan — all without touching anything.

---

## Features

### Search & Queue (manual)
- **Instant movie search** via nzbs.in API with extended metadata
- **Smart quality badges** — 4K, 1080p, BluRay, WEB-DL, HDR, HEVC, Dolby Atmos, and more
- **Recommended badge** — highlights results that are 1080p and within the 1–10 GB ideal range
- **IMDb rating chip** — rating and vote count pulled from Newznab metadata
- **Size colour bar** — green < 3 GB · amber 3–7 GB · red 7–10 GB; results > 10 GB filtered out
- **Language selector** — pick Malayalam / Hindi / Tamil before queuing so the file mover knows which library to use
- **One-click Queue** — sends directly to NZBGet via JSON-RPC

### Auto-download (daily 10:00 UTC)
- Scrapes ottmovierelease.com for streaming-only releases in target languages
- Skips movies already in any library (bidirectional fuzzy match — case-insensitive, strips year/quality tokens)
- Searches nzbs.in for smallest 1080p result between 1 GB and 10 GB
- Queues to NZBGet and records the movie with its language in the tracking database

### File mover (every 5 minutes)
- Reads `.eden_ready.log` written by your NZBGet post-process script
- Looks up each completed file's language from the tracking DB
- Moves the file to the correct library (`/libraries/malayalam`, `/libraries/hindi`, `/libraries/tamil`)
- Renames to clean `Title.Year.ext` format
- Deletes the NZBGet download folder from `STORAGE_PATH` once moved
- Triggers a Plex library scan for the affected language section

### UI
- Animated star-field canvas background
- Glassmorphism result cards with staggered fade-in
- OTT shortcut button (top-right) — quick link to ottmovierelease.com
- Activity panel — full history of auto-queued and moved movies, live stats, manual trigger button
- Fully mobile-responsive — compact layout, full-width Queue button, pill language selector on phones
- Toast notifications for every action

### Infrastructure
- Runs as a **Podman Quadlet** systemd user service — starts automatically at boot, no duplicate instances possible
- Single Gunicorn worker (prevents duplicate APScheduler jobs)
- Hairpin-NAT fix auto-detected in start script (NZBGet hostname → host IP)
- SELinux-compatible volume mounts (`:Z`)

---

## Quick Start

### 1. Configure

```bash
cp .env.example .env
nano .env   # fill in credentials (see table below)
```

### 2. Build & Deploy

```bash
chmod +x podman_start.sh podman_stop.sh
./podman_start.sh
```

Opens at **http://\<SERVER_IP\>:5000**

### 3. (First time) Enable systemd auto-start

The Quadlet file lives at `~/.config/containers/systemd/eden.container`.  
After `./podman_start.sh` runs for the first time, the service is already wired to `default.target` and will start automatically on every reboot.

---

## Configuration (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `PORT` | | `5000` | Host port for the web UI |
| `NZBS_API_KEY` | ✓ | — | nzbs.in API key |
| `NZBGET_URL` | ✓ | — | NZBGet server URL (http or https) |
| `NZBGET_USERNAME` | | `nzbget` | NZBGet username |
| `NZBGET_PASSWORD` | ✓ | — | NZBGet password |
| `NZB_CATEGORY` | | `Evaluate` | NZBGet category for queued downloads |
| `MAX_SIZE_GB` | | `10` | Maximum NZB size shown in search results |
| `STORAGE_PATH` | ✓ | — | NZBGet download folder (mounted as `/media` in container) |
| `SEARCH_MALAYALAM` | | — | Path to Malayalam library (mounted as `/libraries/malayalam`) |
| `SEARCH_HINDI` | | — | Path to Hindi library (mounted as `/libraries/hindi`) |
| `SEARCH_TAMIL` | | — | Path to Tamil library (mounted as `/libraries/tamil`) |
| `PLEX_URL` | | — | Plex server URL (e.g. `http://localhost:32400`) |
| `PLEX_TOKEN` | | — | Plex auth token |
| `PLEX_SECTION_MALAYALAM` | | — | Plex library section ID for Malayalam |
| `PLEX_SECTION_HINDI` | | — | Plex library section ID for Hindi |
| `PLEX_SECTION_TAMIL` | | — | Plex library section ID for Tamil |

> `.env` is never committed to git.

---

## NZBGet Post-Process Script

Add a line to your post-process script so the file mover knows a download is ready:

```bash
echo "$(date +%s)|${NZBPP_DIRECTORY##*/}/${NZBPP_NZBNAME}.mkv" >> /path/to/STORAGE_PATH/.eden_ready.log
```

The file mover reads this log every 5 minutes, matches the entry to its tracked language, and moves the file to the correct library.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/api/search?q=<title>` | Search nzbs.in |
| `POST` | `/api/queue` | Queue an NZB to NZBGet |
| `GET` | `/api/health` | Health check + active config |
| `GET` | `/api/activity` | Auto-download history + stats |
| `POST` | `/api/scheduler/trigger` | Manually trigger the auto-download job |
| `POST` | `/api/scheduler/process-files` | Manually trigger the file mover |

### POST `/api/queue` body

```json
{
  "url": "https://nzbs.in/...",
  "title": "Movie Title (2026)",
  "language": "hindi"
}
```

`language` must be one of `malayalam`, `hindi`, or `tamil`.

---

## End-to-End Workflow

```
ottmovierelease.com (daily 10:00 UTC)
         ↓
  Scrape OTT-only Malayalam / Hindi / Tamil releases
         ↓
  Skip if already in library or tracking DB (fuzzy match)
         ↓
  Search nzbs.in → smallest 1080p result between 1–10 GB
         ↓
  Queue to NZBGet (category: Evaluate)
         ↓
  NZBGet downloads → post-process script appends to .eden_ready.log
         ↓
  File mover (every 5 min) reads log → looks up language
         ↓
  Move to /libraries/{lang}/Title.Year.ext
  Delete source folder from STORAGE_PATH
  Trigger Plex library scan
```

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11 · Flask 3 · Gunicorn |
| Scheduler | APScheduler (BackgroundScheduler, UTC) |
| Scraper | BeautifulSoup4 (ottmovierelease.com) |
| Search | nzbs.in Newznab API |
| Downloader | NZBGet JSON-RPC |
| Media server | Plex (scan via REST API) |
| Frontend | Vanilla HTML/CSS/JS — no frameworks, no CDN |
| Container | Podman 5 (rootless) · Quadlet systemd service |

---

## Changelog

### v1.3.0 — Systemd & Mobile
- Podman Quadlet (`eden.container`) — starts at boot, no duplicates
- Full mobile-responsive layout (640 px and 400 px breakpoints)
- `podman_start.sh` now builds image then hands off to `systemctl --user restart`

### v1.2.0 — Plex & OTT Button
- Plex library scan triggered after every successful file move (per language section)
- OTT shortcut button fixed top-right, links to ottmovierelease.com

### v1.1.0 — UI Improvements
- Language selector pills (Malayalam / Hindi / Tamil) before Queue button
- Selected language recorded in tracking DB for file mover
- Recommended badge on 1080p + 1–10 GB results
- Source folder deleted from STORAGE_PATH after file moves
- PROTECTED_DIRS check made fully case-insensitive (handles `PreRoll`, `TEMP`, etc.)
- Logo replaced with custom `Logo_White.png`
- Bidirectional fuzzy duplicate detection

### v1.0.0 — Initial Release
- Movie search with nzbs.in API
- Quality badges, IMDb rating chip, size colour bar
- 10 GB filter, one-click NZBGet queue
- Daily auto-download scheduler (10:00 UTC)
- 5-minute file mover with language-based routing
- Cinematic dark UI — star field, glassmorphism, toasts
- Hairpin-NAT auto-detection for local NZBGet hostnames
