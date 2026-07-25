import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["SOLARR_DB"] = tempfile.mktemp(prefix="solarr-q-", suffix=".db")
sys.path.insert(0, os.path.join(BASE, "app"))

import db
import quality

db.init()
quality.seed_defaults()

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"[PASS] {label}")
    else:
        FAIL += 1; print(f"[FAIL] {label} {extra}")


# ---- parser ----
p = quality.parse_release("Blade Runner 2049 2017 2160p BluRay REMUX HDR x265-FraMeSToR", "movies")
check("parse: quality Remux-2160p", p["quality"] == "Remux-2160p", p)
check("parse: resolution 2160p", p["resolution"] == "2160p", p)
check("parse: source remux", p["source"] == "remux", p)
check("parse: hdr true", p["hdr"] is True, p)
check("parse: codec x265", p["codec"] == "x265", p)
check("parse: group FraMeSToR", p["group"] == "FraMeSToR", p)

p2 = quality.parse_release("Some.Movie.2020.1080p.WEB-DL.DDP5.1.x264-NTb", "movies")
check("parse: WEBDL-1080p", p2["quality"] == "WEBDL-1080p", p2)

p3 = quality.parse_release("Some.Movie.2020.HDTV.720p.x264", "shows")
check("parse: HDTV-720p", p3["quality"] == "HDTV-720p", p3)

p4 = quality.parse_release("Some Movie 2019 CAM XViD", "movies")
check("parse: CAM", p4["quality"] == "CAM", p4)

pg = quality.parse_release("Celeste [FitGirl Repack]", "games")
check("parse game: Repack", pg["quality"] == "Repack", pg)

# ---- custom format scoring ----
score, matched = quality.custom_format_score("movies", p)   # 2160p remux hdr x265
check("cf: HDR + x265 matched", "HDR" in matched and "x265 / HEVC" in matched, matched)
check("cf: score = 55 (40 hdr + 15 x265)", score == 55, (score, matched))

gscore, gmatched = quality.custom_format_score("games", pg)  # fitgirl repack
check("cf game: FitGirl +50", gscore == 50 and "FitGirl Repack" in gmatched, (gscore, gmatched))

gdenuvo = quality.parse_release("BigGame-Denuvo", "games")
ds, _ = quality.custom_format_score("games", gdenuvo)
check("cf game: Denuvo -100", ds == -100, ds)

# ---- profile + evaluate ----
prof = quality.load_profile(db.one("SELECT * FROM profiles WHERE app='movies'"))

# a CAM should be rejected (not in allowed list)
dec = quality.evaluate({"title": "Movie 2019 CAM", "seeders": 10, "size": 1e9}, prof, "movies")
check("evaluate: CAM rejected", not dec["accepted"] and "not in profile" in dec["reason"], dec)

# a remux 2160p should be accepted with cf 55
dec2 = quality.evaluate({"title": p["title"], "seeders": 100, "size": 40e9}, prof, "movies")
check("evaluate: remux accepted", dec2["accepted"], dec2)
check("evaluate: remux rank highest", dec2["rank"] == quality.rank_of("movies", "Remux-2160p"), dec2)
check("evaluate: remux cf 55", dec2["cf_score"] == 55, dec2)

# min_format_score gate
strict = dict(prof); strict["min_format_score"] = 60
dec3 = quality.evaluate({"title": p["title"], "size": 40e9}, strict, "movies")
check("evaluate: below min CF rejected", not dec3["accepted"] and "below minimum" in dec3["reason"], dec3)

# ---- ranking: pick best among mixed releases ----
releases = [
    {"title": "Movie 2020 1080p WEB-DL x264-NTb", "seeders": 50, "size": 3e9},
    {"title": "Movie 2020 2160p BluRay REMUX HDR-FraMeSToR", "seeders": 20, "size": 50e9},
    {"title": "Movie 2020 720p HDTV x264", "seeders": 200, "size": 1e9},
    {"title": "Movie 2020 CAM", "seeders": 999, "size": 0.9e9},
]
best = quality.pick_best(releases, prof, "movies")
check("rank: best is the 2160p remux (quality wins over seeders)", "REMUX" in best["title"], best)
accepted, evaluated = quality.rank_releases(releases, prof, "movies")
check("rank: CAM excluded from accepted", all("CAM" not in a["title"] for a in accepted), [a["title"] for a in accepted])

# same quality, custom formats break the tie
tie = [
    {"title": "Movie 2020 1080p WEB-DL x264-NTb", "seeders": 80, "size": 3e9},
    {"title": "Movie 2020 1080p WEB-DL x265 HDR-NTb", "seeders": 10, "size": 3e9},
]
best_tie = quality.pick_best(tie, prof, "movies")
check("rank: CF score breaks quality tie (HDR/x265 wins)", "x265" in best_tie["title"], best_tie)

# ---- upgrade logic ----
# current = WEBDL-1080p (below cutoff Bluray-1080p), candidate = Bluray-1080p -> upgrade
cur = {"rank": quality.rank_of("movies", "WEBDL-1080p"), "cf_score": 0}
cand = {"rank": quality.rank_of("movies", "Bluray-1080p"), "cf_score": 0}
check("upgrade: WEBDL->Bluray is upgrade", quality.is_upgrade(cur, cand, prof, "movies"))

# current already at cutoff with enough CF -> no upgrade
cur2 = {"rank": quality.rank_of("movies", "Bluray-1080p"), "cf_score": 0}
cand2 = {"rank": quality.rank_of("movies", "Remux-2160p"), "cf_score": 55}
check("upgrade: at cutoff -> no further upgrade (cutoff respected)", not quality.is_upgrade(cur2, cand2, prof, "movies"))

# same quality but higher CF, below cutoff-format-score -> upgrade
prof_cf = dict(prof); prof_cf["cutoff"] = "Remux-2160p"; prof_cf["cutoff_format_score"] = 100
cur3 = {"rank": quality.rank_of("movies", "WEBDL-1080p"), "cf_score": 0}
cand3 = {"rank": quality.rank_of("movies", "WEBDL-1080p"), "cf_score": 55}
check("upgrade: same quality higher CF is upgrade", quality.is_upgrade(cur3, cand3, prof_cf, "movies"))

# downgrade rejected
check("upgrade: downgrade rejected", not quality.is_upgrade(cand, cur, prof, "movies"))

# upgrade disabled
prof_noup = dict(prof); prof_noup["upgrade_allowed"] = 0
check("upgrade: disabled honored", not quality.is_upgrade(cur, cand, prof_noup, "movies"))

print(f"\n==== {PASS} passed, {FAIL} failed ====")
os.remove(os.environ["SOLARR_DB"])
sys.exit(1 if FAIL else 0)
