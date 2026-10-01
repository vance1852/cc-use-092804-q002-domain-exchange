"""无第三方依赖的隔离交换闸口 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import GatewayError, ValidationFailed
from .service import GatewayService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到闸口领域服务，便于无网络单元测试。"""

    def __init__(self, service: GatewayService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(
                    201,
                    self.service.create_user(
                        payload["user_id"], payload["display_name"], payload["role"], payload.get("domain", "")
                    ),
                )
            if method == "POST" and path == "/contracts":
                return Response(201, self.service.create_contract(actor, payload))
            if (
                method == "POST"
                and len(parts) == 5
                and parts[0] == "contracts"
                and parts[2] == "versions"
                and parts[4] == "freeze"
            ):
                return Response(
                    200,
                    self.service.freeze_contract(actor, parts[1], int(parts[3]), int(payload["expected_revision"])),
                )
            if (
                method == "POST"
                and len(parts) == 5
                and parts[0] == "contracts"
                and parts[2] == "versions"
                and parts[4] == "revoke"
            ):
                return Response(
                    200, self.service.revoke_contract(actor, parts[1], int(parts[3]), payload["reason"])
                )
            if (
                method == "GET"
                and len(parts) == 4
                and parts[0] == "contracts"
                and parts[2] == "versions"
            ):
                return Response(200, self.service.get_contract(actor, parts[1], int(parts[3])))
            if method == "POST" and path == "/tickets":
                return Response(201, self.service.issue_ticket(actor, payload))
            if method == "GET" and path == "/tickets/pending":
                return Response(200, self.service.pending_tickets(actor))
            if method == "POST" and len(parts) == 3 and parts[0] == "tickets" and parts[2] == "consume":
                return Response(
                    200,
                    self.service.confirm_consumption(
                        actor, parts[1], payload["content_sha256"], int(payload["expected_revision"])
                    ),
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "tickets" and parts[2] == "trace":
                return Response(200, self.service.transfer_trace(actor, parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "tickets":
                return Response(200, self.service.get_ticket(actor, parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except GatewayError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ExchangeGateway/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动控制域与 AI 域隔离交换闸口 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("exchange-gateway.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(GatewayService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
