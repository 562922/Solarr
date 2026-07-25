# Solarr backend

A unified request + download-orchestration backend that combines the roles of
Overseerr/Jellyseerr, Radarr, Sonarr, and a Gamearr into one Flask service. It
sits in front of your existing stack and drives the full flow:

    request → Prowlarr search → release selection → download client
            → import/organize → library rescan → notify

It talks to Jellyfin (movies/shows library), RomM (games library), qBittorrent
(downloads), Prowlarr (indexers), and TMDb/IGDB (metadata). All connection
details are entered in the setup wizard — nothing is baked into env vars.

## Quick start (Docker)

Turnkey full stack (Solarr + Prowlarr + qBittorrent + Jellyfin + RomM):

    docker compose up -d --build          # uses docker-compose.yaml
    # open http://<host>:5000 and complete the setup wizard

Add to an existing stack: copy the `solarr:` block from `docker-compose.snippet.yaml`
into your compose file, point `build:` at this folder, and give Solarr the SAME
library mount your qBittorrent/Jellyfin/RomM use. Then `docker compose up -d --build solarr`.

Run locally without Docker:

    pip install -r requirements.txt
    cd app && python main.py              # http://localhost:5000

## Connections (entered in the setup wizard, nothing baked in)

| Service     | In-network address        | Auth                                   | Used for                    |
|-------------|---------------------------|----------------------------------------|-----------------------------|
| Prowlarr    | http://prowlarr:9696      | API key (`X-Api-Key`)                  | indexer search              |
| qBittorrent | qbittorrent:8080          | username/password (v2 WebUI API)       | downloads                   |
| Jellyfin    | http://jellyfin:8096      | API key (`X-Emby-Token`)               | movie/show library + rescan |
| RomM        | http://romm:8080          | Basic auth, or Bearer from `/api/token`| game library                |
| TMDb / IGDB | (public APIs)             | keys under Seerr → Metadata            | discovery metadata          |

Each has a **Test** button in the wizard. **Games import**: Solarr writes ROMs to
`<library>/roms/<platform>/`; RomM's built-in **filesystem watcher** auto-imports
them (RomM has no plain REST scan endpoint — scanning runs through its task queue),
so mount that path into RomM at `/romm/library`.

## Shared-mount requirement

qBittorrent, Jellyfin, RomM and Solarr must share the same library mount so imports
hardlink/move instead of copying across containers. The example compose mounts the
host library at `/library` in every relevant service.

## Layout

    app/
      main.py          Flask app + all API routes
      db.py            SQLite schema + helpers
      auth.py          users, roles, sessions, login gate
      config_store.py  typed getters for saved services
      pipeline.py      search, release scoring/selection, grab, import, rescan, notify, library sync
      worker.py        background poller: finishes downloads → import → available
      clients/         prowlarr, qbittorrent, jellyfin, romm, metadata (TMDb+IGDB)
    tests/
      mock_services.py fake Prowlarr/qBittorrent/Jellyfin/RomM/webhook (writes real files)
      test_e2e.py      full pipeline test (35 checks)

    python3 tests/test_e2e.py           # runs the whole flow against mocks

## API surface (all under /api)

    setup/status, setup/test, setup/complete
    login, logout, me
    discover, library/<type>, search?q=&type=, detail/<type>/<id>
    request (POST), requests (GET)
    settings/general (GET/POST)
    settings/prowlarr (GET/POST), settings/prowlarr/disconnect
    settings/download-clients (GET/POST/PUT/DELETE + /test)
    settings/root-folders (GET/POST/DELETE)
    settings/<app>/profiles, /tags, /connections (GET/POST + DELETE by id)
    settings/media-servers (GET/POST)
    jobs/sync-libraries, jobs/poll-downloads
    health

## What is production-real here

- Real HTTP clients + connection tests for Prowlarr, qBittorrent, Jellyfin, RomM,
  TMDb, IGDB.
- Setup wizard persistence, auth (hashed passwords, sessions), full settings CRUD.
- Prowlarr as the single indexer source (indexers are synced/displayed read-only).
- **Quality-profile / custom-format engine** (`quality.py`): a release-title
  parser (resolution, source, codec, HDR, proper/repack, release group), per-app
  quality definitions with ranks and size limits, quality profiles (allowed
  qualities, cutoff, upgrade-allowed, min & cutoff custom-format scores), custom
  formats built from scored specifications, and the decision engine that
  accepts/rejects a release, ranks acceptable releases by (quality rank →
  custom-format score → proper/repack → seeders), and makes upgrade decisions
  (quality upgrades up to the cutoff, and custom-format upgrades up to the cutoff
  format score). Wired into the grab pipeline and an upgrade-search job.
- The end-to-end pipeline: category-scoped search, profile-driven release
  selection, grab to the right download client + save path, background completion
  polling, import with **real** zip extraction + primary-file detection +
  arr-style placement (movies/shows into the library, games into
  `roms/<platform>/`), library rescan on the correct server, and grab/import
  notifications.
- Unified catalogue synced from Jellyfin + RomM.

## What is simplified (honest scope)

- **Sonarr specifics** (per-season/episode tracking, season packs, episode
  monitoring) are modeled at the title level, not episode level.
- **Archive extraction** handles `.zip`; `.7z`/`.rar` need external tooling and are
  flagged rather than bundled.
- The custom-format spec types cover title/group/resolution/source/codec/HDR;
  Radarr's full spec catalogue (language, indexer flags, release size, quality
  modifier, etc.) is a superset.
- Metadata detail is fetched live but not deeply cached.

## What remains for true arr parity (next phases)

- Sonarr episode/season model and calendar; RSS/interval auto-search for wanted items.
- Scene-name parsing edge cases and richer renaming templates.
- Retry/blocklist on failed grabs; manual release picker.
- The remaining custom-format specification types and release-profile preferred words.
- Optional Bazarr-style subtitle handling.
