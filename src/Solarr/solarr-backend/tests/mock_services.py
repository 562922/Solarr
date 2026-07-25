"""
Fake Prowlarr / qBittorrent / Jellyfin / RomM / webhook servers for e2e tests.
The qBittorrent mock actually writes files to a shared downloads dir and reports
them complete, so the real import pipeline has real bytes to move.
"""
import os
import zipfile
from threading import Thread
from flask import Flask, jsonify, request

DOWNLOADS = os.environ.get("MOCK_DOWNLOADS", "/tmp/solarr-downloads")
os.makedirs(DOWNLOADS, exist_ok=True)

prowlarr = Flask("mock_prowlarr")
qb = Flask("mock_qb")
jellyfin = Flask("mock_jellyfin")
romm = Flask("mock_romm")
hook = Flask("mock_hook")
tmdb = Flask("mock_tmdb")
igdb = Flask("mock_igdb")
anilist = Flask("mock_anilist")


def _ani(mid, title, fmt):
    return {"id": mid, "format": fmt, "episodes": 12 if fmt != "MOVIE" else None,
            "averageScore": 85, "startDate": {"year": 2019},
            "title": {"romaji": title, "english": title}, "coverImage": {"large": "c.jpg", "extraLarge": "c.jpg"},
            "description": "<p>An anime.</p>", "genres": ["Action", "Adventure"],
            "studios": {"nodes": [{"name": "Studio Ghibli"}]}, "countryOfOrigin": "JP", "duration": 24}


@anilist.post("/")
def anilist_graphql():
    body = request.get_json(force=True, silent=True) or {}
    q = body.get("query", "")
    if "Page" in q:
        title = (body.get("variables") or {}).get("q", "Anime")
        return jsonify({"data": {"Page": {"media": [_ani(1, title, "TV"), _ani(2, title + " Movie", "MOVIE")]}}})
    mid = int((body.get("variables") or {}).get("id", 1))
    return jsonify({"data": {"Media": _ani(mid, "Cowboy Bebop", "TV")}})
listsrv = Flask("mock_lists")

STATE = {"jellyfin_scans": 0, "romm_rescans": 0, "notifications": [], "torrents": []}


# ---- Prowlarr ----
@prowlarr.get("/api/v1/system/status")
def pw_status():
    if request.headers.get("X-Api-Key") != "PWKEY":
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"version": "1.30.0"})


@prowlarr.get("/api/v1/indexer")
def pw_indexers():
    return jsonify([
        {"name": "1337x", "protocol": "torrent", "privacy": "public",
         "enable": True, "capabilities": {"categories": [{"name": "Movies"}, {"name": "TV"}, {"name": "PC/Games"}]}},
        {"name": "GazelleGames", "protocol": "torrent", "privacy": "private",
         "enable": True, "capabilities": {"categories": [{"name": "PC/Games"}]}},
    ])


@prowlarr.get("/api/v1/search")
def pw_search():
    q = request.args.get("query", "release")
    cats = request.args.getlist("categories")
    is_game = any(c in ("1000", "4000", "4050") for c in cats)
    if is_game:
        return jsonify([
            {"title": f"{q} [FitGirl Repack]", "seeders": 80, "size": 8_000_000_000,
             "magnetUrl": f"magnet:?xt=urn:btih:fitgirl-{q}", "indexer": "GazelleGames", "protocol": "torrent"},
            {"title": f"{q}-CODEX", "seeders": 40, "size": 30_000_000_000,
             "magnetUrl": f"magnet:?xt=urn:btih:codex-{q}", "indexer": "1337x", "protocol": "torrent"},
            {"title": f"{q} [DODI Repack]", "seeders": 20, "size": 9_000_000_000,
             "magnetUrl": f"magnet:?xt=urn:btih:dodi-{q}", "indexer": "GazelleGames", "protocol": "torrent"},
        ])
    return jsonify([
        {"title": f"{q} 1080p WEB-DL", "seeders": 40, "size": 3_000_000_000,
         "magnetUrl": f"magnet:?xt=urn:btih:webdl-{q}", "indexer": "1337x", "protocol": "torrent"},
        {"title": f"{q} 2160p BluRay REMUX", "seeders": 120, "size": 40_000_000_000,
         "magnetUrl": f"magnet:?xt=urn:btih:remux-{q}", "indexer": "1337x", "protocol": "torrent"},
        {"title": f"{q} CAM", "seeders": 5, "size": 900_000_000,
         "magnetUrl": f"magnet:?xt=urn:btih:cam-{q}", "indexer": "1337x", "protocol": "torrent"},
    ])


# ---- qBittorrent ----
@qb.post("/api/v2/auth/login")
def qb_login():
    return "Ok."


@qb.get("/api/v2/app/version")
def qb_version():
    return "v4.6.5"


@qb.post("/api/v2/torrents/add")
def qb_add():
    urls = request.form.get("urls", "")
    category = request.form.get("category", "")
    savepath = request.form.get("savepath") or os.path.join(DOWNLOADS, category)
    os.makedirs(savepath, exist_ok=True)
    # derive a name from the magnet hash tail
    name = urls.split(":")[-1] if urls else "download"
    # produce content appropriate to category: a zip containing a rom for games,
    # a bare .mkv for movies/tv.
    if "game" in category.lower():
        rom = os.path.join(savepath, f"{name}.sfc")
        with open(rom, "wb") as f:
            f.write(b"ROMDATA" * 100)
        archive = os.path.join(savepath, f"{name}.zip")
        with zipfile.ZipFile(archive, "w") as z:
            z.write(rom, arcname=f"{name}.sfc")
        os.remove(rom)
        content = archive
    else:
        content = os.path.join(savepath, f"{name}.mkv")
        with open(content, "wb") as f:
            f.write(b"VIDEODATA" * 100)
    STATE["torrents"].append({"name": name, "category": category, "progress": 1.0,
                              "content_path": content, "save_path": savepath, "state": "pausedUP"})
    return "Ok."


@qb.get("/api/v2/torrents/info")
def qb_info():
    cat = request.args.get("category")
    ts = STATE["torrents"]
    if cat:
        ts = [t for t in ts if t["category"] == cat]
    return jsonify(ts)


# ---- Jellyfin ----
@jellyfin.get("/System/Info")
def jf_info():
    if request.headers.get("X-Emby-Token") != "JFKEY":
        return jsonify({}), 401
    return jsonify({"Version": "10.9.0"})


@jellyfin.post("/Library/Refresh")
def jf_refresh():
    STATE["jellyfin_scans"] += 1
    return ("", 204)


@jellyfin.get("/Items")
def jf_items():
    kind = request.args.get("IncludeItemTypes")
    if kind == "Movie":
        return jsonify({"Items": [
            {"Name": "Interstellar", "Id": "jf-1", "ProductionYear": 2014},
            {"Name": "Dune: Part Two", "Id": "jf-2", "ProductionYear": 2024},
        ]})
    return jsonify({"Items": [{"Name": "Severance", "Id": "jf-3", "ProductionYear": 2022}]})


# ---- RomM ----
@romm.get("/api/heartbeat")
def rm_hb():
    STATE["romm_rescans"] += 1   # rescan() verifies reachability via heartbeat
    return jsonify({"status": "ok"})


@romm.post("/api/library/rescan")
def rm_rescan():
    STATE["romm_rescans"] += 1
    return jsonify({"status": "scanning"})


@romm.get("/api/roms")
def rm_roms():
    return jsonify({"items": [
        {"id": 1, "name": "Chrono Trigger", "platform_name": "snes", "path_cover_small": ""},
        {"id": 2, "name": "Super Metroid", "platform_name": "snes", "path_cover_small": ""},
    ]})


# ---- notification webhook ----
@hook.post("/hook")
def hook_recv():
    STATE["notifications"].append(request.get_json(silent=True) or {})
    return jsonify({"ok": True})


# ---- TMDb ----
def _movie(mid, name, year):
    return {"id": mid, "title": name, "release_date": f"{year}-01-01", "poster_path": "/p.jpg",
            "vote_average": 8.0, "vote_count": 1200, "overview": f"{name} overview."}

def _tv(tid, name, year):
    return {"id": tid, "name": name, "first_air_date": f"{year}-01-01", "poster_path": "/p.jpg",
            "vote_average": 8.5, "vote_count": 900, "overview": f"{name} overview."}

@tmdb.get("/search/movie")
def tmdb_search_movie():
    q = request.args.get("query", "Movie")
    anime = {"id": 999, "title": "Some Anime Film", "release_date": "2021-01-01", "poster_path": "/a.jpg",
             "vote_average": 8.0, "vote_count": 10, "overview": "anime", "genre_ids": [16], "original_language": "ja"}
    return jsonify({"results": [_movie(101, q, 2024), anime]})

@tmdb.get("/search/tv")
def tmdb_search_tv():
    q = request.args.get("query", "Show")
    anime = {"id": 998, "name": "Some Anime Series", "first_air_date": "2020-01-01", "poster_path": "/a.jpg",
             "vote_average": 8.0, "vote_count": 10, "overview": "anime", "genre_ids": [16], "original_language": "ja"}
    return jsonify({"results": [_tv(201, q, 2022), anime]})

@tmdb.get("/movie/<int:mid>")
def tmdb_movie(mid):
    return jsonify({"id": mid, "title": "Blade Runner 2049", "release_date": "2017-10-06",
                    "poster_path": "/p.jpg", "vote_average": 8.0, "vote_count": 1200,
                    "overview": "A young blade runner discovers a secret.",
                    "genres": [{"name": "Sci-Fi"}, {"name": "Thriller"}], "runtime": 164,
                    "original_language": "en",
                    "production_companies": [{"name": "Warner Bros."}],
                    "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [{"certification": "R"}]}]}})

@tmdb.get("/tv/<int:tid>")
def tmdb_tv(tid):
    return jsonify({"id": tid, "name": "Severance", "first_air_date": "2022-02-18",
                    "poster_path": "/p.jpg", "vote_average": 8.5, "vote_count": 900,
                    "overview": "Workers with severed memories.", "genres": [{"name": "Drama"}],
                    "episode_run_time": [50], "networks": [{"name": "Apple TV+"}],
                    "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
                    "seasons": [{"season_number": 1, "episode_count": 2, "name": "Season 1", "air_date": "2022-02-18"}]})

@tmdb.get("/tv/<int:tid>/season/<int:sn>")
def tmdb_season(tid, sn):
    return jsonify({"episodes": [
        {"episode_number": 1, "name": "Good News About Hell", "air_date": "2022-02-18"},
        {"episode_number": 2, "name": "Half Loop", "air_date": "2020-01-01"},
    ]})


# ---- IGDB + Twitch ----
@igdb.post("/oauth2/token")
def twitch_token():
    return jsonify({"access_token": "MOCKTOKEN", "expires_in": 3600})

@igdb.post("/games")
def igdb_games():
    body = request.get_data(as_text=True) or ""
    game = {"id": 55, "name": "Celeste", "first_release_date": 1516752000,
            "cover": {"url": "//img/celeste.jpg"}, "genres": [{"name": "Platformer"}],
            "platforms": [{"name": "PC"}], "rating": 92, "rating_count": 400,
            "summary": "Climb the mountain.", "involved_companies": [{"company": {"name": "Maddy Makes Games"}}]}
    return jsonify([game])


@listsrv.get("/json-list")
def list_json():
    return jsonify({"items": [
        {"title": "Parasite", "year": 2019, "id": 496243},
        {"title": "Whiplash", "year": 2014, "id": 244786},
    ]})


@listsrv.get("/rss-list")
def list_rss():
    xml = ('<?xml version="1.0"?><rss><channel><title>My Feed</title>'
           '<item><title>The Menu</title></item>'
           '<item><title>Sing Sing</title></item>'
           '</channel></rss>')
    return xml, 200, {"Content-Type": "application/rss+xml"}


def run_all():
    for a, port in [(prowlarr, 9101), (qb, 9102), (jellyfin, 9103), (romm, 9104), (hook, 9105),
                    (tmdb, 9106), (igdb, 9107), (listsrv, 9108), (anilist, 9109)]:
        Thread(target=lambda a=a, p=port: a.run(port=p, debug=False, use_reloader=False), daemon=True).start()


if __name__ == "__main__":
    run_all()
    import time
    time.sleep(3600)
