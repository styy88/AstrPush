import asyncio
import base64
import secrets
from io import BytesIO
from multiprocessing import Process, Queue
from typing import Any, Dict, Optional

import aiohttp
from PIL import Image as ImageP

import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain


@register(
    plugin_id="AstrPush",
    author="Raven95676",
    name="AstrPush",
    version="0.2.0"
)
class AstrPush(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config: AstrBotConfig = config
        self.in_queue: Optional[Queue] = None  # 消息队列（子进程→主进程）
        self.process: Optional[Process] = None  # API服务子进程
        self._running: bool = False  # 运行状态标记

    async def initialize(self) -> None:
        """初始化AstrPush插件（生成Token、启动API服务子进程）"""
        # 生成默认Token（若未配置）
        if not self.config["api"].get("token"):
            self.config["api"]["token"] = secrets.token_urlsafe(32)
            self.config.save_config()
            logger.info(f"AstrPush自动生成API Token: {self.config['api']['token']}")

        # 验证必填配置
        required_configs = ["token", "default_umo"]
        missing = [k for k in required_configs if not self.config["api"].get(k)]
        if missing:
            logger.error(f"AstrPush插件配置不完整，缺少: {missing}，请检查配置文件后重启")
            return

        # 初始化消息队列和子进程
        self.in_queue = Queue(maxsize=100)  # 队列最大100条消息
        self.process = Process(
            target=run_server,  # 子进程入口（来自api.py）
            args=(
                self.config["api"]["token"],
                self.config["api"].get("host", "0.0.0.0"),
                self.config["api"].get("port", 9966),
                self.in_queue,
                self.config["api"]["default_umo"],  # 传递默认umo
            ),
            daemon=True  # 主进程退出时自动终止子进程
        )
        self.process.start()
        self._running = True
        logger.info(f"AstrPush插件初始化完成，API服务PID: {self.process.pid}")

        # 启动消息处理协程
        asyncio.create_task(self._process_messages())

    async def _process_messages(self) -> None:
        """从队列接收消息并发送到微信"""
        while self._running and self.in_queue:
            try:
                # 从队列获取消息（非阻塞，超时1秒）
                message: Dict[str, Any] = await asyncio.get_event_loop().run_in_executor(
                    None, self.in_queue.get, True, 1
                )
            except Exception:
                continue  # 队列为空时继续循环

            # 处理消息
            message_id = message.get("message_id", "unknown")
            try:
                logger.info(f"AstrPush处理消息 [ID: {message_id}]")
                result: Dict[str, Any] = {"message_id": message_id, "success": True}

                # 根据消息类型构造消息链
                if message["type"] == "image":
                    # 图片消息（base64编码）
                    try:
                        image_data = base64.b64decode(message["content"])
                        ImageP.open(BytesIO(image_data)).verify()  # 验证图片格式
                        chain = MessageChain(chain=[Comp.Image.fromBytes(image_data)])
                    except Exception as e:
                        raise ValueError(f"图片处理失败: {str(e)}")
                else:
                    # 文本消息（默认）
                    chain = MessageChain(chain=[Comp.Plain(message["content"])])

                # 发送消息到微信
                await self.context.send_message(message["umo"], chain)
                logger.info(f"AstrPush消息发送成功 [ID: {message_id}]")

            except Exception as e:
                error_msg = f"消息处理失败: {str(e)}"
                logger.error(f"AstrPush [ID: {message_id}] {error_msg}")
                result.update({"success": False, "error": error_msg})
            finally:
                # 发送回调（如果配置）
                if callback_url := message.get("callback_url"):
                    asyncio.create_task(self._send_callback(callback_url, result))

    async def _send_callback(self, url: str, data: Dict[str, Any]) -> None:
        """发送消息处理结果回调"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(url, json=data) as resp:
                    if resp.status >= 400:
                        logger.warning(f"AstrPush回调请求失败 [URL: {url}]，状态码: {resp.status}")
        except Exception as e:
            logger.error(f"AstrPush回调请求异常 [URL: {url}]: {str(e)}")

    async def terminate(self) -> None:
        """停止AstrPush插件（清理子进程和队列）"""
        self._running = False
        logger.info("AstrPush插件开始停止...")

        # 终止子进程
        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
            logger.info(f"AstrPush API服务子进程已终止 (PID: {self.process.pid})")

        # 清空队列
        if self.in_queue:
            while not self.in_queue.empty():
                self.in_queue.get()
            logger.info("AstrPush消息队列已清空")

        logger.info("AstrPush插件已停止")


# 导入子进程入口函数（避免循环导入）
from .api import run_server  # noqa: E402
