import os
import sys
import time
import tempfile
import shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS = tempfile.mkdtemp(prefix="solarr-dl-")
LIB_MOVIES = tempfile.mkdtemp(prefix="solarr-movies-")
LIB_SHOWS = tempfile.mkdtemp(prefix="solarr-shows-")
LIB_GAMES = tempfile.mkdtemp(prefix="solarr-games-")
DB_PATH = tempfile.mktemp(prefix="solarr-", suffix=".db")

os.environ["MOCK_DOWNLOADS"] = DOWNLOADS
os.environ["SOLARR_DB"] = DB_PATH

sys.path.insert(0, os.path.join(BASE, "tests"))
sys.path.insert(0, os.path.join(BASE, "app"))

import mock_services
mock_services.run_all()
time.sleep(1)

import main as backend
import worker
from clients import qbittorrent

app = backend.app
app.testing = True
c = app.test_client()

PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {label}")
    else:
        FAIL += 1
        print(f"[FAIL] {label} {extra}")


# 1. setup not complete initially
check("setup status = incomplete", c.get("/api/setup/status").get_json()["complete"] is False)

# 2. connection tests via wizard
r = c.post("/api/setup/test", json={"service": "prowlarr", "config": {"url": "http://127.0.0.1:9101", "apiKey": "PWKEY"}}).get_json()
check("prowlarr test ok", r["ok"], r)
r = c.post("/api/setup/test", json={"service": "prowlarr", "config": {"url": "http://127.0.0.1:9101", "apiKey": "WRONG"}}).get_json()
check("prowlarr bad key rejected", not r["ok"], r)
r = c.post("/api/setup/test", json={"service": "qbittorrent", "config": {"host": "127.0.0.1", "port": "9102", "username": "a", "password": "b"}}).get_json()
check("qbittorrent test ok", r["ok"], r)
r = c.post("/api/setup/test", json={"service": "jellyfin", "config": {"url": "http://127.0.0.1:9103", "apiKey": "JFKEY"}}).get_json()
check("jellyfin test ok", r["ok"], r)
r = c.post("/api/setup/test", json={"service": "romm", "config": {"url": "http://127.0.0.1:9104"}}).get_json()
check("romm test ok", r["ok"], r)

# 3. complete setup with services + root folders + prowlarr
setup_payload = {
    "account": {"username": "mel", "password": "pw12345"},
    "mediaServers": [
        {"type": "Jellyfin", "url": "http://127.0.0.1:9103", "apiKey": "JFKEY"},
        {"type": "RomM", "url": "http://127.0.0.1:9104"},
    ],
    "downloadClients": [
        {"type": "qBittorrent", "name": "qb", "host": "127.0.0.1", "port": "9102",
         "username": "a", "password": "b", "catMovie": "movies", "catShow": "tv", "catGame": "games"}
    ],
    "prowlarr": {"url": "http://127.0.0.1:9101", "apiKey": "PWKEY", "connected": True},
    "rootFolders": [
        {"path": LIB_MOVIES, "media": {"movies": True}},
        {"path": LIB_SHOWS, "media": {"shows": True}},
        {"path": LIB_GAMES, "media": {"games": True}},
    ],
}
r = c.post("/api/setup/complete", json=setup_payload).get_json()
check("setup complete", r.get("ok"), r)
check("setup status = complete", c.get("/api/setup/status").get_json()["complete"] is True)

# 4. auth gate + login
check("discover blocked before login", c.get("/api/discover").status_code == 401)
r = c.post("/api/login", json={"username": "mel", "password": "nope"})
check("login wrong pw rejected", r.status_code == 401)
r = c.post("/api/login", json={"username": "mel", "password": "pw12345"})
check("login ok", r.get_json().get("ok"))

# 5. prowlarr indexers now visible (synced, read-only)
r = c.get("/api/settings/prowlarr").get_json()
check("prowlarr connected in settings", r["prowlarr"]["connected"])
check("indexers synced from prowlarr", len(r["indexers"]) == 2, r["indexers"])

# 6. library sync pulls Jellyfin + RomM items
r = c.post("/api/jobs/sync-libraries").get_json()
check("library sync added items", r["added"] >= 4, r)
movies = c.get("/api/library/movie").get_json()
games = c.get("/api/library/game").get_json()
check("jellyfin movies imported", any(m["title"] == "Interstellar" for m in movies), movies)
check("romm games imported", any(g["title"] == "Chrono Trigger" for g in games), games)

# 7. download-client CRUD works (the reported-broken area)
r = c.get("/api/settings/download-clients").get_json()
check("one download client from setup", len(r) == 1)
c.post("/api/settings/download-clients", json={"type": "Transmission", "name": "seedbox", "host": "10.0.0.9", "port": "9091"})
r = c.get("/api/settings/download-clients").get_json()
check("add download client", len(r) == 2)
new_id = [x for x in r if x["name"] == "seedbox"][0]["id"]
c.put(f"/api/settings/download-clients/{new_id}", json={"type": "Transmission", "name": "seedbox-2", "host": "10.0.0.9", "port": "9091"})
r = c.get("/api/settings/download-clients").get_json()
check("edit download client", any(x["name"] == "seedbox-2" for x in r))
c.delete(f"/api/settings/download-clients/{new_id}")
r = c.get("/api/settings/download-clients").get_json()
check("delete download client", len(r) == 1)

# 8. add a webhook connection so we can assert notifications fire
c.post("/api/settings/all/connections", json={"name": "hook", "type": "Webhook", "url": "http://127.0.0.1:9105/hook"})

# 9. request a MOVIE -> should grab the REMUX (highest score), go Downloading
r = c.post("/api/request", json={"media_type": "movie", "title": "Blade Runner 2049", "external_id": "tt1"}).get_json()
check("movie request grabbed", r.get("ok"), r)
check("best-quality release chosen (remux)", "REMUX" in (r.get("release") or ""), r)

# 10. request a GAME -> grabbed
r = c.post("/api/request", json={"media_type": "game", "title": "Celeste", "platform": "PC", "external_id": "g1"}).get_json()
check("game request grabbed", r.get("ok"), r)

# 11. run the download poller -> imports finished downloads, flips to Available
worker.poll_once()
reqs = {x["title"]: x for x in c.get("/api/requests").get_json()}
check("movie request -> Available", reqs["Blade Runner 2049"]["status"] == "Available", reqs["Blade Runner 2049"])
check("game request -> Available", reqs["Celeste"]["status"] == "Available", reqs["Celeste"])

# 12. imported files actually landed in the right libraries
movie_files = []
for dp, _, fs in os.walk(LIB_MOVIES):
    movie_files += fs
game_files = []
for dp, _, fs in os.walk(LIB_GAMES):
    game_files += fs
check("movie file imported to movies library", any(f.endswith(".mkv") for f in movie_files), movie_files)
check("game rom extracted+imported to roms/PC", any(f.endswith(".sfc") for f in game_files), game_files)
check("game placed under roms/<platform>", any("roms" in dp and "pc" in dp for dp, _, _ in os.walk(LIB_GAMES)))

# 13. rescans + notifications fired
check("jellyfin rescan triggered", mock_services.STATE["jellyfin_scans"] >= 1)
check("romm reachable for import (watcher auto-scans)", mock_services.STATE["romm_rescans"] >= 1)
notif_events = [n.get("event") for n in mock_services.STATE["notifications"]]
check("grab notifications fired", notif_events.count("grab") >= 2, notif_events)
check("import notifications fired", notif_events.count("import") >= 2, notif_events)

# 14. catalogue now shows them available
movies = {m["title"]: m for m in c.get("/api/library/movie").get_json()}
check("catalogue movie now available", movies["Blade Runner 2049"]["status"] == "available", movies.get("Blade Runner 2049"))

# 15. profiles + tags CRUD
c.post("/api/settings/movies/profiles", json={"name": "Ultra-HD", "cutoff": "2160p"})
check("add profile", any(p["name"] == "Ultra-HD" for p in c.get("/api/settings/movies/profiles").get_json()))
c.post("/api/settings/games/tags", json={"label": "retro"})
check("add tag", any(t["label"] == "retro" for t in c.get("/api/settings/games/tags").get_json()))

# 16. quality engine surfaced through the API
qd = c.get("/api/settings/movies/quality-definitions").get_json()
check("quality definitions seeded", len(qd) >= 20, len(qd))
cf = c.get("/api/settings/movies/custom-formats").get_json()
check("custom formats seeded with specs", any(f["name"] == "HDR" and f["specs"] for f in cf), cf)

ev = c.post("/api/settings/movies/evaluate",
            json={"title": "Movie 2020 2160p BluRay REMUX HDR x265-FraMeSToR", "size": 40_000_000_000}).get_json()
check("evaluate: remux accepted via API", ev["decision"]["accepted"], ev)
check("evaluate: parsed Remux-2160p", ev["parsed"]["quality"] == "Remux-2160p", ev["parsed"])
check("evaluate: CF score 55 via API", ev["decision"]["cf_score"] == 55, ev["decision"])

ev2 = c.post("/api/settings/movies/evaluate", json={"title": "Movie 2020 CAM"}).get_json()
check("evaluate: CAM rejected via API", not ev2["decision"]["accepted"], ev2)

# 17. real upgrade flow: seed an available movie at a LOW quality, then run the
#     upgrade search (mock indexer offers a 2160p REMUX) -> it should upgrade.
import db as _db
mv_prof = _db.one("SELECT * FROM profiles WHERE app='movies'")
_db.run("INSERT INTO media(media_type,title,status,source,quality,quality_rank,cf_score,added_at) "
        "VALUES('movie','Old Movie','available','request','WEBDL-720p',?,0,?)",
        (__import__("quality").rank_of("movies", "WEBDL-720p"), _db.now()))
up = c.post("/api/jobs/search-upgrades").get_json()
titles = [u["title"] for u in up["upgraded"]]
check("upgrade search grabbed a better release", "Old Movie" in titles, up)
worker.poll_once()
oldm = [m for m in c.get("/api/library/movie").get_json() if m["title"] == "Old Movie"][0]
check("upgraded movie re-imported at higher quality", oldm["quality_rank"] > __import__("quality").rank_of("movies", "WEBDL-720p"), oldm)

print(f"\n==== {PASS} passed, {FAIL} failed ====")

for d in (DOWNLOADS, LIB_MOVIES, LIB_SHOWS, LIB_GAMES):
    shutil.rmtree(d, ignore_errors=True)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

sys.exit(1 if FAIL else 0)
