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
from urllib.parse import quote, unquote, parse_qs, urlparse

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
    log("收到 GitHub 取消信号，任务即将停止")
    # 最终取消通知由 n8n 查询 GitHub Action 状态后发送，
    # 避免进程即将被 Runner 强制终止时回调来不及发出。
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
    "等什么君": "dengshimeijun",
    "邓寓君": "dengshimeijun",
    "dengshimeijun": "dengshimeijun",
    "等什么君(邓寓君)": "dengshimeijun",
    "等什么君（邓寓君）": "dengshimeijun",
    "邓寓君(等什么君)": "dengshimeijun",
    "邓寓君（等什么君）": "dengshimeijun",
}

ARTIST_FOLDER_NAMES = {
    "jolintsai": "蔡依林",
    "zhaolusi": "赵露思",
    "dengshimeijun": "等什么君",
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


def is_chinese_song(title, artist):
    """标题或歌手包含汉字时，按中文歌曲处理歌词。"""
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", f"{title} {artist}"))


def simplify_chinese_lyrics(lyrics, title, artist):
    """中文歌曲统一为简体；含中文的双语歌词全部保留，外文歌曲原样保留。"""
    lyrics = str(lyrics or "").replace("\r\n", "\n").replace("\r", "\n")
    if not lyrics or not is_chinese_song(title, artist):
        return lyrics.strip()
    # 中文歌曲只转换繁体，不删除任何歌词行；双语歌词中的外文同步保留。
    return to_simplified(lyrics).strip()


def canonical_artist(value):
    raw = unicodedata.normalize("NFKC", to_simplified(value)).strip().casefold()
    compact = dedup_key(raw)
    direct = ARTIST_ALIASES.get(raw, ARTIST_ALIASES.get(compact))
    if direct:
        return direct
    # 将“等什么君(邓寓君)”“邓寓君（等什么君）”等写法拆成别名片段；
    # 只要所有片段指向同一规范艺人，就统一使用同一个身份和文件夹。
    fragments = re.split(r"[\s&+/、,，;；()（）\[\]【】]+", raw)
    mapped = [ARTIST_ALIASES.get(fragment, ARTIST_ALIASES.get(dedup_key(fragment)))
              for fragment in fragments if fragment]
    mapped = [item for item in mapped if item]
    if mapped and len(set(mapped)) == 1:
        return mapped[0]
    return compact


def canonical_title(value):
    return dedup_key(value)


def normalize_folder_label(value):
    """统一文件夹显示名；去除中文字符之间被音源错误插入的空格。"""
    text = unicodedata.normalize("NFKC", to_simplified(str(value or ""))).strip()
    text = re.sub(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])", "", text)
    return text or "unknown"


def artist_folder_name(value):
    """将中英文艺人别名统一为稳定的文件夹名称。"""
    key = canonical_artist(value)
    return normalize_folder_label(ARTIST_FOLDER_NAMES.get(key, str(value).strip()))


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
    for platform, value in (song.get("platform_ids") or {}).items():
        if value:
            keys.append(("platform-id", platform, str(value).casefold()))
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
                candidates.append({"title": title, "artist": artist, "platform_ids": {"qq_song_mid": row.get("song_mid")}, "artist_ids": [], "recording_id": None, "isrc": None, "year": None})
    except Exception as exc:
        log(f"QQ 实时目录搜索失败：{exc}")

    try:
        data = request_json(KUWO_API, {"msg": query, "page": 1, "limit": 100}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
        rows = data.get("data", []) if isinstance(data, dict) else []
        for row in rows:
            title, artist = row.get("song"), row.get("singer")
            if accept(title, artist):
                candidates.append({"title": title, "artist": artist, "platform_ids": {"kuwo_rid": row.get("rid")}, "artist_ids": [], "recording_id": None, "isrc": None, "year": None})
    except Exception as exc:
        log(f"酷我实时目录搜索失败：{exc}")

    try:
        data = request_json(NETEASE_API, {"type": "search", "id": query, "limit": 100, "page": 1, "server": "netease"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=1)
        rows = data if isinstance(data, list) else []
        for row in rows:
            title, artist = row.get("name"), row.get("artist")
            if accept(title, artist):
                netease_song_id = parse_qs(urlparse(str(row.get("url") or "")).query).get("id", [""])[0]
                netease_cover_id = parse_qs(urlparse(str(row.get("pic") or "")).query).get("id", [""])[0]
                candidates.append({"title": title, "artist": artist, "platform_ids": {"netease_song_id": netease_song_id, "netease_cover_id": netease_cover_id}, "artist_ids": [], "recording_id": None, "isrc": None, "year": None})
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
        if mode == "search":
            parts = query.split()
            if len(parts) >= 2:
                if not exact_pair_match(song, query):
                    continue
            else:
                # 单项搜索必须是歌曲名或歌手名完全匹配；
                # 禁止“寂寞沙洲”匹配到“寂寞沙洲冷”等相似标题。
                title_match = canonical_title(song.get("title", "")) == canonical_title(query)
                artist_match = canonical_artist(song.get("artist", "")) == canonical_artist(query)
                if not (title_match or artist_match):
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
        detail = request_json(QQ_API, {"msg": f"{title} {artist}", "type": "json", "mid": row["song_mid"]}, SOURCE_HEADERS, timeout=DETAIL_TIMEOUT, retries=1)
        source_title = str(detail.get("song_title") or detail.get("song_name") or row.get("song_title") or title).strip()
        for tier, label in (("sq", "SQ"), ("pq", "PQ")):
            url = detail.get(f"song_play_url_{tier}")
            filename = detail.get(f"song_filename_{tier}")
            if url and filename and str(filename).lower().endswith(".flac"):
                return {"url": url, "filename": filename, "filename_title": source_title, "size": int(detail.get(f"song_size_{tier}_str") or 0), "source": "QQ", "quality": label, "platform_ids": {"qq_song_id": detail.get("song_id"), "qq_song_mid": detail.get("song_mid") or row.get("song_mid"), "qq_singer_id": detail.get("singer_id"), "qq_singer_mid": detail.get("singer_mid")}}
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
            return {"url": url, "filename": f"{title}.flac", "filename_title": str(item.get("song") or title).strip(), "size": size_bytes(item.get("size", "")), "source": "酷我", "quality": f"FLAC {item.get('bitrate', '2000')}kbps", "platform_ids": {"kuwo_id": item.get("id"), "kuwo_rid": item.get("rid") or row.get("rid")}}
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
                return {"url": download_url, "filename": f"{title}.flac", "filename_title": str(row_title).strip(), "size": size, "source": "网易云", "quality": "FLAC", "platform_ids": {"netease_song_id": song_id, "netease_cover_id": parse_qs(urlparse(str(row.get("pic") or "")).query).get("id", [""])[0]}}
        except Exception:
            pass
    return None


def find_source(song):
    found = []
    for func in (qq_search, kuwo_search, netease_search):
        try:
            item = func(song["title"], song["artist"])
            if item:
                # 保留目录发现和音源详情中的平台 ID。
                merged = {**item, **song}
                merged["platform_ids"] = {**item.get("platform_ids", {}), **song.get("platform_ids", {})}
                found.append(merged)
        except Exception as exc:
            log(f"{func.__name__} 搜索失败：{exc}")
    if not found:
        return None
    # 下载源严格按 QQ → 网易云 → 酷我；每个源内部已优先选择自身最高 FLAC 档位。
    source_priority = {"QQ": 0, "网易云": 1, "酷我": 2}
    return min(found, key=lambda x: source_priority.get(x.get("source"), 99))


def netease_metadata(title, artist):
    """网易云元数据独立于音频格式；先歌手精确匹配，失败时安全回退到唯一歌曲名。"""
    try:
        title_rows = []
        seen_urls = set()
        for search_text in (f"{title} {artist}", title):
            rows = request_json(
                NETEASE_API,
                {"type": "search", "id": search_text, "limit": 30, "page": 1, "server": "netease"},
                SOURCE_HEADERS,
                timeout=SOURCE_TIMEOUT,
                retries=1,
            )
            for row in rows if isinstance(rows, list) else []:
                row_title = str(row.get("name") or "").strip()
                row_artist = str(row.get("artist") or "").strip()
                if canonical_title(row_title) != canonical_title(title):
                    continue
                row_key = str(row.get("url") or row.get("lrc") or f"{row_title}|{row_artist}")
                if row_key in seen_urls:
                    continue
                seen_urls.add(row_key)
                title_rows.append(row)
                if canonical_artist(row_artist) == canonical_artist(artist):
                    selected = row
                    break
            else:
                continue
            if canonical_artist(str(selected.get("artist") or "")) == canonical_artist(artist):
                break
        else:
            selected = None

        if 'selected' not in locals() or selected is None:
            selected = title_rows[0] if len(title_rows) == 1 else None
        if selected is None:
            log(f"网易云未匹配到歌曲信息：{title} - {artist}")
            return {}

        row_title = str(selected.get("name") or "").strip()
        row_artist = str(selected.get("artist") or "").strip()
        cover_url = str(selected.get("pic") or "").strip()
        lyric_url = str(selected.get("lrc") or "").strip()
        lyrics = ""
        if lyric_url:
            lyric = requests.get(lyric_url, headers=SOURCE_HEADERS, timeout=DETAIL_TIMEOUT)
            if lyric.ok:
                lyrics = simplify_chinese_lyrics(lyric.text, title, artist)
        suffix = "歌手精确匹配" if canonical_artist(row_artist) == canonical_artist(artist) else "歌曲名唯一匹配"
        log(f"网易云歌曲信息已找到：{row_title} - {row_artist}（{suffix}，歌词={'有' if lyrics else '无'}，封面={'有' if cover_url else '无'}）")
        return {"album": str(selected.get("album") or ""), "cover_url": cover_url, "lyrics": lyrics}
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
                # Last.fm 可能只让某一个尺寸的 CDN 地址失效；
                # 按大图到小图依次尝试，成功一个即可写入封面。
                cover_urls = []
                for image in reversed(images):
                    image_url = str(image.get("#text") or "").strip() if isinstance(image, dict) else ""
                    if image_url and image_url not in cover_urls:
                        cover_urls.append(image_url)
                if cover_urls and not netease.get("cover_url"):
                    cover = None
                    for cover_url in cover_urls:
                        candidate = requests.get(cover_url, headers=SOURCE_HEADERS, timeout=30)
                        if candidate.status_code == 404:
                            continue
                        candidate.raise_for_status()
                        if candidate.content:
                            cover = candidate
                            break
                    if cover is None:
                        log("Last.fm 封面地址均不可用，跳过封面；专辑信息继续使用")
                    else:
                        picture = Picture()
                        picture.type = 3
                        picture.mime = cover.headers.get("Content-Type", "image/jpeg").split(";")[0]
                        picture.desc = "Cover"
                        picture.data = cover.content
                        audio.clear_pictures()
                        audio.add_picture(picture)
            except Exception as exc:
                if "Last.fm API 错误 6" in str(exc):
                    log(f"Last.fm 未找到歌曲信息：{title} - {artist}")
                else:
                    log(f"Last.fm 封面/专辑信息获取失败：{exc}")

        # LRCLIB 作为网易云无歌词时的补充。
        if not netease.get("lyrics"):
            try:
                lyric = requests.get("https://lrclib.net/api/get", params={"track_name": title, "artist_name": artist}, timeout=30)
                if lyric.status_code == 200:
                    lyric_data = lyric.json()
                    lyrics = lyric_data.get("syncedLyrics") or lyric_data.get("plainLyrics")
                    lyrics = simplify_chinese_lyrics(lyrics, title, artist)
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
    # 挂载存储对撇号和反斜杠的转义不一致，文件名统一去除这两类字符。
    value = str(value).replace("/'", "'").replace("\\'", "'").replace('\\"', '"')
    value = value.replace("'", "").replace("\\", "")
    value = re.sub(r'[/:*?"<>|\x00-\x1f]', "_", value).strip().rstrip(" .")
    return (value or "unknown")[:180]


def alist_auth():
    required = ["ALIST_URL", "ALIST_TOKEN"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        fail("缺少 AList Secret: " + ", ".join(missing))
    return (os.environ["ALIST_URL"].rstrip("/"), os.environ["ALIST_TOKEN"])


def alist_headers(auth, extra=None):
    headers = {"Authorization": auth[1]}
    if extra:
        headers.update(extra)
    return headers


def alist_file_path(filename=None, subfolder=None):
    base = (os.getenv("ALIST_PATH") or "/cd18/Music").strip("/")
    parts = [part for part in base.split("/") if part]
    if subfolder:
        parts.append(safe_name(str(subfolder).strip("/")))
    if filename:
        # 最后一层防线：文件名绝不能把 / 或 \ 传给 AList。
        parts.append(safe_name(str(filename).strip("/")))
    return "/" + "/".join(parts)


def alist_api(auth, endpoint):
    return f"{auth[0]}/api/fs/{endpoint.lstrip('/')}"


def ensure_alist_folder(auth, subfolder=None):
    path = alist_file_path(subfolder=subfolder)
    r = requests.post(alist_api(auth, "mkdir"), headers=alist_headers(auth, {"Content-Type": "application/json"}), json={"path": path}, timeout=60)
    if r.status_code >= 400:
        try:
            data = r.json()
        except ValueError:
            data = {}
        # AList 已存在目录时返回错误，后续 list/put 仍可正常进行。
        if data.get("code") not in (200, 400):
            r.raise_for_status()


def alist_listing(auth, subfolder=None):
    path = alist_file_path(subfolder=subfolder)
    r = requests.post(alist_api(auth, "list"), headers=alist_headers(auth, {"Content-Type": "application/json"}), json={"path": path, "password": "", "page": 1, "per_page": 1000, "refresh": True}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError(f"AList 列目录失败：{data.get('message', data)}")
    result = {}
    for item in (data.get("data") or {}).get("content", []) or []:
        if isinstance(item, dict) and item.get("name"):
            raw_size = item.get("size")
            try:
                size = int(raw_size or 0)
            except (TypeError, ValueError):
                match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KB|KiB|MB|MiB|GB|GiB)\s*$", str(raw_size or ""), re.I)
                if match:
                    number, unit = float(match.group(1)), match.group(2).lower()
                    size = int(number * {"b": 1, "kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2, "gb": 1024**3, "gib": 1024**3}[unit])
                else:
                    size = 0
            result[str(item["name"])] = size
    return result


def choose_filename(auth, base_filename, size, subfolder=None):
    """同名且相近则跳过；同名不同体积则追加 [xx.xxMB]。"""
    files = alist_listing(auth, subfolder=subfolder)
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


def upload(auth, local_path, filename, subfolder=None):
    filename = safe_name(filename)
    path = alist_file_path(filename, subfolder=subfolder)
    expected = local_path.stat().st_size
    log(f"AList API 上传：{filename}")
    encoded_path = quote(path, safe="/")
    headers = alist_headers(auth, {"File-Path": encoded_path, "Content-Length": str(expected), "Content-Type": "audio/flac", "As-Task": "false"})
    with local_path.open("rb") as handle:
        r = requests.put(alist_api(auth, "put"), headers=headers, data=handle, timeout=600)
    try:
        data = r.json()
    except ValueError as exc:
        raise RuntimeError(f"AList 上传返回非 JSON：HTTP {r.status_code}") from exc
    if r.status_code >= 400 or data.get("code") != 200:
        # 部分挂载盘会先完成写入，再因解析远端时间失败而返回错误。
        # 只有按文件名和大小确认远端文件存在时，才将此类响应计为成功。
        last_observed = None
        for attempt in range(3):
            if attempt:
                time.sleep(5)
            try:
                files = alist_listing(auth, subfolder=subfolder)
                last_observed = files.get(filename)
                if filename in files and last_observed and abs(last_observed - expected) <= SIZE_TOLERANCE:
                    log(f"AList 返回错误，但远程文件已确认存在：{filename}")
                    return
            except Exception as verify_exc:
                last_observed = f"确认接口异常：{verify_exc}"
        message = data.get("message", data) if isinstance(data, dict) else data
        if last_observed is not None:
            log(f"AList 上传后确认未通过：文件={filename}，远程大小={last_observed}，本地大小={expected}")
        # 挂载盘已写入文件，但 AList 在构造响应时解析非标准时间失败。
        # 该特征错误发生在写入之后；目录接口也可能继承同一时间解析问题。
        if isinstance(message, str) and message.startswith("parsing time "):
            log(f"AList 返回时间解析错误，按文件已提交处理：{filename}")
            return
        raise RuntimeError(f"AList 上传失败：{message}")


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
        "error": payload.get("error", ""),
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
    auth = alist_auth()
    ensure_alist_folder(auth)
    log("AList API 连接正常，开始逐首处理；单项歌曲名搜索保存到歌曲名文件夹，歌手搜索保存到歌手文件夹")
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
        filename_title = safe_name(str(found.get("filename_title") or original["title"]).strip())
        # 单项歌曲名搜索：所有歌手/演唱版本统一放入歌曲名文件夹；
        # 歌手搜索及“歌曲名+歌手名”搜索仍按歌手分文件夹。
        single_title_search = (
            mode == "search"
            and len(query.split()) == 1
            and canonical_title(original.get("title", "")) == canonical_title(query)
        )
        target_folder = safe_name(normalize_folder_label(original.get("title") or filename_title)) if single_title_search else artist_folder
        ensure_alist_folder(auth, target_folder)
        log(f"[{index}/{len(songs)}] 目标文件夹：{target_folder}")
        base_filename = safe_name(f"{filename_title} {original['artist']}.flac")
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
            log(f"[{index}/{len(songs)}] 下载完成：{actual / 1048576:.2f} MiB，上传前检查 AList 目录文件")
            if found["size"] and abs(actual - found["size"]) > SIZE_TOLERANCE:
                raise RuntimeError(f"体积异常 {actual}/{found['size']}")
            embed_metadata(local, found)
            actual = local.stat().st_size
            filename = choose_filename(auth, base_filename, actual, subfolder=target_folder)
            if filename is None:
                local.unlink(missing_ok=True)
                skipped += 1
                log(f"[{index}/{len(songs)}] 跳过：AList 已存在相同文件")
                continue
            upload(auth, local, filename, subfolder=target_folder)
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
        message = str(exc)
        print(f"ERROR: {message}", file=sys.stderr, flush=True)
        if ACTIVE_PAYLOAD is not None:
            ACTIVE_PAYLOAD["status"] = "failed"
            ACTIVE_PAYLOAD["cancelled"] = False
            ACTIVE_PAYLOAD["error"] = message
            ACTIVE_PAYLOAD.setdefault("success_count", 0)
            ACTIVE_PAYLOAD.setdefault("failed_songs", [])
            try:
                callback(ACTIVE_PAYLOAD)
                log("异常状态已回调 n8n")
            except Exception as callback_exc:
                print(f"ERROR: 异常回调失败：{callback_exc}", file=sys.stderr, flush=True)
        sys.exit(1)
