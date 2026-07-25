"""
RomM client.

RomM's real API (docs.romm.app):
  - GET  /api/heartbeat            public; used for the connection check
  - GET  /api/roms                 authenticated; lists the game library
  - auth: HTTP Basic (user:pass) OR a Bearer token from /api/token
Scanning is NOT a plain REST call in RomM - it runs through a socket.io task
queue. In practice RomM ships a *filesystem watcher* that automatically scans
when new files appear under /romm/library. Solarr therefore imports a game by
placing it in the RomM library folder and lets RomM's watcher pick it up; the
`rescan()` call below simply confirms RomM is reachable (and is a hook where a
socket.io trigger could be added for setups with the watcher disabled).
"""
import requests


def _headers(server):
    return {"Authorization": f"Bearer {server['api_key']}"} if server.get("api_key") else {}


def _auth(server):
    if server.get("username") and server.get("password"):
        return (server["username"], server["password"])
    return None


def test_connection(url, api_key=None, username=None, password=None):
    url = (url or "").rstrip("/")
    if not url:
        return False, "RomM URL missing"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    auth = (username, password) if username and password else None
    try:
        # heartbeat is public and confirms the instance is up
        r = requests.get(f"{url}/api/heartbeat", headers=headers, auth=auth, timeout=6)
        if r.status_code != 200:
            return False, f"RomM returned {r.status_code}"
        # if credentials were supplied, confirm they authenticate against a protected route
        if headers or auth:
            rr = requests.get(f"{url}/api/roms", params={"limit": 1}, headers=headers, auth=auth, timeout=6)
            if rr.status_code in (401, 403):
                return False, "RomM authentication failed"
        return True, "Connected"
    except requests.RequestException as e:
        return False, f"Could not reach RomM: {e}"


def rescan(server):
    """RomM auto-imports via its filesystem watcher once a file lands in the
    library. We verify reachability so a failed import surfaces clearly."""
    url = server["url"].rstrip("/")
    try:
        r = requests.get(f"{url}/api/heartbeat", headers=_headers(server), auth=_auth(server), timeout=8)
        return r.status_code == 200
    except requests.RequestException:
        return False


def list_games(server, limit=200):
    url = server["url"].rstrip("/")
    try:
        r = requests.get(f"{url}/api/roms", params={"limit": limit, "order_by": "name"},
                         headers=_headers(server), auth=_auth(server), timeout=12)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        out = []
        for g in items:
            out.append({
                "title": g.get("name") or g.get("fs_name") or g.get("title"),
                "external_id": str(g.get("id")),
                "platform": g.get("platform_name") or g.get("platform_slug") or "",
                "cover_url": g.get("path_cover_small") or g.get("path_cover_s")
                             or g.get("url_cover") or "",
            })
        return out
    except (requests.RequestException, ValueError):
        return []
