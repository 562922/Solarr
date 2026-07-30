import sqlite3
import os
import json
from datetime import datetime

DB = os.getenv("SOLARR_DB", "solarr.db")


def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    db = connect()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT DEFAULT 'user',   -- admin | user
            email TEXT,
            auto_approve INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- single logical row (id=1) for the Prowlarr connection
        CREATE TABLE IF NOT EXISTS prowlarr(
            id INTEGER PRIMARY KEY CHECK (id = 1),
            url TEXT, api_key TEXT, connected INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS media_servers(
            id INTEGER PRIMARY KEY,
            kind TEXT,            -- jellyfin | romm
            name TEXT,
            url TEXT,
            api_key TEXT,
            username TEXT,
            password TEXT
        );

        CREATE TABLE IF NOT EXISTS download_clients(
            id INTEGER PRIMARY KEY,
            name TEXT,
            type TEXT,            -- qBittorrent | ...
            host TEXT,
            port TEXT,
            username TEXT,
            password TEXT,
            cat_movie TEXT DEFAULT 'movies',
            cat_show TEXT DEFAULT 'tv',
            cat_game TEXT DEFAULT 'games',
            cat_anime TEXT DEFAULT 'anime',
            enabled INTEGER DEFAULT 1,
            for_movies INTEGER DEFAULT 1,
            for_shows INTEGER DEFAULT 1,
            for_games INTEGER DEFAULT 1,
            for_anime INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS root_folders(
            id INTEGER PRIMARY KEY,
            path TEXT,
            for_movies INTEGER DEFAULT 0,
            for_shows INTEGER DEFAULT 0,
            for_games INTEGER DEFAULT 0,
            for_anime INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS profiles(
            id INTEGER PRIMARY KEY,
            app TEXT,             -- movies | shows | games
            name TEXT,
            cutoff TEXT,                       -- quality name where upgrading stops
            allowed_qualities TEXT DEFAULT '[]',   -- JSON list of allowed quality names
            upgrade_allowed INTEGER DEFAULT 1,
            min_format_score INTEGER DEFAULT 0,    -- reject below this custom-format score
            cutoff_format_score INTEGER DEFAULT 0  -- stop upgrading once CF score reached
        );

        CREATE TABLE IF NOT EXISTS quality_definitions(
            id INTEGER PRIMARY KEY,
            app TEXT,
            name TEXT,
            rank INTEGER,          -- higher = better
            min_size_mb REAL DEFAULT 0,
            max_size_mb REAL DEFAULT 0   -- 0 = unlimited
        );

        CREATE TABLE IF NOT EXISTS custom_formats(
            id INTEGER PRIMARY KEY,
            app TEXT,
            name TEXT,
            score INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS custom_format_specs(
            id INTEGER PRIMARY KEY,
            format_id INTEGER,
            type TEXT,             -- title | group | resolution | source | codec | hdr
            value TEXT,
            negate INTEGER DEFAULT 0,
            FOREIGN KEY(format_id) REFERENCES custom_formats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tags(
            id INTEGER PRIMARY KEY,
            app TEXT,
            label TEXT
        );

        CREATE TABLE IF NOT EXISTS connections(
            id INTEGER PRIMARY KEY,
            app TEXT,             -- movies | shows | games | all
            name TEXT,
            type TEXT,            -- Discord | Webhook | ...
            url TEXT,
            on_grab INTEGER DEFAULT 1,
            on_import INTEGER DEFAULT 1
        );

        -- unified catalogue (synced from Jellyfin/RomM + created by requests)
        CREATE TABLE IF NOT EXISTS media(
            id INTEGER PRIMARY KEY,
            media_type TEXT,      -- movie | show | game | anime
            title TEXT,
            external_id TEXT,     -- tmdb/tvdb/igdb id
            year INTEGER,
            platform TEXT,        -- games
            cover_url TEXT,
            status TEXT DEFAULT 'not_owned',  -- not_owned | requested | downloading | available
            source TEXT,          -- jellyfin | romm | request
            quality TEXT,
            cf_score INTEGER DEFAULT 0,
            quality_rank INTEGER DEFAULT 0,
            monitored INTEGER DEFAULT 1,
            -- enrichment
            overview TEXT,
            genres TEXT,          -- JSON list
            score INTEGER,
            votes INTEGER,
            studio TEXT,          -- studio / network / developer
            runtime INTEGER,
            language TEXT,
            content_rating TEXT,
            enriched INTEGER DEFAULT 0,
            added_at TEXT
        );

        CREATE TABLE IF NOT EXISTS seasons(
            id INTEGER PRIMARY KEY,
            series_id INTEGER,    -- media.id of the show
            season_number INTEGER,
            episode_count INTEGER DEFAULT 0,
            monitored INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS episodes(
            id INTEGER PRIMARY KEY,
            series_id INTEGER,
            season_number INTEGER,
            episode_number INTEGER,
            title TEXT,
            air_date TEXT,
            monitored INTEGER DEFAULT 1,
            status TEXT DEFAULT 'missing',  -- missing | downloading | available
            quality TEXT,
            cf_score INTEGER DEFAULT 0,
            quality_rank INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS blocklist(
            id INTEGER PRIMARY KEY,
            media_type TEXT,
            title TEXT,
            release_title TEXT,
            reason TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS release_profiles(
            id INTEGER PRIMARY KEY,
            app TEXT,
            name TEXT,
            required TEXT DEFAULT '',   -- comma terms; all must be present
            ignored TEXT DEFAULT '',    -- comma terms; any present -> reject
            preferred TEXT DEFAULT '[]' -- JSON [[term, score], ...]
        );

        CREATE TABLE IF NOT EXISTS lists(
            id INTEGER PRIMARY KEY,
            app TEXT,             -- movies | shows | games
            name TEXT,
            type TEXT,            -- Trakt | IMDb | TMDb | IGDB | RSS | Custom
            url TEXT,
            enabled INTEGER DEFAULT 1,
            auto_add INTEGER DEFAULT 0,   -- 0 = add to catalogue only; 1 = auto-request
            profile_id INTEGER,
            last_synced TEXT,
            last_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY,
            media_type TEXT,
            title TEXT,
            external_id TEXT,
            platform TEXT,
            profile_id INTEGER,
            status TEXT,          -- Wanted | Searching | Downloading | Importing | Available | Failed
            release_title TEXT,
            download_hash TEXT,
            quality TEXT,
            cf_score INTEGER DEFAULT 0,
            quality_rank INTEGER DEFAULT 0,
            requested_by TEXT,
            message TEXT,
            requested_at TEXT,
            updated_at TEXT
        );

        -- indexers pushed to Solarr by Prowlarr's App sync (push model)
        CREATE TABLE IF NOT EXISTS synced_indexers(
            id INTEGER PRIMARY KEY,
            name TEXT,
            implementation TEXT,      -- Torznab | Newznab
            base_url TEXT,
            api_path TEXT,
            api_key TEXT,
            categories TEXT DEFAULT '[]',
            enable INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 25,
            added_at TEXT
        );
        """
    )
    # --- lightweight migrations for existing databases ---
    cols = [r["name"] for r in db.execute("PRAGMA table_info(root_folders)")]
    if "for_anime" not in cols:
        db.execute("ALTER TABLE root_folders ADD COLUMN for_anime INTEGER DEFAULT 0")
    dccols = [r["name"] for r in db.execute("PRAGMA table_info(download_clients)")]
    if "cat_anime" not in dccols:
        db.execute("ALTER TABLE download_clients ADD COLUMN cat_anime TEXT DEFAULT 'anime'")
    if "for_anime" not in dccols:
        db.execute("ALTER TABLE download_clients ADD COLUMN for_anime INTEGER DEFAULT 1")
    db.commit()
    db.close()


# ---- settings key/value -------------------------------------------------
def get_setting(key, default=None):
    db = connect()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    db.close()
    return row["value"] if row else default


def set_setting(key, value):
    db = connect()
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()
    db.close()


def setup_complete():
    return get_setting("setup_complete") == "1"


# ---- generic row helpers ------------------------------------------------
def rows(sql, args=()):
    db = connect()
    r = [dict(x) for x in db.execute(sql, args).fetchall()]
    db.close()
    return r


def one(sql, args=()):
    db = connect()
    r = db.execute(sql, args).fetchone()
    db.close()
    return dict(r) if r else None


def run(sql, args=()):
    db = connect()
    cur = db.execute(sql, args)
    db.commit()
    last = cur.lastrowid
    db.close()
    return last


def now():
    return datetime.now().isoformat(timespec="seconds")
