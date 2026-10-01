from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from exchange_gateway.acceptance import run as acceptance_run
from exchange_gateway.api import JsonApplication
from exchange_gateway.clock import FrozenClock
from exchange_gateway.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from exchange_gateway.service import GatewayService
from exchange_gateway.storage import connect


CONTRACT = {
    "contract_id": "env-summary",
    "version": 1,
    "message_kind": "environment_summary",
    "source_domain": "ai-perception",
    "target_domain": "motion-control",
    "field_set": ["summary_id", "obstacles", "free_space", "generated_at"],
}


def ticket_payload(ticket_id: str, key: str, content: str, **overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "ticket_id": ticket_id,
        "contract_id": "env-summary",
        "contract_version": 1,
        "fields": ["summary_id", "obstacles"],
        "content_sha256": content,
        "ttl_seconds": 600,
        "idempotency_key": key,
    }
    payload.update(overrides)
    return payload


class GatewayServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = GatewayService(self.connection, self.clock)
        self.service.create_user("arch", "电子架构负责人", "architect")
        self.service.create_user("ai-pub", "感知域发布代理", "sender", "ai-perception")
        self.service.create_user("mc-sub", "控制域接收代理", "receiver", "motion-control")
        self.service.create_user("audit", "安全审计员", "auditor")
        self.service.create_contract("arch", CONTRACT)
        self.service.freeze_contract("arch", "env-summary", 1, 1)

    def tearDown(self) -> None:
        self.connection.close()

    def issue(self, ticket_id: str = "ticket-1", key: str = "key-1", content: str = "a" * 64, **overrides):
        return self.service.issue_ticket("ai-pub", ticket_payload(ticket_id, key, content, **overrides))

    def test_frozen_contract_allows_ticket_and_consumption(self) -> None:
        ticket = self.issue()
        self.assertEqual(ticket["state"], "issued")
        self.assertEqual(ticket["expires_at"], "2026-09-24T08:10:00Z")
        receipt = self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        self.assertEqual(receipt["state"], "consumed")
        self.assertFalse(receipt["replayed"])
        self.assertEqual(receipt["contract_version"], 1)
        trace = self.service.transfer_trace("audit", "ticket-1")
        self.assertEqual(trace["crossing"]["source_domain"], "ai-perception")
        self.assertEqual(trace["crossing"]["target_domain"], "motion-control")
        self.assertEqual(trace["crossing"]["contract_frozen_by"], "arch")
        self.assertEqual(trace["crossing"]["ticket_issued_by"], "ai-pub")
        self.assertEqual(trace["crossing"]["consumed_by"], "mc-sub")
        self.assertEqual(trace["crossing"]["adopted_contract_version"], 1)
        kinds = [event["event_type"] for event in trace["events"]]
        self.assertEqual(
            kinds,
            ["contract.created", "contract.frozen", "ticket.issued", "transfer.consumed"],
        )

    def test_draft_contract_rejects_issue(self) -> None:
        self.service.create_contract("arch", dict(CONTRACT, contract_id="target-trajectory", version=1))
        with self.assertRaises(InvalidState):
            self.service.issue_ticket(
                "ai-pub",
                ticket_payload("ticket-x", "key-x", "c" * 64, contract_id="target-trajectory"),
            )

    def test_issue_replay_returns_original_and_conflicts_on_different_content(self) -> None:
        first = self.issue()
        second = self.issue()
        self.assertTrue(second["replayed"])
        self.assertEqual(first["expires_at"], second["expires_at"])
        self.assertEqual(first["issued_at"], second["issued_at"])
        count = self.connection.execute("SELECT count(*) FROM transfer_tickets").fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(Conflict):
            self.issue(content="b" * 64)
        with self.assertRaises(Conflict):
            self.issue(key="key-other")
        count = self.connection.execute("SELECT count(*) FROM transfer_tickets").fetchone()[0]
        self.assertEqual(count, 1)

    def test_sender_domain_must_match_contract_direction(self) -> None:
        self.service.create_user("mc-pub", "控制域发布代理", "sender", "motion-control")
        with self.assertRaises(Forbidden):
            self.service.issue_ticket("mc-pub", ticket_payload("ticket-2", "key-2", "a" * 64))
        with self.assertRaises(Forbidden):
            self.service.issue_ticket("mc-sub", ticket_payload("ticket-3", "key-3", "a" * 64))

    def test_fields_must_stay_within_minimal_set(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.issue(fields=["summary_id", "raw_point_cloud"])
        ticket = self.issue(fields=["generated_at", "free_space", "obstacles", "summary_id"])
        self.assertEqual(ticket["fields"], ["free_space", "generated_at", "obstacles", "summary_id"])

    def test_receiver_domain_content_and_revision_are_checked(self) -> None:
        self.service.create_user("ai-sub", "感知域接收代理", "receiver", "ai-perception")
        self.issue()
        with self.assertRaises(Forbidden):
            self.service.confirm_consumption("ai-sub", "ticket-1", "a" * 64, 1)
        with self.assertRaises(Conflict):
            self.service.confirm_consumption("mc-sub", "ticket-1", "b" * 64, 1)
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 7)
        with self.assertRaises(NotFound):
            self.service.confirm_consumption("mc-sub", "ticket-missing", "a" * 64, 1)
        receipt = self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        self.assertEqual(receipt["state"], "consumed")

    def test_consumption_replay_returns_same_receipt(self) -> None:
        self.issue()
        first = self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        second = self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(first["confirmed_at"], second["confirmed_at"])
        with self.assertRaises(Conflict):
            self.service.confirm_consumption("mc-sub", "ticket-1", "b" * 64, 1)
        count = self.connection.execute("SELECT count(*) FROM consumption_receipts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_revoke_blocks_pending_and_keeps_consumed_traceable(self) -> None:
        self.issue("ticket-1", "key-1", "a" * 64)
        self.issue("ticket-2", "key-2", "b" * 64)
        consumed = self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        revoked = self.service.revoke_contract("arch", "env-summary", 1, "安全规则升级")
        self.assertEqual(revoked["blocked_tickets"], ["ticket-2"])
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-sub", "ticket-2", "b" * 64, 1)
        with self.assertRaises(InvalidState):
            self.issue("ticket-3", "key-3", "c" * 64)
        replay = self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        self.assertEqual(replay["receipt_id"], consumed["receipt_id"])
        trace = self.service.transfer_trace("audit", "ticket-1")
        self.assertEqual(trace["receipt"]["receipt_id"], consumed["receipt_id"])
        self.assertEqual(trace["contract"]["state"], "revoked")
        blocked = self.service.get_ticket("audit", "ticket-2")
        self.assertEqual(blocked["state"], "blocked")
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_expired_ticket_cannot_be_revived_by_late_receipt(self) -> None:
        self.issue(ttl_seconds=30)
        self.clock.advance(seconds=31)
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        ticket = self.service.get_ticket("audit", "ticket-1")
        self.assertEqual(ticket["state"], "expired")
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
        events = self.connection.execute(
            "SELECT event_type FROM gateway_audit_events WHERE entity_type='ticket' AND entity_id='ticket-1' "
            "ORDER BY event_id"
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["ticket.issued", "ticket.expired"])

    def test_pending_tickets_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "gateway.sqlite3"
            connection = connect(database)
            clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
            service = GatewayService(connection, clock)
            service.create_user("arch", "电子架构负责人", "architect")
            service.create_user("ai-pub", "感知域发布代理", "sender", "ai-perception")
            service.create_user("mc-sub", "控制域接收代理", "receiver", "motion-control")
            service.create_contract("arch", CONTRACT)
            service.freeze_contract("arch", "env-summary", 1, 1)
            service.issue_ticket("ai-pub", ticket_payload("ticket-1", "key-1", "a" * 64))
            connection.close()

            connection = connect(database)
            service = GatewayService(connection, clock)
            pending = service.pending_tickets("mc-sub")
            self.assertEqual([item["ticket_id"] for item in pending["tickets"]], ["ticket-1"])
            receipt = service.confirm_consumption("mc-sub", "ticket-1", "a" * 64, 1)
            self.assertEqual(receipt["state"], "consumed")
            self.assertEqual(service.pending_tickets("mc-sub")["tickets"], [])
            connection.close()

    def test_audit_chain_detects_tampering(self) -> None:
        self.issue()
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE gateway_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_contract("ai-pub", dict(CONTRACT, contract_id="other"))
        with self.assertRaises(Forbidden):
            self.service.freeze_contract("ai-pub", "env-summary", 1, 2)
        with self.assertRaises(Forbidden):
            self.service.revoke_contract("mc-sub", "env-summary", 1, "越权")
        with self.assertRaises(Forbidden):
            self.service.transfer_trace("ai-pub", "ticket-1")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("ai-pub")

    def test_version_specific_freezing(self) -> None:
        self.service.create_contract("arch", dict(CONTRACT, version=2, field_set=["summary_id"]))
        with self.assertRaises(InvalidState):
            self.service.issue_ticket(
                "ai-pub", ticket_payload("ticket-2", "key-2", "a" * 64, contract_version=2, fields=["summary_id"])
            )
        self.service.freeze_contract("arch", "env-summary", 2, 1)
        ticket = self.service.issue_ticket(
            "ai-pub", ticket_payload("ticket-2", "key-2", "a" * 64, contract_version=2, fields=["summary_id"])
        )
        self.assertEqual(ticket["contract_version"], 2)
        receipt = self.service.confirm_consumption("mc-sub", "ticket-2", "a" * 64, 1)
        self.assertEqual(receipt["contract_version"], 2)
        trace = self.service.transfer_trace("audit", "ticket-2")
        self.assertEqual(trace["crossing"]["adopted_contract_version"], 2)


class GatewayApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.app = JsonApplication(GatewayService(self.connection, self.clock))

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, actor: str) -> tuple[int, dict]:
        response = self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8")
        )
        return response.status, response.body

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_full_crossing_over_http(self) -> None:
        status, _ = self.post("/users", {"user_id": "arch", "display_name": "架构", "role": "architect"}, "arch")
        self.assertEqual(status, 201)
        self.post("/users", {"user_id": "ai-pub", "display_name": "发送", "role": "sender", "domain": "ai-perception"}, "arch")
        self.post("/users", {"user_id": "mc-sub", "display_name": "接收", "role": "receiver", "domain": "motion-control"}, "arch")
        self.post("/users", {"user_id": "audit", "display_name": "审计", "role": "auditor"}, "arch")
        status, body = self.post("/contracts", CONTRACT, "arch")
        self.assertEqual(status, 201)
        self.assertEqual(body["state"], "draft")
        status, body = self.post("/contracts/env-summary/versions/1/freeze", {"expected_revision": 1}, "arch")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "frozen")
        status, body = self.post("/tickets", ticket_payload("ticket-1", "key-1", "a" * 64), "ai-pub")
        self.assertEqual(status, 201)
        self.assertEqual(body["state"], "issued")
        response = self.app.handle("GET", "/tickets/pending", {"X-Actor-Id": "mc-sub"})
        self.assertEqual([item["ticket_id"] for item in response.body["tickets"]], ["ticket-1"])
        status, body = self.post("/tickets/ticket-1/consume", {"content_sha256": "a" * 64, "expected_revision": 1}, "mc-sub")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "consumed")
        response = self.app.handle("GET", "/tickets/ticket-1/trace", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["crossing"]["adopted_contract_version"], 1)
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "audit"})
        self.assertTrue(response.body["valid"])

    def test_error_shape(self) -> None:
        response = self.app.handle("POST", "/contracts", {"X-Actor-Id": "nobody"}, b"{}")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")
        response = self.app.handle("GET", "/tickets/ticket-1", {"X-Actor-Id": "nobody"})
        self.assertEqual(response.status, 404)
        response = self.app.handle("POST", "/tickets", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


class GatewayAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pending_after_restart"], ["ticket-001", "ticket-002"])
        self.assertEqual(result["adopted_contract_version"], 1)
        self.assertEqual(result["released_by"], "arch-1")
        self.assertEqual(result["confirmed_by"], "mc-sub-1")
        self.assertEqual(result["blocked_tickets"], ["ticket-002"])
        self.assertTrue(result["blocked_error"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
