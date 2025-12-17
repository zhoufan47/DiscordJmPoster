import asyncio
import discord
import os
import json
import sys
import logging
from logging.handlers import TimedRotatingFileHandler
from contextlib import asynccontextmanager
from discord.ext import commands
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import uvicorn


# ================= 1. 日志配置 (Logger Setup) =================

def setup_logger():
    log_dir = "logs"
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

# ================= 2. 配置加载逻辑 (环境变量优先) =================
CONFIG_FILE = "config.json"


def load_config():
    # 默认配置结构
    config = {
        "discord_token": "",
        "target_forum_channel_id": 0,
        "api_host": "0.0.0.0",
        "api_port": 8000,
        "proxy_url": ""
    }

    # 1. 尝试从文件加载 (作为基础值)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            logger.warning(f"读取配置文件出错，将仅使用环境变量: {e}")

    # 2. 尝试从环境变量加载 (覆盖文件配置)
    # 环境变量名通常大写
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

    # 3. 最终校验
    if not config["discord_token"] or config["discord_token"] == "YOUR_TOKEN_HERE":
        logger.error("未配置 DISCORD_TOKEN。请在 config.json 或 环境变量 DISCORD_TOKEN 中设置。")
        # 即使这里退出，Docker 也会不断重启，这符合预期
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


# ================= 3. API 数据模型 =================
class PublishRequest(BaseModel):
    title: str
    content: str
    cover: Optional[str] = None
    tags: List[str] = []
    attachment: List[str] = []


# ================= 4. 初始化服务 =================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    proxy=PROXY_URL if PROXY_URL else None
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在启动 API 服务与 Discord Bot (Docker Ready)...")
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


# ================= 5. 核心逻辑 =================

@app.get("/")
async def root():
    return {"status": "running", "container": True}


@app.post("/api/publish")
async def publish_post(request: PublishRequest):
    logger.info(f"收到请求 | 标题: {request.title}")

    await bot.wait_until_ready()

    channel = bot.get_channel(TARGET_FORUM_CHANNEL_ID)
    if not channel:
        logger.error(f"找不到频道 {TARGET_FORUM_CHANNEL_ID}")
        raise HTTPException(status_code=500, detail="Bot无法找到指定频道")

    if not isinstance(channel, discord.ForumChannel):
        raise HTTPException(status_code=400, detail="目标不是论坛频道")

    applied_tags = []
    if request.tags:
        available_tags = channel.available_tags
        tag_map = {t.name.lower(): t for t in available_tags}
        for tag_name in request.tags:
            found_tag = tag_map.get(tag_name.lower())
            if found_tag:
                applied_tags.append(found_tag)

    discord_files = []
    opened_files_handles = []

    def add_file(path):
        if not path: return
        # Docker 环境下的路径检查
        if not os.path.exists(path):
            logger.error(f"容器内找不到文件: {path}")
            raise HTTPException(status_code=400, detail=f"文件不存在 (请检查路径是否已挂载到容器): {path}")
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

        logger.info(f"正在发布...")
        thread_with_message = await channel.create_thread(
            name=request.title,
            content=request.content,
            files=discord_files,
            applied_tags=applied_tags
        )

        thread = thread_with_message.thread
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