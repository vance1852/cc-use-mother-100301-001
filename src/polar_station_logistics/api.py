"""越冬物资配额与装载决策服务的 HTTP/JSON 边界。

/logistics 前缀下的请求由本模块分派，其余路径回退到基础服务路由，
因此一个进程即可同时提供主体登记与物流决策能力。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from polar_station_foundation.api import route as foundation_route
from polar_station_foundation.errors import DomainError
from polar_station_foundation.service import DomainService

from .service import LogisticsService
from .storage import open_database


def _segments(path: str) -> list[str]:
    return [segment for segment in urlparse(path).path.strip("/").split("/") if segment]


def _created(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def logistics_route(service: LogisticsService, method: str, path: str,
                    body: dict[str, Any], actor_id: str) -> tuple[int, dict[str, Any]]:
    """分派 /logistics 前缀下的物流决策请求。"""

    body = body or {}
    segments = _segments(path)
    tail = segments[1:]
    if method == "POST" and tail == ["items"]:
        return _created(service.register_supply_item(actor_id=actor_id, **body))
    if method == "POST" and tail == ["batches"]:
        return _created(service.register_batch(actor_id=actor_id, **body))
    if method == "POST" and tail == ["flights"]:
        return _created(service.register_flight(actor_id=actor_id, **body))
    if method == "POST" and tail == ["declarations"]:
        return _created(service.submit_declaration(actor_id=actor_id, **body))
    if method == "POST" and tail == ["emergency-requests"]:
        return _created(service.create_emergency_request(actor_id=actor_id, **body))
    if len(tail) == 2 and tail[0] == "flights" and method == "GET":
        return 200, service.get_flight(tail[1])
    if len(tail) == 3 and tail[0] == "flights" and method == "POST":
        flight_id = tail[1]
        action = tail[2]
        if action == "holds":
            return _created(service.add_cargo_hold(actor_id=actor_id, flight_id=flight_id, **body))
        if action == "reserves":
            return _created(service.set_reserve_requirement(actor_id=actor_id, flight_id=flight_id, **body))
        if action == "quotas":
            return _created(service.set_institution_quota(actor_id=actor_id, flight_id=flight_id, **body))
        if action == "freeze":
            return _created(service.freeze_flight(actor_id=actor_id, flight_id=flight_id, **body))
        if action == "decide":
            return _created(service.run_decision(actor_id=actor_id, flight_id=flight_id, **body))
        if action == "seal":
            return _created(service.seal_flight(actor_id=actor_id, flight_id=flight_id, **body))
        if action == "reallocate":
            return _created(service.reallocate(actor_id=actor_id, flight_id=flight_id, **body))
    if len(tail) == 3 and tail[0] == "flights" and method == "GET":
        flight_id = tail[1]
        if tail[2] == "conservation":
            return 200, service.conservation_report(flight_id)
        if tail[2] == "pending-loads":
            return 200, service.pending_load_confirmations(flight_id)
        if tail[2] == "decisions":
            return 200, {"items": service.list_decisions(flight_id)}
        if tail[2] == "snapshot":
            return 200, service.get_snapshot(flight_id)
    if len(tail) == 3 and tail[0] == "declarations" and method == "POST":
        declaration_id = tail[1]
        if tail[2] == "relations":
            return _created(service.add_declaration_relation(
                actor_id=actor_id, from_declaration_id=declaration_id, **body))
        if tail[2] == "withdraw":
            return _created(service.withdraw_declaration(
                actor_id=actor_id, declaration_id=declaration_id, **body))
    if len(tail) == 3 and tail[0] == "declarations" and tail[2] == "explain" and method == "GET":
        return 200, service.explain_declaration(tail[1])
    if len(tail) == 2 and tail[0] == "emergency-requests" and method == "GET":
        return 200, service.get_emergency_request(tail[1])
    if len(tail) == 3 and tail[0] == "emergency-requests" and tail[2] == "approvals" and method == "POST":
        return _created(service.approve_emergency(actor_id=actor_id, emergency_id=tail[1], **body))
    if len(tail) == 3 and tail[0] == "allocations" and method == "POST":
        allocation_id = tail[1]
        if tail[2] == "confirm-load":
            return _created(service.confirm_load(actor_id=actor_id, allocation_id=allocation_id, **body))
        if tail[2] == "claim":
            return _created(service.claim_allocation(actor_id=actor_id, allocation_id=allocation_id, **body))
    return 404, {"error": "route_not_found", "message": "接口不存在"}


def route(logistics: LogisticsService, foundation: DomainService, method: str, path: str,
          body: dict[str, Any] | None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把请求分派到物流决策服务或基础服务。"""

    headers = headers or {}
    actor_id = headers.get("X-Actor-Id", "")
    if urlparse(path).path.startswith("/logistics"):
        try:
            return logistics_route(logistics, method, path, body or {}, actor_id)
        except DomainError as exc:
            return exc.status, {"error": exc.code, "message": str(exc)}
        except (TypeError, ValueError) as exc:
            return 400, {"error": "invalid_request", "message": str(exc)}
    return foundation_route(foundation, method, path, body, headers)


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    logistics: LogisticsService
    foundation: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.logistics, self.foundation, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动越冬物资配额与装载决策 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动越冬物资配额与装载决策服务")
    parser.add_argument("--database", default="logistics.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = open_database(args.database)
    Handler.logistics = LogisticsService(database)
    Handler.foundation = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
