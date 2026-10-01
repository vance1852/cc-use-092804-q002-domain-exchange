"""贯通契约冻结、票据签发、重启续传、消费确认、撤回阻止与审计还原的离线验收。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import GatewayService
from .storage import connect


CONTRACT = {
    "contract_id": "env-summary",
    "version": 1,
    "message_kind": "environment_summary",
    "source_domain": "ai-perception",
    "target_domain": "motion-control",
    "field_set": ["summary_id", "obstacles", "free_space", "generated_at"],
}


def _users(service: GatewayService) -> None:
    service.create_user("arch-1", "电子架构负责人", "architect")
    service.create_user("ai-pub-1", "感知域发布代理", "sender", "ai-perception")
    service.create_user("mc-sub-1", "控制域接收代理", "receiver", "motion-control")
    service.create_user("audit-1", "安全审计员", "auditor")


def run() -> dict[str, object]:
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory(prefix="exchange-gateway-") as temporary:
        database = Path(temporary) / "gateway.sqlite3"
        connection = connect(database)
        service = GatewayService(connection, clock)
        _users(service)
        service.create_contract("arch-1", CONTRACT)
        service.freeze_contract("arch-1", "env-summary", 1, 1)
        first = service.issue_ticket(
            "ai-pub-1",
            {
                "ticket_id": "ticket-001",
                "contract_id": "env-summary",
                "contract_version": 1,
                "fields": ["summary_id", "obstacles", "free_space", "generated_at"],
                "content_sha256": "a" * 64,
                "ttl_seconds": 600,
                "idempotency_key": "delivery-001",
            },
        )
        service.issue_ticket(
            "ai-pub-1",
            {
                "ticket_id": "ticket-002",
                "contract_id": "env-summary",
                "contract_version": 1,
                "fields": ["summary_id", "obstacles"],
                "content_sha256": "b" * 64,
                "ttl_seconds": 600,
                "idempotency_key": "delivery-002",
            },
        )
        connection.close()

        # 模拟服务重启：重新打开同一数据库，未决交接继续处理。
        connection = connect(database)
        service = GatewayService(connection, clock)
        pending = service.pending_tickets("mc-sub-1")
        receipt = service.confirm_consumption("mc-sub-1", "ticket-001", "a" * 64, 1)
        revoked = service.revoke_contract("arch-1", "env-summary", 1, "安全规则升级，暂停旧版摘要通道")
        try:
            service.confirm_consumption("mc-sub-1", "ticket-002", "b" * 64, 1)
            blocked_error = ""
        except InvalidState as exc:
            blocked_error = str(exc)
        trace = service.transfer_trace("audit-1", "ticket-001")
        audit = service.audit_chain("audit-1")
        connection.close()
    return {
        "status": "ok",
        "ticket": first["ticket_id"],
        "expires_at": first["expires_at"],
        "pending_after_restart": [item["ticket_id"] for item in pending["tickets"]],
        "receipt_id": receipt["receipt_id"],
        "adopted_contract_version": trace["crossing"]["adopted_contract_version"],
        "released_by": trace["crossing"]["contract_frozen_by"],
        "confirmed_by": trace["crossing"]["consumed_by"],
        "blocked_tickets": revoked["blocked_tickets"],
        "blocked_error": blocked_error,
        "trace_events": [event["event_type"] for event in trace["events"]],
        "audit": audit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行隔离交换闸口离线验收")
    parser.parse_args(argv)
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
