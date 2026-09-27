"""提供协同台账的 HTTP/JSON 边界，并与基础层路由组合。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from night_market_foundation import api as foundation_api
from night_market_foundation.errors import DomainError, ValidationError
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from .service import PlanRejected, SchedulingService


def route(service: SchedulingService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]] | None:
    """分派协同台账请求；路径不属于本模块时返回 None 交由基础层处理。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[0] != "scheduling":
        return None
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    rest = segments[1:]
    try:
        if method == "POST" and rest == ["participants"]:
            return _receipt(service.register_participant(actor_id=actor_id, **body))
        if method == "POST" and rest == ["qualifications"]:
            return _receipt(service.add_qualification(actor_id=actor_id, **body))
        if method == "POST" and rest == ["availability"]:
            return _receipt(service.add_availability(actor_id=actor_id, **body))
        if method == "POST" and rest == ["zones"]:
            return _receipt(service.register_zone(actor_id=actor_id, **body))
        if method == "POST" and rest == ["posts"]:
            return _receipt(service.register_post(actor_id=actor_id, **body))
        if method == "POST" and rest == ["post-dependencies"]:
            return _receipt(service.add_post_dependency(actor_id=actor_id, **body))
        if method == "POST" and rest == ["shifts"]:
            return _receipt(service.register_shift(actor_id=actor_id, **body))
        if method == "POST" and len(rest) == 3 and rest[0] == "shifts" and rest[2] == "confirm":
            return _receipt(service.confirm_schedule(actor_id=actor_id, shift_id=rest[1], **body))
        if method == "GET" and len(rest) == 3 and rest[0] == "shifts" and rest[2] == "versions":
            return 200, {"items": service.shift_versions(actor_id=actor_id, shift_id=rest[1])}
        if method == "GET" and len(rest) == 3 and rest[0] == "shifts" \
                and rest[2] == "uncovered-dependencies":
            return 200, service.uncovered_dependencies(
                actor_id=actor_id, shift_id=rest[1], at=query.get("at", [None])[0])
        if method == "POST" and rest == ["shortages"]:
            return _receipt(service.report_shortage(actor_id=actor_id, **body))
        if method == "POST" and len(rest) == 3 and rest[0] == "shortages" and rest[2] == "proposals":
            return 200, service.generate_proposals(actor_id=actor_id, shortage_id=rest[1], **body)
        if method == "GET" and len(rest) == 3 and rest[0] == "shortages" and rest[2] == "proposals":
            return 200, {"items": service.list_proposals(actor_id=actor_id, shortage_id=rest[1])}
        if method == "GET" and len(rest) == 3 and rest[0] == "shortages" and rest[2] == "rejections":
            return 200, {"items": service.dispatch_rejections(actor_id=actor_id, shortage_id=rest[1])}
        if method == "POST" and len(rest) == 3 and rest[0] == "proposals" and rest[2] == "confirm":
            return _receipt(service.confirm_proposal(actor_id=actor_id, proposal_id=rest[1], **body))
        if method == "POST" and rest == ["checkins"]:
            return _receipt(service.record_checkin(actor_id=actor_id, **body))
        if method == "POST" and rest == ["takeovers"]:
            return _receipt(service.register_takeover(actor_id=actor_id, **body))
        if method == "POST" and len(rest) == 3 and rest[0] == "handovers" and rest[2] == "complete":
            return _receipt(service.complete_handover(actor_id=actor_id, handover_id=rest[1], **body))
        if method == "GET" and rest == ["handovers", "pending"]:
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.pending_handovers(actor_id=actor_id, site_id=site_id)}
        if method == "GET" and len(rest) == 3 and rest[0] == "zones" and rest[2] == "responsible":
            at = query.get("at", [""])[0]
            if not at:
                raise ValidationError("at 不能为空")
            return 200, service.zone_responsible(actor_id=actor_id, zone_id=rest[1], at=at)
        if method == "GET" and len(rest) == 3 and rest[0] == "posts" and rest[2] == "roster":
            shift_id = query.get("shift_id", [""])[0]
            if not shift_id:
                raise ValidationError("shift_id 不能为空")
            return 200, service.post_roster(actor_id=actor_id, post_id=rest[1], shift_id=shift_id)
        if method == "GET" and len(rest) == 2 and rest[0] == "participants":
            return 200, service.get_participant(actor_id=actor_id, participant_id=rest[1])
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except PlanRejected as exc:
        return exc.status, {"error": "plan_rejected", "message": str(exc), "issues": exc.issues}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def combined_route(scheduling: SchedulingService, foundation: DomainService):
    """组合协同台账与基础层路由。"""

    def dispatch(method: str, path: str, body: dict[str, Any] | None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        result = route(scheduling, method, path, body, headers)
        if result is not None:
            return result
        return foundation_api.route(foundation, method, path, body, headers)

    return dispatch


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为组合路由调用。"""

    dispatch = None

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = self.dispatch(self.command, self.path, body,
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
    """启动协同台账本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动中医文化夜市协同台账服务")
    parser.add_argument("--database", default="scheduling.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    foundation = DomainService(database)
    scheduling = SchedulingService(database)
    Handler.dispatch = staticmethod(combined_route(scheduling, foundation))
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
