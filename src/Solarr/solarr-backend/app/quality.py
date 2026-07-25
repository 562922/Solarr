import re
import json
import db

# ===========================================================================
# Default quality definitions (rank ascending; higher rank = better).
# Video set is shared by movies & shows; games get their own set.
# ===========================================================================
VIDEO_QUALITIES = [
    "Unknown", "CAM", "SDTV", "DVD",
    "WEBRip-480p", "WEBDL-480p", "Bluray-480p", "Bluray-576p",
    "HDTV-720p", "WEBRip-720p", "WEBDL-720p", "Bluray-720p",
    "HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p", "Remux-1080p",
    "HDTV-2160p", "WEBRip-2160p", "WEBDL-2160p", "Bluray-2160p", "Remux-2160p",
]
GAME_QUALITIES = ["Unknown", "Portable", "Compressed", "Repack", "Scene", "Original"]

DEFAULT_CUSTOM_FORMATS = {
    "movies": [
        ("HDR", 40, [("hdr", r"\b(hdr10|hdr|dv|dolby.?vision)\b", 0)]),
        ("x265 / HEVC", 15, [("codec", r"\b(x265|h\.?265|hevc)\b", 0)]),
        ("Proper/Repack", 5, [("title", r"\b(proper|repack)\b", 0)]),
        ("Low-quality release group", -80, [("title", r"\b(yify|yts)\b", 0)]),
    ],
    "shows": [
        ("HDR", 40, [("hdr", r"\b(hdr10|hdr|dv|dolby.?vision)\b", 0)]),
        ("x265 / HEVC", 15, [("codec", r"\b(x265|h\.?265|hevc)\b", 0)]),
        ("Proper/Repack", 5, [("title", r"\b(proper|repack)\b", 0)]),
    ],
    "anime": [
        ("Dual Audio", 40, [("title", r"\b(dual.?audio|multi.?audio)\b", 0)]),
        ("x265 / HEVC", 15, [("codec", r"\b(x265|h\.?265|hevc)\b", 0)]),
        ("Proper/Repack", 5, [("title", r"\b(proper|repack)\b", 0)]),
    ],
    "games": [
        ("FitGirl Repack", 50, [("title", r"fitgirl", 0)]),
        ("DODI Repack", 40, [("title", r"dodi", 0)]),
        ("GOG", 30, [("title", r"\bgog\b", 0)]),
        ("Denuvo", -100, [("title", r"denuvo", 0)]),
    ],
}

DEFAULT_PROFILES = {
    "movies": {
        "name": "HD-1080p", "cutoff": "Bluray-1080p", "upgrade_allowed": 1,
        "min_format_score": 0, "cutoff_format_score": 0,
        "allowed": ["HDTV-720p", "WEBDL-720p", "Bluray-720p", "HDTV-1080p",
                    "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p", "Remux-1080p",
                    "WEBDL-2160p", "Bluray-2160p", "Remux-2160p"],
    },
    "shows": {
        "name": "HD-720p/1080p", "cutoff": "WEBDL-1080p", "upgrade_allowed": 1,
        "min_format_score": 0, "cutoff_format_score": 0,
        "allowed": ["HDTV-720p", "WEBRip-720p", "WEBDL-720p", "Bluray-720p",
                    "HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p"],
    },
    "anime": {
        "name": "Anime HD", "cutoff": "WEBDL-1080p", "upgrade_allowed": 1,
        "min_format_score": 0, "cutoff_format_score": 0,
        "allowed": ["HDTV-720p", "WEBRip-720p", "WEBDL-720p", "Bluray-720p",
                    "HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p"],
    },
    "games": {
        "name": "Any", "cutoff": "Original", "upgrade_allowed": 1,
        "min_format_score": 0, "cutoff_format_score": 0,
        "allowed": ["Compressed", "Repack", "Scene", "Original"],
    },
}


def seed_defaults():
    """Populate quality definitions, a default profile, and example custom
    formats for each app. Safe to call once on setup."""
    if db.one("SELECT id FROM quality_definitions LIMIT 1"):
        return
    sets = {"movies": VIDEO_QUALITIES, "shows": VIDEO_QUALITIES, "anime": VIDEO_QUALITIES, "games": GAME_QUALITIES}
    for app, names in sets.items():
        for rank, name in enumerate(names):
            db.run("INSERT INTO quality_definitions(app,name,rank,min_size_mb,max_size_mb) VALUES(?,?,?,0,0)",
                   (app, name, rank))
    for app, p in DEFAULT_PROFILES.items():
        db.run("INSERT INTO profiles(app,name,cutoff,allowed_qualities,upgrade_allowed,min_format_score,cutoff_format_score) "
               "VALUES(?,?,?,?,?,?,?)",
               (app, p["name"], p["cutoff"], json.dumps(p["allowed"]),
                p["upgrade_allowed"], p["min_format_score"], p["cutoff_format_score"]))
    for app, formats in DEFAULT_CUSTOM_FORMATS.items():
        for name, score, specs in formats:
            fid = db.run("INSERT INTO custom_formats(app,name,score) VALUES(?,?,?)", (app, name, score))
            for stype, value, negate in specs:
                db.run("INSERT INTO custom_format_specs(format_id,type,value,negate) VALUES(?,?,?,?)",
                       (fid, stype, value, negate))


# ===========================================================================
# Release-title parser
# ===========================================================================
RES_PATTERNS = [
    ("2160p", r"\b(2160p|4k|uhd)\b"),
    ("1080p", r"\b1080p\b"),
    ("720p", r"\b720p\b"),
    ("576p", r"\b576p\b"),
    ("480p", r"\b(480p|480i)\b"),
]
GROUP_RE = re.compile(r"-([A-Za-z0-9]+)\s*$")
GROUP_BRACKET_RE = re.compile(r"\[([A-Za-z0-9._-]+)\]\s*$")


def _resolution(t):
    for name, pat in RES_PATTERNS:
        if re.search(pat, t):
            return name
    return None


def _source(t):
    if re.search(r"\bremux\b", t): return "remux"
    if re.search(r"\b(bluray|blu-ray|bdrip|brrip|bdremux)\b", t): return "bluray"
    if re.search(r"\b(web-?dl|webdl|amzn|nf|dsnp)\b", t): return "webdl"
    if re.search(r"\b(web-?rip|webrip)\b", t): return "webrip"
    if re.search(r"\bhdtv\b", t): return "hdtv"
    if re.search(r"\b(dvdrip|dvd)\b", t): return "dvd"
    if re.search(r"\b(cam|hdcam|ts|telesync|telecine)\b", t): return "cam"
    if re.search(r"\bsdtv\b", t): return "sdtv"
    return None


def _video_quality(source, res):
    if source == "cam": return "CAM"
    if source == "sdtv": return "SDTV"
    if source == "dvd": return "DVD"
    if source in ("remux", "bluray", "webdl", "webrip", "hdtv"):
        r = res or ("720p" if source == "hdtv" else "1080p")
        prefix = {"remux": "Remux", "bluray": "Bluray", "webdl": "WEBDL", "webrip": "WEBRip", "hdtv": "HDTV"}[source]
        if source == "remux" and r not in ("1080p", "2160p"):
            r = "1080p"
        return f"{prefix}-{r}"
    # source unknown: infer from resolution alone
    if res in ("1080p", "2160p", "720p"):
        return f"WEBDL-{res}"
    return "Unknown"


def _game_quality(t):
    if re.search(r"\b(fitgirl|dodi|repack)\b", t): return "Repack"
    if re.search(r"\b(iso|full|original)\b", t): return "Original"
    if re.search(r"\bgog\b", t): return "Original"
    if re.search(r"\b(compressed|kaos|xatab)\b", t): return "Compressed"
    if re.search(r"\bportable\b", t): return "Portable"
    if re.search(r"-(razor1911|codex|plaza|skidrow|tenoke|rune|flt|goldberg)\b", t): return "Scene"
    return "Unknown"


def _group(title):
    m = GROUP_BRACKET_RE.search(title) or GROUP_RE.search(title)
    return m.group(1) if m else ""


def parse_release(title, app):
    t = (title or "").lower()
    res = _resolution(t)
    source = _source(t)
    if app == "games":
        quality = _game_quality(t)
        res = None
    else:
        quality = _video_quality(source, res)
    codec = "x265" if re.search(r"\b(x265|h\.?265|hevc)\b", t) else \
        "x264" if re.search(r"\b(x264|h\.?264|avc)\b", t) else \
        "av1" if re.search(r"\bav1\b", t) else ""
    hdr = bool(re.search(r"\b(hdr10|hdr|dv|dolby.?vision)\b", t))
    if re.search(r"\b(multi|dual[.\s-]?audio)\b", t):
        language = "multi"
    elif re.search(r"\b(vostfr|french|truefrench)\b", t):
        language = "french"
    elif re.search(r"\b(spanish|castellano|latino)\b", t):
        language = "spanish"
    elif re.search(r"\b(german|deutsch)\b", t):
        language = "german"
    elif re.search(r"\b(japanese|jpn)\b", t):
        language = "japanese"
    else:
        language = "english"
    edm = re.search(r"\b(extended|remaster(?:ed)?|director.?s?.?cut|uncut|unrated|imax|criterion|theatrical)\b", t)
    edition = edm.group(1) if edm else ""
    ym = re.search(r"\b(19|20)\d{2}\b", t)
    return {
        "title": title, "quality": quality, "resolution": res, "source": source or "",
        "codec": codec, "hdr": hdr, "language": language, "edition": edition,
        "year": int(ym.group(0)) if ym else None,
        "proper": bool(re.search(r"\bproper\b", t)),
        "repack": bool(re.search(r"\brepack\b", t)),
        "group": _group(title),
    }


# ===========================================================================
# Quality lookups
# ===========================================================================
def quality_def(app, name):
    return db.one("SELECT * FROM quality_definitions WHERE app=? AND name=?", (app, name))


def rank_of(app, name):
    q = quality_def(app, name)
    return q["rank"] if q else 0


# ===========================================================================
# Custom-format scoring
# ===========================================================================
def _spec_matches(spec, parsed):
    val = spec["value"]
    t = spec["type"]
    if t == "title":
        hit = bool(re.search(val, parsed["title"], re.IGNORECASE))
    elif t == "group":
        hit = val.lower() == (parsed["group"] or "").lower()
    elif t == "resolution":
        hit = val.lower() == (parsed["resolution"] or "").lower()
    elif t == "source":
        hit = val.lower() == (parsed["source"] or "").lower()
    elif t == "codec":
        hit = bool(re.search(val, parsed["title"], re.IGNORECASE))
    elif t == "hdr":
        hit = parsed["hdr"]
    elif t == "language":
        hit = val.lower() == (parsed.get("language") or "").lower()
    elif t == "edition":
        hit = bool(parsed.get("edition")) and (val.lower() in (parsed.get("edition") or "").lower())
    elif t == "year":
        hit = str(parsed.get("year") or "") == str(val)
    else:
        hit = False
    return (not hit) if spec.get("negate") else hit


def custom_format_score(app, parsed):
    total = 0
    matched = []
    formats = db.rows("SELECT * FROM custom_formats WHERE app=?", (app,))
    for f in formats:
        specs = db.rows("SELECT * FROM custom_format_specs WHERE format_id=?", (f["id"],))
        if specs and all(_spec_matches(s, parsed) for s in specs):
            total += f["score"]
            matched.append(f["name"])
    return total, matched


# ===========================================================================
# Profiles
# ===========================================================================
def load_profile(profile_row):
    p = dict(profile_row)
    try:
        p["allowed"] = json.loads(p.get("allowed_qualities") or "[]")
    except (ValueError, TypeError):
        p["allowed"] = []
    return p


# ===========================================================================
# Release profiles (preferred / required / ignored terms)
# ===========================================================================
def _terms(s):
    return [x.strip().lower() for x in (s or "").split(",") if x.strip()]


def release_profile_adjust(app, title):
    """Returns (ok, reason, preferred_score). Applies required/ignored/preferred
    words from any release profile for this app."""
    t = (title or "").lower()
    pref = 0
    for rp in db.rows("SELECT * FROM release_profiles WHERE app=?", (app,)):
        for term in _terms(rp["required"]):
            if term not in t:
                return False, f"missing required term '{term}'", 0
        for term in _terms(rp["ignored"]):
            if term in t:
                return False, f"contains ignored term '{term}'", 0
        try:
            for term, score in json.loads(rp.get("preferred") or "[]"):
                if term.lower() in t:
                    pref += score
        except (ValueError, TypeError):
            pass
    return True, "", pref


# ===========================================================================
# Decision engine
# ===========================================================================
def evaluate(release, profile, app):
    """Decide whether a release is acceptable under a profile. Returns a dict
    with accepted, reason, quality, rank, cf_score, and a sortable score."""
    parsed = parse_release(release.get("title", ""), app)
    quality = parsed["quality"]
    rank = rank_of(app, quality)
    cf_score, matched = custom_format_score(app, parsed)
    rp_ok, rp_reason, rp_score = release_profile_adjust(app, release.get("title", ""))
    cf_score += rp_score

    result = {
        "title": release.get("title"), "quality": quality, "rank": rank,
        "cf_score": cf_score, "matched_formats": matched,
        "proper": parsed["proper"] or parsed["repack"],
        "seeders": release.get("seeders", 0) or 0, "size": release.get("size", 0) or 0,
        "accepted": True, "reason": "",
    }

    if not rp_ok:
        result["accepted"] = False
        result["reason"] = f"Release profile: {rp_reason}"
        return result
    if quality not in profile["allowed"]:
        result["accepted"] = False
        result["reason"] = f"Quality '{quality}' not in profile"
        return result
    if cf_score < profile.get("min_format_score", 0):
        result["accepted"] = False
        result["reason"] = f"Custom-format score {cf_score} below minimum {profile['min_format_score']}"
        return result

    qd = quality_def(app, quality)
    size_mb = (release.get("size", 0) or 0) / (1024 * 1024)
    if qd and size_mb:
        if qd["max_size_mb"] and size_mb > qd["max_size_mb"]:
            result["accepted"] = False
            result["reason"] = f"Size {size_mb:.0f}MB exceeds max for {quality}"
            return result
        if qd["min_size_mb"] and size_mb < qd["min_size_mb"]:
            result["accepted"] = False
            result["reason"] = f"Size {size_mb:.0f}MB below min for {quality}"
            return result
    return result


def _sort_key(d):
    return (d["rank"], d["cf_score"], 1 if d["proper"] else 0, d["seeders"])


def rank_releases(releases, profile, app):
    evaluated = [evaluate(r, profile, app) for r in releases]
    for e, r in zip(evaluated, releases):
        e["_release"] = r
    accepted = [e for e in evaluated if e["accepted"]]
    accepted.sort(key=_sort_key, reverse=True)
    return accepted, evaluated


def pick_best(releases, profile, app):
    accepted, _ = rank_releases(releases, profile, app)
    return accepted[0] if accepted else None


def is_upgrade(current, candidate, profile, app):
    """current/candidate: dicts with rank + cf_score. Returns True if candidate
    is a worthwhile upgrade under the profile."""
    if not profile.get("upgrade_allowed", 1):
        return False
    cutoff_rank = rank_of(app, profile["cutoff"])
    cutoff_cf = profile.get("cutoff_format_score", 0)
    # already satisfied the cutoff on both quality and custom-format score
    if current["rank"] >= cutoff_rank and current["cf_score"] >= cutoff_cf:
        return False
    if candidate["rank"] > current["rank"]:
        return True
    if candidate["rank"] == current["rank"] and candidate["cf_score"] > current["cf_score"]:
        return True
    return False
