"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .scheduling import SchedulingService
from .service import DomainService
from .storage import Database


def _required(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


def route(service: SchedulingService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        # ---- 协同台账 ----
        if method == "POST" and parsed.path == "/scheduling/participants":
            receipt = service.register_participant(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/qualifications":
            receipt = service.register_qualification(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/availability":
            receipt = service.register_availability(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/zones":
            receipt = service.register_zone(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/shifts":
            receipt = service.create_shift(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/assignments":
            receipt = service.create_assignment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/replacement-plans":
            receipt = service.generate_replacement_plan(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/plan-confirmations":
            receipt = service.confirm_plan_option(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/checkins":
            receipt = service.record_checkin(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/takeovers":
            receipt = service.register_takeover(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/scheduling/handover-completions":
            receipt = service.complete_handover_item(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/scheduling/shifts":
            query = parse_qs(parsed.query)
            return 200, service.get_shift(actor_id=actor_id, shift_id=_required(query, "shift_id"))
        if method == "GET" and parsed.path == "/scheduling/shifts/revision":
            query = parse_qs(parsed.query)
            return 200, service.shift_revision(actor_id=actor_id,
                                               shift_id=_required(query, "shift_id"),
                                               version=int(_required(query, "version")))
        if method == "GET" and parsed.path == "/scheduling/replacement-plans":
            query = parse_qs(parsed.query)
            plans = service.list_replacement_plans(actor_id=actor_id,
                                                   plan_id=query.get("plan_id", [None])[0],
                                                   shift_id=query.get("shift_id", [None])[0])
            return 200, {"items": plans}
        if method == "GET" and parsed.path == "/scheduling/checkins":
            query = parse_qs(parsed.query)
            return 200, {"items": service.list_checkins(actor_id=actor_id,
                                                        shift_id=_required(query, "shift_id"))}
        if method == "GET" and parsed.path == "/scheduling/zones/responsible":
            query = parse_qs(parsed.query)
            return 200, service.zone_responsible(actor_id=actor_id,
                                                 zone_id=_required(query, "zone_id"),
                                                 at=_required(query, "at"))
        if method == "GET" and parsed.path == "/scheduling/shifts/uncovered-dependencies":
            query = parse_qs(parsed.query)
            return 200, service.uncovered_dependencies(actor_id=actor_id,
                                                       shift_id=_required(query, "shift_id"))
        if method == "GET" and parsed.path == "/scheduling/dispatch-logs":
            query = parse_qs(parsed.query)
            logs = service.dispatch_logs(actor_id=actor_id,
                                         shift_id=query.get("shift_id", [None])[0],
                                         result=query.get("result", [None])[0])
            return 200, {"items": logs}
        if method == "GET" and parsed.path == "/scheduling/takeovers":
            query = parse_qs(parsed.query)
            return 200, {"items": service.list_takeovers(actor_id=actor_id,
                                                         shift_id=_required(query, "shift_id"))}
        if method == "GET" and parsed.path == "/scheduling/handovers/pending":
            return 200, {"items": service.pending_handovers(actor_id=actor_id)}
        if method == "GET" and parsed.path == "/scheduling/participants":
            query = parse_qs(parsed.query)
            items = service.participants_view(actor_id=actor_id,
                                              shift_id=_required(query, "shift_id"),
                                              view=query.get("view", [None])[0])
            return 200, {"items": items}
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

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = SchedulingService(database)
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
