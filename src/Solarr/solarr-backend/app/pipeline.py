import os
import re
import json
import shutil
import zipfile
import tempfile
from datetime import date
import requests

import db
import config_store
import quality
from clients import prowlarr, qbittorrent, jellyfin, romm, metadata


# ---------------------------------------------------------------------------
# Grab: choose a download client, resolve save path, hand off the release.
# ---------------------------------------------------------------------------
def _save_path(media_type, req):
    rf = config_store.root_folder_for(media_type)
    base = rf["path"] if rf else "/downloads"
    if media_type == "game":
        plat = _platform_slug(req.get("platform") or "misc")
        return os.path.join(base, "roms", plat)
    return base


def grab(req):
    mt = req["media_type"]
    app = {"movie": "movies", "show": "shows", "game": "games", "anime": "anime"}[mt]
    clients = config_store.download_clients(mt)
    if not clients:
        return False, "No download client configured for this type"

    prow = db.one("SELECT * FROM profiles WHERE id=?", (req.get("profile_id"),)) \
        or config_store.default_profile(app)
    if not prow:
        return False, "No quality profile configured"
    profile = quality.load_profile(prow)

    results = _search_filtered(req["title"], mt, req["title"])
    if not results:
        return False, "No releases found on any indexer"

    best = quality.pick_best(results, profile, app)
    if not best:
        # everything was rejected — surface why (first rejection reason)
        _, evaluated = quality.rank_releases(results, profile, app)
        reason = next((e["reason"] for e in evaluated if not e["accepted"]), "no acceptable release")
        return False, f"No release passed the '{profile['name']}' profile: {reason}"

    rel = best["_release"]
    client = clients[0]
    category = config_store.category_for(client, mt)
    save = _save_path(mt, req)
    link = rel.get("magnet") or rel.get("download_url")

    if not qbittorrent.add(client, link, category, save):
        return False, "Download client rejected the release"

    fmt = (" +" + str(best["cf_score"])) if best["cf_score"] else ""
    db.run(
        "UPDATE requests SET status='Downloading', release_title=?, quality=?, cf_score=?, quality_rank=?, "
        "download_hash=?, message=?, updated_at=? WHERE id=?",
        (rel["title"], best["quality"], best["cf_score"], best["rank"], rel.get("title"),
         f"Grabbed {best['quality']}{fmt} from {rel.get('indexer', 'indexer')}", db.now(), req["id"]),
    )
    return True, rel["title"]


# ---------------------------------------------------------------------------
# Import: find the finished download, extract/organize into the library,
# then trigger a rescan on the right media server.
# ---------------------------------------------------------------------------
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v"}
ROM_EXT = {".sfc", ".smc", ".nes", ".gba", ".gb", ".gbc", ".n64", ".z64",
           ".iso", ".chd", ".nsp", ".xci", ".rom", ".bin", ".gen", ".md"}
ARCHIVE_EXT = {".zip"}  # .7z/.rar require external tools (see notes)


def _platform_slug(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "misc").lower()) or "misc"


def _primary_file(root, media_type):
    wanted = VIDEO_EXT if media_type in ("movie", "show") else ROM_EXT
    best, best_size = None, -1
    for dirpath, _, files in os.walk(root):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(dirpath, f)
            size = os.path.getsize(full)
            if ext in wanted and size > best_size:
                best, best_size = full, size
    if best:
        return best
    # fallback: largest file overall
    for dirpath, _, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            size = os.path.getsize(full)
            if size > best_size:
                best, best_size = full, size
    return best


def _extract_if_archive(path, workdir):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            z.extractall(workdir)
        return workdir
    if ext in {".7z", ".rar"}:
        raise RuntimeError(f"{ext} extraction needs external tooling (not bundled)")
    return None


def _target_name(req, ext):
    mt = req["media_type"]
    title = re.sub(r'[\\/:*?"<>|]', "", req["title"])
    if mt == "movie":
        return os.path.join(f"{title}", f"{title}{ext}")
    if mt in ("show", "anime"):
        m = re.search(r"(.+?)[.\s]+S(\d+)E(\d+)", title, re.IGNORECASE)
        if m:
            series = m.group(1).strip()
            sn, en = int(m.group(2)), int(m.group(3))
            return os.path.join(series, f"Season {sn:02d}", f"{series} - S{sn:02d}E{en:02d}{ext}")
        return os.path.join(f"{title}", f"{title}{ext}")
    return f"{title}{ext}"  # game rom lands directly in roms/{platform}/


def import_download(req, content_path):
    """content_path: the finished download's file or folder on the shared mount."""
    mt = req["media_type"]
    src = content_path
    tmp = None

    if os.path.isfile(content_path) and os.path.splitext(content_path)[1].lower() in ARCHIVE_EXT:
        tmp = tempfile.mkdtemp(prefix="solarr-extract-")
        _extract_if_archive(content_path, tmp)
        src = tmp

    root = src if os.path.isdir(src) else os.path.dirname(src)
    primary = src if os.path.isfile(src) else _primary_file(root, mt)
    if not primary:
        return False, "No importable file found in download"

    ext = os.path.splitext(primary)[1].lower()
    dest_base = _save_path(mt, req) if mt == "game" else \
        (config_store.root_folder_for(mt) or {"path": "/library"})["path"]
    dest = os.path.join(dest_base, _target_name(req, ext))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(primary, dest)

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    try:
        _write_metadata_files(mt, req, dest)
    except Exception:
        pass

    _rescan(mt)
    return True, dest


def _xesc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _write_metadata_files(mt, req, dest):
    """Write Kodi/Jellyfin-style .nfo sidecars (+ optional poster) when the
    per-app Metadata setting is enabled. Games are managed by RomM, so skipped."""
    if mt not in ("movie", "show"):
        return
    app = {"movie": "movies", "show": "shows", "anime": "anime"}[mt]
    if db.get_setting(f"meta_{app}_nfo", "0") != "1":
        return
    m = db.one("SELECT * FROM media WHERE media_type=? AND title=?", (mt, req["title"])) or {}
    genres = []
    try:
        genres = json.loads(m.get("genres") or "[]")
    except (ValueError, TypeError):
        genres = []
    folder = os.path.dirname(dest)
    root = "movie" if mt == "movie" else "tvshow"
    lines = [f"<{root}>",
             f"  <title>{_xesc(m.get('title') or req['title'])}</title>",
             f"  <year>{m.get('year') or ''}</year>",
             f"  <plot>{_xesc(m.get('overview'))}</plot>",
             f"  <rating>{(m.get('score') or 0) / 10.0:.1f}</rating>",
             f"  <studio>{_xesc(m.get('studio'))}</studio>",
             f"  <mpaa>{_xesc(m.get('content_rating'))}</mpaa>"]
    lines += [f"  <genre>{_xesc(g)}</genre>" for g in genres]
    if m.get("external_id"):
        src = "tmdb" if mt in ("movie", "show") else ""
        lines.append(f'  <uniqueid type="{src}">{_xesc(m.get("external_id"))}</uniqueid>')
    lines.append(f"</{root}>")
    nfo_name = "movie.nfo" if mt == "movie" else "tvshow.nfo"
    with open(os.path.join(folder, nfo_name), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    if db.get_setting(f"meta_{app}_poster", "0") == "1" and m.get("cover_url"):
        try:
            img = requests.get(m["cover_url"], timeout=10)
            if img.status_code == 200:
                with open(os.path.join(folder, "poster.jpg"), "wb") as f:
                    f.write(img.content)
        except requests.RequestException:
            pass


def _rescan(media_type):
    if media_type == "game":
        s = config_store.first_server("romm")
        if s:
            romm.rescan(s)
    else:
        s = config_store.first_server("jellyfin")
        if s:
            jellyfin.scan(s)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def notify(app, event, req):
    for conn in config_store.connections(app):
        flag = "on_grab" if event == "grab" else "on_import"
        if not conn.get(flag) or not conn.get("url"):
            continue
        payload = {"event": event, "title": req["title"], "type": req["media_type"],
                   "status": req.get("status"), "release": req.get("release_title")}
        try:
            requests.post(conn["url"], json=payload, timeout=6)
        except requests.RequestException:
            pass


# ---------------------------------------------------------------------------
# Library sync: pull existing items from Jellyfin + RomM into the catalogue.
# ---------------------------------------------------------------------------
def sync_libraries():
    added = 0
    jf = config_store.first_server("jellyfin")
    if jf:
        for mt in ("movie", "show"):
            for it in jellyfin.list_items(jf, mt):
                added += _upsert_media(mt, it, source="jellyfin")
    rm = config_store.first_server("romm")
    if rm:
        for it in romm.list_games(rm):
            it2 = {"title": it["title"], "external_id": it["external_id"],
                   "year": None, "platform": it.get("platform"), "cover_url": it.get("cover_url")}
            added += _upsert_media("game", it2, source="romm")
    return added


def _upsert_media(media_type, it, source):
    existing = db.one(
        "SELECT id FROM media WHERE media_type=? AND title=?",
        (media_type, it["title"]),
    )
    if existing:
        db.run("UPDATE media SET status='available', source=? WHERE id=?", (source, existing["id"]))
        return 0
    db.run(
        "INSERT INTO media(media_type,title,external_id,year,platform,cover_url,status,source,added_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (media_type, it["title"], it.get("external_id"), it.get("year"),
         it.get("platform"), it.get("cover_url"), "available", source, db.now()),
    )
    return 1


# ---------------------------------------------------------------------------
# Upgrade search: re-check available titles and grab a better release if the
# quality profile says it's an upgrade over what we already have.
# ---------------------------------------------------------------------------
def search_upgrades():
    """Returns list of {title, from, to, reason} for titles upgraded."""
    upgraded = []
    available = db.rows("SELECT * FROM media WHERE status='available'")
    for m in available:
        mt = m["media_type"]
        app = {"movie": "movies", "show": "shows", "game": "games", "anime": "anime"}[mt]
        prow = config_store.default_profile(app)
        if not prow:
            continue
        profile = quality.load_profile(prow)
        cutoff_rank = quality.rank_of(app, profile["cutoff"])

        current = {"rank": m.get("quality_rank") or 0, "cf_score": m.get("cf_score") or 0}
        # already at/above cutoff on both axes -> skip the indexer hit entirely
        if current["rank"] >= cutoff_rank and current["cf_score"] >= profile.get("cutoff_format_score", 0):
            continue

        results = [r for r in prowlarr.search(m["title"], mt) if r.get("magnet") or r.get("download_url")]
        best = quality.pick_best(results, profile, app)
        if not best:
            continue
        candidate = {"rank": best["rank"], "cf_score": best["cf_score"]}
        if not quality.is_upgrade(current, candidate, profile, app):
            continue

        clients = config_store.download_clients(mt)
        if not clients:
            continue
        client = clients[0]
        rel = best["_release"]
        save = _save_path(mt, {"platform": m.get("platform")})
        link = rel.get("magnet") or rel.get("download_url")
        if qbittorrent.add(client, link, config_store.category_for(client, mt), save):
            rid = db.run(
                "INSERT INTO requests(media_type,title,external_id,platform,profile_id,status,release_title,"
                "quality,cf_score,quality_rank,requested_by,message,requested_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mt, m["title"], m.get("external_id"), m.get("platform"), prow["id"], "Downloading",
                 rel["title"], best["quality"], best["cf_score"], best["rank"], "system",
                 f"Upgrade: {m.get('quality') or 'unknown'} -> {best['quality']}", db.now(), db.now()),
            )
            db.run("UPDATE media SET status='downloading' WHERE id=?", (m["id"],))
            upgraded.append({
                "title": m["title"],
                "from": m.get("quality") or "unknown",
                "to": best["quality"],
                "reason": f"rank {current['rank']}->{candidate['rank']}, cf {current['cf_score']}->{candidate['cf_score']}",
                "request_id": rid,
            })
    return upgraded


# ===========================================================================
# Blocklist + filtered search
# ===========================================================================
def blocklisted_titles(media_title):
    return {b["release_title"] for b in
            db.rows("SELECT release_title FROM blocklist WHERE title=?", (media_title,))}


def add_blocklist(media_type, title, release_title, reason):
    db.run("INSERT INTO blocklist(media_type,title,release_title,reason,created_at) VALUES(?,?,?,?,?)",
           (media_type, title, release_title, reason, db.now()))


def _search_filtered(query, media_type, media_title):
    bl = blocklisted_titles(media_title)
    return [r for r in prowlarr.search(query, media_type)
            if (r.get("magnet") or r.get("download_url")) and r.get("title") not in bl]


# ===========================================================================
# Metadata enrichment
# ===========================================================================
def enrich_media(m):
    """Fetch full metadata for a media row and store it. Resolves external_id
    by title search when missing (e.g. items synced from Jellyfin/RomM)."""
    ext = m.get("external_id")
    if not ext or not str(ext).isdigit():
        results = metadata.search(m["title"], m["media_type"])
        if not results:
            return False
        ext = results[0]["external_id"]
        db.run("UPDATE media SET external_id=? WHERE id=?", (ext, m["id"]))
    d = metadata.detail(ext, m["media_type"])
    if not d:
        return False
    db.run(
        "UPDATE media SET overview=?, genres=?, score=?, votes=?, studio=?, runtime=?, "
        "language=?, content_rating=?, year=COALESCE(year,?), platform=COALESCE(NULLIF(platform,''),?), "
        "cover_url=CASE WHEN cover_url IS NULL OR cover_url='' THEN ? ELSE cover_url END, "
        "enriched=1 WHERE id=?",
        (d.get("overview"), json.dumps(d.get("genres", [])), d.get("score"), d.get("votes"),
         d.get("studio"), d.get("runtime"), d.get("language"), d.get("content_rating"),
         d.get("year"), d.get("platform"), d.get("cover_url"), m["id"]),
    )
    if m["media_type"] == "show":
        populate_series(m["id"], ext, d.get("seasons", []))
    return True


def enrich_all(limit=200):
    n = 0
    for m in db.rows("SELECT * FROM media WHERE enriched=0 LIMIT ?", (limit,)):
        try:
            if enrich_media(m):
                n += 1
        except Exception:
            pass
    return n


# ===========================================================================
# Series / seasons / episodes (Sonarr model)
# ===========================================================================
def populate_series(series_id, external_id, seasons):
    for s in seasons:
        sn = s.get("season_number")
        if sn is None:
            continue
        if not db.one("SELECT id FROM seasons WHERE series_id=? AND season_number=?", (series_id, sn)):
            db.run("INSERT INTO seasons(series_id,season_number,episode_count,monitored) VALUES(?,?,?,1)",
                   (series_id, sn, s.get("episode_count") or 0))
        for e in metadata.tmdb_episodes(external_id, sn):
            en = e.get("episode_number")
            if en is None:
                continue
            if not db.one("SELECT id FROM episodes WHERE series_id=? AND season_number=? AND episode_number=?",
                          (series_id, sn, en)):
                db.run("INSERT INTO episodes(series_id,season_number,episode_number,title,air_date,monitored,status) "
                       "VALUES(?,?,?,?,?,1,'missing')",
                       (series_id, sn, en, e.get("title"), e.get("air_date")))


def grab_episode(ep):
    series = db.one("SELECT * FROM media WHERE id=?", (ep["series_id"],))
    if not series:
        return False, "series not found"
    clients = config_store.download_clients("show")
    if not clients:
        return False, "no download client for shows"
    profile = quality.load_profile(config_store.default_profile("shows"))
    q = f"{series['title']} S{ep['season_number']:02d}E{ep['episode_number']:02d}"
    results = _search_filtered(q, "show", series["title"])
    best = quality.pick_best(results, profile, "shows")
    if not best:
        return False, "no acceptable release"
    client = clients[0]
    rel = best["_release"]
    save = _save_path("show", {"title": series["title"]})
    link = rel.get("magnet") or rel.get("download_url")
    if not qbittorrent.add(client, link, config_store.category_for(client, "show"), save):
        return False, "client rejected release"
    db.run("UPDATE episodes SET status='downloading', quality=?, cf_score=?, quality_rank=? WHERE id=?",
           (best["quality"], best["cf_score"], best["rank"], ep["id"]))
    db.run("INSERT INTO requests(media_type,title,external_id,profile_id,status,release_title,quality,"
           "cf_score,quality_rank,requested_by,message,requested_at,updated_at) "
           "VALUES('show',?,?,?,?,?,?,?,?,?,?,?,?)",
           (q, series.get("external_id"), None, "Downloading", rel["title"], best["quality"],
            best["cf_score"], best["rank"], "system", f"Episode grab S{ep['season_number']:02d}E{ep['episode_number']:02d}",
            db.now(), db.now()))
    return True, rel["title"]


# ===========================================================================
# Wanted / interval search (RSS-equivalent auto-grab)
# ===========================================================================
def grab_aired_episodes(series_id=None):
    """Grab every monitored, missing episode that has already aired (optionally
    limited to one series). Returns a list of what was grabbed."""
    today = date.today().isoformat()
    where = ("monitored=1 AND status='missing' AND air_date IS NOT NULL "
             "AND air_date<>'' AND air_date<=?")
    args = [today]
    if series_id:
        where += " AND series_id=?"
        args.append(series_id)
    grabbed = []
    for ep in db.rows(f"SELECT * FROM episodes WHERE {where}", tuple(args)):
        ok, msg = grab_episode(ep)
        if ok:
            s = db.one("SELECT title FROM media WHERE id=?", (ep["series_id"],))
            grabbed.append({"title": f"{s['title']} S{ep['season_number']:02d}E{ep['episode_number']:02d}",
                            "type": "episode", "release": msg})
    return grabbed


def search_wanted():
    """Grab monitored movies without a file and monitored episodes that have aired."""
    grabbed = []
    for m in db.rows("SELECT * FROM media WHERE media_type='movie' AND monitored=1 "
                     "AND status NOT IN ('available','downloading')"):
        rid = db.run("INSERT INTO requests(media_type,title,external_id,platform,profile_id,status,"
                     "requested_by,requested_at,updated_at) VALUES('movie',?,?,?,?,?,?,?,?)",
                     (m["title"], m.get("external_id"), m.get("platform"),
                      (config_store.default_profile("movies") or {}).get("id"),
                      "Searching", "system", db.now(), db.now()))
        req = db.one("SELECT * FROM requests WHERE id=?", (rid,))
        ok, msg = grab(req)
        if ok:
            db.run("UPDATE media SET status='downloading' WHERE id=?", (m["id"],))
            grabbed.append({"title": m["title"], "type": "movie", "release": msg})
        else:
            db.run("UPDATE requests SET status='Failed', message=?, updated_at=? WHERE id=?", (msg, db.now(), rid))
    grabbed.extend(grab_aired_episodes())
    return grabbed


# ===========================================================================
# Manual release picker
# ===========================================================================
def list_releases(media_type, title, profile_id=None):
    app = {"movie": "movies", "show": "shows", "game": "games", "anime": "anime"}[media_type]
    prow = (db.one("SELECT * FROM profiles WHERE id=?", (profile_id,)) if profile_id
            else None) or config_store.default_profile(app)
    profile = quality.load_profile(prow)
    results = _search_filtered(title, media_type, title)
    _, evaluated = quality.rank_releases(results, profile, app)
    bl = blocklisted_titles(title)
    out = []
    for e in evaluated:
        rel = e["_release"]
        out.append({
            "title": e["title"], "quality": e["quality"], "rank": e["rank"],
            "cf_score": e["cf_score"], "accepted": e["accepted"], "reason": e["reason"],
            "seeders": e["seeders"], "size": e["size"], "indexer": rel.get("indexer"),
            "link": rel.get("magnet") or rel.get("download_url"),
            "blocklisted": rel.get("title") in bl,
        })
    out.sort(key=lambda x: (x["accepted"], x["rank"], x["cf_score"], x["seeders"]), reverse=True)
    return out


def grab_specific(media_type, title, link, release_title, quality_name, cf_score, rank, external_id=None, platform=None):
    clients = config_store.download_clients(media_type)
    if not clients:
        return False, "no download client"
    client = clients[0]
    save = _save_path(media_type, {"platform": platform, "title": title})
    if not qbittorrent.add(client, link, config_store.category_for(client, media_type), save):
        return False, "client rejected release"
    rid = db.run("INSERT INTO requests(media_type,title,external_id,platform,status,release_title,quality,"
                 "cf_score,quality_rank,requested_by,message,requested_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (media_type, title, external_id, platform, "Downloading", release_title, quality_name,
                  cf_score, rank, "manual", "Manually selected release", db.now(), db.now()))
    if not db.one("SELECT id FROM media WHERE media_type=? AND title=?", (media_type, title)):
        db.run("INSERT INTO media(media_type,title,external_id,platform,status,source,added_at) "
               "VALUES(?,?,?,?,?,?,?)", (media_type, title, external_id, platform, "downloading", "request", db.now()))
    else:
        db.run("UPDATE media SET status='downloading' WHERE media_type=? AND title=?", (media_type, title))
    return True, rid


# ===========================================================================
# Import lists (auto-add)
# ===========================================================================
APP_TO_MT = {"movies": "movie", "shows": "show", "games": "game"}


def _fetch_list(lst):
    """Fetch a list URL and return [{title, year, external_id, platform}].
    Supports JSON ({items:[...]} or a bare array) and simple RSS/Atom feeds."""
    try:
        r = requests.get(lst["url"], timeout=15,
                         headers={"User-Agent": "Solarr/1.0", "Accept": "application/json, application/xml;q=0.9"})
        r.raise_for_status()
    except requests.RequestException:
        return []
    ct = r.headers.get("Content-Type", "")
    body = r.text
    items = []
    # JSON
    if "json" in ct or body.lstrip().startswith(("{", "[")):
        try:
            data = r.json()
            rows = data.get("items", data) if isinstance(data, dict) else data
            for it in rows:
                if isinstance(it, str):
                    items.append({"title": it})
                elif isinstance(it, dict):
                    items.append({
                        "title": it.get("title") or it.get("name"),
                        "year": it.get("year"),
                        "external_id": str(it["id"]) if it.get("id") is not None else it.get("external_id"),
                        "platform": it.get("platform"),
                    })
            return [i for i in items if i.get("title")]
        except ValueError:
            pass
    # RSS/Atom: pull <title> elements (skip the channel title = first)
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", body, re.S)
    for t in titles[1:]:
        t = re.sub(r"<[^>]+>", "", t).strip()
        if t:
            items.append({"title": t})
    return items


def sync_list(lst):
    mt = APP_TO_MT.get(lst["app"], "movie")
    added, requested = 0, 0
    for it in _fetch_list(lst):
        title = it["title"]
        existing = db.one("SELECT * FROM media WHERE media_type=? AND title=?", (mt, title))
        if not existing:
            db.run("INSERT INTO media(media_type,title,external_id,year,platform,status,source,added_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (mt, title, it.get("external_id"), it.get("year"), it.get("platform"),
                    "not_owned", f"list:{lst['name']}", db.now()))
            added += 1
            existing = db.one("SELECT * FROM media WHERE media_type=? AND title=?", (mt, title))
        if lst.get("auto_add") and existing and existing["status"] in ("not_owned", None):
            if auto_request(mt, title, existing.get("external_id"), existing.get("platform"),
                            lst.get("profile_id"), existing["id"]):
                requested += 1
    db.run("UPDATE lists SET last_synced=?, last_count=? WHERE id=?",
           (db.now(), added, lst["id"]))
    return {"list": lst["name"], "added": added, "requested": requested}


def sync_lists():
    return [sync_list(l) for l in db.rows("SELECT * FROM lists WHERE enabled=1")]


def auto_request(mt, title, external_id, platform, profile_id, media_id):
    """Create a request from a list item and kick off the grab, mirroring the
    manual request flow (episode-aware for shows)."""
    app = {"movie": "movies", "show": "shows", "game": "games", "anime": "anime"}[mt]
    pid = profile_id or (config_store.default_profile(app) or {}).get("id")
    rid = db.run("INSERT INTO requests(media_type,title,external_id,platform,profile_id,status,"
                 "requested_by,message,requested_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (mt, title, external_id, platform, pid, "Searching", "list",
                  "Auto-added from list", db.now(), db.now()))
    db.run("UPDATE media SET status='requested' WHERE id=?", (media_id,))
    m = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    try:
        enrich_media(m)
    except Exception:
        pass
    if mt == "show":
        grabbed = grab_aired_episodes(media_id)
        db.run("UPDATE requests SET status=?, message=?, updated_at=? WHERE id=?",
               ("Downloading" if grabbed else "Wanted", f"{len(grabbed)} episode(s) grabbed", db.now(), rid))
        if grabbed:
            db.run("UPDATE media SET status='downloading' WHERE id=?", (media_id,))
        return True
    req = db.one("SELECT * FROM requests WHERE id=?", (rid,))
    ok, msg = grab(req)
    if ok:
        db.run("UPDATE media SET status='downloading' WHERE id=?", (media_id,))
        notify(app, "grab", db.one("SELECT * FROM requests WHERE id=?", (rid,)))
    else:
        db.run("UPDATE requests SET status='Failed', message=?, updated_at=? WHERE id=?", (msg, db.now(), rid))
    return ok
