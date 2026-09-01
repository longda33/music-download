#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Action worker: MusicBrainz discovery -> syy.py sources -> WebDAV."""
import json
import os
import re
import signal
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, parse_qs, urlparse

import requests

try:
    from mutagen.flac import FLAC, Picture
except ImportError:
    FLAC = None
    Picture = None

try:
    from opencc import OpenCC
    TRAD_TO_SIMP = OpenCC("t2s")
except ImportError:
    TRAD_TO_SIMP = None

NETEASE_BASE = "https://api.qijieya.cn"
try:
    import syy  # syy.py must be in the repository root
    QQ_API = syy.TANG_API
    KUWO_API = syy.KUWO_API
    NETEASE_API = syy.METING_API
except ImportError:
    QQ_API = "https://tang.api.s01s.cn/music_open_api.php"
    KUWO_API = "https://oiapi.net/api/Kuwo"
    NETEASE_API = f"{NETEASE_BASE}/meting/"

MB_API = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": "music-download-action/1.0 (n8n workflow)"}
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
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


def artist_credit_info(credit):
    names, ids = [], []
    for item in credit or []:
        artist = item.get("artist", {})
        name = artist.get("name") or item.get("name")
        artist_id = artist.get("id")
        if name and name not in names:
            names.append(name)
            ids.append(artist_id or "")
    if not 1 <= len(names) <= 2:
        return None, []
    return " & ".join(names), [x for x in ids if x]


def one_artist(credit):
    return artist_credit_info(credit)[0]


def mb_get(path, params):
    time.sleep(1.1)
    return request_json(f"{MB_API}/{path}", params={**params, "fmt": "json"}, headers=MB_HEADERS)


def lastfm_get(method, params):
    api_key = os.getenv("LASTFM_API_KEY")
    if not api_key:
        fail("缺少 LASTFM_API_KEY")
    query = {"method": method, "api_key": api_key, "format": "json", **params}
    r = requests.get(LASTFM_API, params=query, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Last.fm API 错误 {data['error']}: {data.get('message', '')}")
    return data


def lastfm_recording(row):
    if not isinstance(row, dict):
        return None
    title = str(row.get("name", "")).strip()
    artist_data = row.get("artist", "")
    artist = str(artist_data.get("name", "") if isinstance(artist_data, dict) else artist_data).strip()
    artist_id = artist_data.get("mbid") if isinstance(artist_data, dict) else None
    return {"title": title, "artist": artist, "artist_ids": [artist_id] if artist_id else [], "recording_id": row.get("mbid"), "isrc": row.get("isrc"), "year": None, "lastfm_url": row.get("url")} if title and artist else None


ARTIST_ALIASES = {
    "蔡依林": "jolintsai",
    "jolin": "jolintsai",
    "jolin tsai": "jolintsai",
    "jolintsai": "jolintsai",
    "jolin蔡依林": "jolintsai",
    "蔡依林 (jolin tsai)": "jolintsai",
    "蔡依林（jolin tsai）": "jolintsai",
    "赵露思": "zhaolusi",
    "rosy": "zhaolusi",
    "rosy zhao": "zhaolusi",
    "赵露思 rosy": "zhaolusi",
    "赵露思 (rosy)": "zhaolusi",
    "赵露思(rosy)": "zhaolusi",
    "赵露思（rosy）": "zhaolusi",
}

ARTIST_FOLDER_NAMES = {
    "jolintsai": "蔡依林",
    "zhaolusi": "赵露思",
}


def dedup_key(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(ch for ch in value if ch.isalnum())


def to_simplified(value):
    value = str(value or "")
    if TRAD_TO_SIMP is not None:
        return TRAD_TO_SIMP.convert(value)
    # GitHub Action 会安装 OpenCC；此表仅作为依赖异常时的保底。
    return value.translate(str.maketrans("趙露思周杰倫林憶蓮張信哲蔡依林樂門國體風學這個後臺", "赵露思周杰伦林忆莲张信哲蔡依林乐门国体风学这个后台"))


def canonical_artist(value):
    raw = unicodedata.normalize("NFKC", to_simplified(value)).strip().casefold()
    compact = dedup_key(raw)
    return ARTIST_ALIASES.get(raw, ARTIST_ALIASES.get(compact, compact))


def canonical_title(value):
    return dedup_key(value)


def artist_folder_name(value):
    """将中英文艺人别名统一为稳定的文件夹名称。"""
    key = canonical_artist(value)
    return ARTIST_FOLDER_NAMES.get(key, to_simplified(str(value).strip()))


def identity_keys(song):
    keys = []
    if song.get("isrc"):
        keys.append(("isrc", dedup_key(song["isrc"])))
    if song.get("recording_id"):
        keys.append(("recording", str(song["recording_id"]).casefold()))
    title_key = canonical_title(song.get("title", ""))
    for artist_id in song.get("artist_ids", []):
        if artist_id:
            keys.append(("artist-id-title", str(artist_id).casefold(), title_key))
    keys.append(("name", title_key, canonical_artist(song.get("artist", ""))))
    return keys


def platform_discover(query):
    """从实时音源目录发现歌曲，不依赖 MusicBrainz 收录。"""
    candidates = []
    parts = query.split()
    pair_terms = []
    if len(parts) >= 2:
        for cut in range(1, len(parts)):
            left, right = " ".join(parts[:cut]), " ".join(parts[cut:])
            pair_terms.extend([(left, right), (right, left)])

    def accept(title, artist):
        title, artist = str(title or "").strip(), str(artist or "").strip()
        if not title or not artist:
            return False
        if len(parts) == 1:
            return True
        return any(canonical_title(title) == canonical_title(t)
                   and canonical_artist(artist) == canonical_artist(a)
                   for t, a in pair_terms)

    try:
        rows = request_json(QQ_API, {"msg": query, "type": "json"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
        for row in rows if isinstance(rows, list) else []:
            title = row.get("song_title") or row.get("song_name")
            artist = row.get("singer_name")
            if accept(title, artist):
                candidates.append({"title": title, "artist": artist, "artist_ids": [], "recording_id": None, "isrc": None, "year": None})
    except Exception as exc:
        log(f"QQ 实时目录搜索失败：{exc}")

    try:
        data = request_json(KUWO_API, {"msg": query, "page": 1, "limit": 100}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
        rows = data.get("data", []) if isinstance(data, dict) else []
        for row in rows:
            title, artist = row.get("song"), row.get("singer")
            if accept(title, artist):
                candidates.append({"title": title, "artist": artist, "artist_ids": [], "recording_id": None, "isrc": None, "year": None})
    except Exception as exc:
        log(f"酷我实时目录搜索失败：{exc}")

    try:
        data = request_json(NETEASE_API, {"type": "search", "id": query, "limit": 100, "page": 1, "server": "netease"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
        rows = data if isinstance(data, list) else []
        for row in rows:
            title, artist = row.get("name"), row.get("artist")
            if accept(title, artist):
                candidates.append({"title": title, "artist": artist, "artist_ids": [], "recording_id": None, "isrc": None, "year": None})
    except Exception as exc:
        log(f"网易云实时目录搜索失败：{exc}")
    return candidates


def exact_pair_match(song, query):
    """校验双项命令，防止模糊搜索把相似歌名当成目标歌曲。"""
    parts = query.split()
    if len(parts) < 2:
        return True
    title = canonical_title(song.get("title", ""))
    artist = canonical_artist(song.get("artist", ""))
    for cut in range(1, len(parts)):
        left, right = " ".join(parts[:cut]), " ".join(parts[cut:])
        if (title == canonical_title(left) and artist == canonical_artist(right)) or (title == canonical_title(right) and artist == canonical_artist(left)):
            return True
    return False


def discover_songs(mode, query):
    songs = []
    if mode == "search":
        songs.extend(platform_discover(query))
        log(f"实时音源目录初步发现：{len(songs)} 首")
    # MusicBrainz：较完整的目录来源
    try:
        if mode == "singer":
            found = mb_get("artist", {"query": f'artist:"{query}"', "limit": 5})
            artists = found.get("artists", [])
            if artists:
                mbid = artists[0]["id"]
                offset = 0
                while offset < 1000:
                    page = mb_get("recording", {"artist": mbid, "limit": 100, "offset": offset, "inc": "isrcs"})
                    rows = page.get("recordings", [])
                    if not rows:
                        break
                    for row in rows:
                        artist, artist_ids = artist_credit_info(row.get("artist-credit"))
                        if artist and row.get("title"):
                            songs.append({"title": row["title"].strip(), "artist": artist, "artist_ids": artist_ids, "recording_id": row.get("id"), "isrc": (row.get("isrcs") or [None])[0], "year": None})
                    offset += len(rows)
                    if len(rows) < 100:
                        break
        elif mode == "search":
            # 两个搜索项=歌曲+歌手，只下载一首；一个搜索项=宽搜，全部下载。
            parts = query.split()
            if len(parts) == 1:
                # 单项可能是歌手名：优先抓取该艺人的完整目录。
                artist_found = mb_get("artist", {"query": f'artist:"{query}"', "limit": 5})
                artists = artist_found.get("artists", [])
                if artists:
                    mbid = artists[0].get("id")
                    offset = 0
                    while mbid and offset < 1000:
                        page = mb_get("recording", {"artist": mbid, "limit": 100, "offset": offset, "inc": "isrcs"})
                        rows = page.get("recordings", [])
                        if not rows:
                            break
                        for row in rows:
                            artist, artist_ids = artist_credit_info(row.get("artist-credit"))
                            if artist and row.get("title"):
                                songs.append({"title": row["title"].strip(), "artist": artist, "artist_ids": artist_ids, "recording_id": row.get("id"), "isrc": (row.get("isrcs") or [None])[0], "year": None})
                        offset += len(rows)
                        if len(rows) < 100:
                            break
                # 同时补充单个歌曲名的全部演唱版本。
                found = mb_get("recording", {"query": f'recording:"{query}"', "limit": 100, "inc": "artists+isrcs"})
                for row in found.get("recordings", []):
                    artist, artist_ids = artist_credit_info(row.get("artist-credit"))
                    title = row.get("title", "").strip()
                    if artist and title and canonical_title(title) == canonical_title(query):
                        songs.append({"title": title, "artist": artist, "artist_ids": artist_ids, "recording_id": row.get("id"), "isrc": (row.get("isrcs") or [None])[0], "year": None})
            else:
                # 支持“歌名 歌手名”和“歌手名 歌名”；尝试每个空格切分的两种顺序。
                queries = []
                queries = []
                for cut in range(1, len(parts)):
                    left, right = " ".join(parts[:cut]), " ".join(parts[cut:])
                    queries.extend([(left, right), (right, left)])
                for title_part, artist_part in queries:
                    lucene = f'recording:"{title_part}"'
                    if artist_part:
                        lucene += f' AND artist:"{artist_part}"'
                    found = mb_get("recording", {"query": lucene, "limit": 20, "inc": "artists+isrcs"})
                    for row in found.get("recordings", []):
                        artist, artist_ids = artist_credit_info(row.get("artist-credit"))
                        title = row.get("title", "").strip()
                        if artist and title:
                            songs.append({"title": title, "artist": artist, "artist_ids": artist_ids, "recording_id": row.get("id"), "isrc": (row.get("isrcs") or [None])[0], "year": None})
                    if songs:
                        break
            try:
                data = lastfm_get("track.search", {"track": query, "limit": 20, "page": 1})
                for row in data.get("results", {}).get("trackmatches", {}).get("track", []):
                    item = lastfm_recording(row)
                    if item:
                        songs.append(item)
            except Exception as exc:
                log(f"Last.fm 搜索失败，使用 MusicBrainz 结果：{exc}")
        else:
            found = mb_get("recording", {"query": f'recording:"{query}"', "limit": 100, "inc": "isrcs"})
            for row in found.get("recordings", []):
                artist, artist_ids = artist_credit_info(row.get("artist-credit"))
                title = row.get("title", "").strip()
                if artist and canonical_title(title) == canonical_title(query):
                    songs.append({"title": title, "artist": artist, "artist_ids": artist_ids, "recording_id": row.get("id"), "isrc": (row.get("isrcs") or [None])[0], "year": None})
    except Exception as exc:
        log(f"MusicBrainz 暂不可用，继续使用 Last.fm：{exc}")

    # Last.fm：补充热门歌曲及不同演唱版本
    try:
        if mode == "singer":
            data = lastfm_get("artist.getTopTracks", {"artist": query, "limit": 100, "page": 1, "autocorrect": 1})
            rows = data.get("toptracks", {}).get("track", [])
        else:
            data = lastfm_get("track.search", {"track": query, "limit": 100, "page": 1})
            rows = data.get("results", {}).get("trackmatches", {}).get("track", [])
        for row in rows:
            item = lastfm_recording(row)
            if item:
                songs.append(item)
    except Exception as exc:
        log(f"Last.fm 暂不可用，继续使用 MusicBrainz：{exc}")

    seen = set()
    result = []
    for song in songs:
        if mode == "search" and not exact_pair_match(song, query):
            continue
        artists = [{"artist": {"name": name.strip()}} for name in song["artist"].split(" & ") if name.strip()]
        if not song["title"] or not one_artist(artists):
            continue
        keys = identity_keys(song)
        if not song["title"] or not one_artist(artists):
            continue
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
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
        source_title = str(detail.get("song_title") or detail.get("song_name") or row.get("song_title") or title).strip()
        for tier, label in (("sq", "SQ"), ("pq", "PQ")):
            url = detail.get(f"song_play_url_{tier}")
            filename = detail.get(f"song_filename_{tier}")
            if url and filename and str(filename).lower().endswith(".flac"):
                return {"url": url, "filename": filename, "filename_title": source_title, "size": int(detail.get(f"song_size_{tier}_str") or 0), "source": "QQ", "quality": label}
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
    # 酷我 HAR：先搜索，再用 msg+n+br=1 获取真实无损地址。
    query = f"{title} {artist}"
    data = request_json(KUWO_API, {"msg": query, "page": 1, "limit": 10}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
    rows = data.get("data", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return None

    def size_bytes(value):
        match = re.search(r"([0-9]+(?:\\.[0-9]+)?)\\s*(Mi?B|Gi?B|Ki?B|B)", str(value), re.I)
        if not match:
            return 0
        number, unit = float(match.group(1)), match.group(2).lower()
        factor = {"b": 1, "kib": 1024, "kb": 1024, "mib": 1024**2, "mb": 1024**2, "gib": 1024**3, "gb": 1024**3}[unit]
        return int(number * factor)

    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        if canonical_title(row.get("song", "")) != canonical_title(title):
            continue
        if canonical_artist(row.get("singer", "")) != canonical_artist(artist):
            continue
        types = row.get("types", [])
        has_flac = isinstance(types, list) and any(
            isinstance(t, dict) and str(t.get("format", "")).lower() == "flac"
            for t in types
        )
        if not has_flac:
            continue
        detail = request_json(KUWO_API, {"msg": query, "n": index, "br": 1}, SOURCE_HEADERS, timeout=DETAIL_TIMEOUT, retries=1)
        item = detail.get("data", {}) if isinstance(detail, dict) else {}
        url = item.get("url") if isinstance(item, dict) else ""
        fmt = str(item.get("format", "")).lower() if isinstance(item, dict) else ""
        if url and fmt == "flac" and str(url).lower().split("?")[0].endswith(".flac"):
            return {"url": url, "filename": f"{title}.flac", "filename_title": str(item.get("song") or title).strip(), "size": size_bytes(item.get("size", "")), "source": "酷我", "quality": f"FLAC {item.get('bitrate', '2000')}kbps"}
    return None


def netease_search(title, artist):
    data = request_json(NETEASE_API, {"type": "search", "id": f"{title} {artist}", "limit": 10, "page": 1, "server": "netease"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
    for row in (data if isinstance(data, list) else []):
        row_title, row_artist = row.get("name", ""), row.get("artist", "")
        if canonical_title(row_title) != canonical_title(title) or canonical_artist(row_artist) != canonical_artist(artist):
            continue
        search_url = row.get("url", "")
        song_id = parse_qs(urlparse(search_url).query).get("id", [""])[0]
        if not song_id:
            continue
        # 搜索和下载接口分开：明确请求网易云无损档位 br=2000。
        download_url = f"{NETEASE_API}?server=netease&type=url&id={song_id}&br=2000"
        try:
            probe = requests.get(download_url, headers=SOURCE_HEADERS, timeout=60, allow_redirects=True, stream=True)
            content_type = probe.headers.get("content-type", "").lower()
            is_flac = ".flac" in probe.url.lower() or "audio/flac" in content_type or "audio/x-flac" in content_type
            size = int(probe.headers.get("content-length", 0) or 0)
            probe.close()
            if is_flac:
                return {"url": download_url, "filename": f"{title}.flac", "filename_title": str(row_title).strip(), "size": size, "source": "网易云", "quality": "FLAC"}
        except Exception:
            pass
    return None


def find_source(song):
    found = []
    for func in (qq_search, kuwo_search, netease_search):
        try:
            item = func(song["title"], song["artist"])
            if item:
                # 保留目录发现阶段的原始标题/歌手，音源只补充下载字段。
                found.append({**item, **song})
        except Exception as exc:
            log(f"{func.__name__} 搜索失败：{exc}")
    if not found:
        return None
    # 下载源严格按 QQ → 网易云 → 酷我；每个源内部已优先选择自身最高 FLAC 档位。
    source_priority = {"QQ": 0, "网易云": 1, "酷我": 2}
    return min(found, key=lambda x: source_priority.get(x.get("source"), 99))


def netease_metadata(title, artist):
    """网易云元数据独立于音频格式；即使播放地址是 MP3，也读取歌词和封面。"""
    try:
        rows = request_json(
            NETEASE_API,
            {"type": "search", "id": f"{title} {artist}", "limit": 30, "page": 1, "server": "netease"},
            SOURCE_HEADERS,
            timeout=SOURCE_TIMEOUT,
            retries=1,
        )
        for row in rows if isinstance(rows, list) else []:
            row_title = str(row.get("name") or "").strip()
            row_artist = str(row.get("artist") or "").strip()
            if canonical_title(row_title) != canonical_title(title):
                continue
            if canonical_artist(row_artist) != canonical_artist(artist):
                continue

            # search 返回的 pic/lrc 已经是 Meting 独立元数据接口，
            # 不检查 type=url 的音频格式，避免 MP3 音频导致元数据丢失。
            cover_url = str(row.get("pic") or "").strip()
            lyric_url = str(row.get("lrc") or "").strip()
            lyrics = ""
            if lyric_url:
                lyric = requests.get(lyric_url, headers=SOURCE_HEADERS, timeout=DETAIL_TIMEOUT)
                if lyric.ok:
                    lyrics = lyric.text.strip()
            log(f"网易云歌曲信息已找到：{row_title} - {row_artist}（歌词={'有' if lyrics else '无'}，封面={'有' if cover_url else '无'}）")
            return {"album": str(row.get("album") or ""), "cover_url": cover_url, "lyrics": lyrics}
        log(f"网易云未匹配到歌曲信息：{title} - {artist}")
    except Exception as exc:
        log(f"网易云元数据获取失败，准备回退：{exc}")
    return {}


def embed_metadata(local_path, song):
    """将歌曲信息、歌词和封面写入 FLAC，并验证写入结果。"""
    if FLAC is None or Picture is None:
        raise RuntimeError("未安装 mutagen，无法封装歌曲元数据")
    title, artist = song.get("title", ""), song.get("artist", "")
    try:
        audio = FLAC(str(local_path))
        # 下载源返回的原始歌曲名优先，保留 (Live)、现场版等版本标识。
        title = str(song.get("filename_title") or song.get("title", "")).strip()
        artist = str(song.get("artist", "")).strip()
        audio["title"] = [title]
        audio["artist"] = [artist]
        if song.get("album"):
            audio["album"] = [song["album"]]
        audio["comment"] = [f"Source: {song.get('source', '')}; Quality: {song.get('quality', '')}"]

        # 网易云优先提供封面、歌词和专辑信息。
        netease = netease_metadata(title, artist)
        if netease.get("album") and not song.get("album"):
            audio["album"] = [netease["album"]]
        if netease.get("lyrics"):
            audio["lyrics"] = [netease["lyrics"]]
        if netease.get("cover_url"):
            cover = requests.get(netease["cover_url"], headers=SOURCE_HEADERS, timeout=30)
            cover.raise_for_status()
            picture = Picture()
            picture.type = 3
            picture.mime = cover.headers.get("Content-Type", "image/jpeg").split(";")[0]
            picture.desc = "Cover"
            picture.data = cover.content
            audio.clear_pictures()
            audio.add_picture(picture)

        # 网易云无结果时，Last.fm 提供封面和专辑信息。
        info = {}
        if os.getenv("LASTFM_API_KEY"):
            try:
                info = lastfm_get("track.getInfo", {"artist": artist, "track": title, "autocorrect": 1}).get("track", {})
                album = info.get("album") or {}
                album_name = album.get("title")
                if album_name and not song.get("album") and not netease.get("album"):
                    audio["album"] = [album_name]
                images = album.get("image") or []
                cover_url = next((x.get("#text") for x in reversed(images) if x.get("#text")), "")
                if cover_url and not netease.get("cover_url"):
                    cover = requests.get(cover_url, timeout=30)
                    cover.raise_for_status()
                    picture = Picture()
                    picture.type = 3
                    picture.mime = cover.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    picture.desc = "Cover"
                    picture.data = cover.content
                    audio.clear_pictures()
                    audio.add_picture(picture)
            except Exception as exc:
                log(f"Last.fm 封面/专辑信息获取失败：{exc}")

        # LRCLIB 作为网易云无歌词时的补充。
        if not netease.get("lyrics"):
            try:
                lyric = requests.get("https://lrclib.net/api/get", params={"track_name": title, "artist_name": artist}, timeout=30)
                if lyric.status_code == 200:
                    lyric_data = lyric.json()
                    lyrics = lyric_data.get("syncedLyrics") or lyric_data.get("plainLyrics")
                    if lyrics:
                        audio["lyrics"] = [lyrics]
            except Exception as exc:
                log(f"LRCLIB 歌词获取失败：{exc}")
        audio.save()

        # 重新打开验证，禁止未封装文件继续上传。
        verified = FLAC(str(local_path))
        if verified.get("title", [""])[0] != title or verified.get("artist", [""])[0] != artist:
            raise RuntimeError("FLAC 标题或歌手验证失败")
        if netease.get("lyrics") and not verified.get("lyrics"):
            raise RuntimeError("网易云歌词未写入 FLAC")
        if netease.get("cover_url") and not verified.pictures:
            raise RuntimeError("网易云封面未写入 FLAC")
        log(f"元数据封装并验证完成：{title} - {artist}（歌词={'有' if verified.get('lyrics') else '无'}，封面={'有' if verified.pictures else '无'}）")
    except Exception as exc:
        raise RuntimeError(f"FLAC 元数据封装失败：{exc}") from exc

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
    if mode == "search" and len(query.split()) >= 2:
        songs = songs[:1]
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
        artist_folder = safe_name(artist_folder_name(found["artist"]))
        ensure_webdav_folder(auth, artist_folder)
        log(f"[{index}/{len(songs)}] 目标文件夹：{artist_folder}")
        filename_title = safe_name(str(found.get("filename_title") or original["title"]).strip())
        base_filename = safe_name(f"{filename_title} - {original['artist']}.flac")
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
            embed_metadata(local, found)
            actual = local.stat().st_size
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
