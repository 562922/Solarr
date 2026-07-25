import threading
import time

import db
import config_store
import pipeline
from clients import qbittorrent

_thread = None
_stop = threading.Event()


def poll_once():
    """Check every Downloading request; import the ones that have finished."""
    reqs = db.rows("SELECT * FROM requests WHERE status='Downloading'")
    for req in reqs:
        mt = req["media_type"]
        clients = config_store.download_clients(mt)
        if not clients:
            continue
        client = clients[0]
        category = config_store.category_for(client, mt)
        try:
            t = qbittorrent.find_by_name(client, category, req["title"])
        except Exception:
            t = None
        if not t:
            continue
        if (t.get("progress", 0) or 0) < 1.0:
            continue

        content = t.get("content_path") or t.get("save_path")
        db.run("UPDATE requests SET status='Importing', updated_at=? WHERE id=?", (db.now(), req["id"]))
        try:
            ok, msg = pipeline.import_download(req, content)
        except Exception as e:
            ok, msg = False, f"import error: {e}"

        if ok:
            db.run("UPDATE requests SET status='Available', message=?, updated_at=? WHERE id=?",
                   (f"Imported to {msg}", db.now(), req["id"]))
            fresh = db.one("SELECT * FROM requests WHERE id=?", (req["id"],))
            db.run("UPDATE media SET status='available', quality=?, cf_score=?, quality_rank=? "
                   "WHERE media_type=? AND title=?",
                   (fresh.get("quality"), fresh.get("cf_score") or 0, fresh.get("quality_rank") or 0,
                    mt, req["title"]))
            _mark_episode(req, "available")
            app = {"movie": "movies", "show": "shows", "game": "games", "anime": "anime"}[mt]
            pipeline.notify(app, "import", fresh)
        else:
            # blocklist the bad release and retry with the next-best acceptable one
            if req.get("release_title"):
                pipeline.add_blocklist(mt, req["title"], req["release_title"], msg)
            retry = dict(req); retry["release_title"] = None
            ok2, msg2 = pipeline.grab(retry)
            if ok2:
                db.run("UPDATE requests SET message=?, updated_at=? WHERE id=?",
                       (f"Retried after failure: {msg2}", db.now(), req["id"]))
            else:
                db.run("UPDATE requests SET status='Failed', message=?, updated_at=? WHERE id=?",
                       (f"{msg}; retry: {msg2}", db.now(), req["id"]))
                _mark_episode(req, "missing")


def _mark_episode(req, status):
    """If a request title looks like 'Series SxxExx', reflect status on the episode."""
    import re
    m = re.search(r"(.+?)\s+S(\d+)E(\d+)", req["title"], re.IGNORECASE)
    if not m:
        return
    series = db.one("SELECT id FROM media WHERE media_type='show' AND title=?", (m.group(1).strip(),))
    if series:
        db.run("UPDATE episodes SET status=? WHERE series_id=? AND season_number=? AND episode_number=?",
               (status, series["id"], int(m.group(2)), int(m.group(3))))


def _loop(interval):
    while not _stop.wait(interval):
        try:
            poll_once()
        except Exception:
            pass


def start(interval=15):
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), daemon=True)
    _thread.start()


def stop():
    _stop.set()
