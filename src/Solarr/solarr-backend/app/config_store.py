import db


# ---- Prowlarr -----------------------------------------------------------
def get_prowlarr():
    return db.one("SELECT * FROM prowlarr WHERE id=1") or {
        "url": "", "api_key": "", "connected": 0
    }


def save_prowlarr(url, api_key, connected):
    db.run(
        "INSERT INTO prowlarr(id,url,api_key,connected) VALUES(1,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET url=?, api_key=?, connected=?",
        (url, api_key, int(connected), url, api_key, int(connected)),
    )


# ---- media servers ------------------------------------------------------
def media_servers(kind=None):
    if kind:
        return db.rows("SELECT * FROM media_servers WHERE kind=?", (kind,))
    return db.rows("SELECT * FROM media_servers")


def first_server(kind):
    r = db.rows("SELECT * FROM media_servers WHERE kind=? LIMIT 1", (kind,))
    return r[0] if r else None


# ---- download clients ---------------------------------------------------
def download_clients(for_type=None):
    cs = db.rows("SELECT * FROM download_clients WHERE enabled=1")
    if for_type:
        col = {"movie": "for_movies", "show": "for_shows", "game": "for_games", "anime": "for_anime"}[for_type]
        cs = [c for c in cs if c[col]]
    return cs


def category_for(client, media_type):
    return {"movie": client["cat_movie"], "show": client["cat_show"],
            "game": client["cat_game"], "anime": client["cat_anime"]}[media_type]


# ---- root folders -------------------------------------------------------
def root_folder_for(media_type):
    col = {"movie": "for_movies", "show": "for_shows", "game": "for_games", "anime": "for_anime"}[media_type]
    r = db.rows(f"SELECT * FROM root_folders WHERE {col}=1 LIMIT 1")
    return r[0] if r else None


# ---- profiles / tags / connections -------------------------------------
def profiles(app):
    return db.rows("SELECT * FROM profiles WHERE app=?", (app,))


def default_profile(app):
    r = profiles(app)
    return r[0] if r else None


def connections(app):
    return db.rows(
        "SELECT * FROM connections WHERE app=? OR app='all'", (app,)
    )
