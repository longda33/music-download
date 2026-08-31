#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Action worker: MusicBrainz discovery -> syy.py sources -> WebDAV."""
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

try:
    import syy  # syy.py must be in the repository root
    QQ_API = syy.TANG_API
    KUWO_API = syy.KUWO_API
    NETEASE_API = syy.METING_API
except ImportError:
    QQ_API = "https://tang.api.s01s.cn/music_open_api.php"
    KUWO_API = "https://oiapi.net/api/Kuwo"
    NETEASE_API = "https://api.qijieya.cn/meting/"

SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_TOKEN = None
SOURCE_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"}
RETRIES = 3
SOURCE_TIMEOUT = 12
DETAIL_TIMEOUT = 8
SIZE_TOLERANCE = 3 * 1024 * 1024


def log(message):
    print(f"[music-download] {message}", flush=True)


ACTIVE_PAYLOAD = None


def handle_cancel(signum, frame):
    if ACTIVE_PAYLOAD is not None:
        log("收到 GitHub 取消信号，正在回调 n8n")
        ACTIVE_PAYLOAD["status"] = "cancelled"
        ACTIVE_PAYLOAD["cancelled"] = True
        ACTIVE_PAYLOAD.setdefault("success_count", 0)
        ACTIVE_PAYLOAD.setdefault("failed_songs", [])
        try:
            callback(ACTIVE_PAYLOAD)
            log("取消状态已回调 n8n")
        except Exception as exc:
            log(f"取消回调失败：{exc}")
    raise SystemExit(0)


signal.signal(signal.SIGTERM, handle_cancel)
signal.signal(signal.SIGINT, handle_cancel)


def fail(message):
    raise RuntimeError(message)


def request_json(url, params=None, headers=None, timeout=60, retries=RETRIES):
    error = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            error = exc
            if attempt + 1 < RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"请求失败 {url}: {error}")


def one_artist(credit):
    """返回单人或双人演唱者；三人及以上合唱返回 None。"""
    names = []
    for item in credit or []:
        artist = item.get("artist", {})
        name = artist.get("name") or item.get("name")
        if name and name not in names:
            names.append(name)
    return " & ".join(names) if 1 <= len(names) <= 2 else None


def spotify_token():
    global SPOTIFY_TOKEN
    if SPOTIFY_TOKEN:
        return SPOTIFY_TOKEN
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        fail("缺少 Spotify Secret: SPOTIFY_CLIENT_ID、SPOTIFY_CLIENT_SECRET")
    r = requests.post(SPOTIFY_TOKEN_URL, auth=(client_id, client_secret), data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    SPOTIFY_TOKEN = r.json()["access_token"]
    return SPOTIFY_TOKEN


def spotify_get(path, params=None):
    r = requests.get(f"{SPOTIFY_API}/{path.lstrip('/')}", params=params, headers={"Authorization": f"Bearer {spotify_token()}"}, timeout=30)
    if r.status_code >= 400:
        detail = r.text[:500].replace("\n", " ")
        raise RuntimeError(f"Spotify API HTTP {r.status_code}: {detail}")
    return r.json()


def spotify_artist(query):
    data = spotify_get("search", {"q": f'artist:"{query}"', "type": "artist", "limit": 10})
    artists = data.get("artists", {}).get("items", [])
    if not artists:
        return None
    exact = [a for a in artists if str(a.get("name", "")).casefold() == query.casefold()]
    return (exact or artists)[0]


def spotify_song(title, artist=None):
    q = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
    data = spotify_get("search", {"q": q, "type": "track", "limit": 50, "market": "US"})
    return data.get("tracks", {}).get("items", [])


def spotify_recording(track):
    artists = " & ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
    return {"title": track.get("name", "").strip(), "artist": artists, "year": (track.get("album", {}).get("release_date") or "")[:4], "spotify_id": track.get("id")}


def discover_songs(mode, query):
    songs = []
    if mode == "singer":
        artist = spotify_artist(query)
        if not artist:
            return []
        offset = 0
        seen_albums = set()
        while offset < 1000:
            page = spotify_get("artists/{}/albums".format(artist["id"]), {"include_groups": "album,single,compilation", "limit": 50, "offset": offset, "market": "US"})
            albums = page.get("items", [])
            if not albums:
                break
            for album in albums:
                album_id = album.get("id")
                if not album_id or album_id in seen_albums:
                    continue
                seen_albums.add(album_id)
                tracks = spotify_get("albums/{}/tracks".format(album_id), {"limit": 50, "market": "US"}).get("items", [])
                for track in tracks:
                    if track.get("artists"):
                        songs.append(spotify_recording(track))
            offset += len(albums)
            if len(albums) < 50:
                break
    else:
        songs = [spotify_recording(t) for t in spotify_song(query) if t.get("artists")]

    seen = set()
    result = []
    for song in songs:
        if not song["title"] or not one_artist([{"artist": {"name": n}} for n in song["artist"].split(" & ")]):
            continue
        key = (song["title"].casefold(), song["artist"].casefold())
        if key not in seen:
            seen.add(key)
            result.append(song)
    return result


def qq_search(title, artist):
    rows = request_json(QQ_API, {"msg": f"{title} {artist}", "type": "json"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
    if not isinstance(rows, list):
        return None
    for row in rows[:3]:
        if not isinstance(row, dict) or not row.get("song_mid"):
            continue
        detail = request_json(QQ_API, {"mid": row["song_mid"]}, SOURCE_HEADERS, timeout=DETAIL_TIMEOUT, retries=1)
        for tier, label in (("sq", "SQ"), ("pq", "PQ")):
            url = detail.get(f"song_play_url_{tier}")
            filename = detail.get(f"song_filename_{tier}")
            if url and filename and str(filename).lower().endswith(".flac"):
                return {"url": url, "filename": filename, "size": int(detail.get(f"song_size_{tier}_str") or 0), "source": "QQ", "quality": label}
    return None


def recursive_flac(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and "url" in key.lower() and ".flac" in item.lower():
                return item
            found = recursive_flac(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = recursive_flac(item)
            if found:
                return found
    return None


def kuwo_search(title, artist):
    data = request_json(KUWO_API, {"msg": f"{title} {artist}", "page": 1, "limit": 20}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
    for row in (data.get("data", [])[:3] if isinstance(data, dict) else []):
        if not isinstance(row, dict) or not row.get("rid"):
            continue
        for params in ({"rid": row["rid"]}, {"msg": row["rid"]}):
            try:
                detail = request_json(KUWO_API, params, SOURCE_HEADERS, timeout=DETAIL_TIMEOUT, retries=1)
                url = recursive_flac(detail)
                if url:
                    size = 0
                    head = requests.head(url, headers=SOURCE_HEADERS, timeout=30, allow_redirects=True)
                    if head.headers.get("content-length"):
                        size = int(head.headers["content-length"])
                    return {"url": url, "filename": f"{title}.flac", "size": size, "source": "酷我", "quality": "FLAC"}
            except Exception:
                pass
    return None


def netease_search(title, artist):
    data = request_json(NETEASE_API, {"type": "search", "id": f"{title} {artist}", "limit": 10, "page": 1, "server": "netease"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
    for row in (data if isinstance(data, list) else []):
        url = row.get("url") if isinstance(row, dict) else None
        if not url:
            continue
        try:
            r = requests.get(url, headers=SOURCE_HEADERS, timeout=60, allow_redirects=True)
            r.raise_for_status()
            real = r.text.strip()
            if ".flac" in real.lower():
                size = int(r.headers.get("content-length", 0) or 0)
                return {"url": real, "filename": f"{title}.flac", "size": size, "source": "网易云", "quality": "FLAC"}
        except Exception:
            pass
    return None


def find_source(song):
    for func in (qq_search, kuwo_search, netease_search):
        try:
            found = func(song["title"], song["artist"])
            if found:
                return {**song, **found}
        except Exception:
            continue
    return None


def safe_name(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value)).strip().rstrip(" .")
    return (value or "unknown")[:180]


def webdav_auth():
    required = ["WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        fail("缺少 WebDAV Secret: " + ", ".join(missing))
    return (os.environ["WEBDAV_USERNAME"], os.environ["WEBDAV_PASSWORD"])


def webdav_path(filename=None, subfolder=None):
    base = os.environ["WEBDAV_URL"].rstrip("/")
    folder = os.getenv("WEBDAV_FOLDER", "Music").strip("/")
    parts = folder.split("/") if folder else []
    if subfolder:
        parts.append(str(subfolder).strip("/"))
    if filename:
        parts.append(filename)
    path = "/".join(quote(part, safe="") for part in parts)
    return f"{base}/{path}"


def ensure_webdav_folder(auth, subfolder=None):
    # 目录应提前创建；已存在时 WebDAV 返回 405，忽略即可。
    url = webdav_path(subfolder=subfolder)
    r = requests.request("MKCOL", url, auth=auth, timeout=60)
    if r.status_code not in (201, 405, 409):
        r.raise_for_status()


def webdav_listing(auth, subfolder=None):
    xml = "<?xml version=\"1.0\" encoding=\"utf-8\"?><d:propfind xmlns:d=\"DAV:\"><d:prop><d:displayname/><d:getcontentlength/></d:prop></d:propfind>"
    r = requests.request("PROPFIND", webdav_path(subfolder=subfolder), auth=auth, headers={"Depth": "1", "Content-Type": "application/xml"}, data=xml.encode(), timeout=60)
    r.raise_for_status()
    root = __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(r.content)
    ns = {"d": "DAV:"}
    result = {}
    for response in root.findall("d:response", ns):
        name = response.findtext("d:propstat/d:prop/d:displayname", default="", namespaces=ns)
        length = response.findtext("d:propstat/d:prop/d:getcontentlength", default="0", namespaces=ns)
        if name:
            result[name] = int(length or 0)
    return result


def choose_filename(auth, base_filename, size, subfolder=None):
    """同名且相近则跳过；同名不同体积则追加 [xx.xxMB]。"""
    files = webdav_listing(auth, subfolder=subfolder)
    if base_filename in files and abs(files[base_filename] - size) <= SIZE_TOLERANCE:
        return None
    if base_filename not in files:
        return base_filename
    stem, ext = os.path.splitext(base_filename)
    marked = f"{stem} [{size / (1024 * 1024):.2f}MB]{ext}"
    if marked not in files:
        return marked
    n = 2
    while f"{stem} [{size / (1024 * 1024):.2f}MB] ({n}){ext}" in files:
        n += 1
    return f"{stem} [{size / (1024 * 1024):.2f}MB] ({n}){ext}"


def remote_size(auth, url):
    try:
        head = requests.head(url, auth=auth, timeout=30, allow_redirects=True)
        if head.status_code in (200, 204) and head.headers.get("content-length"):
            return int(head.headers["content-length"])
    except Exception:
        pass
    try:
        r = requests.request("PROPFIND", url, auth=auth, headers={"Depth": "0"}, timeout=30)
        if r.status_code in (200, 207):
            root = __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(r.content)
            ns = {"d": "DAV:"}
            value = root.findtext(".//d:getcontentlength", default="", namespaces=ns)
            return int(value) if value else None
    except Exception:
        pass
    return None


def upload(auth, local_path, filename, subfolder=None):
    url = webdav_path(filename, subfolder=subfolder)
    expected = local_path.stat().st_size
    log(f"WebDAV PUT：{filename}")
    with local_path.open("rb") as handle:
        r = requests.put(url, auth=auth, data=handle, headers={"Content-Length": str(expected), "Content-Type": "audio/flac"}, timeout=600)
    if r.status_code in (200, 201, 204):
        return
    if r.status_code == 405:
        actual = remote_size(auth, url)
        if actual == expected:
            log(f"WebDAV 返回 405，但远程文件已确认存在：{filename}")
            return
    raise RuntimeError(f"WebDAV PUT 失败 HTTP {r.status_code}: {url}")


def callback(payload):
    url = payload.get("callback_url")
    if not url:
        return
    headers = {"Content-Type": "application/json"}
    token = payload.get("callback_token")
    if token:
        headers["X-Callback-Token"] = token
    result = {
        "chat_id": payload.get("chat_id"),
        "status": payload.get("status", "completed"),
        "cancelled": payload.get("cancelled", False),
        "success_count": payload.get("success_count", 0),
        "failed_songs": payload.get("failed_songs", []),
    }
    requests.post(url, headers=headers, json=result, timeout=30).raise_for_status()


def main():
    global ACTIVE_PAYLOAD
    raw = os.getenv("EVENT_PAYLOAD", "")
    if not raw:
        fail("EVENT_PAYLOAD 为空")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    ACTIVE_PAYLOAD = payload
    mode, query = payload.get("mode", "singer"), str(payload.get("query", "")).strip()
    if not query:
        fail("缺少 query")
    log(f"开始任务：mode={mode}, query={query}")
    songs = discover_songs(mode, query)
    log(f"目录检索完成：共 {len(songs)} 首，三人及以上合唱已过滤")
    auth = webdav_auth()
    ensure_webdav_folder(auth)
    log("WebDAV 连接正常，开始逐首处理；文件将保存到歌手名文件夹")
    work = Path("downloaded_music")
    work.mkdir(exist_ok=True)
    success = 0
    skipped = 0
    failed = []
    for index, original in enumerate(songs, 1):
        label = f"{original['title']} - {original['artist']}"
        log(f"[{index}/{len(songs)}] 搜索音源：{label}")
        found = find_source(original)
        if not found:
            log(f"[{index}/{len(songs)}] 失败：三个音源都没有可用 FLAC")
            failed.append(label)
            continue
        log(f"[{index}/{len(songs)}] 找到音源：{found['source']} {found.get('quality', 'FLAC')}")
        artist_folder = safe_name(found["artist"])
        ensure_webdav_folder(auth, artist_folder)
        log(f"[{index}/{len(songs)}] 目标文件夹：{artist_folder}")
        base_filename = safe_name(f"{found['title']} - {found['artist']}.flac")
        local = work / base_filename
        try:
            r = requests.get(found["url"], headers=SOURCE_HEADERS, stream=True, timeout=300)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0) or found.get("size", 0) or 0)
            downloaded = 0
            last_report = time.monotonic()
            with local.open("wb") as handle:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= 3:
                            if total:
                                percent = downloaded * 100 / total
                                log(f"[{index}/{len(songs)}] 下载进度：{downloaded / 1048576:.2f}/{total / 1048576:.2f} MiB ({percent:.1f}%)")
                            else:
                                log(f"[{index}/{len(songs)}] 已下载：{downloaded / 1048576:.2f} MiB")
                            last_report = now
            actual = local.stat().st_size
            log(f"[{index}/{len(songs)}] 下载完成：{actual / 1048576:.2f} MiB，检查 WebDAV 文件")
            if found["size"] and abs(actual - found["size"]) > SIZE_TOLERANCE:
                raise RuntimeError(f"体积异常 {actual}/{found['size']}")
            filename = choose_filename(auth, base_filename, actual, subfolder=artist_folder)
            if filename is None:
                local.unlink(missing_ok=True)
                skipped += 1
                log(f"[{index}/{len(songs)}] 跳过：WebDAV 已存在相同文件")
                continue
            upload(auth, local, filename, subfolder=artist_folder)
            local.unlink(missing_ok=True)
            success += 1
            log(f"[{index}/{len(songs)}] 上传完成：{filename}")
        except Exception as exc:
            local.unlink(missing_ok=True)
            failed.append(label)
            log(f"[{index}/{len(songs)}] 失败：{exc}")
    log(f"任务完成：上传 {success} 首，跳过 {skipped} 首，失败 {len(failed)} 首")
    payload["success_count"] = success
    payload["failed_songs"] = failed
    callback(payload)
    print(json.dumps({"success_count": success, "failed_songs": failed}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
