import requests


def test_connection(url, api_key):
    url = (url or "").rstrip("/")
    if not url or not api_key:
        return False, "Jellyfin URL or API key missing"
    try:
        r = requests.get(f"{url}/System/Info",
                         headers={"X-Emby-Token": api_key}, timeout=6)
        if r.status_code == 200:
            return True, "Connected"
        if r.status_code in (401, 403):
            return False, "Invalid Jellyfin API key"
        return False, f"Jellyfin returned {r.status_code}"
    except requests.RequestException as e:
        return False, f"Could not reach Jellyfin: {e}"


def scan(server):
    url = server["url"].rstrip("/")
    try:
        requests.post(f"{url}/Library/Refresh",
                      headers={"X-Emby-Token": server["api_key"]}, timeout=10)
        return True
    except requests.RequestException:
        return False


def list_items(server, media_type):
    url = server["url"].rstrip("/")
    include = {"movie": "Movie", "show": "Series"}.get(media_type, "Movie")
    try:
        r = requests.get(
            f"{url}/Items",
            headers={"X-Emby-Token": server["api_key"]},
            params={"IncludeItemTypes": include, "Recursive": "true",
                    "Fields": "ProductionYear", "SortBy": "DateCreated", "SortOrder": "Descending"},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("Items", [])
        return [{
            "title": it.get("Name"),
            "external_id": it.get("Id"),
            "year": it.get("ProductionYear"),
        } for it in items]
    except requests.RequestException:
        return []
