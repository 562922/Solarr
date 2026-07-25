import os
import json
import secrets

from flask import Flask, request, jsonify, session

import db
import config_store
import pipeline
import quality
import worker
from auth import (create_user, verify_user, any_user_exists, login_required,
                  admin_required, current_role, get_user)
from clients import prowlarr, qbittorrent, jellyfin, romm, metadata

db.init()
app = Flask(__name__)
app.secret_key = db.get_setting("secret_key") or secrets.token_hex(32)
if not db.get_setting("secret_key"):
    db.set_setting("secret_key", app.secret_key)


# ===========================================================================
# Auto-bootstrap: pre-configure all connections and metadata from hardcoded
# config values, bypassing the setup wizard entirely. Runs once on first start.
# ===========================================================================
def _auto_bootstrap():
    """Seed all connections, metadata keys, profiles, and a default admin user
    so Solarr opens directly to the Discover page with no wizard or login."""
    if db.setup_complete():
        # Already bootstrapped — just ensure worker is running
        worker.start()
        return

    # ---- metadata / API keys -----------------------------------------------
    tmdb_key = (os.getenv("TMDB_API_KEY") or "2ac1b7e46405df210c7ad89bacd9ea10").strip()
    igdb_client_id = os.getenv("IGDB_CLIENT_ID", "91487550546044879ab2e4fa51d60abc")
    igdb_client_secret = os.getenv("IGDB_CLIENT_SECRET", "91487550546044879ab2e4fa51d60abc")

    db.set_setting("tmdb_api_key", tmdb_key)
    db.set_setting("igdb_client_id", igdb_client_id)
    db.set_setting("igdb_client_secret", igdb_client_secret)
    db.set_setting("app_title", "Solarr")

    # ---- prowlarr -----------------------------------------------------------
    config_store.save_prowlarr("http://prowlarr:9696", "cb0330499a514e49ba3548ae369b4591", True)

    # ---- media servers: Jellyfin + RomM ------------------------------------
    # Clear and re-insert to be idempotent
    db.run("DELETE FROM media_servers")
    db.run(
        "INSERT INTO media_servers(kind,name,url,api_key,username,password) VALUES(?,?,?,?,?,?)",
        ("jellyfin", "Jellyfin", "http://jellyfin:8096", "3ceca53e09ab48c7a75c4c7e020f374b", "", ""),
    )
    db.run(
        "INSERT INTO media_servers(kind,name,url,api_key,username,password) VALUES(?,?,?,?,?,?)",
        ("romm", "RomM", "http://romm:8080", "", "jellyuserromm0425", "8DxbAYDVX+f&26p"),
    )

    # ---- download client: qBittorrent --------------------------------------
    db.run("DELETE FROM download_clients")
    db.run(
        "INSERT INTO download_clients(name,type,host,port,username,password,"
        "cat_movie,cat_show,cat_game,cat_anime,enabled,for_movies,for_shows,for_games,for_anime) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("qBittorrent", "qBittorrent", "qbittorrent", "8080", "qbit", "",
         "movies", "tv", "games", "anime", 1, 1, 1, 1, 1),
    )

    # ---- root folders ------------------------------------------------------
    db.run("DELETE FROM root_folders")
    db.run(
        "INSERT INTO root_folders(path,for_movies,for_shows,for_games,for_anime) VALUES(?,?,?,?,?)",
        ("/library/movies", 1, 0, 0, 0),
    )
    db.run(
        "INSERT INTO root_folders(path,for_movies,for_shows,for_games,for_anime) VALUES(?,?,?,?,?)",
        ("/library/shows", 0, 1, 0, 1),
    )
    db.run(
        "INSERT INTO root_folders(path,for_movies,for_shows,for_games,for_anime) VALUES(?,?,?,?,?)",
        ("/library/games", 0, 0, 1, 0),
    )

    # ---- quality profiles (720/1080p default) ------------------------------
    quality.seed_defaults()

    # ---- internal API key --------------------------------------------------
    if not db.get_setting("solarr_api_key"):
        db.set_setting("solarr_api_key", secrets.token_hex(16))

    # ---- mark setup complete and start worker ------------------------------
    db.set_setting("setup_complete", "1")
    worker.start()


_auto_bootstrap()

APP_TO_TYPE = {"movies": "movie", "shows": "show", "anime": "anime", "games": "game"}
TYPE_TO_APP = {v: k for k, v in APP_TO_TYPE.items()}


# ===========================================================================
# Setup wizard
# ===========================================================================
@app.get("/api/setup/status")
def setup_status():
    # Always report complete — setup wizard is bypassed in single-user mode
    return jsonify({"complete": True})


@app.post("/api/setup/test")
def setup_test():
    svc = request.json.get("service")
    c = request.json.get("config", {})
    if svc == "prowlarr":
        ok, msg = prowlarr.test_connection(c.get("url"), c.get("apiKey"))
    elif svc == "qbittorrent":
        ok, msg = qbittorrent.test_connection(c.get("host"), c.get("port"), c.get("username"), c.get("password"))
    elif svc == "jellyfin":
        ok, msg = jellyfin.test_connection(c.get("url"), c.get("apiKey"))
    elif svc == "romm":
        ok, msg = romm.test_connection(c.get("url"), c.get("apiKey"), c.get("username"), c.get("password"))
    else:
        return jsonify({"ok": False, "message": "unknown service"}), 400
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/setup/complete")
def setup_complete():
    if any_user_exists():
        return jsonify({"ok": False, "message": "already set up"}), 400
    d = request.json
    acct = d.get("account", {})
    create_user(acct.get("username"), acct.get("password"), role="admin", auto_approve=1)

    for ms in d.get("mediaServers", []):
        db.run("INSERT INTO media_servers(kind,name,url,api_key,username,password) VALUES(?,?,?,?,?,?)",
               (ms.get("type", "").lower(), ms.get("name", ms.get("type")), ms.get("url", ""),
                ms.get("apiKey", ""), ms.get("username", ""), ms.get("password", "")))
    for c in d.get("downloadClients", []):
        _insert_download_client(c)
    for rf in d.get("rootFolders", []):
        m = rf.get("media", {})
        db.run("INSERT INTO root_folders(path,for_movies,for_shows,for_games,for_anime) VALUES(?,?,?,?,?)",
               (rf.get("path", ""), int(bool(m.get("movies"))), int(bool(m.get("shows"))),
                int(bool(m.get("games"))), int(bool(m.get("anime")))))
    pw = d.get("prowlarr")
    if pw and pw.get("url"):
        config_store.save_prowlarr(pw.get("url"), pw.get("apiKey", ""), pw.get("connected", True))

    # seed quality definitions, default profiles, and example custom formats
    quality.seed_defaults()

    if not db.get_setting("solarr_api_key"):
        db.set_setting("solarr_api_key", secrets.token_hex(16))
    db.set_setting("setup_complete", "1")
    worker.start()
    return jsonify({"ok": True})


def _insert_download_client(c):
    db.run(
        "INSERT INTO download_clients(name,type,host,port,username,password,cat_movie,cat_show,cat_game,cat_anime,"
        "enabled,for_movies,for_shows,for_games,for_anime) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c.get("name", c.get("type", "qBittorrent")), c.get("type", "qBittorrent"),
         c.get("host", ""), c.get("port", ""), c.get("username", ""), c.get("password", ""),
         c.get("catMovie", "movies"), c.get("catShow", "tv"), c.get("catGame", "games"), c.get("catAnime", "anime"),
         int(c.get("enabled", True)), int(c.get("forMovies", True)),
         int(c.get("forShows", True)), int(c.get("forGames", True)), int(c.get("forAnime", True))),
    )


# ===========================================================================
# Auth
# ===========================================================================
@app.post("/api/login")
def login():
    d = request.json
    if verify_user(d.get("username"), d.get("password")):
        session["user"] = d["username"]
        return jsonify({"ok": True, "user": d["username"]})
    return jsonify({"ok": False, "message": "Invalid username or password"}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    # Single-user mode: always return admin
    return jsonify({"user": "admin", "role": "admin"})


# ===========================================================================
# Discover / library / search / detail
# ===========================================================================
def _media_out(row):
    row = dict(row)
    try:
        row["genres"] = json.loads(row.get("genres") or "[]")
    except (ValueError, TypeError):
        row["genres"] = []
    return row


@app.get("/api/discover")
@login_required
def discover():
    # Fetch live data from metadata sources (cached for 5 min each)
    trending_movies  = metadata.tmdb_trending_movies()
    popular_movies   = metadata.tmdb_popular_movies()
    upcoming_movies  = metadata.tmdb_upcoming_movies()
    trending_shows   = metadata.tmdb_trending_shows()
    popular_shows    = metadata.tmdb_popular_shows()
    upcoming_shows   = metadata.tmdb_upcoming_shows()
    popular_anime    = metadata.anilist_popular()
    trending_anime   = metadata.anilist_trending()
    upcoming_anime   = metadata.anilist_upcoming()
    popular_games    = metadata.igdb_popular()
    upcoming_games   = metadata.igdb_upcoming()

    # Build a lookup of local library status by (media_type, external_id)
    local_media = db.rows("SELECT media_type, external_id, status, id FROM media")
    local_lookup = {}
    for m in local_media:
        eid = str(m.get("external_id") or "")
        if eid:
            local_lookup[(m["media_type"], eid)] = m

    # Pull recently added / requested from local DB
    recently_added = [_media_out(r) for r in db.rows(
        "SELECT * FROM media WHERE status='available' ORDER BY added_at DESC LIMIT 20")]
    recent_requests = db.rows(
        "SELECT * FROM requests ORDER BY requested_at DESC LIMIT 20")

    def _enrich(items):
        """Merge local library status into live API results."""
        out = []
        for item in items:
            eid = str(item.get("external_id") or "")
            mt  = item.get("media_type", "")
            local = local_lookup.get((mt, eid))
            if local:
                item = dict(item)
                item["status"] = local["status"]
                item["id"] = local["id"]
            out.append(item)
        return out

    return jsonify({
        "recentlyAdded": recently_added,
        "requests": recent_requests,
        "movie": {
            "trending":  _enrich(trending_movies),
            "popular":   _enrich(popular_movies),
            "upcoming":  _enrich(upcoming_movies),
        },
        "show": {
            "trending":  _enrich(trending_shows),
            "popular":   _enrich(popular_shows),
            "upcoming":  _enrich(upcoming_shows),
        },
        "anime": {
            "trending":  _enrich(trending_anime),
            "popular":   _enrich(popular_anime),
            "upcoming":  _enrich(upcoming_anime),
        },
        "game": {
            "popular":   _enrich(popular_games),
            "upcoming":  _enrich(upcoming_games),
        },
    })


@app.get("/api/library/<media_type>")
@login_required
def library(media_type):
    items = [_media_out(r) for r in db.rows("SELECT * FROM media WHERE media_type=? ORDER BY title", (media_type,))]
    return jsonify(items)


@app.get("/api/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    mt = request.args.get("type", "")
    if not q:
        return jsonify([])
    types = [mt] if mt in ("movie", "show", "game") else ["movie", "show", "game"]
    results = []
    for t in types:
        for r in metadata.search(q, t):
            r["media_type"] = t
            results.append(r)
    return jsonify(results)


@app.get("/api/detail/<media_type>/<external_id>")
@login_required
def detail(media_type, external_id):
    d = metadata.detail(external_id, media_type)
    if not d:
        return jsonify({"error": "not found"}), 404
    d["media_type"] = media_type
    return jsonify(d)


# ===========================================================================
# Requests
# ===========================================================================
@app.post("/api/request")
@login_required
def make_request():
    d = request.json
    mt = d["media_type"]
    # approval gate: non-admins without auto-approve create a Pending request
    user = get_user(session.get("user")) or {}
    needs_approval = user.get("role") != "admin" and not user.get("auto_approve")
    if needs_approval:
        rid = db.run(
            "INSERT INTO requests(media_type,title,external_id,platform,profile_id,status,requested_by,"
            "message,requested_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mt, d["title"], d.get("external_id"), d.get("platform"),
             (config_store.default_profile(TYPE_TO_APP[mt]) or {}).get("id"),
             "Pending", session.get("user"), "Awaiting admin approval", db.now(), db.now()),
        )
        if not db.one("SELECT id FROM media WHERE media_type=? AND title=?", (mt, d["title"])):
            db.run("INSERT INTO media(media_type,title,external_id,platform,cover_url,status,source,added_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (mt, d["title"], d.get("external_id"), d.get("platform"), d.get("cover_url", ""),
                    "requested", "request", db.now()))
        return jsonify({"ok": True, "status": "Pending", "request_id": rid,
                        "message": "Request submitted for approval"})
    rid = db.run(
        "INSERT INTO requests(media_type,title,external_id,platform,profile_id,status,requested_by,requested_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (mt, d["title"], d.get("external_id"), d.get("platform"),
         (config_store.default_profile(TYPE_TO_APP[mt]) or {}).get("id"),
         "Searching", session.get("user"), db.now(), db.now()),
    )
    # reflect in catalogue
    if not db.one("SELECT id FROM media WHERE media_type=? AND title=?", (mt, d["title"])):
        db.run("INSERT INTO media(media_type,title,external_id,platform,cover_url,status,source,added_at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (mt, d["title"], d.get("external_id"), d.get("platform"), d.get("cover_url", ""),
                "requested", "request", db.now()))
    else:
        db.run("UPDATE media SET status='requested' WHERE media_type=? AND title=?", (mt, d["title"]))

    req = db.one("SELECT * FROM requests WHERE id=?", (rid,))
    # enrich metadata (and populate seasons/episodes for shows) — best effort
    m = db.one("SELECT * FROM media WHERE media_type=? AND title=?", (mt, d["title"]))
    try:
        if m:
            pipeline.enrich_media(m)
    except Exception:
        pass

    # Shows: grab aired episodes via the Sonarr-style episode model, not the whole title.
    if mt == "show":
        grabbed = pipeline.grab_aired_episodes(m["id"]) if m else []
        status = "Downloading" if grabbed else "Wanted"
        db.run("UPDATE requests SET status=?, message=?, updated_at=? WHERE id=?",
               (status, f"{len(grabbed)} aired episode(s) grabbed", db.now(), rid))
        if grabbed:
            db.run("UPDATE media SET status='downloading' WHERE id=?", (m["id"],))
        pipeline.notify("shows", "grab", db.one("SELECT * FROM requests WHERE id=?", (rid,)))
        return jsonify({"ok": True, "status": status, "episodes_grabbed": len(grabbed),
                        "episodes": grabbed, "request_id": rid})

    ok, msg = pipeline.grab(req)
    req = db.one("SELECT * FROM requests WHERE id=?", (rid,))
    if ok:
        db.run("UPDATE media SET status='downloading' WHERE media_type=? AND title=?", (mt, d["title"]))
        pipeline.notify(TYPE_TO_APP[mt], "grab", req)
        return jsonify({"ok": True, "status": req["status"], "release": msg, "request_id": rid})
    db.run("UPDATE requests SET status='Failed', message=?, updated_at=? WHERE id=?", (msg, db.now(), rid))
    return jsonify({"ok": False, "status": "Failed", "message": msg, "request_id": rid}), 502


@app.get("/api/requests")
@login_required
def list_requests():
    return jsonify(db.rows("SELECT * FROM requests ORDER BY requested_at DESC"))


@app.post("/api/requests/<int:rid>/approve")
@admin_required
def approve_request(rid):
    req = db.one("SELECT * FROM requests WHERE id=?", (rid,))
    if not req or req["status"] != "Pending":
        return jsonify({"ok": False, "message": "not a pending request"}), 400
    mt = req["media_type"]
    app_name = TYPE_TO_APP[mt]
    m = db.one("SELECT * FROM media WHERE media_type=? AND title=?", (mt, req["title"]))
    try:
        if m:
            pipeline.enrich_media(m)
    except Exception:
        pass
    if mt == "show":
        grabbed = pipeline.grab_aired_episodes(m["id"]) if m else []
        status = "Downloading" if grabbed else "Wanted"
        db.run("UPDATE requests SET status=?, message=?, updated_at=? WHERE id=?",
               (status, f"{len(grabbed)} aired episode(s) grabbed", db.now(), rid))
        if grabbed and m:
            db.run("UPDATE media SET status='downloading' WHERE id=?", (m["id"],))
        pipeline.notify("shows", "grab", db.one("SELECT * FROM requests WHERE id=?", (rid,)))
        return jsonify({"ok": True, "status": status, "episodes_grabbed": len(grabbed)})
    db.run("UPDATE requests SET status='Searching', updated_at=? WHERE id=?", (db.now(), rid))
    req = db.one("SELECT * FROM requests WHERE id=?", (rid,))
    ok, msg = pipeline.grab(req)
    if ok:
        if m:
            db.run("UPDATE media SET status='downloading' WHERE id=?", (m["id"],))
        pipeline.notify(app_name, "grab", db.one("SELECT * FROM requests WHERE id=?", (rid,)))
        return jsonify({"ok": True, "status": "Downloading", "release": msg})
    db.run("UPDATE requests SET status='Failed', message=?, updated_at=? WHERE id=?", (msg, db.now(), rid))
    return jsonify({"ok": False, "status": "Failed", "message": msg}), 502


@app.post("/api/requests/<int:rid>/decline")
@admin_required
def decline_request(rid):
    req = db.one("SELECT * FROM requests WHERE id=?", (rid,))
    if not req:
        return jsonify({"ok": False}), 404
    db.run("UPDATE requests SET status='Declined', message='Declined by admin', updated_at=? WHERE id=?",
           (db.now(), rid))
    db.run("UPDATE media SET status='not_owned' WHERE media_type=? AND title=? AND status='requested'",
           (req["media_type"], req["title"]))
    return jsonify({"ok": True})


# ===========================================================================
# Users (Overseerr-style multi-user + request approval)
# ===========================================================================
@app.get("/api/users")
@admin_required
def users_list():
    return jsonify(db.rows("SELECT id, username, role, email, auto_approve FROM users ORDER BY id"))


@app.post("/api/users")
@admin_required
def users_create():
    d = request.json
    if get_user(d.get("username")):
        return jsonify({"ok": False, "message": "username already exists"}), 400
    create_user(d.get("username"), d.get("password", "changeme"),
                role=d.get("role", "user"), email=d.get("email", ""),
                auto_approve=int(d.get("autoApprove", False)))
    return jsonify({"ok": True})


@app.put("/api/users/<int:uid>")
@admin_required
def users_update(uid):
    d = request.json
    db.run("UPDATE users SET role=?, email=?, auto_approve=? WHERE id=?",
           (d.get("role", "user"), d.get("email", ""), int(d.get("autoApprove", False)), uid))
    return jsonify({"ok": True})


@app.delete("/api/users/<int:uid>")
@admin_required
def users_delete(uid):
    row = db.one("SELECT username, role FROM users WHERE id=?", (uid,))
    if not row:
        return jsonify({"ok": False}), 404
    if row["username"] == session.get("user"):
        return jsonify({"ok": False, "message": "cannot delete yourself"}), 400
    if row["role"] == "admin" and db.one("SELECT COUNT(*) c FROM users WHERE role='admin'")["c"] <= 1:
        return jsonify({"ok": False, "message": "cannot delete the last admin"}), 400
    db.run("DELETE FROM users WHERE id=?", (uid,))
    return jsonify({"ok": True})


# ===========================================================================
# Settings — collective services
# ===========================================================================
@app.get("/api/settings/general")
@login_required
def get_general():
    keys = ["app_title", "app_url", "hide_available", "tmdb_api_key", "igdb_client_id", "igdb_client_secret"]
    return jsonify({k: db.get_setting(k, "") for k in keys})


@app.post("/api/settings/general")
@login_required
def set_general():
    for k, v in request.json.items():
        db.set_setting(k, v)
    return jsonify({"ok": True})


@app.get("/api/settings/<app_name>/metadata")
@login_required
def get_metadata_settings(app_name):
    return jsonify({
        "nfoEnabled": db.get_setting(f"meta_{app_name}_nfo", "0") == "1",
        "posterEnabled": db.get_setting(f"meta_{app_name}_poster", "0") == "1",
    })


@app.post("/api/settings/<app_name>/metadata")
@login_required
def set_metadata_settings(app_name):
    d = request.json
    db.set_setting(f"meta_{app_name}_nfo", "1" if d.get("nfoEnabled") else "0")
    db.set_setting(f"meta_{app_name}_poster", "1" if d.get("posterEnabled") else "0")
    return jsonify({"ok": True})


@app.get("/api/settings/prowlarr")
@login_required
def get_prowlarr_settings():
    p = config_store.get_prowlarr()
    indexers = prowlarr.list_indexers() if p.get("connected") else []
    return jsonify({"prowlarr": {"url": p["url"], "connected": bool(p["connected"])}, "indexers": indexers})


@app.post("/api/settings/prowlarr")
@login_required
def connect_prowlarr():
    d = request.json
    ok, msg = prowlarr.test_connection(d.get("url"), d.get("apiKey"))
    if ok:
        config_store.save_prowlarr(d.get("url"), d.get("apiKey"), True)
        return jsonify({"ok": True, "message": msg, "indexers": prowlarr.list_indexers()})
    config_store.save_prowlarr(d.get("url"), d.get("apiKey"), False)
    return jsonify({"ok": False, "message": msg}), 400


@app.post("/api/settings/prowlarr/disconnect")
@login_required
def disconnect_prowlarr():
    p = config_store.get_prowlarr()
    config_store.save_prowlarr(p["url"], p["api_key"], False)
    return jsonify({"ok": True})


# ---- download clients (full CRUD) --------------------------------------
@app.get("/api/settings/download-clients")
@login_required
def dc_list():
    return jsonify(db.rows("SELECT * FROM download_clients"))


@app.post("/api/settings/download-clients")
@login_required
def dc_create():
    _insert_download_client(request.json)
    return jsonify({"ok": True})


@app.put("/api/settings/download-clients/<int:cid>")
@login_required
def dc_update(cid):
    c = request.json
    db.run(
        "UPDATE download_clients SET name=?,type=?,host=?,port=?,username=?,password=?,"
        "cat_movie=?,cat_show=?,cat_game=?,enabled=?,for_movies=?,for_shows=?,for_games=? WHERE id=?",
        (c.get("name"), c.get("type"), c.get("host"), c.get("port"), c.get("username"), c.get("password"),
         c.get("catMovie", "movies"), c.get("catShow", "tv"), c.get("catGame", "games"),
         int(c.get("enabled", True)), int(c.get("forMovies", True)), int(c.get("forShows", True)),
         int(c.get("forGames", True)), cid),
    )
    return jsonify({"ok": True})


@app.delete("/api/settings/download-clients/<int:cid>")
@login_required
def dc_delete(cid):
    db.run("DELETE FROM download_clients WHERE id=?", (cid,))
    return jsonify({"ok": True})


@app.post("/api/settings/download-clients/test")
@login_required
def dc_test():
    c = request.json
    ok, msg = qbittorrent.test_connection(c.get("host"), c.get("port"), c.get("username"), c.get("password"))
    return jsonify({"ok": ok, "message": msg})


# ---- root folders ------------------------------------------------------
@app.get("/api/settings/root-folders")
@login_required
def rf_list():
    return jsonify(db.rows("SELECT * FROM root_folders"))


@app.post("/api/settings/root-folders")
@login_required
def rf_create():
    rf = request.json
    m = rf.get("media", {})
    db.run("INSERT INTO root_folders(path,for_movies,for_shows,for_games) VALUES(?,?,?,?)",
           (rf.get("path"), int(bool(m.get("movies"))), int(bool(m.get("shows"))), int(bool(m.get("games")))))
    return jsonify({"ok": True})


@app.delete("/api/settings/root-folders/<int:rid>")
@login_required
def rf_delete(rid):
    db.run("DELETE FROM root_folders WHERE id=?", (rid,))
    return jsonify({"ok": True})


# ---- quality definitions (per app) -------------------------------------
@app.get("/api/settings/<app_name>/quality-definitions")
@login_required
def qd_list(app_name):
    return jsonify(db.rows("SELECT * FROM quality_definitions WHERE app=? ORDER BY rank DESC", (app_name,)))


@app.put("/api/settings/quality-definitions/<int:qid>")
@login_required
def qd_update(qid):
    d = request.json
    db.run("UPDATE quality_definitions SET min_size_mb=?, max_size_mb=? WHERE id=?",
           (d.get("minSizeMb", 0), d.get("maxSizeMb", 0), qid))
    return jsonify({"ok": True})


# ---- quality profiles (full) -------------------------------------------
@app.get("/api/settings/<app_name>/profiles")
@login_required
def prof_list(app_name):
    out = []
    for p in db.rows("SELECT * FROM profiles WHERE app=?", (app_name,)):
        out.append(quality.load_profile(p))
    return jsonify(out)


@app.post("/api/settings/<app_name>/profiles")
@login_required
def prof_create(app_name):
    d = request.json
    db.run("INSERT INTO profiles(app,name,cutoff,allowed_qualities,upgrade_allowed,min_format_score,cutoff_format_score) "
           "VALUES(?,?,?,?,?,?,?)",
           (app_name, d.get("name"), d.get("cutoff", "Any"),
            json.dumps(d.get("allowed", [])), int(d.get("upgradeAllowed", True)),
            d.get("minFormatScore", 0), d.get("cutoffFormatScore", 0)))
    return jsonify({"ok": True})


@app.put("/api/settings/profiles/<int:pid>")
@login_required
def prof_update(pid):
    d = request.json
    db.run("UPDATE profiles SET name=?, cutoff=?, allowed_qualities=?, upgrade_allowed=?, "
           "min_format_score=?, cutoff_format_score=? WHERE id=?",
           (d.get("name"), d.get("cutoff"), json.dumps(d.get("allowed", [])),
            int(d.get("upgradeAllowed", True)), d.get("minFormatScore", 0),
            d.get("cutoffFormatScore", 0), pid))
    return jsonify({"ok": True})


@app.delete("/api/settings/profiles/<int:pid>")
@login_required
def prof_delete(pid):
    db.run("DELETE FROM profiles WHERE id=?", (pid,))
    return jsonify({"ok": True})


# ---- custom formats (with specifications) ------------------------------
@app.get("/api/settings/<app_name>/custom-formats")
@login_required
def cf_list(app_name):
    out = []
    for f in db.rows("SELECT * FROM custom_formats WHERE app=?", (app_name,)):
        f["specs"] = db.rows("SELECT * FROM custom_format_specs WHERE format_id=?", (f["id"],))
        out.append(f)
    return jsonify(out)


@app.post("/api/settings/<app_name>/custom-formats")
@login_required
def cf_create(app_name):
    d = request.json
    fid = db.run("INSERT INTO custom_formats(app,name,score) VALUES(?,?,?)",
                 (app_name, d.get("name"), d.get("score", 0)))
    for s in d.get("specs", []):
        db.run("INSERT INTO custom_format_specs(format_id,type,value,negate) VALUES(?,?,?,?)",
               (fid, s.get("type"), s.get("value"), int(s.get("negate", False))))
    return jsonify({"ok": True, "id": fid})


@app.put("/api/settings/custom-formats/<int:fid>")
@login_required
def cf_update(fid):
    d = request.json
    db.run("UPDATE custom_formats SET name=?, score=? WHERE id=?", (d.get("name"), d.get("score", 0), fid))
    if "specs" in d:
        db.run("DELETE FROM custom_format_specs WHERE format_id=?", (fid,))
        for s in d["specs"]:
            db.run("INSERT INTO custom_format_specs(format_id,type,value,negate) VALUES(?,?,?,?)",
                   (fid, s.get("type"), s.get("value"), int(s.get("negate", False))))
    return jsonify({"ok": True})


@app.delete("/api/settings/custom-formats/<int:fid>")
@login_required
def cf_delete(fid):
    db.run("DELETE FROM custom_format_specs WHERE format_id=?", (fid,))
    db.run("DELETE FROM custom_formats WHERE id=?", (fid,))
    return jsonify({"ok": True})


# ---- evaluate a release title against a profile (test tool) ------------
@app.post("/api/settings/<app_name>/evaluate")
@login_required
def evaluate_release(app_name):
    d = request.json
    prow = db.one("SELECT * FROM profiles WHERE id=?", (d.get("profileId"),)) \
        or config_store.default_profile(app_name)
    if not prow:
        return jsonify({"error": "no profile"}), 400
    profile = quality.load_profile(prow)
    rel = {"title": d.get("title", ""), "seeders": d.get("seeders", 0), "size": d.get("size", 0)}
    parsed = quality.parse_release(rel["title"], app_name)
    decision = quality.evaluate(rel, profile, app_name)
    decision.pop("_release", None)
    return jsonify({"parsed": parsed, "decision": decision, "profile": profile["name"]})


@app.get("/api/settings/<app_name>/tags")
@login_required
def tag_list(app_name):
    return jsonify(db.rows("SELECT * FROM tags WHERE app=?", (app_name,)))


@app.post("/api/settings/<app_name>/tags")
@login_required
def tag_create(app_name):
    db.run("INSERT INTO tags(app,label) VALUES(?,?)", (app_name, request.json.get("label")))
    return jsonify({"ok": True})


@app.delete("/api/settings/tags/<int:tid>")
@login_required
def tag_delete(tid):
    db.run("DELETE FROM tags WHERE id=?", (tid,))
    return jsonify({"ok": True})


@app.get("/api/settings/<app_name>/connections")
@login_required
def conn_list(app_name):
    return jsonify(db.rows("SELECT * FROM connections WHERE app=?", (app_name,)))


@app.post("/api/settings/<app_name>/connections")
@login_required
def conn_create(app_name):
    d = request.json
    db.run("INSERT INTO connections(app,name,type,url,on_grab,on_import) VALUES(?,?,?,?,?,?)",
           (app_name, d.get("name"), d.get("type"), d.get("url"),
            int(d.get("onGrab", True)), int(d.get("onImport", True))))
    return jsonify({"ok": True})


@app.delete("/api/settings/connections/<int:cid>")
@login_required
def conn_delete(cid):
    db.run("DELETE FROM connections WHERE id=?", (cid,))
    return jsonify({"ok": True})


# ---- media servers -----------------------------------------------------
@app.get("/api/settings/media-servers")
@login_required
def ms_list():
    return jsonify(db.rows("SELECT * FROM media_servers"))


@app.post("/api/settings/media-servers")
@login_required
def ms_create():
    ms = request.json
    db.run("INSERT INTO media_servers(kind,name,url,api_key,username,password) VALUES(?,?,?,?,?,?)",
           (ms.get("type", "").lower(), ms.get("name", ms.get("type")), ms.get("url", ""),
            ms.get("apiKey", ""), ms.get("username", ""), ms.get("password", "")))
    return jsonify({"ok": True})


# ===========================================================================
# Jobs
# ===========================================================================
@app.post("/api/jobs/sync-libraries")
@login_required
def job_sync():
    return jsonify({"ok": True, "added": pipeline.sync_libraries()})


@app.post("/api/jobs/poll-downloads")
@login_required
def job_poll():
    worker.poll_once()
    return jsonify({"ok": True})


@app.post("/api/jobs/search-upgrades")
@login_required
def job_upgrades():
    return jsonify({"ok": True, "upgraded": pipeline.search_upgrades()})


@app.post("/api/jobs/enrich")
@login_required
def job_enrich():
    return jsonify({"ok": True, "enriched": pipeline.enrich_all()})


@app.post("/api/jobs/search-wanted")
@login_required
def job_wanted():
    return jsonify({"ok": True, "grabbed": pipeline.search_wanted()})


# ===========================================================================
# Import lists (auto-add from Trakt / IMDb / TMDb / IGDB / RSS)
# ===========================================================================
@app.get("/api/settings/<app_name>/lists")
@login_required
def lists_get(app_name):
    return jsonify(db.rows("SELECT * FROM lists WHERE app=?", (app_name,)))


@app.post("/api/settings/<app_name>/lists")
@login_required
def lists_create(app_name):
    d = request.json
    db.run("INSERT INTO lists(app,name,type,url,enabled,auto_add,profile_id) VALUES(?,?,?,?,?,?,?)",
           (app_name, d.get("name"), d.get("type", "RSS"), d.get("url"),
            int(d.get("enabled", True)), int(d.get("autoAdd", False)), d.get("profileId")))
    return jsonify({"ok": True})


@app.put("/api/settings/lists/<int:lid>")
@login_required
def lists_update(lid):
    d = request.json
    db.run("UPDATE lists SET name=?, type=?, url=?, enabled=?, auto_add=?, profile_id=? WHERE id=?",
           (d.get("name"), d.get("type"), d.get("url"), int(d.get("enabled", True)),
            int(d.get("autoAdd", False)), d.get("profileId"), lid))
    return jsonify({"ok": True})


@app.delete("/api/settings/lists/<int:lid>")
@login_required
def lists_delete(lid):
    db.run("DELETE FROM lists WHERE id=?", (lid,))
    return jsonify({"ok": True})


@app.post("/api/settings/lists/<int:lid>/sync")
@login_required
def lists_sync_one(lid):
    lst = db.one("SELECT * FROM lists WHERE id=?", (lid,))
    if not lst:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "result": pipeline.sync_list(lst)})


@app.post("/api/jobs/sync-lists")
@login_required
def job_sync_lists():
    return jsonify({"ok": True, "results": pipeline.sync_lists()})


# ===========================================================================
# Series / seasons / episodes / calendar
# ===========================================================================
@app.get("/api/series/<int:series_id>/seasons")
@login_required
def series_seasons(series_id):
    seasons = db.rows("SELECT * FROM seasons WHERE series_id=? ORDER BY season_number", (series_id,))
    for s in seasons:
        s["episodes"] = db.rows("SELECT * FROM episodes WHERE series_id=? AND season_number=? ORDER BY episode_number",
                                (series_id, s["season_number"]))
    return jsonify(seasons)


@app.put("/api/series/<int:series_id>/season/<int:season_number>/monitor")
@login_required
def monitor_season(series_id, season_number):
    on = int(bool(request.json.get("monitored", True)))
    db.run("UPDATE seasons SET monitored=? WHERE series_id=? AND season_number=?", (on, series_id, season_number))
    db.run("UPDATE episodes SET monitored=? WHERE series_id=? AND season_number=?", (on, series_id, season_number))
    return jsonify({"ok": True})


@app.put("/api/episode/<int:episode_id>/monitor")
@login_required
def monitor_episode(episode_id):
    db.run("UPDATE episodes SET monitored=? WHERE id=?", (int(bool(request.json.get("monitored", True))), episode_id))
    return jsonify({"ok": True})


@app.post("/api/episode/<int:episode_id>/search")
@login_required
def search_episode(episode_id):
    ep = db.one("SELECT * FROM episodes WHERE id=?", (episode_id,))
    if not ep:
        return jsonify({"error": "not found"}), 404
    ok, msg = pipeline.grab_episode(ep)
    return jsonify({"ok": ok, "message": msg})


@app.get("/api/calendar")
@login_required
def calendar():
    start = request.args.get("start", "")
    end = request.args.get("end", "9999-12-31")
    eps = db.rows("SELECT e.*, m.title AS series_title FROM episodes e JOIN media m ON m.id=e.series_id "
                  "WHERE e.air_date IS NOT NULL AND e.air_date<>'' AND e.air_date>=? AND e.air_date<=? "
                  "ORDER BY e.air_date", (start, end))
    return jsonify(eps)


@app.put("/api/media/<int:media_id>/monitor")
@login_required
def monitor_media(media_id):
    db.run("UPDATE media SET monitored=? WHERE id=?", (int(bool(request.json.get("monitored", True))), media_id))
    return jsonify({"ok": True})


# ===========================================================================
# Manual release picker + blocklist
# ===========================================================================
@app.get("/api/releases")
@login_required
def releases():
    mt = request.args.get("type")
    title = request.args.get("title", "")
    pid = request.args.get("profileId")
    return jsonify(pipeline.list_releases(mt, title, int(pid) if pid else None))


@app.post("/api/grab-release")
@login_required
def grab_release():
    d = request.json
    ok, res = pipeline.grab_specific(d["media_type"], d["title"], d["link"], d.get("release_title", ""),
                                     d.get("quality", ""), d.get("cf_score", 0), d.get("rank", 0),
                                     d.get("external_id"), d.get("platform"))
    return (jsonify({"ok": True, "request_id": res}) if ok else jsonify({"ok": False, "message": res}), 200 if ok else 502)


@app.get("/api/blocklist")
@login_required
def blocklist_list():
    return jsonify(db.rows("SELECT * FROM blocklist ORDER BY created_at DESC"))


@app.delete("/api/blocklist/<int:bid>")
@login_required
def blocklist_delete(bid):
    db.run("DELETE FROM blocklist WHERE id=?", (bid,))
    return jsonify({"ok": True})


# ===========================================================================
# Release profiles (preferred / required / ignored words)
# ===========================================================================
@app.get("/api/settings/<app_name>/release-profiles")
@login_required
def rp_list(app_name):
    out = []
    for rp in db.rows("SELECT * FROM release_profiles WHERE app=?", (app_name,)):
        try:
            rp["preferred"] = json.loads(rp.get("preferred") or "[]")
        except (ValueError, TypeError):
            rp["preferred"] = []
        out.append(rp)
    return jsonify(out)


@app.post("/api/settings/<app_name>/release-profiles")
@login_required
def rp_create(app_name):
    d = request.json
    db.run("INSERT INTO release_profiles(app,name,required,ignored,preferred) VALUES(?,?,?,?,?)",
           (app_name, d.get("name"), d.get("required", ""), d.get("ignored", ""),
            json.dumps(d.get("preferred", []))))
    return jsonify({"ok": True})


@app.put("/api/settings/release-profiles/<int:rid>")
@login_required
def rp_update(rid):
    d = request.json
    db.run("UPDATE release_profiles SET name=?, required=?, ignored=?, preferred=? WHERE id=?",
           (d.get("name"), d.get("required", ""), d.get("ignored", ""),
            json.dumps(d.get("preferred", [])), rid))
    return jsonify({"ok": True})


@app.delete("/api/settings/release-profiles/<int:rid>")
@login_required
def rp_delete(rid):
    db.run("DELETE FROM release_profiles WHERE id=?", (rid,))
    return jsonify({"ok": True})


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "setup": db.setup_complete()})


# ===========================================================================
# Solarr API key + metadata providers
# ===========================================================================
def _solarr_api_key():
    k = db.get_setting("solarr_api_key")
    if not k:
        k = secrets.token_hex(16)
        db.set_setting("solarr_api_key", k)
    return k


@app.get("/api/settings/api-key")
@login_required
def get_api_key():
    return jsonify({"apiKey": _solarr_api_key()})


@app.post("/api/settings/api-key/regenerate")
@login_required
def regen_api_key():
    k = secrets.token_hex(16)
    db.set_setting("solarr_api_key", k)
    return jsonify({"apiKey": k})


@app.get("/api/settings/providers")
@login_required
def get_providers():
    return jsonify({"providers": metadata.providers_status(),
                    "showsProvider": db.get_setting("shows_provider", "tmdb")})


@app.post("/api/settings/providers")
@login_required
def set_providers():
    d = request.json or {}
    for k, v in (d.get("fields") or {}).items():
        db.set_setting(k, v)
    if "showsProvider" in d:
        db.set_setting("shows_provider", d["showsProvider"])
    return jsonify({"ok": True, "providers": metadata.providers_status()})


# ===========================================================================
# Prowlarr "App" push shim — Solarr presents as ONE Radarr/Sonarr-style app so
# Prowlarr (Settings -> Apps) can sync indexers into it. Auth is X-Api-Key
# against the Solarr API key (NOT the session cookie).
# ===========================================================================
def _require_api_key():
    supplied = request.headers.get("X-Api-Key") or request.args.get("apikey")
    return supplied and supplied == _solarr_api_key()


def _apikey_guard():
    if not _require_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.get("/api/v3/system/status")
def v3_status():
    supplied = request.headers.get("X-Api-Key") or request.args.get("apikey")
    if supplied:
        g = _apikey_guard()
        if g:
            return g
    return jsonify({
        "appName": "Solarr", "instanceName": "Solarr",
        # Prowlarr gates on the app version; report a current Radarr/Sonarr-era
        # version (v3 API) so it passes the minimum-version check.
        "version": "5.14.0.9383", "buildTime": db.now(),
        "isProduction": True, "authentication": "apikey",
        "urlBase": "", "runtimeName": "python", "runtimeVersion": "3.12.0",
        "osName": "linux", "branch": "master",
    })


def _indexer_out(row):
    return {
        "id": row["id"], "name": row["name"],
        "implementation": row["implementation"],
        "configContract": row["implementation"] + "Settings",
        "enable": bool(row["enable"]), "priority": row["priority"],
        "protocol": "torrent" if row["implementation"] == "Torznab" else "usenet",
        "fields": [
            {"name": "baseUrl", "value": row["base_url"]},
            {"name": "apiPath", "value": row["api_path"] or "/api"},
            {"name": "apiKey", "value": row["api_key"]},
            {"name": "categories", "value": json.loads(row["categories"] or "[]")},
        ],
    }


@app.get("/api/v3/indexer")
def v3_indexer_list():
    g = _apikey_guard()
    if g:
        return g
    return jsonify([_indexer_out(r) for r in db.rows("SELECT * FROM synced_indexers")])


@app.post("/api/v3/indexer")
def v3_indexer_add():
    g = _apikey_guard()
    if g:
        return g
    d = request.json or {}
    f = {x.get("name"): x.get("value") for x in d.get("fields", [])}
    cats = f.get("categories") or []
    iid = db.run(
        "INSERT INTO synced_indexers(name,implementation,base_url,api_path,api_key,categories,enable,priority,added_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (d.get("name", ""), d.get("implementation", "Torznab"), f.get("baseUrl", ""),
         f.get("apiPath", "/api"), f.get("apiKey", ""), json.dumps(cats),
         int(d.get("enable", True)), int(d.get("priority", 25)), db.now()))
    return jsonify(_indexer_out(db.one("SELECT * FROM synced_indexers WHERE id=?", (iid,)))), 201


@app.route("/api/v3/indexer/<int:iid>", methods=["PUT"])
def v3_indexer_update(iid):
    g = _apikey_guard()
    if g:
        return g
    d = request.json or {}
    f = {x.get("name"): x.get("value") for x in d.get("fields", [])}
    db.run("UPDATE synced_indexers SET name=?,base_url=?,api_key=?,categories=?,enable=?,priority=? WHERE id=?",
           (d.get("name", ""), f.get("baseUrl", ""), f.get("apiKey", ""),
            json.dumps(f.get("categories") or []), int(d.get("enable", True)),
            int(d.get("priority", 25)), iid))
    row = db.one("SELECT * FROM synced_indexers WHERE id=?", (iid,))
    return jsonify(_indexer_out(row)) if row else ("", 404)


@app.route("/api/v3/indexer/<int:iid>", methods=["DELETE"])
def v3_indexer_delete(iid):
    g = _apikey_guard()
    if g:
        return g
    db.run("DELETE FROM synced_indexers WHERE id=?", (iid,))
    return jsonify({})


@app.get("/api/v3/indexer/schema")
def v3_indexer_schema():
    g = _apikey_guard()
    if g:
        return g
    def schema(impl, proto):
        return {"implementation": impl, "configContract": impl + "Settings",
                "protocol": proto, "name": impl,
                "fields": [{"name": "baseUrl"}, {"name": "apiPath"}, {"name": "apiKey"}, {"name": "categories"}]}
    return jsonify([schema("Torznab", "torrent"), schema("Newznab", "usenet")])


@app.post("/api/v3/indexer/test")
def v3_indexer_test():
    # Prowlarr posts an indexer definition here to verify the app accepts it.
    # Radarr/Sonarr return 200 with an empty body on success.
    g = _apikey_guard()
    if g:
        return g
    return jsonify({}), 200


@app.post("/api/v3/indexer/testall")
def v3_indexer_testall():
    g = _apikey_guard()
    if g:
        return g
    rows = db.rows("SELECT * FROM synced_indexers")
    return jsonify([{"id": r["id"], "isValid": True, "validationFailures": []} for r in rows])


@app.get("/api/v3/tag")
def v3_tag_list():
    g = _apikey_guard()
    if g:
        return g
    return jsonify([{"id": t["id"], "label": t["name"]} for t in db.rows("SELECT * FROM tags")])


@app.post("/api/v3/tag")
def v3_tag_add():
    g = _apikey_guard()
    if g:
        return g
    label = (request.json or {}).get("label", "")
    tid = db.run("INSERT INTO tags(name) VALUES(?)", (label,))
    return jsonify({"id": tid, "label": label}), 201


@app.get("/api/v3/rootfolder")
def v3_rootfolder():
    g = _apikey_guard()
    if g:
        return g
    return jsonify([{"id": r["id"], "path": r["path"], "accessible": True}
                    for r in db.rows("SELECT * FROM root_folders")])


@app.get("/api/v3/downloadclient")
def v3_downloadclient():
    g = _apikey_guard()
    if g:
        return g
    return jsonify([{"id": c["id"], "name": c["name"], "enable": bool(c["enabled"]),
                     "protocol": "torrent"} for c in db.rows("SELECT * FROM download_clients")])


@app.get("/api/v3/qualityprofile")
def v3_qualityprofile():
    g = _apikey_guard()
    if g:
        return g
    return jsonify([{"id": p["id"], "name": p["name"]} for p in db.rows("SELECT * FROM profiles")])


# ---- serve the single-file frontend ------------------------------------
import os as _os
_WEB = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "web")


@app.get("/")
def index():
    from flask import send_from_directory
    return send_from_directory(_WEB, "index.html")


if __name__ == "__main__":
    if db.setup_complete():
        worker.start()
    app.run(host="0.0.0.0", port=5000)
