from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from exchange_gateway.api import JsonApplication
from exchange_gateway.clock import FrozenClock
from exchange_gateway.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from exchange_gateway.service import GatewayService
from exchange_gateway.storage import connect


TRAJECTORY_CONTRACT = {
    "contract_id": "trajectory-ai-mc",
    "message_kind": "target_trajectory",
    "source_domain": "ai-perception",
    "target_domain": "motion-control",
    "required_fields": ["trajectory_id", "waypoints", "horizon_ms"],
    "allowed_versions": ["1.0", "1.1"],
    "ticket_ttl_seconds": 300,
}

RECEIPT_CONTRACT = {
    "contract_id": "receipt-mc-ai",
    "message_kind": "execution_receipt",
    "source_domain": "motion-control",
    "target_domain": "ai-perception",
    "required_fields": ["trajectory_id", "status", "finished_at"],
    "allowed_versions": ["1.0"],
    "ticket_ttl_seconds": 120,
}


def submission(transfer_id: str, digest_char: str, key: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "transfer_id": transfer_id,
        "contract_id": "trajectory-ai-mc",
        "payload_version": "1.1",
        "content_sha256": digest_char * 64,
        "fields": ["trajectory_id", "waypoints", "horizon_ms"],
        "idempotency_key": key,
    }
    payload.update(overrides)
    return payload


def confirmation(digest_char: str, contract_revision: int = 2, ticket_revision: int = 1) -> dict[str, object]:
    return {
        "domain_id": "motion-control",
        "expected_contract_revision": contract_revision,
        "expected_ticket_revision": ticket_revision,
        "content_sha256": digest_char * 64,
    }


class GatewayServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = GatewayService(self.connection, self.clock)
        self.service.create_user("arch", "电子架构负责人", "architect")
        self.service.register_domain("arch", "ai-perception", "AI 感知计算域")
        self.service.register_domain("arch", "motion-control", "运动控制计算域")
        self.service.create_user("ai-sender", "AI 域发送代理", "gateway_sender", "ai-perception")
        self.service.create_user("mc-sender", "控制域发送代理", "gateway_sender", "motion-control")
        self.service.create_user("mc-receiver", "控制域接收代理", "gateway_receiver", "motion-control")
        self.service.create_user("ai-receiver", "AI 域接收代理", "gateway_receiver", "ai-perception")
        self.service.create_user("audit", "审计人员", "auditor")
        self.service.create_contract("arch", TRAJECTORY_CONTRACT)
        self.service.freeze_contract("arch", "trajectory-ai-mc", 1)

    def tearDown(self) -> None:
        self.connection.close()

    def test_frozen_contract_is_immutable_and_bounds_tickets(self) -> None:
        contract = self.service.get_contract("audit", "trajectory-ai-mc")
        self.assertEqual(contract["state"], "frozen")
        self.assertEqual(contract["revision"], 2)
        self.assertEqual(contract["frozen_by"], "arch")
        self.service.create_contract("arch", RECEIPT_CONTRACT)
        with self.assertRaises(InvalidState):
            self.service.submit_transfer("mc-sender", submission("rcpt-1", "c", "k-rcpt-1", contract_id="receipt-mc-ai", payload_version="1.0", fields=["trajectory_id", "status", "finished_at"]))
        with self.assertRaises(InvalidState):
            self.service.freeze_contract("arch", "trajectory-ai-mc", 2)

    def test_submit_issues_time_limited_ticket(self) -> None:
        ticket = self.service.submit_transfer("ai-sender", submission("traj-1", "a", "k-1"))
        self.assertEqual(ticket["state"], "issued")
        self.assertEqual(ticket["contract_revision"], 2)
        self.assertEqual(ticket["expires_at"], "2026-10-01T08:05:00Z")

    def test_submit_enforces_domain_version_and_minimal_fields(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.submit_transfer("mc-sender", submission("traj-2", "a", "k-2"))
        with self.assertRaises(ValidationFailed):
            self.service.submit_transfer("ai-sender", submission("traj-3", "a", "k-3", payload_version="9.9"))
        with self.assertRaises(ValidationFailed):
            self.service.submit_transfer("ai-sender", submission("traj-4", "a", "k-4", fields=["trajectory_id"]))
        with self.assertRaises(ValidationFailed):
            self.service.submit_transfer("ai-sender", submission("traj-5", "not-a-digest", "k-5"))

    def test_duplicate_delivery_returns_original_result(self) -> None:
        first = self.service.submit_transfer("ai-sender", submission("traj-6", "a", "k-6"))
        self.assertEqual(first, self.service.submit_transfer("ai-sender", submission("traj-6", "a", "k-6")))
        self.assertEqual(first, self.service.submit_transfer("ai-sender", submission("traj-6", "a", "k-6-bis")))
        count = self.connection.execute("SELECT count(*) FROM transfer_tickets").fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_number_with_different_content_conflicts(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-7", "a", "k-7"))
        with self.assertRaises(Conflict):
            self.service.submit_transfer("ai-sender", submission("traj-7", "b", "k-7-other"))
        with self.assertRaises(Conflict):
            self.service.submit_transfer("ai-sender", submission("traj-8", "b", "k-7"))

    def test_confirm_consumes_and_replay_returns_original(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-9", "a", "k-9"))
        consumed = self.service.confirm_consumption("mc-receiver", "traj-9", confirmation("a"))
        self.assertEqual(consumed["state"], "consumed")
        self.assertEqual(consumed["adopted_version"], "1.1")
        self.assertFalse(consumed["replayed"])
        replay = self.service.confirm_consumption("mc-receiver", "traj-9", confirmation("a"))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["consumption_id"], consumed["consumption_id"])
        self.assertEqual(replay["confirmed_at"], consumed["confirmed_at"])
        with self.assertRaises(Conflict):
            self.service.confirm_consumption("mc-receiver", "traj-9", confirmation("b"))

    def test_confirm_requires_unchanged_contract_domain_and_ticket(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-10", "a", "k-10"))
        with self.assertRaises(Forbidden):
            self.service.confirm_consumption("ai-receiver", "traj-10", confirmation("a") | {"domain_id": "ai-perception"})
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-receiver", "traj-10", confirmation("a", ticket_revision=7))
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-receiver", "traj-10", confirmation("a", contract_revision=1))
        with self.assertRaises(Conflict):
            self.service.confirm_consumption("mc-receiver", "traj-10", confirmation("b"))
        self.assertEqual(self.service.get_transfer("mc-receiver", "traj-10")["state"], "issued")

    def test_revocation_blocks_pending_and_keeps_consumed_traceable(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-11", "a", "k-11"))
        self.service.submit_transfer("ai-sender", submission("traj-12", "b", "k-12"))
        self.service.confirm_consumption("mc-receiver", "traj-11", confirmation("a"))
        revoked = self.service.revoke_contract("arch", "trajectory-ai-mc", 2, "安全规则撤回")
        self.assertEqual(revoked["blocked_transfers"], 1)
        self.assertEqual(self.service.get_transfer("audit", "traj-12")["state"], "blocked")
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-receiver", "traj-12", confirmation("b"))
        with self.assertRaises(InvalidState):
            self.service.submit_transfer("ai-sender", submission("traj-13", "c", "k-13"))
        trace = self.service.trace_transfer("audit", "traj-11")
        self.assertEqual(trace["ticket"]["state"], "consumed")
        self.assertEqual(trace["consumption"]["adopted_version"], "1.1")
        self.assertEqual(trace["contract"]["state"], "revoked")
        self.assertEqual(trace["contract"]["revoked_by"], "arch")

    def test_out_of_order_receipt_cannot_revive_expired_ticket(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-14", "a", "k-14"))
        self.clock.advance(seconds=301)
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-receiver", "traj-14", confirmation("a"))
        self.assertEqual(self.service.get_transfer("audit", "traj-14")["state"], "expired")
        with self.assertRaises(InvalidState):
            self.service.confirm_consumption("mc-receiver", "traj-14", confirmation("a"))
        self.assertEqual(self.service.get_transfer("audit", "traj-14")["state"], "expired")

    def test_pending_handoffs_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "gateway.sqlite3"
            first = connect(database)
            clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
            service = GatewayService(first, clock)
            service.create_user("arch", "电子架构负责人", "architect")
            service.register_domain("arch", "ai-perception", "AI 感知计算域")
            service.register_domain("arch", "motion-control", "运动控制计算域")
            service.create_user("ai-sender", "AI 域发送代理", "gateway_sender", "ai-perception")
            service.create_user("mc-receiver", "控制域接收代理", "gateway_receiver", "motion-control")
            service.create_user("audit", "审计人员", "auditor")
            service.create_contract("arch", TRAJECTORY_CONTRACT)
            service.freeze_contract("arch", "trajectory-ai-mc", 1)
            service.submit_transfer("ai-sender", submission("traj-15", "a", "k-15"))
            first.close()
            reopened = connect(database)
            try:
                resumed = GatewayService(reopened, clock)
                pending = resumed.pending_transfers("mc-receiver")
                self.assertEqual([item["transfer_id"] for item in pending["pending"]], ["traj-15"])
                consumed = resumed.confirm_consumption("mc-receiver", "traj-15", confirmation("a"))
                self.assertEqual(consumed["state"], "consumed")
                self.assertEqual(resumed.pending_transfers("mc-receiver")["count"], 0)
            finally:
                reopened.close()

    def test_trace_reconstructs_cross_domain_decision(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-16", "a", "k-16"))
        self.service.confirm_consumption("mc-receiver", "traj-16", confirmation("a"))
        trace = self.service.trace_transfer("audit", "traj-16")
        self.assertEqual(trace["direction"], {"source_domain": "ai-perception", "target_domain": "motion-control"})
        self.assertEqual(trace["released_by"], "arch")
        self.assertEqual(trace["adopted_version"], "1.1")
        self.assertEqual(trace["consumption"]["confirmed_by"], "mc-receiver")
        event_types = [event["event_type"] for event in trace["events"]]
        self.assertEqual(
            event_types,
            ["contract.created", "contract.frozen", "transfer.submitted", "transfer.consumed"],
        )
        with self.assertRaises(Forbidden):
            self.service.trace_transfer("ai-sender", "traj-16")
        with self.assertRaises(NotFound):
            self.service.trace_transfer("audit", "traj-missing")

    def test_transfer_read_is_scoped_to_participating_domain(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-17", "a", "k-17"))
        self.assertEqual(self.service.get_transfer("ai-sender", "traj-17")["state"], "issued")
        with self.assertRaises(Forbidden):
            self.service.get_transfer("mc-sender", "traj-17")

    def test_audit_chain_detects_tampering(self) -> None:
        self.service.submit_transfer("ai-sender", submission("traj-18", "a", "k-18"))
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE gateway_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_contract("ai-sender", TRAJECTORY_CONTRACT | {"contract_id": "other"})
        with self.assertRaises(Forbidden):
            self.service.revoke_contract("mc-receiver", "trajectory-ai-mc", 2, "越权")
        with self.assertRaises(Forbidden):
            self.service.submit_transfer("audit", submission("traj-19", "a", "k-19"))
        with self.assertRaises(Forbidden):
            self.service.audit_chain("ai-sender")


class GatewayApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.app = JsonApplication(GatewayService(self.connection, self.clock))
        self.arch = {"X-Actor-Id": "arch"}
        self.app.handle("POST", "/users", body=json.dumps(
            {"user_id": "arch", "display_name": "电子架构负责人", "role": "architect"}
        ).encode())
        for domain_id, name in (("ai-perception", "AI 感知计算域"), ("motion-control", "运动控制计算域")):
            self.app.handle("POST", "/domains", self.arch, json.dumps({"domain_id": domain_id, "name": name}).encode())
        for user_id, role, domain_id in (
            ("ai-sender", "gateway_sender", "ai-perception"),
            ("mc-receiver", "gateway_receiver", "motion-control"),
            ("audit", "auditor", None),
        ):
            payload = {"user_id": user_id, "display_name": user_id, "role": role, "domain_id": domain_id}
            self.app.handle("POST", "/users", body=json.dumps(payload).encode())

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_full_cross_domain_flow_over_http(self) -> None:
        created = self.app.handle("POST", "/contracts", self.arch, json.dumps(TRAJECTORY_CONTRACT).encode())
        self.assertEqual(created.status, 201)
        frozen = self.app.handle("POST", "/contracts/trajectory-ai-mc/freeze", self.arch,
                                   json.dumps({"expected_revision": 1}).encode())
        self.assertEqual(frozen.body["state"], "frozen")
        submitted = self.app.handle("POST", "/transfers", {"X-Actor-Id": "ai-sender"},
                                    json.dumps(submission("traj-api-1", "a", "k-api-1")).encode())
        self.assertEqual(submitted.status, 201)
        pending = self.app.handle("GET", "/transfers/pending", {"X-Actor-Id": "mc-receiver"})
        self.assertEqual(pending.body["count"], 1)
        confirmed = self.app.handle("POST", "/transfers/traj-api-1/confirm", {"X-Actor-Id": "mc-receiver"},
                                    json.dumps(confirmation("a")).encode())
        self.assertEqual(confirmed.status, 200)
        self.assertEqual(confirmed.body["adopted_version"], "1.1")
        trace = self.app.handle("GET", "/transfers/traj-api-1/trace", {"X-Actor-Id": "audit"})
        self.assertEqual(trace.body["released_by"], "arch")
        chain = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "audit"})
        self.assertTrue(chain.body["valid"])

    def test_error_shape_and_missing_actor(self) -> None:
        response = self.app.handle("POST", "/contracts", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("GET", "/contracts/trajectory-ai-mc")
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/contracts/none", self.arch)
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
