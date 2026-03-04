import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiohttp
from fastapi import FastAPI, HTTPException, Query
from pydantic_settings import BaseSettings, SettingsConfigDict

# ----------------- 日志配置 -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("harei_fans")

# ----------------- 配置 -----------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )

    PAGE_SIZE: int = 30
    SESSDATA: str
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
    ROOMS_JSON_PATH: str = "rooms.json"
    REQUEST_INTERVAL_SECONDS: int = 3


settings = Settings()

app = FastAPI(
    title="Bili 粉丝牌缓存服务",
    description="轮询拉取 B 站粉丝牌并暴露缓存接口",
    version="1.1.0"
)

# room_id -> {"uid": 主播uid, "name": 主播名, "medal": 粉丝牌名}
rooms_meta: Dict[int, Dict[str, object]] = {}
# 主播uid -> room_id
owner_uid_to_room_id: Dict[int, int] = {}
# room_id -> {粉丝uid: 粉丝牌等级}
fans_cache_by_room: Dict[int, Dict[int, int]] = {}

# 请求头
HEADERS = {
    "User-Agent": settings.USER_AGENT,
    "Referer": "https://live.bilibili.com"
}


def _load_rooms_config() -> Tuple[Dict[int, Dict[str, object]], Dict[int, int]]:
    room_file = Path(settings.ROOMS_JSON_PATH)
    if not room_file.exists():
        raise RuntimeError(f"rooms 配置文件不存在: {room_file}")

    raw = json.loads(room_file.read_text(encoding="utf-8"))
    room_meta_tmp: Dict[int, Dict[str, object]] = {}
    owner_to_room_tmp: Dict[int, int] = {}

    for room_id_str, room_info in raw.items():
        room_id = int(room_id_str)
        owner_uid = int(room_info["uid"])
        room_meta_tmp[room_id] = {
            "uid": owner_uid,
            "name": room_info.get("name", ""),
            "medal": room_info.get("medal", "")
        }
        owner_to_room_tmp[owner_uid] = room_id

    return room_meta_tmp, owner_to_room_tmp


async def _fetch_room_fans(sess: aiohttp.ClientSession, owner_uid: int) -> Dict[int, int]:
    url_tpl = (
        "https://api.live.bilibili.com/"
        "xlive/general-interface/v1/rank/getFansMembersRank"
        "?page={page}&ruid={ruid}&page_size={ps}"
    )
    page = 1
    room_fans: Dict[int, int] = {}

    while True:
        url = url_tpl.format(page=page, ruid=owner_uid, ps=settings.PAGE_SIZE)
        async with sess.get(url) as resp:
            data = await resp.json()

        if data.get("code") != 0:
            logger.error("拉取粉丝牌失败，ruid=%s code=%s", owner_uid, data.get("code"))
            break

        items = data.get("data", {}).get("item", [])
        if not items:
            break

        for it in items:
            room_fans[int(it["uid"])] = int(it["level"])

        page += 1

    return room_fans


async def _refresh_fans_cache_forever():
    """
    后台任务：轮询 rooms.json 内所有主播 uid，持续刷新内存缓存。
    每次请求间隔 3 秒，不再按分钟间隔批量刷新。
    """
    while True:
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                connector=connector,
                headers=HEADERS,
                cookies={"SESSDATA": settings.SESSDATA}
            ) as sess:
                for room_id, meta in rooms_meta.items():
                    owner_uid = int(meta["uid"])
                    room_fans = await _fetch_room_fans(sess=sess, owner_uid=owner_uid)
                    fans_cache_by_room[room_id] = room_fans
                    logger.info(
                        "粉丝牌缓存已更新，room_id=%s uid=%s 共 %s 条",
                        room_id,
                        owner_uid,
                        len(room_fans)
                    )
                    await asyncio.sleep(settings.REQUEST_INTERVAL_SECONDS)

        except Exception as e:
            logger.error("粉丝牌缓存更新异常：%s", e)
            await asyncio.sleep(settings.REQUEST_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event():
    global rooms_meta, owner_uid_to_room_id
    rooms_meta, owner_uid_to_room_id = _load_rooms_config()
    asyncio.create_task(_refresh_fans_cache_forever())


@app.get("/fans")
async def get_fans(
    room_id: Optional[int] = Query(default=None),
    uid: Optional[int] = Query(default=None)
):
    """
    查询单个房间缓存。
    仅允许 room_id 或 uid 其中一个参数存在。
    返回结构：
    {
      "code": 0,
      "msg": "ok",
      "uid": 主播uid,
      "room_id": 房间号,
      "medal": {...}
    }
    """
    if (room_id is None and uid is None) or (room_id is not None and uid is not None):
        raise HTTPException(status_code=400, detail="room_id 和 uid 必须且只能传一个")

    if uid is not None:
        room_id = owner_uid_to_room_id.get(uid)
        if room_id is None:
            raise HTTPException(status_code=404, detail="未找到该 uid 对应的房间")

    assert room_id is not None
    room_cache = fans_cache_by_room.get(room_id)
    if room_cache is None:
        raise HTTPException(status_code=503, detail="该房间粉丝牌缓存尚未初始化，请稍后重试")

    owner_uid = int(rooms_meta[room_id]["uid"])
    return {
        "code": 0,
        "msg": "ok",
        "uid": owner_uid,
        "room_id": room_id,
        "medal": room_cache
    }


@app.get("/search")
async def search_uid(uid: int = Query(...)):
    """
    查询指定 uid 在所有缓存中的粉丝牌情况。
    返回：
    {
      "code": 0,
      "msg": "ok",
      "uid": 123,
      "medal": {"粉丝牌名": 22}
    }
    """
    medal_map: Dict[str, int] = {}

    for room_id, room_cache in fans_cache_by_room.items():
        level = room_cache.get(uid)
        if level is None:
            continue
        medal_name = str(rooms_meta.get(room_id, {}).get("medal", room_id))
        medal_map[medal_name] = level

    return {
        "code": 0,
        "msg": "ok",
        "uid": uid,
        "medal": medal_map
    }
