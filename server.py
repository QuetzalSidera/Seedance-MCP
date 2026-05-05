#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import queue
import secrets
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SERVER_NAME = "seedance-local-http"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2025-03-26"


@dataclass
class Session:
    session_id: str
    created_at: float = field(default_factory=time.time)
    outbound_events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)


@dataclass
class Config:
    api_key: str
    base_url: str
    image_create_path: str
    video_create_path: str
    video_get_path_template: str
    image_model: str
    video_model: str
    output_dir: Path
    poll_interval_seconds: float
    poll_timeout_seconds: float
    bind_host: str
    bind_port: int
    mcp_path: str
    auth_token: str | None
    allowed_origins: list[str]
    get_stream_timeout_seconds: float

    @classmethod
    def load(cls, path: str) -> "Config":
        config_path = Path(path).expanduser().resolve()
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))

        server = raw.get("server") or {}
        volc = raw.get("volcengine") or {}
        security = raw.get("security") or {}

        api_key = volc.get("api_key")
        if not api_key:
            raise RuntimeError("Missing volcengine.api_key in config.toml")

        output_dir = Path(server.get("output_dir", "./outputs")).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        allowed_origins = security.get("allowed_origins", ["http://localhost", "http://127.0.0.1"])
        if not isinstance(allowed_origins, list) or not all(isinstance(item, str) for item in allowed_origins):
            raise RuntimeError("security.allowed_origins must be an array of strings")

        return cls(
            api_key=api_key,
            base_url=str(volc.get("base_url", "https://ark.cn-beijing.volces.com")).rstrip("/"),
            image_create_path=str(volc.get("image_create_path", "/api/v3/images/generations")),
            video_create_path=str(volc.get("video_create_path", "/api/v3/contents/generations/tasks")),
            video_get_path_template=str(
                volc.get("video_get_path_template", "/api/v3/contents/generations/tasks/{task_id}")
            ),
            image_model=str(volc.get("image_model", "doubao-seedream-5-0-260128")),
            video_model=str(volc.get("video_model", "doubao-seedance-2-0-260128")),
            output_dir=output_dir,
            poll_interval_seconds=float(server.get("poll_interval_seconds", 5)),
            poll_timeout_seconds=float(server.get("poll_timeout_seconds", 300)),
            bind_host=str(server.get("host", "127.0.0.1")),
            bind_port=int(server.get("port", 8765)),
            mcp_path=normalize_path(str(server.get("mcp_path", "/mcp"))),
            auth_token=security.get("auth_token"),
            allowed_origins=allowed_origins,
            get_stream_timeout_seconds=float(server.get("get_stream_timeout_seconds", 30)),
        )


class AppState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()

    def create_session(self) -> Session:
        session = Session(session_id=secrets.token_urlsafe(24))
        with self.sessions_lock:
            self.sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self.sessions_lock:
            return self.sessions.get(session_id)

    def delete_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self.sessions_lock:
            return self.sessions.pop(session_id, None) is not None


def normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def deep_get(value: Any, *path: Any) -> Any:
    current = value
    for item in path:
        if isinstance(current, dict):
            current = current.get(item)
        elif isinstance(current, list) and isinstance(item, int) and 0 <= item < len(current):
            current = current[item]
        else:
            return None
    return current


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def join_url(base_url: str, path: str) -> str:
    return f"{base_url}{normalize_path(path)}"


def request_json(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url=url, method=method, headers=headers, data=payload)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc.reason}") from exc


def request_sse(method: str, url: str, headers: dict[str, str], body: dict[str, Any]) -> list[dict[str, Any]]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url=url, method=method, headers=headers, data=payload)
    events: list[dict[str, Any]] = []
    current_data_lines: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if current_data_lines:
                        events.append(json.loads("\n".join(current_data_lines)))
                        current_data_lines = []
                    continue
                if line.startswith("data:"):
                    current_data_lines.append(line[5:].lstrip())
            if current_data_lines:
                events.append(json.loads("\n".join(current_data_lines)))
            return events
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc.reason}") from exc


def build_api_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def read_image_as_data_url(path: str) -> str:
    file_path = Path(path).expanduser().resolve()
    raw = file_path.read_bytes()
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def maybe_local_asset_to_url(value: str) -> str:
    if value.startswith(("http://", "https://", "data:", "asset://")):
        return value
    candidate = Path(value).expanduser()
    if candidate.exists():
        return read_image_as_data_url(str(candidate.resolve()))
    return value


def download_file(url: str, output_dir: Path, filename_prefix: str) -> str:
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix or ".bin"
    filename = f"{filename_prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}{suffix}"
    target = output_dir / filename
    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            target.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to download result from {url}: {exc.reason}") from exc
    return str(target)


def extract_task_id(payload: dict[str, Any]) -> str | None:
    return first_non_empty(
        payload.get("id"),
        payload.get("task_id"),
        deep_get(payload, "data", "id"),
        deep_get(payload, "data", "task_id"),
        deep_get(payload, "task", "id"),
    )


def extract_status(payload: dict[str, Any]) -> str | None:
    status = first_non_empty(
        payload.get("status"),
        payload.get("state"),
        deep_get(payload, "data", "status"),
        deep_get(payload, "data", "state"),
        deep_get(payload, "task", "status"),
        deep_get(payload, "task", "state"),
    )
    return status.lower() if isinstance(status, str) else status


def collect_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, list):
        urls: list[str] = []
        for item in value:
            urls.extend(collect_urls(item))
        return urls
    if isinstance(value, dict):
        urls: list[str] = []
        for key, child in value.items():
            if key in {"url", "video_url", "image_url", "download_url"} and isinstance(child, str):
                urls.append(child)
            else:
                urls.extend(collect_urls(child))
        return urls
    return []


def extract_urls(payload: dict[str, Any]) -> list[str]:
    candidates = [
        payload,
        deep_get(payload, "data"),
        deep_get(payload, "output"),
        deep_get(payload, "result"),
        deep_get(payload, "task", "output"),
    ]
    urls: list[str] = []
    for candidate in candidates:
        urls.extend(collect_urls(candidate))
    return list(dict.fromkeys(urls))


def normalize_image_argument(value: Any) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return maybe_local_asset_to_url(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [maybe_local_asset_to_url(item) for item in value]
    raise RuntimeError("image must be a string or an array of strings")


def build_video_content(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    if arguments.get("content") is not None:
        content = arguments["content"]
        if not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
            raise RuntimeError("content must be an array of objects")
        return content

    prompt = arguments.get("prompt")
    if not prompt:
        raise RuntimeError("prompt is required when content is not provided")

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

    def add_items(values: Any, item_type: str, role: str, url_key: str) -> None:
        if values is None:
            return
        items = values if isinstance(values, list) else [values]
        for item in items:
            if not isinstance(item, str):
                raise RuntimeError(f"{item_type} inputs must be strings")
            content.append(
                {
                    "type": item_type,
                    url_key: {"url": maybe_local_asset_to_url(item)},
                    "role": role,
                }
            )

    add_items(arguments.get("images"), "image_url", "reference_image", "image_url")
    add_items(arguments.get("videos"), "video_url", "reference_video", "video_url")
    add_items(arguments.get("audios"), "audio_url", "reference_audio", "audio_url")
    add_items(arguments.get("first_frame_images"), "image_url", "first_frame", "image_url")
    add_items(arguments.get("last_frame_images"), "image_url", "last_frame", "image_url")
    return content


def poll_task(config: Config, task_id: str) -> dict[str, Any]:
    url = join_url(
        config.base_url,
        config.video_get_path_template.format(task_id=urllib.parse.quote(task_id)),
    )
    deadline = time.time() + config.poll_timeout_seconds
    last_payload: dict[str, Any] = {}
    while time.time() < deadline:
        last_payload = request_json("GET", url, build_api_headers(config.api_key))
        status = extract_status(last_payload)
        if status in {"succeeded", "success", "completed", "done"}:
            return last_payload
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(f"Video generation failed: {json.dumps(last_payload, ensure_ascii=False)}")
        time.sleep(config.poll_interval_seconds)
    raise RuntimeError(f"Timed out polling task {task_id}: {json.dumps(last_payload, ensure_ascii=False)}")


def handle_image_tool(config: Config, arguments: dict[str, Any]) -> dict[str, Any]:
    body = {
        "model": arguments.get("model") or config.image_model,
        "prompt": arguments["prompt"],
    }
    image_value = normalize_image_argument(arguments.get("image"))
    if image_value is not None:
        body["image"] = image_value
    if arguments.get("size"):
        body["size"] = arguments["size"]
    if arguments.get("output_format"):
        body["output_format"] = arguments["output_format"]
    if arguments.get("response_format"):
        body["response_format"] = arguments["response_format"]
    if arguments.get("watermark") is not None:
        body["watermark"] = arguments["watermark"]
    if arguments.get("sequential_image_generation"):
        body["sequential_image_generation"] = arguments["sequential_image_generation"]
    if arguments.get("sequential_image_generation_options"):
        body["sequential_image_generation_options"] = arguments["sequential_image_generation_options"]
    if arguments.get("optimize_prompt_options"):
        body["optimize_prompt_options"] = arguments["optimize_prompt_options"]
    if arguments.get("tools"):
        body["tools"] = arguments["tools"]
    if arguments.get("stream") is not None:
        body["stream"] = arguments["stream"]
    extra_body = arguments.get("extra_body") or {}
    if not isinstance(extra_body, dict):
        raise RuntimeError("extra_body must be an object")
    body.update(extra_body)

    request_url = join_url(config.base_url, config.image_create_path)
    events: list[dict[str, Any]] = []
    if body.get("stream") is True:
        headers = build_api_headers(config.api_key)
        headers["Accept"] = "text/event-stream"
        events = request_sse("POST", request_url, headers, body)
        response = {"events": events}
        final_event = next((event for event in reversed(events) if event.get("type") == "image_generation.completed"),
                           {})
        urls = extract_urls(final_event) or extract_urls(response)
    else:
        response = request_json(
            "POST",
            request_url,
            build_api_headers(config.api_key),
            body,
        )
        urls = extract_urls(response)
    downloads: list[str] = []
    if arguments.get("download", True):
        for index, url in enumerate(urls, start=1):
            downloads.append(download_file(url, config.output_dir, f"image-{index}"))
    return {
        "model": body["model"],
        "request_body": body,
        "urls": urls,
        "downloads": downloads,
        "events": events,
        "raw_response": response,
    }


def handle_video_tool(config: Config, arguments: dict[str, Any]) -> dict[str, Any]:
    body = {
        "model": arguments.get("model") or config.video_model,
        "content": build_video_content(arguments),
    }
    if arguments.get("duration"):
        body["duration"] = arguments["duration"]
    if arguments.get("resolution"):
        body["resolution"] = arguments["resolution"]
    if arguments.get("ratio"):
        body["ratio"] = arguments["ratio"]
    elif arguments.get("aspect_ratio"):
        body["ratio"] = arguments["aspect_ratio"]
    if arguments.get("generate_audio") is not None:
        body["generate_audio"] = arguments["generate_audio"]
    if arguments.get("watermark") is not None:
        body["watermark"] = arguments["watermark"]
    if arguments.get("return_last_frame") is not None:
        body["return_last_frame"] = arguments["return_last_frame"]
    if arguments.get("service_tier"):
        body["service_tier"] = arguments["service_tier"]
    if arguments.get("tools"):
        body["tools"] = arguments["tools"]
    extra_body = arguments.get("extra_body") or {}
    if not isinstance(extra_body, dict):
        raise RuntimeError("extra_body must be an object")
    body.update(extra_body)

    create_response = request_json(
        "POST",
        join_url(config.base_url, config.video_create_path),
        build_api_headers(config.api_key),
        body,
    )
    task_id = extract_task_id(create_response)
    if not task_id:
        return {
            "model": body["model"],
            "task_id": None,
            "request_body": body,
            "raw_response": create_response,
            "note": "Task ID not found. Check your endpoint and response schema.",
        }

    task_response = poll_task(config, task_id)
    urls = extract_urls(task_response)
    downloads: list[str] = []
    if arguments.get("download", True):
        for index, url in enumerate(urls, start=1):
            downloads.append(download_file(url, config.output_dir, f"video-{index}"))
    return {
        "model": body["model"],
        "task_id": task_id,
        "status": extract_status(task_response),
        "request_body": body,
        "urls": urls,
        "downloads": downloads,
        "create_response": create_response,
        "task_response": task_response,
    }


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "seedance_text_to_image",
            "description": "Call Seedream image generation API with official single-image, multi-image, grouped-image, web-search, and stream parameters.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "image": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
                    },
                    "size": {"type": "string"},
                    "output_format": {"type": "string"},
                    "response_format": {"type": "string"},
                    "watermark": {"type": "boolean"},
                    "sequential_image_generation": {"type": "string"},
                    "sequential_image_generation_options": {"type": "object"},
                    "optimize_prompt_options": {"type": "object"},
                    "tools": {"type": "array", "items": {"type": "object"}},
                    "stream": {"type": "boolean"},
                    "download": {"type": "boolean", "default": True},
                    "extra_body": {"type": "object"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "seedance_text_to_video",
            "description": "Call Seedance 2.0 content generation task API using official content[] multimodal inputs, then poll and optionally download the result locally.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "content": {"type": "array", "items": {"type": "object"}},
                    "images": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
                    },
                    "videos": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
                    },
                    "audios": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
                    },
                    "first_frame_images": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
                    },
                    "last_frame_images": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
                    },
                    "duration": {"type": "integer"},
                    "resolution": {"type": "string"},
                    "ratio": {"type": "string"},
                    "aspect_ratio": {"type": "string"},
                    "generate_audio": {"type": "boolean"},
                    "watermark": {"type": "boolean"},
                    "return_last_frame": {"type": "boolean"},
                    "service_tier": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "object"}},
                    "download": {"type": "boolean", "default": True},
                    "extra_body": {"type": "object"},
                },
                "anyOf": [{"required": ["prompt"]}, {"required": ["content"]}],
            },
        },
    ]


def initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def make_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
        "structuredContent": result,
    }


def handle_rpc_message(state: AppState, session: Session | None, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "ping":
        return success_response(request_id, {})
    if method == "initialize":
        return success_response(request_id, initialize_result())
    if method == "tools/list":
        return success_response(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name == "seedance_text_to_image":
                result = handle_image_tool(state.config, arguments)
            elif name == "seedance_text_to_video":
                result = handle_video_tool(state.config, arguments)
            else:
                return error_response(request_id, -32601, f"Unknown tool: {name}")
        except Exception as exc:
            return success_response(request_id,
                                    {"content": [{"type": "text", "text": f"Tool error: {exc}"}], "isError": True})
        return success_response(request_id, make_tool_result(result))
    if method is None:
        return None
    return error_response(request_id, -32601, f"Unknown method: {method}")


class MCPHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def app_state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if not self._require_valid_path():
            return
        if not self._require_origin():
            return
        if not self._require_auth():
            return
        self._require_sse_session_stream()

    def do_POST(self) -> None:
        if not self._require_valid_path():
            return
        if not self._require_origin():
            return
        if not self._require_auth():
            return

        raw = self._read_json_body()
        if raw is None:
            return

        is_batch = isinstance(raw, list)
        messages = raw if is_batch else [raw]
        if not all(isinstance(item, dict) for item in messages):
            self._send_json(HTTPStatus.BAD_REQUEST, error_response(None, -32600, "Invalid JSON-RPC payload"))
            return

        has_request = any("id" in item and "method" in item for item in messages)
        has_init = any(item.get("method") == "initialize" for item in messages)

        session = None
        session_id = self.headers.get("Mcp-Session-Id")
        if has_init:
            session = self.app_state.create_session()
        else:
            session = self.app_state.get_session(session_id)
            if has_request and session is None:
                self.send_error(HTTPStatus.BAD_REQUEST, "Missing or invalid Mcp-Session-Id")
                return

        responses = []
        for item in messages:
            response = handle_rpc_message(self.app_state, session, item)
            if response is not None:
                responses.append(response)

        if not has_request:
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        accept = self.headers.get("Accept", "")
        if "text/event-stream" in accept:
            self._send_sse_responses(responses, session.session_id if session else None)
            return

        body: Any
        if is_batch:
            body = responses
        else:
            body = responses[0] if responses else {}
        self._send_json(HTTPStatus.OK, body, session.session_id if has_init and session else None)

    def do_DELETE(self) -> None:
        if not self._require_valid_path():
            return
        if not self._require_origin():
            return
        if not self._require_auth():
            return
        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing Mcp-Session-Id")
            return
        deleted = self.app_state.delete_session(session_id)
        if not deleted:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown session")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require_valid_path(self) -> bool:
        if urllib.parse.urlparse(self.path).path != self.app_state.config.mcp_path:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown path")
            return False
        return True

    def _require_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin in self.app_state.config.allowed_origins:
            return True
        self.send_error(HTTPStatus.FORBIDDEN, "Origin not allowed")
        return False

    def _require_auth(self) -> bool:
        token = self.app_state.config.auth_token
        if not token:
            return True
        header = self.headers.get("Authorization")
        if header == f"Bearer {token}":
            return True
        self.send_error(HTTPStatus.UNAUTHORIZED, "Unauthorized")
        return False

    def _read_json_body(self) -> dict[str, Any] | list[Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return None

    def _send_json(self, status: HTTPStatus, body: Any, session_id: str | None = None) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()

    def _send_sse_event(self, data: dict[str, Any], event_id: str | None = None) -> None:
        if event_id:
            self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_sse_responses(self, responses: list[dict[str, Any]], session_id: str | None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        for response in responses:
            self._send_sse_event(response, uuid.uuid4().hex)

    def _require_sse_session_stream(self) -> None:
        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self.send_error(HTTPStatus.NOT_ACCEPTABLE, "Accept must include text/event-stream")
            return

        session_id = self.headers.get("Mcp-Session-Id")
        session = self.app_state.get_session(session_id)
        if session_id and session is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown session")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        self.wfile.write(b": connected\n\n")
        self.wfile.flush()

        deadline = time.time() + self.app_state.config.get_stream_timeout_seconds
        while time.time() < deadline:
            if session is None:
                time.sleep(0.5)
                continue
            try:
                event = session.outbound_events.get(timeout=1)
            except queue.Empty:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                continue
            self._send_sse_event(event, uuid.uuid4().hex)


class MCPHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler],
                 app_state: AppState):
        super().__init__(server_address, handler_class)
        self.app_state = app_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seedance local MCP server over Streamable HTTP")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = Config.load(args.config)
    state = AppState(config)

    httpd = MCPHTTPServer((config.bind_host, config.bind_port), MCPHandler, state)
    print(
        f"{SERVER_NAME} listening on http://{config.bind_host}:{config.bind_port}{config.mcp_path}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
