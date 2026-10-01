"""隔离交换闸口的离线验收：契约冻结、票据签发、跨域消费、撤回阻断与重启续办。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import GatewayService
from .storage import connect, inspect_schema


def _base_payload(transfer_id: str, contract_id: str, digest_char: str, key: str) -> dict[str, object]:
    return {
        "transfer_id": transfer_id,
        "contract_id": contract_id,
        "payload_version": "1.1",
        "content_sha256": digest_char * 64,
        "fields": ["trajectory_id", "waypoints", "horizon_ms"],
        "idempotency_key": key,
    }


def run(workspace: Path) -> dict[str, object]:
    clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory(prefix="exchange-gateway-") as temporary:
        database = Path(temporary) / "gateway.sqlite3"
        connection = connect(database)
        try:
            service = GatewayService(connection, clock)
            service.create_user("arch-1", "电子架构负责人", "architect")
            service.register_domain("arch-1", "ai-perception", "AI 感知计算域")
            service.register_domain("arch-1", "motion-control", "运动控制计算域")
            service.create_user("ai-sender-1", "AI 域发送代理", "gateway_sender", "ai-perception")
            service.create_user("mc-receiver-1", "控制域接收代理", "gateway_receiver", "motion-control")
            service.create_user("mc-sender-1", "控制域发送代理", "gateway_sender", "motion-control")
            service.create_user("ai-receiver-1", "AI 域接收代理", "gateway_receiver", "ai-perception")
            service.create_user("audit-1", "审计人员", "auditor")
            service.create_contract("arch-1", {
                "contract_id": "trajectory-ai-mc",
                "message_kind": "target_trajectory",
                "source_domain": "ai-perception",
                "target_domain": "motion-control",
                "required_fields": ["trajectory_id", "waypoints", "horizon_ms"],
                "allowed_versions": ["1.0", "1.1"],
                "ticket_ttl_seconds": 300,
            })
            service.freeze_contract("arch-1", "trajectory-ai-mc", 1)
            first = service.submit_transfer("ai-sender-1", _base_payload("traj-0001", "trajectory-ai-mc", "a", "submit-traj-0001"))
            service.submit_transfer("ai-sender-1", _base_payload("traj-0002", "trajectory-ai-mc", "b", "submit-traj-0002"))
            consumed_before_restart = service.confirm_consumption("mc-receiver-1", "traj-0001", {
                "domain_id": "motion-control",
                "expected_contract_revision": first["contract_revision"],
                "expected_ticket_revision": first["revision"],
                "content_sha256": "a" * 64,
            })
        finally:
            connection.close()
        # 模拟服务重启：重新打开同一 SQLite 文件，继续处理未决交接。
        reopened = connect(database)
        try:
            service = GatewayService(reopened, clock)
            pending = service.pending_transfers("mc-receiver-1")
            second_ticket = next(item for item in pending["pending"] if item["transfer_id"] == "traj-0002")
            consumed_after_restart = service.confirm_consumption("mc-receiver-1", "traj-0002", {
                "domain_id": "motion-control",
                "expected_contract_revision": second_ticket["contract_revision"],
                "expected_ticket_revision": second_ticket["revision"],
                "content_sha256": "b" * 64,
            })
            service.create_contract("arch-1", {
                "contract_id": "receipt-mc-ai",
                "message_kind": "execution_receipt",
                "source_domain": "motion-control",
                "target_domain": "ai-perception",
                "required_fields": ["trajectory_id", "status", "finished_at"],
                "allowed_versions": ["1.0"],
                "ticket_ttl_seconds": 120,
            })
            service.freeze_contract("arch-1", "receipt-mc-ai", 1)
            service.submit_transfer("mc-sender-1", {
                "transfer_id": "rcpt-0001",
                "contract_id": "receipt-mc-ai",
                "payload_version": "1.0",
                "content_sha256": "c" * 64,
                "fields": ["trajectory_id", "status", "finished_at"],
                "idempotency_key": "submit-rcpt-0001",
            })
            revoked = service.revoke_contract("arch-1", "receipt-mc-ai", 2, "安全规则撤回演练")
            blocked_confirm_rejected = False
            try:
                service.confirm_consumption("ai-receiver-1", "rcpt-0001", {
                    "domain_id": "ai-perception",
                    "expected_contract_revision": 2,
                    "expected_ticket_revision": 1,
                    "content_sha256": "c" * 64,
                })
            except InvalidState:
                blocked_confirm_rejected = True
            trace = service.trace_transfer("audit-1", "traj-0001")
            chain = service.audit_chain("audit-1")
            schema = inspect_schema(reopened)
        finally:
            reopened.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    if not blocked_confirm_rejected:
        raise RuntimeError("撤回后的确认未被阻止")
    return {
        "status": "ok",
        "consumed_before_restart": consumed_before_restart["consumption_id"],
        "pending_after_restart": pending["count"],
        "consumed_after_restart": consumed_after_restart["consumption_id"],
        "blocked_transfers": revoked["blocked_transfers"],
        "blocked_confirm_rejected": blocked_confirm_rejected,
        "released_by": trace["released_by"],
        "adopted_version": trace["adopted_version"],
        "audit": chain,
        "schema": schema,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行隔离交换闸口离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
