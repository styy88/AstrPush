import asyncio
import base64
import json
import os
import secrets
from io import BytesIO
from multiprocessing import Process, Queue
from typing import Any, Dict, Optional

import aiohttp
from PIL import Image as ImageP

import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain


@register("AstrPush", "styy88", "Astrbot微信推送插件", "1.0.0")
class AstrPush(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        # ==================== 关键：构造插件数据目录路径 ====================
        # 当前插件代码目录（main.py所在目录）：AstrBot/data/plugin/AstrPush/
        self.plugin_code_dir = os.path.abspath(os.path.dirname(__file__))
        # 从代码目录向上两级到AstrBot/data/目录：../../ → data/
        self.data_root = os.path.abspath(
            os.path.join(self.plugin_code_dir, "..", "..")  # ../../ 从 plugin/AstrPush/ → data/
        )
        # 插件数据目录：data/plugin_data/AstrPush/（独立于代码目录）
        self.plugin_data_dir = os.path.join(
            self.data_root, "plugin_data", "AstrPush"  # data/plugin_data/AstrPush/
        )
        # 配置文件路径：data/plugin_data/AstrPush/config.json
        self.config_path = os.path.join(self.plugin_data_dir, "config.json")

        # 确保数据目录存在（首次运行自动创建）
        os.makedirs(self.plugin_data_dir, exist_ok=True)
        logger.debug(f"AstrPush插件代码目录: {self.plugin_code_dir}")
        logger.debug(f"AstrPush插件数据目录: {self.plugin_data_dir}")

        # 加载/生成配置文件（数据目录下的config.json）
        self.config: Dict[str, Any] = self._load_or_generate_config()

        # 其他初始化参数
        self.in_queue: Optional[Queue] = None
        self.process: Optional[Process] = None
        self._running: bool = False

    def _load_or_generate_config(self) -> Dict[str, Any]:
        """加载/生成配置文件（路径：data/plugin_data/AstrPush/config.json）"""
        # 1. 定义默认配置模板
        default_config = {
            "api": {
                "host": "0.0.0.0",       # 默认监听地址
                "port": 9966,            # 默认端口
                "token": secrets.token_urlsafe(32),  # 自动生成随机Token
                "default_umo": ""        # 必填：用户需手动填写接收者ID（从AstrBot日志获取）
            }
        }

        # 2. 配置文件不存在：生成默认配置到数据目录
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
            logger.info(f"AstrPush默认配置已生成，请编辑数据目录下的配置文件：")
            logger.info(f"配置路径：{self.config_path}")
            logger.info(f"提示：请在config.json中填写 'api.default_umo'（接收者ID）")
            return default_config

        # 3. 配置文件存在：加载并返回（兼容旧配置，补充缺失字段）
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            # 补充缺失的默认字段（防止配置文件格式过旧）
            config.setdefault("api", {})
            config["api"].setdefault("host", "0.0.0.0")
            config["api"].setdefault("port", 9966)
            config["api"].setdefault("token", secrets.token_urlsafe(32))
            config["api"].setdefault("default_umo", "")
            return config
        except Exception as e:
            logger.error(f"加载配置文件失败：{str(e)}，将使用默认配置")
            return default_config

    async def initialize(self) -> None:
        """初始化插件（检查配置+启动API服务）"""
        api_config = self.config.get("api", {})

        # 检查必填配置（default_umo和token）
        if not api_config.get("token"):
            api_config["token"] = secrets.token_urlsafe(32)  # 兜底生成Token
            self._save_config()  # 保存到数据目录的config.json
        if not api_config.get("default_umo"):
            logger.error(f"AstrPush配置不完整！请编辑数据目录下的配置文件：")
            logger.error(f"配置路径：{self.config_path}")
            logger.error(f"需填写 'api.default_umo'（接收者ID，从AstrBot日志获取）")
            return

        # 启动API服务子进程
        self.in_queue = Queue(maxsize=100)
        self.process = Process(
            target=run_server,
            args=(
                api_config["token"],
                api_config["host"],
                api_config["port"],
                self.in_queue,
                api_config["default_umo"],
            ),
            daemon=True
        )
        self.process.start()
        self._running = True
        logger.info(f"AstrPush插件初始化成功！API服务运行于 {api_config['host']}:{api_config['port']}")
        logger.info(f"配置文件路径：{self.config_path}")
        asyncio.create_task(self._process_messages())

    def _save_config(self) -> None:
        """保存配置到数据目录下的config.json"""
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    async def _process_messages(self) -> None:
        """处理消息队列并发送到微信"""
        while self._running and self.in_queue:
            try:
                message: Dict[str, Any] = await asyncio.get_event_loop().run_in_executor(
                    None, self.in_queue.get, True, 1
                )
            except Exception:
                continue

            message_id = message.get("message_id", "unknown")
            try:
                logger.info(f"AstrPush处理消息 [ID: {message_id}]")
                result: Dict[str, Any] = {"message_id": message_id, "success": True}

                if message["type"] == "image":
                    image_data = base64.b64decode(message["content"])
                    ImageP.open(BytesIO(image_data)).verify()
                    chain = MessageChain(chain=[Comp.Image.fromBytes(image_data)])
                else:
                    chain = MessageChain(chain=[Comp.Plain(message["content"])])

                await self.context.send_message(message["umo"], chain)
                logger.info(f"AstrPush消息发送成功 [ID: {message_id}]")
            except Exception as e:
                error_msg = f"消息处理失败: {str(e)}"
                logger.error(f"AstrPush [ID: {message_id}] {error_msg}")
                result.update({"success": False, "error": error_msg})
            finally:
                if callback_url := message.get("callback_url"):
                    asyncio.create_task(self._send_callback(callback_url, result))

    async def _send_callback(self, url: str, data: Dict[str, Any]) -> None:
        """发送回调请求"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(url, json=data) as resp:
                    if resp.status >= 400:
                        logger.warning(f"AstrPush回调失败 [URL: {url}]，状态码: {resp.status}")
        except Exception as e:
            logger.error(f"AstrPush回调异常 [URL: {url}]: {str(e)}")

    async def terminate(self) -> None:
        """停止插件"""
        self._running = False
        logger.info("AstrPush插件开始停止...")
        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
        if self.in_queue:
            while not self.in_queue.empty():
                self.in_queue.get()
        logger.info("AstrPush插件已停止")


# 导入API服务（来自代码目录下的api.py）
from .api import run_server  # noqa: E402
