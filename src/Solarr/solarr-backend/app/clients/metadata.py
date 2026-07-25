import os
import time
import requests
import db

_igdb_token = {"tok": None, "exp": 0}

# Configurable bases so tests can point these at mock servers.
TMDB_BASE = os.getenv("TMDB_BASE", "https://api.themoviedb.org/3")
TMDB_IMG = os.getenv("TMDB_IMG", "https://image.tmdb.org/t/p")
IGDB_BASE = os.getenv("IGDB_BASE", "https://api.igdb.com/v4")
TWITCH_TOKEN_URL = os.getenv("TWITCH_TOKEN_URL", "https://id.twitch.tv/oauth2/token")


# ---- TMDb (movies & shows) ---------------------------------------------
def _tmdb_key():
    return db.get_setting("tmdb_api_key", "")


def _img(path, size="w342"):
    return f"{TMDB_IMG}/{size}{path}" if path else ""


def tmdb_search(query, media_type):
    key = _tmdb_key()
    if not key:
        return []
    kind = "movie" if media_type == "movie" else "tv"
    try:
        r = requests.get(f"{TMDB_BASE}/search/{kind}",
                         params={"api_key": key, "query": query}, timeout=10)
        r.raise_for_status()
        out = []
        for it in r.json().get("results", [])[:20]:
            # keep anime out of the movies/shows sections: Japanese-language
            # animation is routed to the Anime section (AniList) instead.
            if 16 in (it.get("genre_ids") or []) and it.get("original_language") == "ja":
                continue
            date = it.get("release_date") or it.get("first_air_date") or ""
            out.append({
                "title": it.get("title") or it.get("name"),
                "external_id": str(it.get("id")),
                "year": int(date[:4]) if date[:4].isdigit() else None,
                "cover_url": _img(it.get("poster_path")),
                "overview": it.get("overview", ""),
                "score": round((it.get("vote_average") or 0) * 10),
                "votes": it.get("vote_count", 0),
            })
        return out
    except requests.RequestException:
        return []


def _us_cert_movie(it):
    for r in (it.get("release_dates", {}) or {}).get("results", []):
        if r.get("iso_3166_1") == "US":
            for d in r.get("release_dates", []):
                if d.get("certification"):
                    return d["certification"]
    return ""


def _us_cert_tv(it):
    for r in (it.get("content_ratings", {}) or {}).get("results", []):
        if r.get("iso_3166_1") == "US" and r.get("rating"):
            return r["rating"]
    return ""


def tmdb_detail(external_id, media_type):
    key = _tmdb_key()
    kind = "movie" if media_type == "movie" else "tv"
    append = "release_dates" if kind == "movie" else "content_ratings"
    try:
        r = requests.get(f"{TMDB_BASE}/{kind}/{external_id}",
                         params={"api_key": key, "append_to_response": append}, timeout=10)
        r.raise_for_status()
        it = r.json()
        date = it.get("release_date") or it.get("first_air_date") or ""
        if kind == "movie":
            studio = (it.get("production_companies") or [{}])[0].get("name", "")
            rating = _us_cert_movie(it)
            runtime = it.get("runtime") or 0
        else:
            studio = (it.get("networks") or [{}])[0].get("name", "")
            rating = _us_cert_tv(it)
            runtime = (it.get("episode_run_time") or [0])[0]
        seasons = [{"season_number": s.get("season_number"), "episode_count": s.get("episode_count"),
                    "name": s.get("name"), "air_date": s.get("air_date")}
                   for s in it.get("seasons", []) if s.get("season_number", 0) >= 1]
        return {
            "title": it.get("title") or it.get("name"),
            "external_id": str(it.get("id")),
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "overview": it.get("overview", ""),
            "genres": [g["name"] for g in it.get("genres", [])],
            "runtime": runtime,
            "score": round((it.get("vote_average") or 0) * 10),
            "votes": it.get("vote_count", 0),
            "studio": studio,
            "language": it.get("original_language", ""),
            "content_rating": rating,
            "cover_url": _img(it.get("poster_path"), "w500"),
            "seasons": seasons,
        }
    except requests.RequestException:
        return None


def tmdb_episodes(external_id, season_number):
    key = _tmdb_key()
    try:
        r = requests.get(f"{TMDB_BASE}/tv/{external_id}/season/{season_number}",
                         params={"api_key": key}, timeout=10)
        r.raise_for_status()
        out = []
        for e in r.json().get("episodes", []):
            out.append({
                "episode_number": e.get("episode_number"),
                "title": e.get("name"),
                "air_date": e.get("air_date"),
            })
        return out
    except requests.RequestException:
        return []


# ---- IGDB (games) -------------------------------------------------------
def _igdb_creds():
    return db.get_setting("igdb_client_id", ""), db.get_setting("igdb_client_secret", "")


def _igdb_token_get():
    if _igdb_token["tok"] and time.time() < _igdb_token["exp"]:
        return _igdb_token["tok"]
    cid, secret = _igdb_creds()
    if not cid or not secret:
        return None
    r = requests.post(TWITCH_TOKEN_URL,
                      params={"client_id": cid, "client_secret": secret,
                              "grant_type": "client_credentials"}, timeout=10)
    r.raise_for_status()
    d = r.json()
    _igdb_token["tok"] = d["access_token"]
    _igdb_token["exp"] = time.time() + d.get("expires_in", 3600) - 60
    return _igdb_token["tok"]


def _igdb_headers():
    cid, _ = _igdb_creds()
    return {"Client-ID": cid, "Authorization": f"Bearer {_igdb_token_get()}"}


def _cover(g):
    c = (g.get("cover", {}) or {}).get("url", "") if g.get("cover") else ""
    return ("https:" + c) if c.startswith("//") else c


def igdb_search(query):
    cid, secret = _igdb_creds()
    if not cid or not secret:
        return []
    try:
        r = requests.post(f"{IGDB_BASE}/games", headers=_igdb_headers(),
                          data=f'search "{query}"; fields name,cover.url,first_release_date,platforms.name; limit 20;',
                          timeout=10)
        r.raise_for_status()
        out = []
        for g in r.json():
            yr = time.gmtime(g["first_release_date"]).tm_year if g.get("first_release_date") else None
            plats = [p["name"] for p in g.get("platforms", [])] if g.get("platforms") else []
            out.append({
                "title": g.get("name"), "external_id": str(g.get("id")), "year": yr,
                "platform": plats[0] if plats else "", "cover_url": _cover(g),
            })
        return out
    except (requests.RequestException, ValueError):
        return []


def igdb_detail(external_id):
    cid, secret = _igdb_creds()
    if not cid or not secret:
        return None
    try:
        r = requests.post(f"{IGDB_BASE}/games", headers=_igdb_headers(),
                          data=f"fields name,summary,rating,rating_count,cover.url,genres.name,platforms.name,"
                               f"involved_companies.company.name,first_release_date; where id = {int(external_id)};",
                          timeout=10)
        r.raise_for_status()
        res = r.json()
        if not res:
            return None
        g = res[0]
        companies = [c.get("company", {}).get("name") for c in g.get("involved_companies", [])] if g.get("involved_companies") else []
        yr = time.gmtime(g["first_release_date"]).tm_year if g.get("first_release_date") else None
        return {
            "title": g.get("name"), "external_id": str(g.get("id")),
            "overview": g.get("summary", ""), "year": yr,
            "genres": [x["name"] for x in g.get("genres", [])] if g.get("genres") else [],
            "platform": (g.get("platforms", [{}])[0].get("name") if g.get("platforms") else ""),
            "studio": companies[0] if companies else "",
            "score": round(g.get("rating", 0)) if g.get("rating") else None,
            "votes": g.get("rating_count", 0),
            "cover_url": _cover(g),
        }
    except (requests.RequestException, ValueError):
        return None


# ---- AniList (anime — free, keyless GraphQL) ---------------------------
ANILIST_BASE = os.getenv("ANILIST_BASE", "https://graphql.anilist.co")

_ANILIST_SEARCH = """
query ($q: String) { Page(perPage: 25) { media(search: $q, type: ANIME, sort: SEARCH_MATCH) {
  id format episodes averageScore startDate { year }
  title { romaji english } coverImage { large } description(asHtml: false)
  genres studios(isMain: true) { nodes { name } } } } }
"""

_ANILIST_DETAIL = """
query ($id: Int) { Media(id: $id, type: ANIME) {
  id format episodes duration averageScore startDate { year }
  title { romaji english } coverImage { extraLarge } description(asHtml: false)
  genres studios(isMain: true) { nodes { name } } countryOfOrigin } }
"""


def _strip_html(x):
    import re as _re
    return _re.sub(r"<[^>]+>", "", x or "").replace("&mdash;", "—").strip()


def _anilist_norm(m):
    t = m.get("title") or {}
    st = m.get("studios") or {}
    studio = (st.get("nodes") or [{}])[0].get("name", "") if st.get("nodes") else ""
    fmt = m.get("format") or ""
    return {
        "title": t.get("english") or t.get("romaji"),
        "external_id": str(m.get("id")),
        "year": (m.get("startDate") or {}).get("year"),
        "overview": _strip_html(m.get("description")),
        "genres": m.get("genres") or [],
        "score": m.get("averageScore"),
        "votes": 0,
        "studio": studio,
        "runtime": m.get("duration") or 0,
        "cover_url": (m.get("coverImage") or {}).get("extraLarge")
                     or (m.get("coverImage") or {}).get("large") or "",
        "language": "ja",
        "anime_kind": "movie" if fmt == "MOVIE" else "series",
        "episodes_count": m.get("episodes") or 0,
    }


def anilist_search(query):
    try:
        r = requests.post(ANILIST_BASE, json={"query": _ANILIST_SEARCH, "variables": {"q": query}}, timeout=10)
        r.raise_for_status()
        media = (((r.json() or {}).get("data") or {}).get("Page") or {}).get("media") or []
        return [_anilist_norm(m) for m in media]
    except (requests.RequestException, ValueError):
        return []


def anilist_detail(external_id):
    try:
        r = requests.post(ANILIST_BASE, json={"query": _ANILIST_DETAIL, "variables": {"id": int(external_id)}}, timeout=10)
        r.raise_for_status()
        m = ((r.json() or {}).get("data") or {}).get("Media")
        return _anilist_norm(m) if m else None
    except (requests.RequestException, ValueError):
        return None


# ---- TheTVDB (shows — optional, v4 API, needs a key) -------------------
TVDB_BASE = os.getenv("TVDB_BASE", "https://api4.thetvdb.com/v4")
_tvdb_token = {"tok": None, "exp": 0}


def _tvdb_key():
    return db.get_setting("tvdb_api_key", "")


def _tvdb_token_get():
    if _tvdb_token["tok"] and time.time() < _tvdb_token["exp"]:
        return _tvdb_token["tok"]
    key = _tvdb_key()
    if not key:
        return None
    body = {"apikey": key}
    pin = db.get_setting("tvdb_pin", "")
    if pin:
        body["pin"] = pin
    r = requests.post(f"{TVDB_BASE}/login", json=body, timeout=10)
    r.raise_for_status()
    tok = (r.json().get("data") or {}).get("token")
    _tvdb_token["tok"] = tok
    _tvdb_token["exp"] = time.time() + 25 * 24 * 3600
    return tok


def tvdb_search(query, media_type="show"):
    tok = _tvdb_token_get()
    if not tok:
        return []
    typ = "movie" if media_type == "movie" else "series"
    try:
        r = requests.get(f"{TVDB_BASE}/search", headers={"Authorization": f"Bearer {tok}"},
                         params={"query": query, "type": typ}, timeout=10)
        r.raise_for_status()
        out = []
        for it in (r.json().get("data") or [])[:20]:
            yr = it.get("year")
            out.append({
                "title": it.get("name"),
                "external_id": str(it.get("tvdb_id") or it.get("id", "")).split("-")[-1],
                "year": int(yr) if str(yr).isdigit() else None,
                "overview": it.get("overview", ""),
                "cover_url": it.get("image_url") or it.get("thumbnail") or "",
                "score": None, "votes": 0,
            })
        return out
    except (requests.RequestException, ValueError):
        return []


def tvdb_detail(external_id, media_type="show"):
    tok = _tvdb_token_get()
    if not tok:
        return None
    typ = "movies" if media_type == "movie" else "series"
    try:
        r = requests.get(f"{TVDB_BASE}/{typ}/{external_id}/extended",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        r.raise_for_status()
        d = r.json().get("data") or {}
        comp = d.get("companies")
        studio = ""
        if isinstance(comp, dict):
            studio = (comp.get("studio", [{}]) or [{}])[0].get("name", "")
        return {
            "title": d.get("name"),
            "external_id": str(d.get("id")),
            "year": int(str(d.get("year"))[:4]) if str(d.get("year"))[:4].isdigit() else None,
            "overview": d.get("overview", ""),
            "genres": [g.get("name") for g in d.get("genres", [])],
            "score": round((d.get("score") or 0)),
            "votes": 0,
            "studio": studio,
            "cover_url": d.get("image", ""),
            "language": d.get("originalLanguage", ""),
            "content_rating": "",
            "runtime": d.get("averageRuntime") or 0,
            "seasons": [],
        }
    except (requests.RequestException, ValueError):
        return None


# ---- provider status (drives the "add metadata provider" UI) -----------
def providers_status():
    tvdb_key = _tvdb_key()
    igdb_id, igdb_secret = _igdb_creds()
    return [
        {"id": "tmdb", "name": "TMDB", "scope": "Movies & Shows", "keyless": False,
         "connected": bool(_tmdb_key()),
         "fields": [{"key": "tmdb_api_key", "label": "API key", "secret": True, "value": bool(_tmdb_key())}]},
        {"id": "tvdb", "name": "TheTVDB", "scope": "Shows", "keyless": False,
         "connected": bool(tvdb_key),
         "fields": [{"key": "tvdb_api_key", "label": "API key", "secret": True, "value": bool(tvdb_key)},
                    {"key": "tvdb_pin", "label": "Subscriber PIN (optional)", "secret": False, "value": bool(db.get_setting("tvdb_pin", ""))}]},
        {"id": "igdb", "name": "IGDB", "scope": "Games", "keyless": False,
         "connected": bool(igdb_id and igdb_secret),
         "fields": [{"key": "igdb_client_id", "label": "Client ID", "secret": False, "value": bool(igdb_id)},
                    {"key": "igdb_client_secret", "label": "Client secret", "secret": True, "value": bool(igdb_secret)}]},
        {"id": "anilist", "name": "AniList", "scope": "Anime", "keyless": True, "connected": True, "fields": []},
    ]


# ---- in-memory discover cache (5-minute TTL) ---------------------------
_discover_cache = {}
_CACHE_TTL = 300  # seconds


def _cache_get(key):
    entry = _discover_cache.get(key)
    if entry and time.time() < entry["exp"]:
        return entry["data"]
    return None


def _cache_set(key, data):
    _discover_cache[key] = {"data": data, "exp": time.time() + _CACHE_TTL}
    return data


# ---- TMDB discover endpoints -------------------------------------------
def _tmdb_norm(it, kind):
    """Normalize a raw TMDB result dict into the Solarr card shape."""
    date = it.get("release_date") or it.get("first_air_date") or ""
    return {
        "title": it.get("title") or it.get("name"),
        "external_id": str(it.get("id")),
        "media_type": "movie" if kind == "movie" else "show",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "cover_url": _img(it.get("poster_path"), "w342"),
        "backdrop_url": _img(it.get("backdrop_path"), "w780"),
        "overview": it.get("overview", ""),
        "score": round((it.get("vote_average") or 0) * 10),
        "votes": it.get("vote_count", 0),
        "status": "not_owned",
    }


def _is_anime(it):
    return 16 in (it.get("genre_ids") or []) and it.get("original_language") == "ja"


def tmdb_trending_movies(limit=20):
    cached = _cache_get("tmdb_trending_movies")
    if cached is not None:
        return cached
    key = _tmdb_key()
    if not key:
        return []
    try:
        r = requests.get(f"{TMDB_BASE}/trending/movie/week",
                         params={"api_key": key}, timeout=10)
        r.raise_for_status()
        out = [_tmdb_norm(it, "movie") for it in r.json().get("results", [])[:limit]
               if not _is_anime(it)]
        return _cache_set("tmdb_trending_movies", out)
    except requests.RequestException:
        return []


def tmdb_popular_movies(limit=20):
    cached = _cache_get("tmdb_popular_movies")
    if cached is not None:
        return cached
    key = _tmdb_key()
    if not key:
        return []
    try:
        r = requests.get(f"{TMDB_BASE}/movie/popular",
                         params={"api_key": key}, timeout=10)
        r.raise_for_status()
        out = [_tmdb_norm(it, "movie") for it in r.json().get("results", [])[:limit]
               if not _is_anime(it)]
        return _cache_set("tmdb_popular_movies", out)
    except requests.RequestException:
        return []


def tmdb_upcoming_movies(limit=20):
    cached = _cache_get("tmdb_upcoming_movies")
    if cached is not None:
        return cached
    key = _tmdb_key()
    if not key:
        return []
    try:
        r = requests.get(f"{TMDB_BASE}/movie/upcoming",
                         params={"api_key": key}, timeout=10)
        r.raise_for_status()
        out = [_tmdb_norm(it, "movie") for it in r.json().get("results", [])[:limit]
               if not _is_anime(it)]
        return _cache_set("tmdb_upcoming_movies", out)
    except requests.RequestException:
        return []


def tmdb_trending_shows(limit=20):
    cached = _cache_get("tmdb_trending_shows")
    if cached is not None:
        return cached
    key = _tmdb_key()
    if not key:
        return []
    try:
        r = requests.get(f"{TMDB_BASE}/trending/tv/week",
                         params={"api_key": key}, timeout=10)
        r.raise_for_status()
        out = [_tmdb_norm(it, "show") for it in r.json().get("results", [])[:limit]
               if not _is_anime(it)]
        return _cache_set("tmdb_trending_shows", out)
    except requests.RequestException:
        return []


def tmdb_popular_shows(limit=20):
    cached = _cache_get("tmdb_popular_shows")
    if cached is not None:
        return cached
    key = _tmdb_key()
    if not key:
        return []
    try:
        r = requests.get(f"{TMDB_BASE}/tv/popular",
                         params={"api_key": key}, timeout=10)
        r.raise_for_status()
        out = [_tmdb_norm(it, "show") for it in r.json().get("results", [])[:limit]
               if not _is_anime(it)]
        return _cache_set("tmdb_popular_shows", out)
    except requests.RequestException:
        return []


def tmdb_upcoming_shows(limit=20):
    cached = _cache_get("tmdb_upcoming_shows")
    if cached is not None:
        return cached
    key = _tmdb_key()
    if not key:
        return []
    try:
        r = requests.get(f"{TMDB_BASE}/tv/on_the_air",
                         params={"api_key": key}, timeout=10)
        r.raise_for_status()
        out = [_tmdb_norm(it, "show") for it in r.json().get("results", [])[:limit]
               if not _is_anime(it)]
        return _cache_set("tmdb_upcoming_shows", out)
    except requests.RequestException:
        return []


# ---- IGDB discover endpoints -------------------------------------------
def igdb_popular(limit=20):
    cached = _cache_get("igdb_popular")
    if cached is not None:
        return cached
    cid, secret = _igdb_creds()
    if not cid or not secret:
        return []
    try:
        r = requests.post(
            f"{IGDB_BASE}/games", headers=_igdb_headers(),
            data=(
                "fields name,cover.url,first_release_date,platforms.name,rating,rating_count,summary;"
                " sort rating_count desc;"
                " where rating_count > 100 & cover != null;"
                f" limit {limit};"
            ),
            timeout=10)
        r.raise_for_status()
        out = []
        for g in r.json():
            yr = time.gmtime(g["first_release_date"]).tm_year if g.get("first_release_date") else None
            plats = [p["name"] for p in g.get("platforms", [])] if g.get("platforms") else []
            c = _cover(g)
            # IGDB covers are small by default; request bigger size
            if c:
                c = c.replace("t_thumb", "t_cover_big")
            out.append({
                "title": g.get("name"),
                "external_id": str(g.get("id")),
                "media_type": "game",
                "year": yr,
                "platform": plats[0] if plats else "",
                "cover_url": c,
                "overview": g.get("summary", ""),
                "score": round(g.get("rating", 0)) if g.get("rating") else None,
                "votes": g.get("rating_count", 0),
                "status": "not_owned",
            })
        return _cache_set("igdb_popular", out)
    except (requests.RequestException, ValueError):
        return []


def igdb_upcoming(limit=20):
    cached = _cache_get("igdb_upcoming")
    if cached is not None:
        return cached
    cid, secret = _igdb_creds()
    if not cid or not secret:
        return []
    now_ts = int(time.time())
    six_months = now_ts + 180 * 86400
    try:
        r = requests.post(
            f"{IGDB_BASE}/games", headers=_igdb_headers(),
            data=(
                "fields name,cover.url,first_release_date,platforms.name,summary;"
                f" where first_release_date >= {now_ts} & first_release_date <= {six_months}"
                " & cover != null;"
                " sort first_release_date asc;"
                f" limit {limit};"
            ),
            timeout=10)
        r.raise_for_status()
        out = []
        for g in r.json():
            yr = time.gmtime(g["first_release_date"]).tm_year if g.get("first_release_date") else None
            plats = [p["name"] for p in g.get("platforms", [])] if g.get("platforms") else []
            c = _cover(g)
            if c:
                c = c.replace("t_thumb", "t_cover_big")
            out.append({
                "title": g.get("name"),
                "external_id": str(g.get("id")),
                "media_type": "game",
                "year": yr,
                "platform": plats[0] if plats else "",
                "cover_url": c,
                "overview": g.get("summary", ""),
                "score": None,
                "votes": 0,
                "status": "not_owned",
            })
        return _cache_set("igdb_upcoming", out)
    except (requests.RequestException, ValueError):
        return []


# ---- AniList discover endpoints ----------------------------------------
_ANILIST_TRENDING = """
query { Page(perPage: 20) { media(type: ANIME, sort: TRENDING_DESC) {
  id format episodes averageScore startDate { year }
  title { romaji english } coverImage { large extraLarge } description(asHtml: false)
  genres studios(isMain: true) { nodes { name } } } } }
"""

_ANILIST_POPULAR = """
query { Page(perPage: 20) { media(type: ANIME, sort: POPULARITY_DESC) {
  id format episodes averageScore startDate { year }
  title { romaji english } coverImage { large extraLarge } description(asHtml: false)
  genres studios(isMain: true) { nodes { name } } } } }
"""

_ANILIST_UPCOMING = """
query { Page(perPage: 20) { media(type: ANIME, status: NOT_YET_RELEASED, sort: POPULARITY_DESC) {
  id format episodes averageScore startDate { year }
  title { romaji english } coverImage { large extraLarge } description(asHtml: false)
  genres studios(isMain: true) { nodes { name } } } } }
"""


def _anilist_fetch(gql, cache_key):
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        r = requests.post(ANILIST_BASE, json={"query": gql}, timeout=10)
        r.raise_for_status()
        media = ((r.json().get("data") or {}).get("Page") or {}).get("media") or []
        out = []
        for m in media:
            n = _anilist_norm(m)
            n["media_type"] = "anime"
            n["status"] = "not_owned"
            out.append(n)
        return _cache_set(cache_key, out)
    except (requests.RequestException, ValueError):
        return []


def anilist_trending():
    return _anilist_fetch(_ANILIST_TRENDING, "anilist_trending")


def anilist_popular():
    return _anilist_fetch(_ANILIST_POPULAR, "anilist_popular")


def anilist_upcoming():
    return _anilist_fetch(_ANILIST_UPCOMING, "anilist_upcoming")
    tvdb_key = _tvdb_key()
    igdb_id, igdb_secret = _igdb_creds()
    return [
        {"id": "tmdb", "name": "TMDB", "scope": "Movies & Shows", "keyless": False,
         "connected": bool(_tmdb_key()),
         "fields": [{"key": "tmdb_api_key", "label": "API key", "secret": True, "value": bool(_tmdb_key())}]},
        {"id": "tvdb", "name": "TheTVDB", "scope": "Shows", "keyless": False,
         "connected": bool(tvdb_key),
         "fields": [{"key": "tvdb_api_key", "label": "API key", "secret": True, "value": bool(tvdb_key)},
                    {"key": "tvdb_pin", "label": "Subscriber PIN (optional)", "secret": False, "value": bool(db.get_setting("tvdb_pin", ""))}]},
        {"id": "igdb", "name": "IGDB", "scope": "Games", "keyless": False,
         "connected": bool(igdb_id and igdb_secret),
         "fields": [{"key": "igdb_client_id", "label": "Client ID", "secret": False, "value": bool(igdb_id)},
                    {"key": "igdb_client_secret", "label": "Client secret", "secret": True, "value": bool(igdb_secret)}]},
        {"id": "anilist", "name": "AniList", "scope": "Anime", "keyless": True, "connected": True, "fields": []},
    ]


# ---- unified dispatch ---------------------------------------------------
def search(query, media_type):
    if media_type == "game":
        return igdb_search(query)
    if media_type == "anime":
        return anilist_search(query)
    if media_type == "show" and _tvdb_key() and db.get_setting("shows_provider", "tmdb") == "tvdb":
        return tvdb_search(query, media_type)
    return tmdb_search(query, media_type)


def detail(external_id, media_type):
    if media_type == "game":
        return igdb_detail(external_id)
    if media_type == "anime":
        return anilist_detail(external_id)
    if media_type == "show" and _tvdb_key() and db.get_setting("shows_provider", "tmdb") == "tvdb":
        return tvdb_detail(external_id, media_type)
    return tmdb_detail(external_id, media_type)
