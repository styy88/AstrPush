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
        # ==================== 配置路径与Schema解析 ====================
        # 1. 路径定义
        self.plugin_code_dir = os.path.abspath(os.path.dirname(__file__))  # 代码目录：data/plugin/AstrPush/
        self.data_root = os.path.abspath(os.path.join(self.plugin_code_dir, "..", ".."))  # 向上两级到data/
        self.plugin_data_dir = os.path.join(self.data_root, "plugin_data", "AstrPush")  # 数据目录：data/plugin_data/AstrPush/
        self.config_path = os.path.join(self.plugin_data_dir, "config.json")  # 配置文件路径
        self.schema_path = os.path.join(self.plugin_code_dir, "_conf_schema.json")  # Schema路径

        # 2. 确保数据目录存在
        os.makedirs(self.plugin_data_dir, exist_ok=True)

        # 3. 加载Schema并生成/加载配置
        self.schema = self._load_schema()  # 读取_conf_schema.json
        self.config = self._load_or_generate_config()  # 生成/加载config.json

        # 其他初始化
        self.in_queue: Optional[Queue] = None
        self.process: Optional[Process] = None
        self._running: bool = False

    def _load_schema(self) -> Dict[str, Any]:
        """读取插件代码目录下的_conf_schema.json"""
        if not os.path.exists(self.schema_path):
            raise FileNotFoundError(f"配置Schema不存在：{self.schema_path}")
        with open(self.schema_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _generate_default_config(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """根据Schema递归生成默认配置（提取default值）"""
        default_config = {}
        if schema.get("type") == "object" and "properties" in schema:
            for key, prop in schema["properties"].items():
                if "default" in prop:
                    default_config[key] = prop["default"]
                else:
                    # 递归处理嵌套对象
                    if prop.get("type") == "object" and "properties" in prop:
                        default_config[key] = self._generate_default_config(prop)
                    else:
                        default_config[key] = None  # 必填项由用户后续填写
        return default_config

    def _load_or_generate_config(self) -> Dict[str, Any]:
        """生成/加载配置文件到 data/plugin_data/AstrPush/config.json"""
        # 1. 生成默认配置（基于_schema.json）
        default_config = self._generate_default_config(self.schema)

        # 2. 配置文件不存在：生成并保存默认配置
        if not os.path.exists(self.config_path):
            # 自动生成token（若Schema中token默认值为空）
            if "api" in default_config and "token" in default_config["api"] and not default_config["api"]["token"]:
                default_config["api"]["token"] = secrets.token_urlsafe(32)
            # 保存到数据目录
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
            logger.info(f"AstrPush默认配置已生成，请编辑数据目录下的配置文件：")
            logger.info(f"配置路径：{self.config_path}")
            logger.info(f"提示：请填写 'api.default_umo'（接收者ID，从AstrBot日志获取）")
            return default_config

        # 3. 配置文件存在：加载并返回（兼容旧配置）
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            # 补充缺失的默认字段（基于Schema）
            self._merge_defaults(config, default_config)
            return config
        except Exception as e:
            logger.error(f"加载配置失败：{str(e)}，使用默认配置")
            return default_config

    def _merge_defaults(self, config: Dict[str, Any], default: Dict[str, Any]) -> None:
        """递归合并配置与默认值（确保新增字段被补充）"""
        for key, value in default.items():
            if key not in config:
                config[key] = value
            elif isinstance(value, dict) and isinstance(config[key], dict):
                self._merge_defaults(config[key], value)

    async def initialize(self) -> None:
        """初始化插件（检查配置+启动服务）"""
        api_config = self.config.get("api", {})

        # 检查必填配置
        required = self.schema.get("api", {}).get("required", [])
        missing = [k for k in required if not api_config.get(k)]
        if missing:
            logger.error(f"AstrPush配置不完整，缺少：{missing}，请编辑：{self.config_path}")
            return

        # 启动API服务
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
        logger.info(f"AstrPush初始化成功！API运行于 {api_config['host']}:{api_config['port']}")
        asyncio.create_task(self._process_messages())

    async def _process_messages(self) -> None:
        """消息处理逻辑（不变）"""
        while self._running and self.in_queue:
            try:
                message: Dict[str, Any] = await asyncio.get_event_loop().run_in_executor(None, self.in_queue.get, True, 1)
            except Exception:
                continue

            message_id = message.get("message_id", "unknown")
            try:
                chain = MessageChain(chain=[Comp.Plain(message["content"])])
                await self.context.send_message(message["umo"], chain)
                logger.info(f"消息发送成功 [ID: {message_id}]")
            except Exception as e:
                logger.error(f"消息处理失败 [ID: {message_id}]: {str(e)}")

    async def terminate(self) -> None:
        """停止插件（不变）"""
        self._running = False
        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
        logger.info("AstrPush插件已停止")


from .api import run_server  # noqa: E402
