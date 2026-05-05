# Seedance Local MCP Server

把 `Seedream 3.0 / 5.0` 生图和 `Seedance 2.0` 生视频封装成一个本地 MCP Server，方便 Claude Code 直接调用。

## 宣传语

让 Claude Code 直接接入 Seedream 与 Seedance，把图像和视频生成能力变成可组合的本地 MCP 工具。

## 功能一览

- 支持 Seedream 3.0 文生图
- 支持 Seedream 5.0 文生图、图生图、多图融合、组图生成
- 支持 Seedream 5.0 联网搜索与流式图片事件收集
- 支持 Seedance 2.0 文生视频、多模态参考、视频编辑、视频延长
- 支持 `python` 本地运行
- 支持 `docker compose` 运行
- 支持 Claude Code 通过 HTTP MCP 接入

## 快速开始

1. 复制配置文件并填写你的 API Key：

```bash
cp config.example.toml config.toml
```

2. 按需选择图片模型：

- Seedream 5.0：保留默认 `doubao-seedream-5-0-260128`
- Seedream 3.0：把 `image_model` 改成 `doubao-seedream-3-0-t2i-250415`

3. 启动服务。

本地运行：

```bash
python3 server.py --config config.toml
```

或 Compose 运行：

```bash
docker compose up --build
```

4. 在 Claude Code 中接入：

```bash
claude mcp add --transport http seedance-local http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer change-me"
```

## Claude Code 配置

如果你更想用 `.mcp.json`：

```json
{
  "mcpServers": {
    "seedance-local": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer change-me"
      }
    }
  }
}
```

## 工具

### `seedance_text_to_image`

最小文生图示例：

```json
{
  "prompt": "充满活力的时尚肖像，杂志封面风格",
  "size": "2K",
  "output_format": "png",
  "response_format": "url",
  "watermark": false
}
```

Seedream 3.0 文生图示例：

```json
{
  "model": "doubao-seedream-3-0-t2i-250415",
  "prompt": "鱼眼镜头，一只猫咪的头部，画面呈现出猫咪的五官因为拍摄方式扭曲的效果。",
  "size": "1024x1024",
  "response_format": "url"
}
```

多图融合示例：

```json
{
  "prompt": "将图1的服装换为图2的服装",
  "image": [
    "https://example.com/look-1.png",
    "https://example.com/look-2.png"
  ],
  "size": "2K",
  "sequential_image_generation": "disabled",
  "output_format": "png",
  "response_format": "url",
  "watermark": false
}
```

### `seedance_text_to_video`

最小文生视频示例：

```json
{
  "prompt": "微距镜头对准叶片上翠绿的玻璃蛙，焦点逐渐转移到透明腹部里跳动的心脏。",
  "ratio": "16:9",
  "duration": 11,
  "watermark": false
}
```

多模态参考示例：

```json
{
  "prompt": "全程使用视频1的第一视角构图，全程使用音频1作为背景音乐，首帧为图片1。",
  "images": [
    "https://example.com/frame-1.jpg",
    "https://example.com/frame-2.jpg"
  ],
  "videos": [
    "https://example.com/reference.mp4"
  ],
  "audios": [
    "https://example.com/music.mp3"
  ],
  "generate_audio": true,
  "ratio": "16:9",
  "duration": 11,
  "watermark": true
}
```

## 运行说明

- 默认地址：`http://127.0.0.1:8765/mcp`
- Compose 文件：`compose.yaml`
- 详细配置说明直接看 `config.example.toml` 内注释
