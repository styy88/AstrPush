# AstrPush

AstrBot轻量级推送插件，通过HTTP API接收消息并转发到微信（支持个人/群聊），支持默认接收者配置，简化外部脚本（如青龙面板签到脚本）的消息推送流程。


## 功能特点
- **HTTP API接口**：提供简单的POST接口，支持文本消息推送。
- **默认接收者**：插件端配置默认消息接收者（umo），外部脚本无需重复指定。
- **Token鉴权**：通过Bearer Token验证请求合法性，防止未授权访问。
- **消息队列**：基于进程间队列处理消息，避免阻塞主程序。


## 安装方法
1. 将插件放入AstrBot的插件目录（通常为 `plugins/AstrPush`）。
2. 启动AstrBot，插件会自动生成默认配置文件（路径：`AstrPush/AstrPush.json`）。
3. 编辑配置文件，填写 `token` 和 `default_umo`（必选）。


## 配置说明
配置文件路径：`AstrPush/AstrPush.json`  
**示例配置**：
```json
{
  "api": {
    "host": "0.0.0.0",      // API监听地址（默认0.0.0.0，允许外部访问）
    "port": 9966,           // API监听端口（默认9966）
    "token": "your_secure_token",  // 鉴权Token（必填，建议使用随机字符串）
    "default_umo": "123456789@chatroom"  // 默认接收者umo（必填，从AstrBot日志获取）
  }
}
