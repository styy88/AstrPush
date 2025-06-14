import asyncio
import uuid
from typing import Any, Dict, Optional

from hypercorn.asyncio import serve
from hypercorn.config import Config
from quart import Quart, abort, jsonify, request

from astrbot.api import logger


class AstrPushServer:
    def __init__(self, token: str, in_queue: Any, default_umo: str):
        """
        初始化AstrPush API服务器
        :param token: 请求鉴权Token
        :param in_queue: 消息队列（用于将消息传递给主进程）
        :param default_umo: 默认消息接收者umo
        """
        self.app = Quart(__name__)
        self.token = token
        self.in_queue = in_queue
        self.default_umo = default_umo  # 默认接收者umo
        self._setup_routes()
        self._server_task: Optional[asyncio.Task] = None

    def _setup_routes(self) -> None:
        """注册API路由"""

        @self.app.errorhandler(400)
        async def bad_request(e: Exception) -> tuple[Dict[str, str], int]:
            return jsonify({"error": "Bad Request", "details": str(e)}), 400

        @self.app.errorhandler(403)
        async def forbidden(e: Exception) -> tuple[Dict[str, str], int]:
            return jsonify({"error": "Forbidden", "details": str(e)}), 403

        @self.app.errorhandler(500)
        async def server_error(e: Exception) -> tuple[Dict[str, str], int]:
            return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

        @self.app.route("/send", methods=["POST"])
        async def send_message() -> Dict[str, Any]:
            """发送消息接口"""
            # 1. 验证Token
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                logger.warning(f"来自 {request.remote_addr} 的请求缺少Token")
                abort(403, description="无效的Authorization头（需使用Bearer Token）")
            
            request_token = auth_header.split(" ")[1]
            if request_token != self.token:
                logger.warning(f"来自 {request.remote_addr} 的无效Token: {request_token}")
                abort(403, description="无效的Token")

            # 2. 解析请求体
            try:
                data = await request.get_json()
            except Exception as e:
                logger.error(f"解析JSON失败: {str(e)}")
                abort(400, description="无效的JSON格式")

            # 3. 验证必填字段
            if not data.get("content"):
                abort(400, description="缺少必填字段: content")

            # 4. 确定接收者umo（优先使用请求中的umo，否则使用默认）
            message_umo = data.get("umo", self.default_umo)
            if not message_umo:
                abort(400, description="未指定接收者umo（请求或插件配置中需提供）")

            # 5. 构造消息并放入队列
            message_id = data.get("message_id", str(uuid.uuid4()))
            message = {
                "message_id": message_id,
                "content": data["content"],
                "umo": message_umo,
                "type": data.get("message_type", "text"),  # 仅支持text
                "callback_url": data.get("callback_url")
            }

            self.in_queue.put(message)
            logger.info(f"AstrPush消息已加入队列 [ID: {message_id}, UMO: {message_umo}]")

            return jsonify({
                "status": "queued",
                "message_id": message_id,
                "queue_size": self.in_queue.qsize()
            })

        @self.app.route("/health", methods=["GET"])
        async def health_check() -> Dict[str, Any]:
            """健康检查接口"""
            return jsonify({
                "status": "ok",
                "queue_size": self.in_queue.qsize(),
                "timestamp": asyncio.get_event_loop().time()
            })

    async def start(self, host: str, port: int) -> None:
        """启动API服务器"""
        config = Config()
        config.bind = [f"{host}:{port}"]
        config.accesslog = None  # 禁用访问日志（AstrBot主日志已足够）
        self._server_task = asyncio.create_task(serve(self.app, config))
        logger.info(f"AstrPush API服务已启动于 {host}:{port}")

        try:
            await self._server_task
        except asyncio.CancelledError:
            logger.info("AstrPush API服务收到关闭请求")
        finally:
            await self.close()

    async def close(self) -> None:
        """关闭服务器资源"""
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        logger.info("AstrPush API服务已关闭")


def run_server(token: str, host: str, port: int, in_queue: Any, default_umo: str) -> None:
    """子进程入口函数（启动AstrPush API服务器）"""
    server = AstrPushServer(token, in_queue, default_umo)
    asyncio.run(server.start(host, port))
