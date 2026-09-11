import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiohttp
import redis.asyncio as redis
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
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 1
    REDIS_PASSWORD: str = ""
    REDIS_KEY_PREFIX: str = "bilifanscache"


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
# room_id -> {粉丝uid: 舰长等级}; None 表示 Redis 舰长键尚未初始化
guard_cache_by_room: Dict[int, Optional[Dict[int, int]]] = {}
redis_client: Optional[redis.Redis] = None

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


def _room_cache_key(room_id: int) -> str:
    return f"{settings.REDIS_KEY_PREFIX}:room:{room_id}:fans"


def _room_guard_cache_key(room_id: int) -> str:
    return f"{settings.REDIS_KEY_PREFIX}:room:{room_id}:guard_levels"


async def _save_room_cache_to_redis(room_id: int, room_fans: Dict[int, int]):
    if redis_client is None:
        return
    payload = json.dumps(room_fans, ensure_ascii=False)
    await redis_client.set(_room_cache_key(room_id), payload)


async def _load_room_cache_from_redis(room_id: int) -> Optional[Dict[int, int]]:
    if redis_client is None:
        return None
    raw = await redis_client.get(_room_cache_key(room_id))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
        return {int(uid): int(level) for uid, level in parsed.items()}
    except Exception as e:
        logger.error("解析 Redis 房间缓存失败，room_id=%s err=%s", room_id, e)
        return None


async def _save_room_guard_cache_to_redis(
    room_id: int,
    room_guards: Dict[int, int],
):
    if redis_client is None:
        return
    payload = json.dumps(room_guards, ensure_ascii=False)
    await redis_client.set(_room_guard_cache_key(room_id), payload)


async def _load_room_guard_cache_from_redis(
    room_id: int,
) -> Optional[Dict[int, int]]:
    if redis_client is None:
        return None
    raw = await redis_client.get(_room_guard_cache_key(room_id))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
        return {int(uid): int(level) for uid, level in parsed.items()}
    except Exception as e:
        logger.error("解析 Redis 舰长缓存失败，room_id=%s err=%s", room_id, e)
        return None


async def _restore_cache_from_redis():
    restored_cnt = 0
    for room_id in rooms_meta:
        room_cache = await _load_room_cache_from_redis(room_id)
        if room_cache is not None:
            fans_cache_by_room[room_id] = room_cache
            restored_cnt += 1
        guard_cache = await _load_room_guard_cache_from_redis(room_id)
        guard_cache_by_room[room_id] = guard_cache
    logger.info("Redis 预热完成，已恢复 %s 个房间缓存", restored_cnt)


async def _fetch_room_data(
    sess: aiohttp.ClientSession,
    owner_uid: int,
) -> Tuple[Dict[int, int], Dict[int, int]]:
    url_tpl = (
        "https://api.live.bilibili.com/"
        "xlive/general-interface/v1/rank/getFansMembersRank"
        "?page={page}&ruid={ruid}&page_size={ps}"
    )
    page = 1
    room_fans: Dict[int, int] = {}
    room_guards: Dict[int, int] = {}

    while True:
        url = url_tpl.format(page=page, ruid=owner_uid, ps=settings.PAGE_SIZE)
        async with sess.get(url) as resp:
            data = await resp.json()

        await asyncio.sleep(settings.REQUEST_INTERVAL_SECONDS)

        if data.get("code") != 0:
            logger.error("拉取粉丝牌失败，ruid=%s code=%s", owner_uid, data.get("code"))
            break

        items = data.get("data", {}).get("item", [])
        if not items:
            break

        for it in items:
            fan_uid = int(it["uid"])
            room_fans[fan_uid] = int(it["level"])
            uinfo_medal = it.get("uinfo_medal")
            if not isinstance(uinfo_medal, dict):
                continue
            guard_level = uinfo_medal.get("guard_level", 0)
            if guard_level in (1, 2, 3):
                room_guards[fan_uid] = int(guard_level)

        page += 1

    return room_fans, room_guards


async def _refresh_fans_cache_forever(initial_only: bool = False) -> None:
    """
    后台任务：轮询 rooms.json 内所有主播 uid，持续刷新内存缓存。
    每次请求间隔 3 秒，不再按分钟间隔批量刷新。

    启动阶段传入 ``initial_only=True`` 时，只补齐 Redis 中没有的房间，
    补齐完成后切换到正常轮询；正常轮询持续处理全部房间。
    """
    warmup_only = initial_only
    if warmup_only:
        missing_count = sum(
            room_id not in fans_cache_by_room
            or guard_cache_by_room.get(room_id) is None
            for room_id in rooms_meta
        )
        logger.info("启动补齐开始，Redis 中缺少 %s 个房间缓存", missing_count)

    while True:
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                connector=connector,
                headers=HEADERS,
                cookies={"SESSDATA": settings.SESSDATA}
            ) as sess:
                for room_id, meta in rooms_meta.items():
                    if (
                        warmup_only
                        and room_id in fans_cache_by_room
                        and guard_cache_by_room.get(room_id) is not None
                    ):
                        continue
                    owner_uid = int(meta["uid"])
                    room_fans, room_guards = await _fetch_room_data(
                        sess=sess,
                        owner_uid=owner_uid,
                    )
                    fans_cache_by_room[room_id] = room_fans
                    guard_cache_by_room[room_id] = room_guards
                    await _save_room_cache_to_redis(room_id=room_id, room_fans=room_fans)
                    await _save_room_guard_cache_to_redis(
                        room_id=room_id,
                        room_guards=room_guards,
                    )
                    logger.info(
                        "粉丝牌缓存已更新，room_id=%s uid=%s 共 %s 条",
                        room_id,
                        owner_uid,
                        len(room_fans)
                    )

        except Exception as e:
            logger.error("粉丝牌缓存更新异常：%s", e)
            await asyncio.sleep(settings.REQUEST_INTERVAL_SECONDS)
            continue

        if warmup_only:
            logger.info("Redis 缺失房间补齐完成，开始正常轮询")
            warmup_only = False


async def _get_room_guard_cache(room_id: int) -> Optional[Dict[int, int]]:
    if room_id in guard_cache_by_room:
        return guard_cache_by_room[room_id]
    guard_cache = await _load_room_guard_cache_from_redis(room_id)
    guard_cache_by_room[room_id] = guard_cache
    return guard_cache


def _build_guard_levels(
    room_fans: Dict[int, int],
    room_guards: Optional[Dict[int, int]],
) -> Dict[int, int] | str | None:
    if room_guards is None:
        return None
    guard_levels = {
        fan_uid: guard_level
        for fan_uid, guard_level in room_guards.items()
        if fan_uid in room_fans and guard_level in (1, 2, 3)
    }
    return guard_levels or "none"


@app.on_event("startup")
async def startup_event():
    global rooms_meta, owner_uid_to_room_id, redis_client
    rooms_meta, owner_uid_to_room_id = _load_rooms_config()

    redis_password = settings.REDIS_PASSWORD or None
    redis_client = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=redis_password,
        decode_responses=True
    )
    await redis_client.ping()
    await _restore_cache_from_redis()
    asyncio.create_task(_refresh_fans_cache_forever(initial_only=True))


@app.on_event("shutdown")
async def shutdown_event():
    if redis_client is not None:
        await redis_client.aclose()


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
      "medal": {...},
      "guard_level": {...} | "none" | null
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
        room_cache = await _load_room_cache_from_redis(room_id)
        if room_cache is not None:
            fans_cache_by_room[room_id] = room_cache
        else:
            raise HTTPException(status_code=503, detail="该房间粉丝牌缓存尚未初始化，请稍后重试")

    guard_cache = await _get_room_guard_cache(room_id)
    owner_uid = int(rooms_meta[room_id]["uid"])
    return {
        "code": 0,
        "msg": "ok",
        "uid": owner_uid,
        "room_id": room_id,
        "medal": room_cache,
        "guard_level": _build_guard_levels(room_cache, guard_cache),
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
      "medal": {"粉丝牌名": 22},
      "guard_level": {"粉丝牌名": 2}
    }
    """
    medal_map: Dict[str, int] = {}
    guard_level_map: Dict[str, int] = {}
    has_uninitialized_guard_cache = False

    for room_id, room_cache in fans_cache_by_room.items():
        level = room_cache.get(uid)
        if level is None:
            continue
        medal_name = str(rooms_meta.get(room_id, {}).get("medal", room_id))
        medal_map[medal_name] = level
        guard_cache = await _get_room_guard_cache(room_id)
        if guard_cache is None:
            has_uninitialized_guard_cache = True
            continue
        guard_level = guard_cache.get(uid)
        if guard_level in (1, 2, 3):
            guard_level_map[medal_name] = guard_level

    guard_levels: Dict[str, int] | str | None
    if has_uninitialized_guard_cache:
        guard_levels = None
    else:
        guard_levels = guard_level_map or "none"

    return {
        "code": 0,
        "msg": "ok",
        "uid": uid,
        "medal": medal_map,
        "guard_level": guard_levels,
    }
