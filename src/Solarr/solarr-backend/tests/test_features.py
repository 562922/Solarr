import os
import sys
import time
import tempfile
import shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS = tempfile.mkdtemp(prefix="solarr-fdl-")
LIB_M = tempfile.mkdtemp(prefix="solarr-fm-")
LIB_S = tempfile.mkdtemp(prefix="solarr-fs-")
LIB_G = tempfile.mkdtemp(prefix="solarr-fg-")
LIB_A = tempfile.mkdtemp(prefix="solarr-fa-")
DB_PATH = tempfile.mktemp(prefix="solarr-f-", suffix=".db")

# env MUST be set before importing metadata (it reads bases at import)
os.environ["MOCK_DOWNLOADS"] = DOWNLOADS
os.environ["SOLARR_DB"] = DB_PATH
os.environ["TMDB_BASE"] = "http://127.0.0.1:9106"
os.environ["TMDB_IMG"] = "http://127.0.0.1:9106/img"
os.environ["IGDB_BASE"] = "http://127.0.0.1:9107"
os.environ["TWITCH_TOKEN_URL"] = "http://127.0.0.1:9107/oauth2/token"
os.environ["ANILIST_BASE"] = "http://127.0.0.1:9109/"

sys.path.insert(0, os.path.join(BASE, "tests"))
sys.path.insert(0, os.path.join(BASE, "app"))

import mock_services
mock_services.run_all()
time.sleep(1)

import main as backend
import worker
import db as _db

app = backend.app
app.testing = True
c = app.test_client()
PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"[PASS] {label}")
    else:
        FAIL += 1; print(f"[FAIL] {label} {extra}")


# ---- setup ----
setup = {
    "account": {"username": "mel", "password": "pw12345"},
    "mediaServers": [{"type": "Jellyfin", "url": "http://127.0.0.1:9103", "apiKey": "JFKEY"},
                     {"type": "RomM", "url": "http://127.0.0.1:9104"}],
    "downloadClients": [{"type": "qBittorrent", "name": "qb", "host": "127.0.0.1", "port": "9102",
                         "username": "a", "password": "b", "catMovie": "movies", "catShow": "tv", "catGame": "games"}],
    "prowlarr": {"url": "http://127.0.0.1:9101", "apiKey": "PWKEY", "connected": True},
    "rootFolders": [{"path": LIB_M, "media": {"movies": True}}, {"path": LIB_S, "media": {"shows": True}},
                    {"path": LIB_G, "media": {"games": True}}, {"path": LIB_A, "media": {"anime": True}}],
}
check("setup complete", c.post("/api/setup/complete", json=setup).get_json().get("ok"))
c.post("/api/login", json={"username": "mel", "password": "pw12345"})
c.post("/api/settings/general", json={"tmdb_api_key": "TMDBKEY", "igdb_client_id": "CID", "igdb_client_secret": "SEC"})

# ============================================================ Iteration 1: enrichment
r = c.post("/api/request", json={"media_type": "movie", "title": "Blade Runner 2049", "external_id": "101"}).get_json()
check("movie request ok", r.get("ok"), r)
mv = _db.one("SELECT * FROM media WHERE media_type='movie' AND title='Blade Runner 2049'")
check("movie enriched: genres", "Sci-Fi" in (mv["genres"] or ""), mv["genres"])
check("movie enriched: studio", mv["studio"] == "Warner Bros.", mv["studio"])
check("movie enriched: content rating", mv["content_rating"] == "R", mv["content_rating"])
check("movie enriched: runtime", mv["runtime"] == 164, mv["runtime"])
check("movie enriched flag set", mv["enriched"] == 1, mv["enriched"])

rg = c.post("/api/request", json={"media_type": "game", "title": "Celeste", "platform": "PC", "external_id": "55"}).get_json()
gm = _db.one("SELECT * FROM media WHERE media_type='game' AND title='Celeste'")
check("game enriched: studio", gm["studio"] == "Maddy Makes Games", gm["studio"])
check("game enriched: score", gm["score"] == 92, gm["score"])

# library endpoint returns parsed genres list
libm = c.get("/api/library/movie").get_json()
blade = [x for x in libm if x["title"] == "Blade Runner 2049"][0]
check("library returns genres as list", isinstance(blade["genres"], list) and "Sci-Fi" in blade["genres"], blade["genres"])

# ============================================================ Iteration 2: seasons/episodes
rs = c.post("/api/request", json={"media_type": "show", "title": "Severance", "external_id": "201"}).get_json()
check("show request ok", rs.get("ok"), rs)
show = _db.one("SELECT * FROM media WHERE media_type='show' AND title='Severance'")
seasons = c.get(f"/api/series/{show['id']}/seasons").get_json()
check("season populated", len(seasons) == 1 and seasons[0]["season_number"] == 1, seasons)
check("episodes populated", len(seasons[0]["episodes"]) == 2, seasons[0]["episodes"] if seasons else None)
check("show request grabbed aired episodes", rs.get("episodes_grabbed", 0) >= 1, rs)

# monitor toggle
c.put(f"/api/episode/{seasons[0]['episodes'][0]['id']}/monitor", json={"monitored": False})
ep0 = _db.one("SELECT monitored FROM episodes WHERE id=?", (seasons[0]["episodes"][0]["id"],))
check("episode monitor toggle", ep0["monitored"] == 0, ep0)

# ============================================================ calendar
cal = c.get("/api/calendar?start=2000-01-01&end=2100-01-01").get_json()
check("calendar returns episodes", len(cal) == 2 and cal[0]["series_title"] == "Severance", cal)

# ============================================================ Iteration 3: wanted search
_db.run("INSERT INTO media(media_type,title,status,source,monitored,added_at) "
        "VALUES('movie','Wanted Movie','not_owned','request',1,?)", (_db.now(),))
wanted = c.post("/api/jobs/search-wanted").get_json()
titles = [g["title"] for g in wanted["grabbed"]]
check("wanted search grabbed the monitored movie", "Wanted Movie" in titles, titles)
wm = _db.one("SELECT status FROM media WHERE title='Wanted Movie'")
check("wanted movie now downloading", wm["status"] == "downloading", wm)

# ============================================================ episode import places into Season folder
worker.poll_once()
found = []
for dp, _, fs in os.walk(LIB_S):
    for f in fs:
        found.append(os.path.join(dp, f))
check("episode imported into Season folder", any("Season 01" in p and "S01E01" in p for p in found), found)

# ============================================================ Iteration 4: release profiles
c.post("/api/settings/movies/release-profiles", json={"name": "No cams", "ignored": "cam,telesync",
       "required": "", "preferred": [["atmos", 20]]})
ev_bad = c.post("/api/settings/movies/evaluate", json={"title": "Movie 2024 1080p WEB-DL CAM"}).get_json()
check("release profile ignores CAM term", not ev_bad["decision"]["accepted"], ev_bad["decision"])
ev_pref = c.post("/api/settings/movies/evaluate",
                 json={"title": "Movie 2024 1080p WEB-DL ATMOS x265"}).get_json()
check("release profile preferred term adds score", ev_pref["decision"]["cf_score"] >= 20 + 15, ev_pref["decision"])

# required-term profile rejects releases lacking it
c.post("/api/settings/shows/release-profiles", json={"name": "Must be internal", "required": "internal"})
ev_req = c.post("/api/settings/shows/evaluate", json={"title": "Show S01E01 1080p WEB-DL"}).get_json()
check("release profile required term rejects", not ev_req["decision"]["accepted"], ev_req["decision"])

# ============================================================ Iteration 4b: new CF spec types
c.post("/api/settings/movies/custom-formats", json={"name": "Multi-language", "score": 25,
       "specs": [{"type": "language", "value": "multi"}]})
ev_lang = c.post("/api/settings/movies/evaluate",
                 json={"title": "Movie 2024 1080p WEB-DL MULTI x264"}).get_json()
check("language CF spec matches", ev_lang["decision"]["cf_score"] >= 25, ev_lang["decision"])

# ============================================================ Iteration 5: manual picker + blocklist
rel = c.get("/api/releases?type=movie&title=Blade Runner 2049").get_json()
check("manual picker returns ranked releases", len(rel) >= 2 and rel[0]["accepted"], rel[:1])
best = rel[0]
gr = c.post("/api/grab-release", json={"media_type": "movie", "title": "Picker Test", "link": best["link"],
            "release_title": best["title"], "quality": best["quality"], "rank": best["rank"],
            "cf_score": best["cf_score"]}).get_json()
check("grab-release grabs the chosen release", gr.get("ok"), gr)

# blocklist + retry: make an import fail by pointing content at a nonexistent path
_db.run("INSERT INTO requests(media_type,title,status,release_title,requested_at,updated_at) "
        "VALUES('movie','FailFirst','Downloading','FailFirst.BadRelease',?,?)", (_db.now(), _db.now()))
# force the qb mock to have a torrent whose content path is broken
import mock_services as ms
ms.STATE["torrents"].append({"name": "FailFirst", "category": "movies", "progress": 1.0,
                             "content_path": "/nonexistent/FailFirst.mkv", "save_path": "/nonexistent", "state": "pausedUP"})
worker.poll_once()
bl = c.get("/api/blocklist").get_json()
check("failed release was blocklisted", any(b["release_title"] == "FailFirst.BadRelease" for b in bl), bl)

# ---- import lists (auto-add) ----
# JSON list, catalogue-only (auto_add off)
c.post("/api/settings/movies/lists", json={"name": "Oscar Winners", "type": "TMDb",
       "url": "http://127.0.0.1:9108/json-list", "enabled": True, "autoAdd": False})
lists = c.get("/api/settings/movies/lists").get_json()
check("list created", any(l["name"] == "Oscar Winners" for l in lists), lists)
lid = [l for l in lists if l["name"] == "Oscar Winners"][0]["id"]
res = c.post(f"/api/settings/lists/{lid}/sync").get_json()
check("json list sync added items", res["result"]["added"] >= 2, res)
lib = c.get("/api/library/movie").get_json()
check("list item Parasite in catalogue as not_owned",
      any(m["title"] == "Parasite" and m["status"] == "not_owned" for m in lib), [m["title"] for m in lib])
check("catalogue-only list did not request", res["result"]["requested"] == 0, res)

# RSS list with auto-add ON -> should create requests + grab
c.post("/api/settings/movies/lists", json={"name": "Watch RSS", "type": "RSS",
       "url": "http://127.0.0.1:9108/rss-list", "enabled": True, "autoAdd": True})
allres = c.post("/api/jobs/sync-lists").get_json()
check("sync-lists job runs all enabled lists", len(allres["results"]) >= 2, allres)
rss = [r for r in allres["results"] if r["list"] == "Watch RSS"]
check("rss list parsed titles", rss and rss[0]["added"] >= 2, allres)
check("auto-add list created requests", rss and rss[0]["requested"] >= 1, allres)
reqs = {r["title"]: r for r in c.get("/api/requests").get_json()}
check("auto-added title became a request", "The Menu" in reqs, list(reqs.keys()))

# edit + delete
c.put(f"/api/settings/lists/{lid}", json={"name": "Oscars", "type": "TMDb",
      "url": "http://127.0.0.1:9108/json-list", "enabled": False, "autoAdd": False})
check("list edited", any(l["name"] == "Oscars" and not l["enabled"] for l in c.get("/api/settings/movies/lists").get_json()))
c.delete(f"/api/settings/lists/{lid}")
check("list deleted", not any(l["id"] == lid for l in c.get("/api/settings/movies/lists").get_json()))

# ---- multi-user + request approval ----
me = c.get("/api/me").get_json()
check("setup user is admin", me.get("role") == "admin", me)
# admin creates a standard (non-auto-approve) user
c.post("/api/users", json={"username": "beth", "password": "pw", "role": "user", "autoApprove": False})
users = c.get("/api/users").get_json()
check("user created", any(u["username"] == "beth" for u in users), users)
# switch to beth and request -> should be Pending (needs approval)
c.post("/api/logout")
c.post("/api/login", json={"username": "beth", "password": "pw"})
check("beth role is user", c.get("/api/me").get_json().get("role") == "user")
pend = c.post("/api/request", json={"media_type": "movie", "title": "Sicario", "external_id": "tt1"}).get_json()
check("non-admin request is Pending", pend.get("status") == "Pending", pend)
prid = pend["request_id"]
# beth cannot approve (admin only)
check("non-admin cannot approve", c.post(f"/api/requests/{prid}/approve").status_code == 403)
check("non-admin cannot list users", c.get("/api/users").status_code == 403)
# admin approves -> grab runs
c.post("/api/logout")
c.post("/api/login", json={"username": "mel", "password": "pw12345"})
appr = c.post(f"/api/requests/{prid}/approve").get_json()
check("admin approval grabs release", appr.get("ok") and appr.get("status") == "Downloading", appr)
reqs = {r["title"]: r for r in c.get("/api/requests").get_json()}
check("approved request now downloading", reqs["Sicario"]["status"] == "Downloading", reqs.get("Sicario"))
# decline flow
c.post("/api/logout"); c.post("/api/login", json={"username": "beth", "password": "pw"})
dec = c.post("/api/request", json={"media_type": "movie", "title": "Prisoners", "external_id": "tt2"}).get_json()
c.post("/api/logout"); c.post("/api/login", json={"username": "mel", "password": "pw12345"})
c.post(f"/api/requests/{dec['request_id']}/decline")
reqs = {r["title"]: r for r in c.get("/api/requests").get_json()}
check("declined request marked Declined", reqs["Prisoners"]["status"] == "Declined", reqs.get("Prisoners"))
# admin request still auto-approves (no regression)
adm = c.post("/api/request", json={"media_type": "movie", "title": "Arrival", "external_id": "tt3"}).get_json()
check("admin request auto-approves (not Pending)", adm.get("status") != "Pending", adm)
# cannot delete last admin / self
mel_id = [u for u in c.get("/api/users").get_json() if u["username"] == "mel"][0]["id"]
check("cannot delete self/last admin", c.delete(f"/api/users/{mel_id}").status_code == 400)
beth_id = [u for u in c.get("/api/users").get_json() if u["username"] == "beth"][0]["id"]
check("admin can delete a user", c.delete(f"/api/users/{beth_id}").get_json().get("ok"))

# ---- per-app metadata (.nfo) generation ----
c.post("/api/settings/movies/metadata", json={"nfoEnabled": True, "posterEnabled": False})
ms = c.get("/api/settings/movies/metadata").get_json()
check("nfo setting persisted", ms["nfoEnabled"] is True, ms)
c.post("/api/request", json={"media_type": "movie", "title": "Nfo Test Movie", "external_id": "tt99"})
worker.poll_once()
# find the nfo belonging to this specific movie
nfo_text = ""
for dp, _, fs in os.walk(LIB_M):
    if "movie.nfo" in fs and "Nfo Test Movie" in dp:
        nfo_text = open(os.path.join(dp, "movie.nfo")).read()
check("movie.nfo written on import", bool(nfo_text))
check("nfo contains title + <movie> root", "<movie>" in nfo_text and "Nfo Test Movie" in nfo_text, nfo_text[:80])

# ---- anime section (AniList, keyless) ----
ra = c.post("/api/request", json={"media_type": "anime", "title": "Cowboy Bebop", "external_id": "1"}).get_json()
check("anime request ok", ra.get("ok"), ra)
an = _db.one("SELECT * FROM media WHERE media_type='anime' AND title='Cowboy Bebop'")
check("anime row created + enriched studio", an and an["studio"] == "Studio Ghibli", an["studio"] if an else None)
check("anime uses AniList score", an and an["score"] == 85, an["score"] if an else None)
# anime lands in the anime library folder (not shows/movies)
worker.poll_once()
anime_files = []
for dp, _, fs in os.walk(LIB_A):
    anime_files += fs
check("anime imported into anime root folder", len(anime_files) >= 1, anime_files)
# discover exposes an anime block
disc = c.get("/api/discover").get_json()
check("discover has anime section", "anime" in disc, list(disc.keys()))

# ---- anime excluded from movies/shows search (TMDb genre-16 + ja filtered) ----
from clients import metadata as _meta
mv_results = _meta.search("Anything", "movie")
check("movie search excludes anime item", all("Anime" not in (x["title"] or "") for x in mv_results), [x["title"] for x in mv_results])
tv_results = _meta.search("Anything", "show")
check("show search excludes anime item", all("Anime" not in (x["title"] or "") for x in tv_results), [x["title"] for x in tv_results])
an_results = _meta.search("Bebop", "anime")
check("anime search returns AniList series + movie", len(an_results) == 2 and any(r["anime_kind"] == "movie" for r in an_results), [r.get("anime_kind") for r in an_results])

# ---- metadata providers ----
prov = c.get("/api/settings/providers").get_json()
names = {p["id"]: p for p in prov["providers"]}
check("four providers listed", set(names) == {"tmdb", "tvdb", "igdb", "anilist"}, list(names))
check("AniList is keyless + connected", names["anilist"]["keyless"] and names["anilist"]["connected"])
check("TMDB connected (key set earlier)", names["tmdb"]["connected"])
check("TheTVDB not connected without key", names["tvdb"]["connected"] is False)
c.post("/api/settings/providers", json={"fields": {"tvdb_api_key": "TVDBKEY"}})
prov2 = {p["id"]: p for p in c.get("/api/settings/providers").get_json()["providers"]}
check("TheTVDB connects after adding key", prov2["tvdb"]["connected"] is True)

# ---- Solarr API key + Prowlarr v3 push shim ----
key = c.get("/api/settings/api-key").get_json()["apiKey"]
check("solarr api key exists", bool(key) and len(key) >= 16, key)
check("v3 status is discoverable without a key", c.get("/api/v3/system/status").status_code == 200)
check("v3 status rejects bad key", c.get("/api/v3/system/status", headers={"X-Api-Key": "wrong"}).status_code == 401)
st = c.get("/api/v3/system/status", headers={"X-Api-Key": key}).get_json()
check("v3 status returns Solarr app", st.get("appName") == "Solarr", st)
check("v3 status reports a modern version", int(st.get("version", "0").split(".")[0]) >= 4, st.get("version"))
add = c.post("/api/v3/indexer", headers={"X-Api-Key": key},
             json={"name": "MyIndexer", "implementation": "Torznab",
                   "fields": [{"name": "baseUrl", "value": "http://prowlarr:9696/1/"},
                              {"name": "apiKey", "value": "abc"},
                              {"name": "categories", "value": [2000, 5000]}]})
check("v3 indexer add returns 201", add.status_code == 201, add.status_code)
iid = add.get_json()["id"]
lst = c.get("/api/v3/indexer", headers={"X-Api-Key": key}).get_json()
check("v3 indexer list shows pushed indexer", any(i["name"] == "MyIndexer" for i in lst), lst)
sch = c.get("/api/v3/indexer/schema", headers={"X-Api-Key": key}).get_json()
check("v3 schema advertises Torznab+Newznab", {s["implementation"] for s in sch} == {"Torznab", "Newznab"}, sch)
check("v3 indexer delete ok", c.delete(f"/api/v3/indexer/{iid}", headers={"X-Api-Key": key}).status_code == 200)
# Prowlarr's App test posts to /api/v3/indexer/test — must accept (200) with the key
check("v3 indexer/test rejects bad key", c.post("/api/v3/indexer/test", headers={"X-Api-Key": "no"}).status_code == 401)
check("v3 indexer/test accepts with key", c.post("/api/v3/indexer/test", headers={"X-Api-Key": key},
       json={"name": "x", "implementation": "Torznab", "fields": []}).status_code == 200)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
for d in (DOWNLOADS, LIB_M, LIB_S, LIB_G):
    shutil.rmtree(d, ignore_errors=True)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
sys.exit(1 if FAIL else 0)
