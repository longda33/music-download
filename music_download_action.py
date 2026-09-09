#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Action worker: MusicBrainz discovery -> syy.py sources -> AList."""
import difflib
import json
import mimetypes
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
HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update(SOURCE_HEADERS)
DEFAULT_HTTP_TIMEOUT = 60
RETRIES = 3
RETRY_INTERVAL = 10
SOURCE_TIMEOUT = 12
DETAIL_TIMEOUT = 8
SIZE_TOLERANCE = 3 * 1024 * 1024
ALLOW_NON_FLAC = False


def parse_download_query(value):
    """解析末尾 -all；只移除控制参数，不改变歌曲名内部的短横线。"""
    text = str(value or "").strip()
    if re.search(r"(?i)-all$", text):
        return re.sub(r"(?i)-all$", "", text).strip(), True
    return text, False


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


def http_request(method, url, **kwargs):
    """通过复用 Session 统一处理请求头、超时和有限重试。"""
    retry_count = kwargs.pop("_retry_count", RETRIES)
    kwargs.setdefault("timeout", DEFAULT_HTTP_TIMEOUT)
    error = None
    for attempt in range(1, retry_count + 1):
        try:
            response = HTTP_SESSION.request(method, url, **kwargs)
            status = getattr(response, "status_code", 0)
            if status == 429 or status >= 500:
                response.close()
                raise RuntimeError(f"HTTP {status}")
            return response
        except Exception as exc:
            error = exc
            if attempt < retry_count:
                time.sleep(RETRY_INTERVAL)
    raise error


def request_json(url, params=None, headers=None, timeout=60, retries=RETRIES):
    error = None
    for attempt in range(1, retries + 1):
        try:
            # JSON 解析/HTTP 状态失败也应重试，但避免与底层 HTTP 重试叠加。
            r = http_request("GET", url, params=params, headers=headers, timeout=timeout, _retry_count=1)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            error = exc
            if attempt < retries:
                time.sleep(RETRY_INTERVAL)
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
    if not 1 <= len(names) <= 4:
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
    r = http_request("GET", LASTFM_API, params=query, timeout=30)
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


ARTIST_FOLDER_NAMES = {}


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
    """使用 Unicode/繁简/标点归一化生成艺人身份，不依赖别名表。"""
    raw = unicodedata.normalize("NFKC", to_simplified(value)).strip().casefold()
    parts = [dedup_key(part) for part in re.split(r"[&+/、,，;；|]+", raw) if part.strip()]
    return "&".join(sorted(set(parts)))


def canonical_title(value):
    return dedup_key(unicodedata.normalize("NFKC", to_simplified(value or "")).strip())


def dedup_title(value):
    """用于去重：Live/现场后缀与原版视为同一首歌，显示标题不改变。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    # 只移除明确位于标题末尾的 Live/现场标识，避免误合并不同歌曲。
    text = re.sub(
        r"(?:\s*[-－–—]?\s*(?:[（(]\s*)?(?:live|现场版?|live版)(?:\s*[）)])?\s*)$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    return canonical_title(text)


def query_terms(query):
    """仅按第一个半角短横线分隔歌曲名和歌手名，避免空格造成歧义。"""
    query = str(query or "").strip()
    if "-" in query:
        title, artist = query.split("-", 1)
        title, artist = title.strip(), artist.strip()
        if title and artist:
            return [title, artist]
    return [query]


VERSION_MARKERS = (
    "粤语", "国语", "普通话", "中文版", "粤语版", "国语版",
    "live", "现场", "现场版", "伴奏", "纯音乐", "instrumental",
    "remix", "dj版", "dj", "翻唱", "cover", "acoustic",
)


def is_title_variant(title, query):
    """单项歌曲名批量搜索时，允许明确标注的同曲不同版本。"""
    title_key = canonical_title(title)
    query_key = canonical_title(query)
    if title_key == query_key or not title_key.startswith(query_key):
        return False
    suffix = title_key[len(query_key):]
    return any(marker in suffix for marker in VERSION_MARKERS)


def normalize_folder_label(value):
    """生成稳定的文件夹名，消除括号、空格和全角字符造成的重复目录。"""
    text = unicodedata.normalize("NFKC", to_simplified(str(value or ""))).strip()
    # 不同平台可能返回全角/半角括号或混用括号；文件夹统一使用半角括号。
    text = text.translate(str.maketrans({"（": "(", "）": ")", "［": "[", "］": "]", "【": "[", "】": "]", "｛": "{", "｝": "}"}))
    # 去掉括号前后空格，也处理括号内部由平台插入的多余空格。
    text = re.sub(r"\s*([()\[\]{}])\s*", r"\1", text)
    # 中文字符之间的错误空格：芳 华 慢 → 芳华慢。
    text = re.sub(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])", "", text)
    # 其余连续空白统一为一个普通空格。
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def artists_match(left, right):
    """使用归一化、包含关系和相似度识别艺人，不依赖规则表或别名穷举。"""
    left_key, right_key = canonical_artist(left), canonical_artist(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    # 仅对单一艺人做模糊判断，避免把合作艺人列表错误合并。
    if "&" in left_key or "&" in right_key:
        return False
    shorter = min(len(left_key), len(right_key))
    if shorter < 4:
        return False
    if left_key in right_key or right_key in left_key:
        return True
    return difflib.SequenceMatcher(None, left_key, right_key).ratio() >= 0.92


def artist_folder_name(value, related=None):
    """生成稳定的艺人文件夹名称，不依赖别名文件。"""
    key = canonical_artist(value)
    return normalize_folder_label(ARTIST_FOLDER_NAMES.get(key) or value)


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
    parts = query_terms(query)
    lookup_query = " ".join(parts)
    pair_terms = []
    if len(parts) >= 2:
        for cut in range(1, len(parts)):
            left, right = " ".join(parts[:cut]), " ".join(parts[cut:])
            pair_terms.extend([(left, right), (right, left)])

    def exact_accept(title, artist):
        """歌曲名-歌手名按艺人精确匹配，并允许同曲的明确版本。"""
        title, artist = str(title or "").strip(), str(artist or "").strip()
        if not title or not artist:
            return False
        if len(parts) == 1:
            return True
        return any(
            canonical_artist(artist) == canonical_artist(a)
            and (canonical_title(title) == canonical_title(t) or is_title_variant(title, t))
            for t, a in pair_terms
        )

    def accept(title, artist):
        """GitHub Action 本地初筛：至少标题或艺人命中。"""
        title, artist = str(title or "").strip(), str(artist or "").strip()
        if not title or not artist:
            return False
        if len(parts) == 1:
            return True
        return any(canonical_title(title) == canonical_title(t)
                   or canonical_artist(artist) == canonical_artist(a)
                   for t, a in pair_terms)

    try:
        for row in qq_primary_discover(lookup_query):
            title, artist = row.get("title"), row.get("artist")
            if accept(title, artist):
                row["_exact_match"] = exact_accept(title, artist)
                row["discovery_source"] = "QQ aa.cab"
                candidates.append(row)
    except Exception as exc:
        log(f"新 QQ 实时目录搜索失败：{exc}")

    try:
        rows = request_json(QQ_API, {"msg": lookup_query, "type": "json"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=RETRIES)
        for row in rows if isinstance(rows, list) else []:
            title = row.get("song_title") or row.get("song_name")
            artist = row.get("singer_name")
            if accept(title, artist):
                candidates.append({"title": title, "artist": artist, "discovery_source": "QQ tang.api.s01s.cn", "platform_ids": {"qq_song_mid": row.get("song_mid")}, "artist_ids": [], "recording_id": None, "isrc": None, "year": None, "_exact_match": exact_accept(title, artist)})
    except Exception as exc:
        log(f"QQ 实时目录搜索失败：{exc}")

    try:
        data = request_json(KUWO_API, {"msg": lookup_query, "page": 1, "limit": 100}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=RETRIES)
        rows = data.get("data", []) if isinstance(data, dict) else []
        for row in rows:
            title, artist = row.get("song"), row.get("singer")
            if accept(title, artist):
                candidates.append({"title": title, "artist": artist, "discovery_source": "酷我", "platform_ids": {"kuwo_rid": row.get("rid")}, "artist_ids": [], "recording_id": None, "isrc": None, "year": None, "_exact_match": exact_accept(title, artist)})
    except Exception as exc:
        log(f"酷我实时目录搜索失败：{exc}")

    try:
        data = request_json(NETEASE_API, {"type": "search", "id": lookup_query, "limit": 100, "page": 1, "server": "netease"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=RETRIES)
        rows = data if isinstance(data, list) else []
        for row in rows:
            title, artist = row.get("name"), row.get("artist")
            if accept(title, artist):
                netease_song_id = parse_qs(urlparse(str(row.get("url") or "")).query).get("id", [""])[0]
                netease_cover_id = parse_qs(urlparse(str(row.get("pic") or "")).query).get("id", [""])[0]
                candidates.append({"title": title, "artist": artist, "discovery_source": "网易云", "platform_ids": {"netease_song_id": netease_song_id, "netease_cover_id": netease_cover_id}, "artist_ids": [], "recording_id": None, "isrc": None, "year": None, "_exact_match": exact_accept(title, artist)})
    except Exception as exc:
        log(f"网易云实时目录搜索失败：{exc}")
    exact, pending = [], []
    for item in candidates:
        (exact if item.pop("_exact_match", False) else pending).append(item)
    if pending:
        log(f"本地匹配未确认 {len(pending)} 首，已跳过")
    return exact


def exact_pair_match(song, query):
    """校验双项命令，防止模糊搜索把相似歌名当成目标歌曲。"""
    parts = query_terms(query)
    if len(parts) < 2:
        return True
    title = canonical_title(song.get("title", ""))
    artist = str(song.get("artist", ""))
    for cut in range(1, len(parts)):
        left, right = " ".join(parts[:cut]), " ".join(parts[cut:])
        if ((artists_match(artist, right)
             and (title == canonical_title(left) or is_title_variant(song.get("title", ""), left)))
                or (artists_match(artist, left)
                    and (title == canonical_title(right) or is_title_variant(song.get("title", ""), right)))):
            return True
    return False


def space_pair_match(song, query):
    """兼容“歌曲名 歌手名”输入，同时允许歌曲名带明确版本。"""
    raw_parts = [part for part in re.split(r"\s+", str(query or "").strip()) if part]
    if len(raw_parts) < 2:
        return False
    title = str(song.get("title", ""))
    artist = str(song.get("artist", ""))
    for cut in range(1, len(raw_parts)):
        left = " ".join(raw_parts[:cut])
        right = " ".join(raw_parts[cut:])
        if ((artists_match(artist, right)
             and (canonical_title(title) == canonical_title(left) or is_title_variant(title, left)))
                or (artists_match(artist, left)
                    and (canonical_title(title) == canonical_title(right) or is_title_variant(title, right)))):
            return True
    return False


def discover_songs(mode, query):
    songs = []
    lookup_query = " ".join(query_terms(query))
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
            # 单项搜索会同时处理歌曲名、歌手名和明确版本。
            parts = query_terms(query)
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
                data = lastfm_get("track.search", {"track": lookup_query, "limit": 20, "page": 1})
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
            data = lastfm_get("track.search", {"track": lookup_query, "limit": 100, "page": 1})
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
            parts = query_terms(query)
            if len(parts) >= 2:
                if not exact_pair_match(song, query):
                    continue
            else:
                # 单项搜索必须是歌曲名或歌手名完全匹配；
                # 禁止“寂寞沙洲”匹配到“寂寞沙洲冷”等相似标题。
                title_match = canonical_title(song.get("title", "")) == canonical_title(query)
                version_match = is_title_variant(song.get("title", ""), query)
                artist_match = canonical_artist(song.get("artist", "")) == canonical_artist(query)
                if not (title_match or version_match or artist_match or space_pair_match(song, query)):
                    continue
        artists = [{"artist": {"name": name.strip()}} for name in re.split(r"[&+/、,，;；|]+", str(song.get("artist", ""))) if name.strip()]
        if not song["title"] or not one_artist(artists):
            continue
        keys = identity_keys(song)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        result.append(song)
    return result


def qq_primary_result(query, index=1):
    """新 QQ 接口：通过 n 获取第 index 条 SQ 结果；不使用旧 QQ 参数。"""
    data = request_json(
        "https://a.aa.cab/qq.music",
        {"msg": query, "n": index, "type": 2 if ALLOW_NON_FLAC else 4},
        SOURCE_HEADERS,
        timeout=SOURCE_TIMEOUT,
        retries=RETRIES,
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        return None
    row = data.get("data")
    return row if isinstance(row, dict) else None


def qq_primary_search(title, artist, index=1):
    """优先使用新 QQ 接口，精确获取指定结果的 SQ 无损地址。"""
    query = f"{title} {artist}"
    row = qq_primary_result(query, index)
    if not row:
        return None
    row_title = str(row.get("song") or row.get("song_name") or "").strip()
    row_artist = str(row.get("singer") or row.get("artist") or "").strip()
    music = str(row.get("music") or row.get("url") or "").strip()
    artist_parts = [part.strip() for part in re.split(r"[,，/&、]+", row_artist) if part.strip()]
    if (canonical_title(row_title) != canonical_title(title)
            or not any(artists_match(part, artist) for part in artist_parts)
            or not music):
        return None
    if not ALLOW_NON_FLAC and not music.lower().split("?")[0].endswith(".flac"):
        return None
    extension = Path(urlparse(music).path).suffix.lower().lstrip(".") or ("flac" if not ALLOW_NON_FLAC else "mp3")
    return {
        "url": music,
        "filename": f"{row_title} {row_artist}.{extension}",
        "filename_title": row_title,
        "size": int(row.get("size") or 0),
        "source": "QQ aa.cab",
        "quality": "标准音质" if ALLOW_NON_FLAC else "SQ无损",
        "platform_ids": {
            "qq_primary_mid": row.get("mid"),
            "qq_primary_media_mid": row.get("media_mid"),
            "qq_primary_album_mid": row.get("album_mid"),
        },
    }


def qq_primary_discover(query):
    """新 QQ 接口搜索：按参考接口使用 msg+num，一次最多返回 60 条。"""
    data = request_json(
        "https://a.aa.cab/qq.music",
        {"msg": query, "num": 60},
        SOURCE_HEADERS,
        timeout=SOURCE_TIMEOUT,
        retries=RETRIES,
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        return []
    raw = data.get("data")
    rows = raw if isinstance(raw, list) else []
    result = []
    seen = set()
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        title = str(row.get("song") or row.get("song_name") or "").strip()
        artist = str(row.get("singer") or row.get("artist") or "").strip()
        mid = str(row.get("mid") or row.get("media_mid") or "")
        key = (canonical_title(title), canonical_artist(artist), mid)
        if not title or not artist or key in seen:
            continue
        seen.add(key)
        index = int(row.get("num") or position)
        result.append({"title": title, "artist": artist, "platform_ids": {"qq_primary_n": index, "qq_primary_mid": row.get("mid"), "qq_primary_media_mid": row.get("media_mid"), "qq_primary_album_mid": row.get("album_mid")}, "artist_ids": [], "recording_id": None, "isrc": None, "year": None})
    return result


def qq_search(title, artist):
    """旧 QQ 接口适配器：保持原有 type=json、song_mid 详情请求不变。"""
    rows = request_json(QQ_API, {"msg": f"{title} {artist}", "type": "json"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=RETRIES)
    if not isinstance(rows, list):
        return None
    for row in rows[:3]:
        if not isinstance(row, dict) or not row.get("song_mid"):
            continue
        row_title = str(row.get("song_title") or row.get("song_name") or "").strip()
        row_artist = str(row.get("singer_name") or row.get("singer") or "").strip()
        if canonical_title(row_title) != canonical_title(title):
            continue
        if canonical_artist(row_artist) != canonical_artist(artist):
            continue
        detail = request_json(QQ_API, {"msg": f"{title} {artist}", "type": "json", "mid": row["song_mid"]}, SOURCE_HEADERS, timeout=DETAIL_TIMEOUT, retries=RETRIES)
        detail_title = str(detail.get("song_title") or detail.get("song_name") or "").strip()
        detail_artist = str(detail.get("singer_name") or detail.get("singer") or "").strip()
        if not detail_title or not detail_artist:
            log(f"QQ 详情缺少歌曲名或歌手名，跳过：{row_title} - {row_artist}")
            continue
        source_title = detail_title
        if canonical_title(source_title) != canonical_title(title) or canonical_artist(detail_artist) != canonical_artist(artist):
            log(f"QQ 结果与目标不一致，跳过：{source_title} - {detail_artist}")
            continue
        for tier, label in (("sq", "SQ"), ("pq", "PQ")):
            url = detail.get(f"song_play_url_{tier}")
            filename = detail.get(f"song_filename_{tier}")
            if url and filename and (ALLOW_NON_FLAC or str(filename).lower().endswith(".flac")):
                extension = Path(urlparse(str(url)).path).suffix.lower().lstrip(".") or Path(str(filename)).suffix.lower().lstrip(".") or "mp3"
                return {"url": url, "filename": filename, "filename_title": source_title, "size": int(detail.get(f"song_size_{tier}_str") or 0), "source": "QQ tang.api.s01s.cn", "quality": label, "extension": extension, "platform_ids": {"qq_song_id": detail.get("song_id"), "qq_song_mid": detail.get("song_mid") or row.get("song_mid"), "qq_singer_id": detail.get("singer_id"), "qq_singer_mid": detail.get("singer_mid")}}
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
    data = request_json(KUWO_API, {"msg": query, "page": 1, "limit": 10}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=RETRIES)
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
        if not ALLOW_NON_FLAC and not has_flac:
            continue
        detail = request_json(KUWO_API, {"msg": query, "n": index, "br": 1}, SOURCE_HEADERS, timeout=DETAIL_TIMEOUT, retries=RETRIES)
        item = detail.get("data", {}) if isinstance(detail, dict) else {}
        detail_title = str(item.get("song") or item.get("name") or "").strip() if isinstance(item, dict) else ""
        detail_artist = str(item.get("singer") or item.get("artist") or "").strip() if isinstance(item, dict) else ""
        if not detail_title or not detail_artist:
            log(f"酷我详情缺少歌曲名或歌手名，跳过：{row.get('song')} - {row.get('singer')}")
            continue
        if canonical_title(detail_title) != canonical_title(title) or canonical_artist(detail_artist) != canonical_artist(artist):
            log(f"酷我结果与目标不一致，跳过：{detail_title} - {detail_artist}")
            continue
        url = item.get("url") if isinstance(item, dict) else ""
        fmt = str(item.get("format", "")).lower() if isinstance(item, dict) else ""
        if url and (ALLOW_NON_FLAC or (fmt == "flac" and str(url).lower().split("?")[0].endswith(".flac"))):
            extension = Path(urlparse(str(url)).path).suffix.lower().lstrip(".") or fmt or "mp3"
            return {"url": url, "filename": f"{title}.{extension}", "filename_title": detail_title, "size": size_bytes(item.get("size", "")), "source": "酷我", "quality": f"{fmt.upper() or 'AUDIO'} {item.get('bitrate', '')}kbps", "extension": extension, "platform_ids": {"kuwo_id": item.get("id"), "kuwo_rid": item.get("rid") or row.get("rid")}}
    return None


def netease_search(title, artist):
    data = request_json(NETEASE_API, {"type": "search", "id": f"{title} {artist}", "limit": 10, "page": 1, "server": "netease"}, SOURCE_HEADERS, timeout=SOURCE_TIMEOUT, retries=RETRIES)
    for row in (data if isinstance(data, list) else []):
        row_title, row_artist = row.get("name", ""), row.get("artist", "")
        if canonical_title(row_title) != canonical_title(title) or canonical_artist(row_artist) != canonical_artist(artist):
            continue
        search_url = row.get("url", "")
        song_id = parse_qs(urlparse(search_url).query).get("id", [""])[0]
        if not song_id:
            continue
        # 搜索和下载接口分开：明确请求网易云无损档位 br=2000。
        download_url = f"{NETEASE_API}?server=netease&type=url&id={song_id}&br={320 if ALLOW_NON_FLAC else 2000}"
        try:
            probe = http_request("GET", download_url, headers=SOURCE_HEADERS, timeout=60, allow_redirects=True, stream=True)
            content_type = probe.headers.get("content-type", "").lower()
            is_flac = ".flac" in probe.url.lower() or "audio/flac" in content_type or "audio/x-flac" in content_type
            size = int(probe.headers.get("content-length", 0) or 0)
            probe.close()
            if ALLOW_NON_FLAC or is_flac:
                extension = Path(urlparse(probe.url).path).suffix.lower().lstrip(".") or ("flac" if is_flac else "mp3")
                return {"url": download_url, "filename": f"{title}.{extension}", "filename_title": str(row_title).strip(), "size": size, "source": "网易云", "quality": "FLAC" if is_flac else "标准音质", "extension": extension, "platform_ids": {"netease_song_id": song_id, "netease_cover_id": parse_qs(urlparse(str(row.get("pic") or "")).query).get("id", [""])[0]}}
        except Exception:
            pass
    return None


def find_source(song, excluded_sources=None):
    """先使用发现该歌曲的音源；解析或实际下载失败时可排除已失败音源。"""
    excluded = set(excluded_sources or ())
    source_funcs = {
        "QQ aa.cab": qq_primary_search,
        "QQ tang.api.s01s.cn": qq_search,
        "网易云": netease_search,
        "酷我": kuwo_search,
    }
    source_order = ["QQ aa.cab", "QQ tang.api.s01s.cn", "网易云", "酷我"]
    preferred = song.get("discovery_source")
    if preferred in source_order:
        ordered_sources = [preferred] + [source for source in source_order if source != preferred]
    else:
        ordered_sources = source_order

    for source in ordered_sources:
        if source in excluded:
            continue
        func = source_funcs[source]
        try:
            if func is qq_primary_search:
                index = int((song.get("platform_ids") or {}).get("qq_primary_n") or 1)
                item = func(song["title"], song["artist"], index=index)
            else:
                item = func(song["title"], song["artist"])
            if item:
                merged = {**item, **song}
                merged["platform_ids"] = {**item.get("platform_ids", {}), **song.get("platform_ids", {})}
                log(f"音源解析：使用 {source}，已成功解析")
                return merged
        except Exception as exc:
            log(f"{source} 搜索失败，准备尝试下一个音源：{exc}")
    return None


def download_audio(found, local, index, total_count):
    """下载音频；HTTP 404/413 等错误交给调用方切换音源或记录失败。"""
    r = http_request("GET", found["url"], headers=SOURCE_HEADERS, stream=True, timeout=300)
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
                        log(f"[{index}/{total_count}] 下载进度：{downloaded / 1048576:.2f}/{total / 1048576:.2f} MiB ({percent:.1f}%)")
                    else:
                        log(f"[{index}/{total_count}] 已下载：{downloaded / 1048576:.2f} MiB")
                    last_report = now
    return local.stat().st_size


def netease_metadata(title, artist):
    """网易云元数据独立于音频格式；先歌手精确匹配，失败时安全回退到唯一歌曲名。"""
    try:
        title_rows = []
        seen_urls = set()
        selected = None
        for search_text in (f"{title} {artist}", title):
            rows = request_json(
                NETEASE_API,
                {"type": "search", "id": search_text, "limit": 30, "page": 1, "server": "netease"},
                SOURCE_HEADERS,
                timeout=SOURCE_TIMEOUT,
                retries=RETRIES,
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

        if selected is None:
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
            lyric = http_request("GET", lyric_url, headers=SOURCE_HEADERS, timeout=DETAIL_TIMEOUT)
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
            cover = http_request("GET", netease["cover_url"], headers=SOURCE_HEADERS, timeout=30)
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
                        candidate = http_request("GET", cover_url, headers=SOURCE_HEADERS, timeout=30)
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
                lyric = http_request("GET", "https://lrclib.net/api/get", params={"track_name": title, "artist_name": artist}, timeout=30)
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
    # 过滤 Windows 保留符号、C0/C1 控制符、零宽字符及 BOM；
    # NFKC 归一化后再检查，兼容全角输入和跨平台挂载路径。
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r'[/:*?"<>|\x00-\x1f\x7f-\x9f\u200b-\u200d\u2060\ufeff]', "_", value)
    value = re.sub(r"[\u2028\u2029]", "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._").rstrip(" .")
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
    r = http_request("POST", alist_api(auth, "mkdir"), headers=alist_headers(auth, {"Content-Type": "application/json"}), json={"path": path}, timeout=60)
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
    r = http_request("POST", alist_api(auth, "list"), headers=alist_headers(auth, {"Content-Type": "application/json"}), json={"path": path, "password": "", "page": 1, "per_page": 1000, "refresh": True}, timeout=60)
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
    content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    headers = alist_headers(auth, {"File-Path": encoded_path, "Content-Length": str(expected), "Content-Type": content_type, "As-Task": "false"})
    with local_path.open("rb") as handle:
        r = http_request("PUT", alist_api(auth, "put"), headers=headers, data=handle, timeout=600)
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
                time.sleep(RETRY_INTERVAL)
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
        "skipped_count": payload.get("skipped_count", 0),
        "failed_songs": payload.get("failed_songs", []),
        "failed_details": payload.get("failed_details", []),
        "error": payload.get("error", ""),
    }
    http_request("POST", url, headers=headers, json=result, timeout=30).raise_for_status()


def main():
    global ACTIVE_PAYLOAD
    raw = os.getenv("EVENT_PAYLOAD", "")
    if not raw:
        fail("EVENT_PAYLOAD 为空")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    ACTIVE_PAYLOAD = payload
    query, query_all = parse_download_query(payload.get("query", ""))
    global ALLOW_NON_FLAC
    ALLOW_NON_FLAC = bool(payload.get("allow_non_flac") or query_all)
    mode = payload.get("mode", "singer")
    if not query:
        fail("缺少 query")
    log(f"开始任务：mode={mode}, query={query}")
    songs = discover_songs(mode, query)
    log(f"目录检索完成：共 {len(songs)} 首，歌曲名-歌手名支持同曲 Live、现场、伴奏、Remix 等明确版本，五人及以上合唱已过滤")
    if not songs:
        message = "未搜索到歌曲，请检查输入的歌曲名称或歌手名称是否正确。"
        log(message)
        payload["status"] = "no_results"
        payload["cancelled"] = False
        payload["success_count"] = 0
        payload["skipped_count"] = 0
        payload["failed_songs"] = []
        payload["failed_details"] = []
        payload["error"] = message
        callback(payload)
        print(json.dumps({"status": "no_results", "success_count": 0, "skipped_count": 0, "failed_songs": [], "failed_details": [], "error": message}, ensure_ascii=False))
        return
    auth = alist_auth()
    ensure_alist_folder(auth)
    log("AList API 连接正常，开始逐首处理；单项歌曲名搜索保存到歌曲名文件夹，歌手搜索保存到歌手文件夹")
    work = Path("downloaded_music")
    work.mkdir(exist_ok=True)
    success = 0
    skipped = 0
    failed = []
    failed_details = []
    # 仅在实际下载完成后按体积去重：同名 Live/原版体积相同才视为同一首。
    downloaded_song_sizes = {}
    for index, original in enumerate(songs, 1):
        label = f"{original['title']} - {original['artist']}"
        log(f"[{index}/{len(songs)}] 搜索音源：{label}")
        found = find_source(original)
        if not found:
            log(f"[{index}/{len(songs)}] 失败：三个音源都没有找到可用音频")
            failed.append(label)
            failed_details.append({
                "title": original.get("title", ""),
                "artist": original.get("artist", ""),
                "stage": "source_resolution",
                "source": "QQ aa.cab/QQ tang.api.s01s.cn/网易云音乐/酷我音乐",
                "error": "三个音源都没有找到可用音频下载地址",
            })
            continue
        log(f"[{index}/{len(songs)}] 找到音源：{found['source']} {found.get('quality', 'FLAC')}")
        filename_title = safe_name(str(found.get("filename_title") or original["title"]).strip())
        # 单项歌曲名搜索：所有歌手/演唱版本统一放入歌曲名文件夹；
        # 歌手搜索及“歌曲名-歌手名”搜索仍按歌手分文件夹。
        single_title_search = (
            mode == "search"
            and len(query_terms(query)) == 1
            and (
                canonical_title(original.get("title", "")) == canonical_title(query)
                or is_title_variant(original.get("title", ""), query)
            )
        )
        artist_folder = ""
        if not single_title_search:
            artist_folder = safe_name(artist_folder_name(found["artist"], original.get("artist")))
        folder_label = query if single_title_search else (original.get("title") or filename_title)
        target_folder = safe_name(normalize_folder_label(folder_label)) if single_title_search else artist_folder
        ensure_alist_folder(auth, target_folder)
        log(f"[{index}/{len(songs)}] 目标文件夹：{target_folder}")
        extension = str(found.get("extension") or Path(str(found.get("filename") or "")).suffix.lstrip(".") or "flac").lower()
        base_filename = safe_name(f"{filename_title} {original['artist']}.{extension}")
        local = work / base_filename
        stage = "download"
        try:
            attempted_sources = set()
            while True:
                attempted_sources.add(str(found.get("source", "unknown")))
                try:
                    actual = download_audio(found, local, index, len(songs))
                    break
                except Exception as download_exc:
                    local.unlink(missing_ok=True)
                    failed_source = str(found.get("source", "unknown"))
                    log(f"[{index}/{len(songs)}] 下载失败：音源={failed_source}，原因={download_exc}；准备切换下一个音源")
                    next_found = find_source(original, excluded_sources=attempted_sources)
                    if not next_found:
                        raise
                    found = next_found
                    filename_title = safe_name(str(found.get("filename_title") or original["title"]).strip())
                    extension = str(found.get("extension") or Path(str(found.get("filename") or "")).suffix.lstrip(".") or "flac").lower()
                    base_filename = safe_name(f"{filename_title} {original['artist']}.{extension}")
                    local = work / base_filename
                    log(f"[{index}/{len(songs)}] 切换音源：{found['source']}")

            log(f"[{index}/{len(songs)}] 下载完成：{actual / 1048576:.2f} MiB，上传前检查 AList 目录文件")
            if found["size"] and abs(actual - found["size"]) > SIZE_TOLERANCE:
                raise RuntimeError(f"体积异常 {actual}/{found['size']}")
            dedup_key_value = (dedup_title(original.get("title", "")), canonical_artist(original.get("artist", "")))
            known_sizes = downloaded_song_sizes.setdefault(dedup_key_value, set())
            if actual in known_sizes:
                local.unlink(missing_ok=True)
                skipped += 1
                log(f"[{index}/{len(songs)}] 跳过：同歌曲已有相同体积文件（{actual} bytes）")
                continue
            known_sizes.add(actual)
            stage = "metadata"
            if extension == "flac":
                embed_metadata(local, found)
            else:
                log(f"[{index}/{len(songs)}] 非 FLAC 音频，跳过 FLAC 元数据封装")
            actual = local.stat().st_size
            stage = "alist_listing"
            filename = choose_filename(auth, base_filename, actual, subfolder=target_folder)
            if filename is None:
                local.unlink(missing_ok=True)
                skipped += 1
                log(f"[{index}/{len(songs)}] 跳过：AList 已存在相同文件")
                continue
            stage = "upload"
            upload(auth, local, filename, subfolder=target_folder)
            local.unlink(missing_ok=True)
            success += 1
            log(f"[{index}/{len(songs)}] 上传完成：{filename}")
        except Exception as exc:
            local.unlink(missing_ok=True)
            failed.append(label)
            failed_details.append({
                "title": original.get("title", ""),
                "artist": original.get("artist", ""),
                "stage": stage,
                "source": found.get("source", "unknown"),
                "error": str(exc),
            })
            log(f"[{index}/{len(songs)}] 失败：阶段={stage}，音源={found.get('source', 'unknown')}，原因={exc}")
    log(f"任务完成：上传 {success} 首，跳过 {skipped} 首，失败 {len(failed)} 首")
    payload["success_count"] = success
    payload["skipped_count"] = skipped
    payload["failed_songs"] = failed
    payload["failed_details"] = failed_details
    payload["status"] = "completed"
    payload["cancelled"] = False
    callback(payload)
    print(json.dumps({"status": "completed", "success_count": success, "skipped_count": skipped, "failed_songs": failed, "failed_details": failed_details}, ensure_ascii=False))


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
            ACTIVE_PAYLOAD.setdefault("failed_details", [{"title": "", "artist": "", "stage": "task", "source": "", "error": message}])
            try:
                callback(ACTIVE_PAYLOAD)
                log("异常状态已回调 n8n")
            except Exception as callback_exc:
                print(f"ERROR: 异常回调失败：{callback_exc}", file=sys.stderr, flush=True)
        sys.exit(1)
