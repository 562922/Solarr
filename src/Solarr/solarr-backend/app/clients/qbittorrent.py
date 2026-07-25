import requests


def _base(client):
    host = client["host"].rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    port = client.get("port")
    return f"{host}:{port}" if port else host


def _session(client):
    s = requests.Session()
    s.post(f"{_base(client)}/api/v2/auth/login",
           data={"username": client.get("username", ""), "password": client.get("password", "")},
           timeout=6)
    return s


def test_connection(host, port, username, password):
    client = {"host": host, "port": port, "username": username, "password": password}
    base = _base(client)
    if not host:
        return False, "qBittorrent host missing"
    try:
        s = _session(client)
        r = s.get(f"{base}/api/v2/app/version", timeout=6)
        if r.status_code == 200:
            return True, f"Connected ({r.text.strip()})"
        return False, "Login failed - check username/password"
    except requests.RequestException as e:
        return False, f"Could not reach qBittorrent: {e}"


def add(client, magnet_or_url, category, save_path=None):
    s = _session(client)
    data = {"urls": magnet_or_url, "category": category}
    if save_path:
        data["savepath"] = save_path
        data["autoTMM"] = "false"
    r = s.post(f"{_base(client)}/api/v2/torrents/add", data=data, timeout=15)
    return r.status_code in (200, 201)


def torrents(client, category=None):
    s = _session(client)
    params = {"category": category} if category else {}
    r = s.get(f"{_base(client)}/api/v2/torrents/info", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def find_by_name(client, category, name_contains):
    for t in torrents(client, category):
        if name_contains.lower() in (t.get("name", "").lower()):
            return t
    return None
