import asyncio
import discord
import os
import json
import sys
import logging
import sqlite3
import datetime
from logging.handlers import TimedRotatingFileHandler
from contextlib import asynccontextmanager
from discord.ext import commands
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import uvicorn


# ================= 1. 日志配置 (Logger Setup) =================

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

# ================= 2. 数据库配置 (SQLite) =================
# 将数据库放在 data 目录，确保 Docker 挂载能持久化保存
DB_PATH = "/data/comic_threads.db"


def init_db():
    # 确保 data 目录存在
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # 创建表: comic_id (主键), thread_id
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
        "api_host": "0.0.0.0",
        "api_port": 8000,
        "proxy_url": ""
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            logger.warning(f"读取配置文件出错，将仅使用环境变量: {e}")

    env_token = os.getenv("DISCORD_TOKEN")
    if env_token: config["discord_token"] = env_token

    env_channel = os.getenv("TARGET_FORUM_CHANNEL_ID")
    if env_channel:
        try:
            config["target_forum_channel_id"] = int(env_channel)
        except ValueError:
            logger.error("环境变量 TARGET_FORUM_CHANNEL_ID 必须是数字")

    env_host = os.getenv("API_HOST")
    if env_host: config["api_host"] = env_host

    env_port = os.getenv("API_PORT")
    if env_port:
        try:
            config["api_port"] = int(env_port)
        except ValueError:
            pass

    env_proxy = os.getenv("PROXY_URL")
    if env_proxy: config["proxy_url"] = env_proxy

    if not config["discord_token"] or config["discord_token"] == "YOUR_TOKEN_HERE":
        logger.error("未配置 DISCORD_TOKEN。")
        sys.exit(1)

    if config["target_forum_channel_id"] == 0:
        logger.error("未配置 TARGET_FORUM_CHANNEL_ID。")
        sys.exit(1)

    return config


config = load_config()
DISCORD_TOKEN = config["discord_token"]
TARGET_FORUM_CHANNEL_ID = config["target_forum_channel_id"]
API_HOST = config.get("api_host", "0.0.0.0")
API_PORT = config.get("api_port", 8000)
PROXY_URL = config.get("proxy_url", "")


# ================= 4. API 数据模型 =================
class PublishRequest(BaseModel):
    title: str
    content: str
    comic_id: Optional[str] = None  # 新增字段: 漫画唯一ID
    cover: Optional[str] = None
    tags: List[str] = []
    attachment: List[str] = []


# ================= 5. 初始化服务 =================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    proxy=PROXY_URL if PROXY_URL else None
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在启动 API 服务与 Discord Bot...")

    # 初始化数据库
    init_db()

    if PROXY_URL:
        logger.info(f"使用代理: {PROXY_URL}")

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

    await bot.wait_until_ready()

    # --- 1. 检查是否存在已有帖子 (如果提供了 comic_id) ---
    if request.comic_id:
        existing_thread_id = None
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT thread_id FROM threads WHERE comic_id=?", (request.comic_id,))
            row = c.fetchone()
            conn.close()
            if row:
                existing_thread_id = row[0]
        except Exception as e:
            logger.error(f"查询数据库失败: {e}")

        # 如果数据库中有记录，尝试获取帖子并回复
        if existing_thread_id:
            try:
                # fetch_channel 可以获取指定 ID 的频道/帖子对象
                thread = await bot.fetch_channel(existing_thread_id)

                # 构造回复内容
                today_str = datetime.date.today().strftime("%Y-%m-%d")
                reply_content = f"{today_str} 又有人尝试下载本漫画。"

                await thread.send(reply_content)
                logger.info(f"漫画 {request.comic_id} 已存在 (Thread: {existing_thread_id})，已发送回复通知。")

                return {
                    "status": "replied",
                    "thread_id": thread.id,
                    "url": thread.jump_url,
                    "note": "Existing thread found, replied instead of creating new."
                }
            except discord.NotFound:
                logger.warning(f"记录中的帖子 {existing_thread_id} 已不存在(可能被删除)，将重新创建。")
                # 帖子找不到了，继续往下执行，创建新帖
            except Exception as e:
                logger.error(f"处理已有帖子时出错: {e}")
                # 出错也继续尝试创建新帖，或者报错取决于需求，这里选择安全失败后继续创建

    # --- 2. 准备创建新帖子 ---
    channel = bot.get_channel(TARGET_FORUM_CHANNEL_ID)
    if not channel:
        logger.error(f"找不到频道 {TARGET_FORUM_CHANNEL_ID}")
        raise HTTPException(status_code=500, detail="Bot无法找到指定频道")

    if not isinstance(channel, discord.ForumChannel):
        raise HTTPException(status_code=400, detail="目标不是论坛频道")

    # 处理标签
    applied_tags = []
    if request.tags:
        available_tags = channel.available_tags
        tag_map = {t.name.lower(): t for t in available_tags}
        for tag_name in request.tags:
            found_tag = tag_map.get(tag_name.lower())
            if found_tag:
                applied_tags.append(found_tag)

    # 处理文件
    discord_files = []
    opened_files_handles = []

    def add_file(path):
        if not path: return
        if not os.path.exists(path):
            logger.error(f"容器内找不到文件: {path}")
            raise HTTPException(status_code=400, detail=f"文件不存在: {path}")
        try:
            f = open(path, 'rb')
            opened_files_handles.append(f)
            discord_files.append(discord.File(fp=f, filename=os.path.basename(path)))
        except Exception as e:
            logger.error(f"读取失败 {path}: {e}")
            raise HTTPException(status_code=500, detail=f"文件读取失败: {e}")

    try:
        if request.cover: add_file(request.cover)
        for path in request.attachment: add_file(path)

        logger.info(f"正在创建新帖子...")
        thread_with_message = await channel.create_thread(
            name=request.title,
            content=request.content,
            files=discord_files,
            applied_tags=applied_tags
        )

        thread = thread_with_message.thread

        # --- 3. 新帖创建成功，保存映射关系到数据库 ---
        if request.comic_id:
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                # 使用 INSERT OR REPLACE 确保如果以前有残留记录被更新
                c.execute("INSERT OR REPLACE INTO threads (comic_id, thread_id) VALUES (?, ?)",
                          (request.comic_id, thread.id))
                conn.commit()
                conn.close()
                logger.info(f"数据库映射已保存: {request.comic_id} -> {thread.id}")
            except Exception as e:
                logger.error(f"保存数据库映射失败: {e}")

        return {"status": "success", "thread_id": thread.id, "url": thread.jump_url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"发布异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for f in opened_files_handles:
            if not f.closed: f.close()


if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)