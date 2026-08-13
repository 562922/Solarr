import requests
import config_store

# Prowlarr / Newznab category buckets per media type
CATEGORIES = {
    "movie": [2000],
    "show": [5000],
    "anime": [5070, 2000, 5000],
    "game": [1000, 4000, 4050],
}


def _cfg():
    p = config_store.get_prowlarr()
    return p["url"].rstrip("/"), p["api_key"]


def test_connection(url, api_key):
    url = (url or "").rstrip("/")
    if not url or not api_key:
        return False, "Prowlarr URL or API key missing"
    try:
        r = requests.get(f"{url}/api/v1/system/status",
                         headers={"X-Api-Key": api_key}, timeout=6)
        if r.status_code == 200:
            return True, "Connected"
        if r.status_code == 401:
            return False, "Invalid Prowlarr API key"
        return False, f"Prowlarr returned {r.status_code}"
    except requests.RequestException as e:
        return False, f"Could not reach Prowlarr: {e}"


def list_indexers():
    url, key = _cfg()
    if not url:
        return []
    try:
        r = requests.get(f"{url}/api/v1/indexer",
                         headers={"X-Api-Key": key}, timeout=8)
        r.raise_for_status()
        out = []
        for ix in r.json():
            caps = ix.get("capabilities", {}) or {}
            cats = ", ".join(sorted({c.get("name", "") for c in caps.get("categories", []) if c.get("name")}))
            out.append({
                "name": ix.get("name"),
                "protocol": (ix.get("protocol") or "").capitalize() or "Torrent",
                "privacy": (ix.get("privacy") or "").capitalize(),
                "categories": cats,
                "enabled": bool(ix.get("enable", True)),
            })
        return out
    except requests.RequestException:
        return []


def _resolve_magnet(url, api_key):
    """Follow a Prowlarr download redirect to extract the magnet URI."""
    if not url:
        return None
    if url.startswith("magnet:"):
        return url
    try:
        r = requests.get(url, headers={"X-Api-Key": api_key},
                         allow_redirects=False, timeout=10)
        location = r.headers.get("Location", "")
        if location.startswith("magnet:"):
            return location
        # Some Prowlarr versions return the magnet in the body
        if r.text.strip().startswith("magnet:"):
            return r.text.strip()
    except requests.RequestException:
        pass
    return None


def search(query, media_type):
    """Query Prowlarr's search API, scoped to the media type's categories."""
    url, key = _cfg()
    if not url:
        return []
    params = [("query", query), ("type", "search")]
    for c in CATEGORIES.get(media_type, []):
        params.append(("categories", c))
    try:
        r = requests.get(f"{url}/api/v1/search",
                         headers={"X-Api-Key": key}, params=params, timeout=20)
        r.raise_for_status()
        results = []
        for item in r.json():
            results.append({
                "title": item.get("title"),
                "seeders": item.get("seeders", 0) or 0,
                "size": item.get("size", 0) or 0,
                "magnet": item.get("magnetUrl") or _resolve_magnet(item.get("downloadUrl") or item.get("link"), key),
                "download_url": item.get("downloadUrl") or item.get("link"),
                "indexer": item.get("indexer"),
                "protocol": item.get("protocol", "torrent"),
            })
        return results
    except requests.RequestException:
        return []