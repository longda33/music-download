# -*- coding: utf-8 -*-

import sys
import re
import time
import random
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests
from tqdm import tqdm


# ============================================================
# 配置
# ============================================================

TANG_API = "https://tang.api.s01s.cn/music_open_api.php"

METING_API = "https://api.qijieya.cn/meting/"

KUWO_API = "https://oiapi.net/api/Kuwo"

# 每个搜索源一次最多搜索多少首
TANG_LIMIT = 20
NETEASE_LIMIT = 30
KUWO_LIMIT = 30

# API 请求之间随机等待
MIN_API_DELAY = 2.0
MAX_API_DELAY = 4.0

# 下载请求之间随机等待
MIN_DOWNLOAD_DELAY = 0.5
MAX_DOWNLOAD_DELAY = 1.5

# 最大重试
MAX_RETRY = 3

# HTTP 超时
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60

# 下载根目录
DOWNLOAD_ROOT = Path("Music")

# 日志
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "music_download.log"

# 临时文件
PART_SUFFIX = ".part"


# ============================================================
# 日志
# ============================================================

LOG_DIR.mkdir(
    parents=True,
    exist_ok=True
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_FILE,
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("music_download")


# ============================================================
# HTTP Session
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/151.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
})


# ============================================================
# 工具
# ============================================================

def api_delay():
    """
    API 请求限速。
    """
    delay = random.uniform(
        MIN_API_DELAY,
        MAX_API_DELAY
    )

    logger.info(
        "API 限速等待 %.1f 秒",
        delay
    )

    time.sleep(delay)


def download_delay():
    """
    下载请求之间稍微等待。
    """
    time.sleep(
        random.uniform(
            MIN_DOWNLOAD_DELAY,
            MAX_DOWNLOAD_DELAY
        )
    )


def safe_filename(name):
    """
    Windows 文件名清理。
    """

    if not name:
        return "未知歌曲"

    name = str(name).strip()

    # Windows 非法字符
    name = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        name
    )

    # 控制字符
    name = re.sub(
        r'[\x00-\x1f]',
        "",
        name
    )

    # Windows 文件名不能以空格/点结束
    name = name.rstrip(" .")

    if not name:
        return "未知歌曲"

    # Windows 保留设备名
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4",
        "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4",
        "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
    }

    if name.upper() in reserved:
        name = "_" + name

    return name


def normalize_text(text):
    """
    用于跨平台去重。
    """

    if not text:
        return ""

    text = str(text).lower().strip()

    # 去掉括号中的部分信息
    text = re.sub(
        r'[\(\[\{].*?[\)\]\}]',
        "",
        text
    )

    # 去掉常见版本标记
    text = re.sub(
        r'\b(live|remix|version|edit|radio edit)\b',
        "",
        text,
        flags=re.I
    )

    # 删除空白
    text = re.sub(
        r'\s+',
        "",
        text
    )

    return text


def song_key(title, artist):
    """
    跨平台去重键。
    """

    return (
        normalize_text(title),
        normalize_text(artist)
    )


# ============================================================
# HTTP JSON
# ============================================================

def get_json(
    url,
    params=None,
    description=""
):

    last_error = None

    for attempt in range(
        1,
        MAX_RETRY + 1
    ):

        api_delay()

        try:

            logger.info(
                "请求 API：%s",
                description
            )

            response = session.get(
                url,
                params=params,
                timeout=(
                    CONNECT_TIMEOUT,
                    READ_TIMEOUT
                )
            )

            status = response.status_code

            logger.info(
                "HTTP %s | %.2fs | %d bytes",
                status,
                response.elapsed.total_seconds(),
                len(response.content)
            )

            # ------------------------------------------------
            # 429
            # ------------------------------------------------

            if status == 429:

                retry_after = (
                    response.headers.get(
                        "Retry-After"
                    )
                )

                try:
                    wait = float(
                        retry_after
                    )
                except:
                    wait = 30 * attempt

                logger.warning(
                    "HTTP 429，等待 %.0f 秒",
                    wait
                )

                time.sleep(wait)

                continue

            # ------------------------------------------------
            # 403
            # ------------------------------------------------

            if status == 403:

                raise RuntimeError(
                    "HTTP 403：服务器拒绝请求"
                )

            # ------------------------------------------------
            # 5xx
            # ------------------------------------------------

            if status >= 500:

                raise RuntimeError(
                    f"HTTP {status}"
                )

            response.raise_for_status()

            return response.json()

        except Exception as e:

            last_error = e

            logger.error(
                "%s 失败 [%d/%d]：%s",
                description,
                attempt,
                MAX_RETRY,
                e
            )

            if attempt < MAX_RETRY:

                wait = min(
                    60,
                    5 * (2 ** (attempt - 1))
                )

                logger.info(
                    "%.0f 秒后重试",
                    wait
                )

                time.sleep(wait)

    raise RuntimeError(
        f"{description} 最终失败：{last_error}"
    )


# ============================================================
# 1. Tang / QQ
# ============================================================

def search_tang(singer):

    data = get_json(
        TANG_API,
        params={
            "msg": singer,
            "type": "json"
        },
        description="Tang / QQ 搜索"
    )

    if not isinstance(data, list):

        raise RuntimeError(
            "Tang 返回格式不是数组"
        )

    songs = []

    seen = set()

    for item in data:

        if not isinstance(item, dict):
            continue

        mid = item.get("song_mid")

        title = (
            item.get("song_title")
            or item.get("song_name")
        )

        artist = item.get(
            "singer_name"
        ) or singer

        if not mid or not title:
            continue

        if mid in seen:
            continue

        seen.add(mid)

        songs.append({
            "source": "Tang",
            "source_id": mid,
            "title": title,
            "artist": artist,
            "album": "",
            "flac_url": None,
            "flac_size": 0,
            "quality": None,
        })

    logger.info(
        "[Tang] 搜索结果：%d",
        len(songs)
    )

    return songs


# ============================================================
# Tang 单曲详情
# ============================================================

def tang_detail(song):

    mid = song["source_id"]

    data = get_json(
        TANG_API,
        params={
            "mid": mid
        },
        description=f"Tang 单曲 {mid}"
    )

    title = (
        data.get("song_name")
        or data.get("song_title")
        or song["title"]
    )

    artist = (
        data.get("singer_name")
        or song["artist"]
    )

    album = (
        data.get("album_name")
        or ""
    )

    # SQ
    sq_url = data.get(
        "song_play_url_sq"
    )

    sq_filename = data.get(
        "song_filename_sq"
    )

    sq_size = (
        data.get("song_size_sq_str")
        or 0
    )

    sq_kbps = (
        data.get("kbps_sq")
        or 0
    )

    if (
        sq_url
        and sq_filename
        and str(sq_filename).lower().endswith(".flac")
    ):

        return {
            **song,
            "title": title,
            "artist": artist,
            "album": album,
            "flac_url": sq_url,
            "flac_size": int(sq_size or 0),
            "quality": f"SQ {sq_kbps}kbps",
        }

    # PQ
    pq_url = data.get(
        "song_play_url_pq"
    )

    pq_filename = data.get(
        "song_filename_pq"
    )

    pq_size = (
        data.get("song_size_pq_str")
        or 0
    )

    pq_kbps = (
        data.get("kbps_pq")
        or 0
    )

    if (
        pq_url
        and pq_filename
        and str(pq_filename).lower().endswith(".flac")
    ):

        return {
            **song,
            "title": title,
            "artist": artist,
            "album": album,
            "flac_url": pq_url,
            "flac_size": int(pq_size or 0),
            "quality": f"PQ {pq_kbps}kbps",
        }

    return {
        **song,
        "title": title,
        "artist": artist,
        "album": album,
        "flac_url": None,
        "flac_size": 0,
        "quality": None,
    }


# ============================================================
# 2. 网易云 / Meting
# ============================================================

def search_netease(singer):

    data = get_json(
        METING_API,
        params={
            "type": "search",
            "id": singer,
            "limit": NETEASE_LIMIT,
            "page": 1,
            "server": "netease",
        },
        description="Meting / 网易云搜索 page=1"
    )

    if not isinstance(data, list):

        raise RuntimeError(
            "Meting 返回格式不是数组"
        )

    songs = []

    seen = set()

    for item in data:

        if not isinstance(item, dict):
            continue

        title = item.get(
            "name"
        )

        artist = item.get(
            "artist"
        ) or singer

        url = item.get(
            "url"
        )

        if not title:
            continue

        # Meting 搜索结果的 url 一般指向
        # type=url&id=歌曲ID
        source_id = ""

        if url:

            match = re.search(
                r'[?&]id=([^&]+)',
                url
            )

            if match:
                source_id = match.group(1)

        key = (
            source_id
            or str(song_key(title, artist))
        )

        if key in seen:
            continue

        seen.add(key)

        songs.append({
            "source": "Netease",
            "source_id": source_id,
            "title": title,
            "artist": artist,
            "album": "",
            "flac_url": url,
            "flac_size": 0,
            "quality": "Meting",
        })

    logger.info(
        "[网易云] 搜索结果：%d",
        len(songs)
    )

    return songs


# ============================================================
# 网易云获取真实 URL
# ============================================================

def meting_get_url(song):

    url = song.get(
        "flac_url"
    )

    if not url:
        return None

    # 搜索接口给的是 Meting URL，
    # 不是实际音频 URL。
    #
    # 只有在搜索结果存在 URL 时，
    # 才继续查询一次真实播放地址。

    try:

        api_url = url

        api_delay()

        response = session.get(
            api_url,
            timeout=(
                CONNECT_TIMEOUT,
                READ_TIMEOUT
            ),
            allow_redirects=True
        )

        response.raise_for_status()

        real_url = response.text.strip()

        if (
            real_url
            and real_url.startswith(
                ("http://", "https://")
            )
        ):

            return real_url

    except Exception as e:

        logger.error(
            "网易云获取下载地址失败：%s - %s",
            song["title"],
            e
        )

    return None


# ============================================================
# 3. 酷我 / OIAPI
# ============================================================

def search_kuwo(singer):

    data = get_json(
        KUWO_API,
        params={
            "msg": singer,
            "page": 1,
            "limit": KUWO_LIMIT,
        },
        description="OIAPI / 酷我搜索 page=1"
    )

    if not isinstance(data, dict):

        raise RuntimeError(
            "酷我返回格式不是对象"
        )

    rows = data.get(
        "data",
        []
    )

    if not isinstance(rows, list):
        rows = []

    songs = []

    seen = set()

    for item in rows:

        if not isinstance(item, dict):
            continue

        title = item.get(
            "song"
        )

        artist = item.get(
            "singer"
        ) or singer

        album = item.get(
            "album"
        ) or ""

        rid = item.get(
            "rid"
        )

        if not title or not rid:
            continue

        if rid in seen:
            continue

        seen.add(rid)

        # 检查 API 是否声明有 FLAC
        flac_type = None

        types = item.get(
            "types",
            []
        )

        if isinstance(types, list):

            for t in types:

                if not isinstance(t, dict):
                    continue

                fmt = str(
                    t.get("format", "")
                ).lower()

                if fmt == "flac":

                    flac_type = t

                    break

        songs.append({
            "source": "Kuwo",
            "source_id": rid,
            "title": title,
            "artist": artist,
            "album": album,
            "flac_url": None,
            "flac_size": 0,
            "quality": (
                f"FLAC {flac_type.get('bitrate')}kbps"
                if flac_type
                else None
            ),
            "kuwo_raw": item,
        })

    logger.info(
        "[酷我] 搜索结果：%d",
        len(songs)
    )

    return songs


# ============================================================
# 酷我获取 FLAC
# ============================================================

def kuwo_get_url(song):
    """按酷我 HAR 使用 msg+n+br=1 获取真实 FLAC 地址。"""
    title = song.get("title", "")
    artist = song.get("artist", "")
    query = f"{title} {artist}"
    try:
        data = get_json(
            KUWO_API,
            params={"msg": query, "page": 1, "limit": KUWO_LIMIT},
            description=f"酷我搜索 {query}"
        )
        rows = data.get("data", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            return None
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            if normalize_text(row.get("song", "")) != normalize_text(title):
                continue
            if normalize_text(row.get("singer", "")) != normalize_text(artist):
                continue
            types = row.get("types", [])
            if not isinstance(types, list) or not any(str(t.get("format", "")).lower() == "flac" for t in types if isinstance(t, dict)):
                continue
            detail = get_json(
                KUWO_API,
                params={"msg": query, "n": index, "br": 1},
                description=f"酷我 FLAC 地址 {title}"
            )
            item = detail.get("data", {}) if isinstance(detail, dict) else {}
            url = item.get("url") if isinstance(item, dict) else None
            if url and str(item.get("format", "")).lower() == "flac" and str(url).lower().split("?")[0].endswith(".flac"):
                return url
    except Exception as e:
        logger.warning("酷我获取 FLAC 地址失败 %s：%s", title, e)
    return None


def find_flac_url(obj):

    """
    在 API JSON 中递归寻找：
    URL + FLAC
    """

    if isinstance(obj, dict):

        for key, value in obj.items():

            if isinstance(value, str):

                low_key = key.lower()
                low_value = value.lower()

                if (
                    "url" in low_key
                    and ".flac" in low_value
                ):

                    return value

            result = find_flac_url(
                value
            )

            if result:
                return result

    elif isinstance(obj, list):

        for item in obj:

            result = find_flac_url(
                item
            )

            if result:
                return result

    return None


# ============================================================
# 合并搜索结果
# ============================================================

def merge_songs(
    tang,
    netease,
    kuwo
):

    all_songs = []

    # 下载优先级：QQ → 网易云 → 酷我
    all_songs.extend(tang)
    all_songs.extend(netease)
    all_songs.extend(kuwo)

    result = []

    seen_platform = set()
    seen_cross = set()

    for song in all_songs:

        source = song["source"]
        source_id = song.get(
            "source_id"
        )

        # ------------------------------------------------
        # 平台 ID 去重
        # ------------------------------------------------

        platform_key = (
            source,
            source_id
        )

        if (
            source_id
            and platform_key in seen_platform
        ):
            continue

        if source_id:
            seen_platform.add(
                platform_key
            )

        # ------------------------------------------------
        # 跨平台标题 + 歌手去重
        # ------------------------------------------------

        cross_key = song_key(
            song["title"],
            song["artist"]
        )

        if cross_key in seen_cross:

            # 如果已有同名歌曲，
            # 优先保留已经存在 FLAC URL 的版本。
            continue

        seen_cross.add(
            cross_key
        )

        result.append(song)

    return result


# ============================================================
# 下载
# ============================================================

def download_file(
    url,
    output,
    expected_size=0
):

    output = Path(output)

    temp = Path(
        str(output) + PART_SUFFIX
    )

    # 已完成
    if output.exists():

        size = output.stat().st_size

        if (
            not expected_size
            or size == expected_size
        ):

            logger.info(
                "已存在，跳过：%s",
                output.name
            )

            return "SKIP"

    for attempt in range(
        1,
        MAX_RETRY + 1
    ):

        try:

            download_delay()

            headers = {}

            existing = (
                temp.stat().st_size
                if temp.exists()
                else 0
            )

            if existing:

                headers["Range"] = (
                    f"bytes={existing}-"
                )

            logger.info(
                "下载 [%d/%d] %s",
                attempt,
                MAX_RETRY,
                output.name
            )

            response = session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(
                    CONNECT_TIMEOUT,
                    READ_TIMEOUT
                )
            )

            # 429
            if response.status_code == 429:

                retry_after = (
                    response.headers.get(
                        "Retry-After"
                    )
                )

                try:
                    wait = float(
                        retry_after
                    )
                except:
                    wait = 30 * attempt

                logger.warning(
                    "下载 HTTP 429，等待 %.0f 秒",
                    wait
                )

                time.sleep(wait)

                continue

            response.raise_for_status()

            # 如果服务器不支持 Range
            if existing and response.status_code == 200:

                logger.info(
                    "服务器不支持断点续传，重新下载"
                )

                existing = 0

                try:
                    temp.unlink()
                except:
                    pass

            # 总大小
            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length:

                total = (
                    int(content_length)
                    + existing
                    if response.status_code == 206
                    else int(content_length)
                )

            else:

                total = (
                    expected_size
                    or None
                )

            mode = (
                "ab"
                if existing
                and response.status_code == 206
                else "wb"
            )

            downloaded = existing

            with open(
                temp,
                mode
            ) as f:

                with tqdm(
                    total=total,
                    initial=existing,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=output.stem[:35],
                    ncols=100
                ) as bar:

                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):

                        if not chunk:
                            continue

                        f.write(chunk)

                        downloaded += len(chunk)

                        bar.update(
                            len(chunk)
                        )

            response.close()

            # 大小校验
            final_size = temp.stat().st_size

            if expected_size:

                if final_size != int(
                    expected_size
                ):

                    raise RuntimeError(
                        f"大小不一致 "
                        f"{final_size}/{expected_size}"
                    )

            # 最低限度 FLAC 检查
            with open(
                temp,
                "rb"
            ) as f:

                header = f.read(4)

            if header != b"fLaC":

                raise RuntimeError(
                    "下载文件不是有效 FLAC "
                    "(文件头不是 fLaC)"
                )

            if output.exists():

                output.unlink()

            temp.replace(
                output
            )

            logger.info(
                "下载成功：%s",
                output
            )

            return "OK"

        except Exception as e:

            logger.error(
                "下载失败 [%d/%d] %s：%s",
                attempt,
                MAX_RETRY,
                output.name,
                e
            )

            if attempt < MAX_RETRY:

                wait = min(
                    60,
                    5 * (2 ** (attempt - 1))
                )

                time.sleep(wait)

    return "FAILED"


# ============================================================
# 主程序
# ============================================================

def main():

    if len(sys.argv) < 2:

        print()
        print(
            '用法：python music_all.py "歌手名"'
        )
        print()
        return 1

    singer = " ".join(
        sys.argv[1:]
    ).strip()

    if not singer:
        return 1

    singer_dir = (
        DOWNLOAD_ROOT /
        safe_filename(singer)
    )

    singer_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    print()
    print("=" * 70)
    print("三源 FLAC 音乐下载器")
    print("=" * 70)
    print()
    print("歌手：", singer)
    print("下载目录：", singer_dir.resolve())
    print()
    print("搜索：")
    print(f"  Tang     : {TANG_LIMIT}")
    print(f"  网易云   : {NETEASE_LIMIT}")
    print(f"  酷我     : {KUWO_LIMIT}")
    print()
    print("注意：只搜索一次，不翻页。")
    print()

    # ========================================================
    # 搜索
    # ========================================================

    tang = []
    netease = []
    kuwo = []

    try:
        tang = search_tang(singer)
    except Exception as e:
        logger.exception(
            "Tang 搜索失败：%s",
            e
        )

    try:
        netease = search_netease(singer)
    except Exception as e:
        logger.exception(
            "网易云搜索失败：%s",
            e
        )

    try:
        kuwo = search_kuwo(singer)
    except Exception as e:
        logger.exception(
            "酷我搜索失败：%s",
            e
        )

    # ========================================================
    # 合并
    # ========================================================

    songs = merge_songs(
        tang,
        netease,
        kuwo
    )

    print()
    print("=" * 70)
    print("搜索结果")
    print("=" * 70)

    print(
        f"Tang     : {len(tang)}"
    )

    print(
        f"网易云   : {len(netease)}"
    )

    print(
        f"酷我     : {len(kuwo)}"
    )

    print(
        f"合并去重 : {len(songs)}"
    )

    print()

    if not songs:

        logger.error(
            "没有任何搜索结果"
        )

        return 1

    # ========================================================
    # 获取下载地址 + 下载
    # ========================================================

    success = 0
    skipped = 0
    no_flac = 0
    failed = 0

    failed_list = []

    for index, song in enumerate(
        songs,
        start=1
    ):

        title = song["title"]
        artist = song["artist"]

        logger.info(
            ""
        )

        logger.info(
            "[%d/%d] %s - %s [%s]",
            index,
            len(songs),
            title,
            artist,
            song["source"]
        )

        # ----------------------------------------------------
        # Tang
        # ----------------------------------------------------

        if song["source"] == "Tang":

            try:

                song = tang_detail(
                    song
                )

            except Exception as e:

                logger.exception(
                    "Tang 详情失败：%s",
                    e
                )

                failed += 1

                failed_list.append(
                    song
                )

                continue

        # ----------------------------------------------------
        # 网易云
        # ----------------------------------------------------

        elif song["source"] == "Netease":

            url = meting_get_url(
                song
            )

            song["flac_url"] = url

            # Meting 返回的真实地址可能
            # 不一定是 FLAC。
            #
            # 只允许明确以 .flac 结尾的地址。
            if (
                not url
                or ".flac" not in url.lower()
            ):

                logger.warning(
                    "网易云没有明确 FLAC 地址：%s",
                    title
                )

                no_flac += 1

                continue

        # ----------------------------------------------------
        # 酷我
        # ----------------------------------------------------

        elif song["source"] == "Kuwo":

            # 只有 API 搜索结果明确存在 FLAC
            # 才继续
            if not song.get("quality"):

                logger.info(
                    "酷我搜索结果没有 FLAC：%s",
                    title
                )

                no_flac += 1

                continue

            try:

                url = kuwo_get_url(
                    song
                )

                song["flac_url"] = url

            except Exception as e:

                logger.exception(
                    "酷我获取 FLAC 地址失败：%s",
                    e
                )

                url = None

            if not url:

                logger.warning(
                    "酷我没有取得 FLAC 下载地址：%s",
                    title
                )

                no_flac += 1

                continue

        # ----------------------------------------------------
        # 检查 URL
        # ----------------------------------------------------

        url = song.get(
            "flac_url"
        )

        if not url:

            logger.warning(
                "没有 FLAC URL：%s",
                title
            )

            no_flac += 1

            continue

        # ----------------------------------------------------
        # 文件名
        # ----------------------------------------------------

        filename = (
            safe_filename(title)
            + ".flac"
        )

        output = (
            singer_dir /
            filename
        )

        # 同名文件处理
        if output.exists():

            if (
                song.get("flac_size")
                and
                output.stat().st_size
                == song["flac_size"]
            ):

                skipped += 1

                logger.info(
                    "已存在：%s",
                    output.name
                )

                continue

        # ----------------------------------------------------
        # 下载
        # ----------------------------------------------------

        result = download_file(
            url=url,
            output=output,
            expected_size=song.get(
                "flac_size",
                0
            )
        )

        if result == "OK":

            success += 1

        elif result == "SKIP":

            skipped += 1

        else:

            failed += 1

            failed_list.append(
                song
            )

    # ========================================================
    # 保存失败列表
    # ========================================================

    if failed_list:

        failed_file = (
            singer_dir /
            "failed.txt"
        )

        with open(
            failed_file,
            "w",
            encoding="utf-8"
        ) as f:

            for song in failed_list:

                f.write(
                    f'{song["title"]} | '
                    f'{song["artist"]} | '
                    f'{song["source"]} | '
                    f'{song["source_id"]}\n'
                )

        logger.warning(
            "失败列表：%s",
            failed_file.resolve()
        )

    # ========================================================
    # 统计
    # ========================================================

    print()
    print("=" * 70)
    print("下载完成")
    print("=" * 70)
    print(
        f"搜索结果      : {len(songs)}"
    )
    print(
        f"下载成功      : {success}"
    )
    print(
        f"已存在跳过    : {skipped}"
    )
    print(
        f"没有 FLAC     : {no_flac}"
    )
    print(
        f"失败          : {failed}"
    )
    print()
    print(
        "音乐目录：",
        singer_dir.resolve()
    )
    print(
        "日志：",
        LOG_FILE.resolve()
    )
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())