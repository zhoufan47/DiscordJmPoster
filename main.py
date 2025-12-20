import asyncio
import discord
import os
import json
import sys
import logging
import sqlite3
import datetime
import aiohttp
from logging.handlers import TimedRotatingFileHandler
from contextlib import asynccontextmanager
from discord.ext import commands
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import uvicorn


# ================= 1. 日志配置 =================

def setup_logger():
    log_dir = "/data/logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logger = logging.getLogger("DiscordBridge")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        log_file = os.path.join(log_dir, "bot_service.log")
        file_handler = TimedRotatingFileHandler(
            log_file, when="midnight", interval=1, backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


logger = setup_logger()

# ================= 2. 数据库配置 =================
DB_PATH = "/data/comic_threads.db"


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS threads
                     (comic_id TEXT PRIMARY KEY, thread_id INTEGER)''')
        conn.commit()
        conn.close()
        logger.info(f"数据库初始化成功: {DB_PATH}")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")


# ================= 3. 配置加载逻辑 =================
CONFIG_FILE = "/data/config.json"


def load_config():
    config = {
        "discord_token": "",
        "target_forum_channel_id": 0,
        "proxy_url": ""
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            logger.warning(f"读取配置文件出错: {e}")

    # 环境变量覆盖
    env_token = os.getenv("DISCORD_TOKEN")
    if env_token: config["discord_token"] = env_token

    env_channel = os.getenv("TARGET_FORUM_CHANNEL_ID")
    if env_channel:
        try:
            config["target_forum_channel_id"] = int(env_channel)
        except ValueError:
            pass

    # PROXY_URL 逻辑优化: 如果没设置，尝试读取系统 HTTP_PROXY
    env_proxy = os.getenv("PROXY_URL")
    if env_proxy:
        config["proxy_url"] = env_proxy
    elif os.getenv("HTTP_PROXY"):
        config["proxy_url"] = os.getenv("HTTP_PROXY")

    if not config["discord_token"]:
        logger.error("未配置 DISCORD_TOKEN")
        sys.exit(1)

    return config


config = load_config()
DISCORD_TOKEN = config["discord_token"]
TARGET_FORUM_CHANNEL_ID = config["target_forum_channel_id"]
PROXY_URL = config.get("proxy_url", "")

# --- 关键修改：显式设置系统环境变量 ---
# 这能确保底层网络库 (aiohttp/requests) 强制走代理，解决参数传递可能不生效的问题
if PROXY_URL:
    os.environ["http_proxy"] = PROXY_URL
    os.environ["https_proxy"] = PROXY_URL
    logger.info(f"已设置系统代理环境变量: {PROXY_URL}")


# ================= 4. API 数据模型 =================
class PublishRequest(BaseModel):
    title: str
    content: str
    comic_id: Optional[str] = None
    cover: Optional[str] = None
    tags: List[str] = []
    attachment: List[str] = []


# ================= 5. 初始化服务 =================
intents = discord.Intents.default()
intents.message_content = True

# 初始化 Bot
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    proxy=PROXY_URL if PROXY_URL else None
)


async def check_proxy_connection(proxy_url):
    """在启动 Bot 前测试代理连接是否真正通畅"""
    target_url = "https://discord.com"
    logger.info(f"正在进行网络自检 (目标: {target_url})...")

    try:
        timeout = aiohttp.ClientTimeout(total=10)  # 10秒超时
        async with aiohttp.ClientSession() as session:
            async with session.get(target_url, proxy=proxy_url, timeout=timeout) as resp:
                logger.info(f"网络自检通过! 状态码: {resp.status}")
                return True
    except asyncio.TimeoutError:
        logger.error("网络自检失败: 连接超时 (Timeout)。请检查代理网速或防火墙。")
    except aiohttp.ClientProxyConnectionError:
        logger.error(f"网络自检失败: 无法连接到代理服务器 ({proxy_url})。请检查代理地址是否正确。")
    except aiohttp.ClientSSLError:
        logger.error("网络自检失败: SSL 证书验证错误。可能是代理服务器拦截了 HTTPS 证书。")
    except Exception as e:
        logger.error(f"网络自检失败: {type(e).__name__} - {e}")

    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化数据库
    init_db()

    # 打印代理信息
    if PROXY_URL:
        logger.info(f"配置代理: {PROXY_URL}")
        is_connected = await check_proxy_connection(PROXY_URL)
        if not is_connected:
            logger.warning("⚠️ 警告: 代理连接测试失败，Bot 可能会启动失败或卡住。")
    else:
        logger.info("未配置代理 (直连模式)")

    logger.info("正在启动 Discord Bot...")
    bot_task = asyncio.create_task(bot.start(DISCORD_TOKEN))

    yield

    logger.info("服务关闭中...")
    if not bot.is_closed():
        await bot.close()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"关闭 Bot 错误: {e}")


app = FastAPI(title="Discord Bot API Bridge", lifespan=lifespan)


@bot.event
async def on_ready():
    logger.info(f'Bot 登录成功: {bot.user} (ID: {bot.user.id})')
    logger.info(f'目标频道 ID: {TARGET_FORUM_CHANNEL_ID}')


# ================= 6. 核心逻辑 =================
@app.get("/")
async def root():
    return {"status": "running", "bot_ready": bot.is_ready()}


@app.post("/api/publish")
async def publish_post(request: PublishRequest):
    logger.info(f"收到请求 | Title: {request.title} | ComicID: {request.comic_id}")
    logger.info(f"Content: {request.content} | Tags: {request.tags}")
    await bot.wait_until_ready()

    # 1. 查重逻辑
    if request.comic_id:
        existing_thread_id = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT thread_id FROM threads WHERE comic_id=?", (request.comic_id,))
            row = c.fetchone()
            conn.close()
            if row: existing_thread_id = row[0]
        except Exception as e:
            logger.error(f"DB Error: {e}")

        if existing_thread_id:
            try:
                thread = await bot.fetch_channel(existing_thread_id)
                today_str = datetime.date.today().strftime("%Y-%m-%d")
                await thread.send(f"{today_str} 又有人尝试下载本漫画。")
                logger.info(f"已回复旧帖: {existing_thread_id}")
                return {"status": "replied", "thread_id": thread.id, "url": thread.jump_url}
            except discord.NotFound:
                logger.warning("旧帖不存在，重新创建")
            except Exception as e:
                logger.error(f"回复出错: {e}")

    # 2. 新建逻辑
    channel = bot.get_channel(TARGET_FORUM_CHANNEL_ID)
    if not channel: raise HTTPException(500, "Bot未连接或找不到频道")

    discord_files = []
    opened_files = []

    def add_file(path):
        if not path: return
        if not os.path.exists(path): raise HTTPException(400, f"文件不存在: {path}")
        f = open(path, 'rb')
        opened_files.append(f)
        discord_files.append(discord.File(f, filename=os.path.basename(path)))

    try:
        if request.cover: add_file(request.cover)
        for p in request.attachment: add_file(p)
        applied_tags = []
        if request.tags and isinstance(channel, discord.ForumChannel):
            tag_map = {t.name.lower(): t for t in channel.available_tags}
            for t in request.tags:
                if t in tag_map: applied_tags.append(tag_map[t])

        thread_with_msg = await channel.create_thread(
            name=request.title, content=request.content, files=discord_files, applied_tags=applied_tags
        )
        thread = thread_with_msg.thread

        if request.comic_id:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("INSERT OR REPLACE INTO threads VALUES (?, ?)", (request.comic_id, thread.id))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"保存DB失败: {e}")

        return {"status": "success", "thread_id": thread.id, "url": thread.jump_url}

    except Exception as e:
        logger.error(f"发布失败: {e}", exc_info=True)
        raise HTTPException(500, str(e))
    finally:
        for f in opened_files: f.close()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)