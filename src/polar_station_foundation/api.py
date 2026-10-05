"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .cargo import CargoService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "") or str(body.get("actor_id", ""))
    # actor_id 统一由请求头或顶层字段提供，不再随业务参数展开
    cargo: Any = service if isinstance(service, CargoService) else None
    params = {key: value for key, value in body.items() if key != "actor_id"}

    def q(name: str, default: str | None = None) -> str | None:
        return parse_qs(parsed.query).get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **params)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **params)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **params)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **params)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, q("category"))]}
        if method == "GET" and parsed.path == "/audit-events":
            return 200, {"items": service.audit_events(int(q("after_sequence", "0") or "0"))}

        if cargo is None:
            return 404, {"error": "route_not_found", "message": "接口不存在"}

        # ------------------------------------------------ 越冬物资配额与装载
        if method == "POST" and parsed.path == "/voyages":
            out = cargo.create_voyage(actor_id=actor_id, **params)
            return 200 if out.get("replayed") else 201, out
        if method == "GET" and parsed.path == "/voyages":
            return 200, cargo.get_voyage(q("voyage_id", ""))
        if method == "POST" and parsed.path == "/materials":
            out = cargo.register_material(actor_id=actor_id, **params)
            return 200 if out.get("replayed") else 201, out
        if method == "POST" and parsed.path == "/critical-reserves":
            return 200, cargo.add_critical_reserve(actor_id=actor_id, **params)
        if method == "POST" and parsed.path == "/org-quotas":
            return 200, cargo.set_org_quota(actor_id=actor_id, **params)
        if method == "POST" and parsed.path == "/applications":
            out = cargo.submit_application(actor_id=actor_id, **params)
            return 200 if out.get("replayed") else 201, out
        if method == "POST" and parsed.path == "/applications/revise":
            return 200, cargo.revise_application(actor_id=actor_id, **params)
        if method == "POST" and parsed.path == "/applications/cancel":
            return 200, cargo.cancel_application(actor_id=actor_id, **params)
        if method == "GET" and parsed.path == "/applications":
            return 200, {"items": cargo.list_applications(q("voyage_id", ""), q("status"))}
        if method == "GET" and parsed.path == "/application":
            return 200, cargo.get_application(q("voyage_id", ""), q("application_id", ""))
        if method == "POST" and parsed.path == "/relations":
            return 200, cargo.record_relation(actor_id=actor_id, **params)
        if method == "GET" and parsed.path == "/relations":
            return 200, {"items": cargo.list_relations(q("voyage_id", ""))}
        if method == "POST" and parsed.path == "/voyages/freeze":
            return 200, cargo.freeze_voyage(actor_id=actor_id, **params)
        if method == "GET" and parsed.path == "/freeze-manifest":
            return 200, cargo.freeze_manifest(q("voyage_id", ""), int(q("freeze_version", "1") or "1"))
        if method == "POST" and parsed.path == "/emergency-releases":
            out = cargo.request_emergency_release(actor_id=actor_id, **params)
            return 200 if out.get("replayed") else 201, out
        if method == "POST" and parsed.path == "/emergency-approvals":
            return 200, cargo.approve_emergency_release(actor_id=actor_id, **params)
        if method == "GET" and parsed.path == "/emergency-releases":
            return 200, {"items": cargo.list_emergency_releases(q("voyage_id", ""))}
        if method == "POST" and parsed.path == "/loading/confirm":
            return 200, cargo.confirm_loading(actor_id=actor_id, **params)
        if method == "POST" and parsed.path == "/issues/confirm":
            return 200, cargo.confirm_issue(actor_id=actor_id, **params)
        if method == "GET" and parsed.path == "/loading/pending":
            return 200, {"items": cargo.pending_loading(q("voyage_id", ""))}
        if method == "POST" and parsed.path == "/cargo/shortfall":
            return 200, cargo.report_shortfall(actor_id=actor_id, **params)
        if method == "POST" and parsed.path == "/cargo/expiry":
            return 200, cargo.report_expiry(actor_id=actor_id, **params)
        if method == "POST" and parsed.path == "/cargo/flight-cancellation":
            return 200, cargo.report_flight_cancellation(actor_id=actor_id, **params)
        if method == "POST" and parsed.path == "/cargo/swap":
            return 200, cargo.swap_to_substitute(actor_id=actor_id, **params)
        if method == "GET" and parsed.path == "/allocation-ledger":
            return 200, {"items": cargo.ledger(q("voyage_id", ""))}
        if method == "GET" and parsed.path == "/application-explanation":
            return 200, cargo.explain_application(q("voyage_id", ""), q("application_id", ""))
        if method == "GET" and parsed.path == "/conservation":
            return 200, cargo.verify_conservation(q("voyage_id", ""))
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
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
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动极地科考站协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = CargoService(database)
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
