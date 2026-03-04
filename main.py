# main.py

import os
import asyncio
import logging
from typing import Dict

import aiohttp
from fastapi import FastAPI, HTTPException
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

    BILI_UID: int
    PAGE_SIZE: int = 30
    SESSDATA: str
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
    UPDATE_INTERVAL_MINUTES: int = 30

settings = Settings()

app = FastAPI(
    title="Bili 粉丝牌缓存服务",
    description="定时拉取 B 站粉丝牌并暴露缓存接口",
    version="1.0.0"
)

# 全局内存缓存
fans_cache: Dict[int, int] = {}

# 请求头
HEADERS = {
    "User-Agent": settings.USER_AGENT,
    "Referer": "https://live.bilibili.com"
}

async def _refresh_fans_cache():
    """
    后台任务：定时拉取 B 站粉丝牌并更新内存缓存。
    每次迭代内新建 TCPConnector，ssl=False 跳过证书校验。
    """
    url_tpl = (
        "https://api.live.bilibili.com/"
        "xlive/general-interface/v1/rank/getFansMembersRank"
        "?page={page}&ruid={ruid}&page_size={ps}"
    )

    while True:
        tmp: Dict[int, int] = {}
        try:
            # 每次循环都新建 connector
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                connector=connector,
                headers=HEADERS,
                cookies={"SESSDATA": settings.SESSDATA}
            ) as sess:
                page = 1
                while True:
                    url = url_tpl.format(
                        page=page,
                        ruid=settings.BILI_UID,
                        ps=settings.PAGE_SIZE
                    )
                    async with sess.get(url) as resp:
                        data = await resp.json()
                    if data.get("code") != 0:
                        logger.error(f"拉取粉丝牌失败，code={data.get('code')}")
                        break

                    items = data["data"].get("item", [])
                    if not items:
                        break

                    for it in items:
                        tmp[it["uid"]] = it["level"]

                    page += 1
                    await asyncio.sleep(3)

            fans_cache.clear()
            fans_cache.update(tmp)
            logger.info(f"粉丝牌缓存已更新，共 {len(tmp)} 条")

        except Exception as e:
            logger.error(f"粉丝牌缓存更新异常：{e}")

        # 下一次更新前休眠
        await asyncio.sleep(settings.UPDATE_INTERVAL_MINUTES * 60)

@app.on_event("startup")
async def startup_event():
    # 启动后台缓存更新任务
    asyncio.create_task(_refresh_fans_cache())

@app.get("/fans_cache")
async def get_fans_cache():
    """
    返回当前粉丝牌缓存：
    {
      "fans_cache": {
        "1048135385": 12,
        "123456789": 3,
        ...
      }
    }
    """
    if not fans_cache:
        raise HTTPException(status_code=503, detail="粉丝牌缓存尚未初始化，请稍后重试")
    return {"fans_cache": fans_cache}
