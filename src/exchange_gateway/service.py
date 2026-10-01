"""隔离交换闸口的领域用例：契约冻结、票据签发、消费确认、撤回与审计还原。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import Confirmation, ContractDraft, TransferSubmission
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "architect": {"domain.register", "contract.write"},
    "gateway_sender": {"transfer.submit", "transfer.read"},
    "gateway_receiver": {"transfer.confirm", "transfer.read"},
    "auditor": {"transfer.read", "trace.read", "audit.read"},
}

DOMAIN_ROLES = {"gateway_sender", "gateway_receiver"}
SUBMIT_SCOPE = "transfer-submit"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class GatewayService:
    """在单个 SQLite 连接上提供隔离交换闸口的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM gateway_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM gateway_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO gateway_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ------------------------------------------------------------------
    # 用户与计算域
    # ------------------------------------------------------------------

    def create_user(
        self, user_id: str, display_name: str, role: str, domain_id: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role in DOMAIN_ROLES:
            if domain_id is None:
                raise ValidationFailed("发送方和接收方必须绑定计算域")
            domain = self.connection.execute(
                "SELECT domain_id FROM exchange_domains WHERE domain_id=?", (domain_id,)
            ).fetchone()
            if domain is None:
                raise NotFound("计算域不存在")
        elif domain_id is not None:
            raise ValidationFailed("架构人员和审计人员不绑定计算域")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO gateway_users(user_id,display_name,role,domain_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, domain_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "domain_id": domain_id}

    def register_domain(self, actor_id: str, domain_id: str, name: str) -> dict[str, Any]:
        self._require(actor_id, "domain.register")
        if not domain_id.strip() or not name.strip():
            raise ValidationFailed("计算域编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO exchange_domains(domain_id,name,created_by,created_at) VALUES(?,?,?,?)",
                    (domain_id.strip(), name.strip(), actor_id, self._now()),
                )
                self._audit("domain", domain_id.strip(), "domain.registered", actor_id, {"name": name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("计算域已经存在") from exc
        return {"domain_id": domain_id.strip(), "name": name.strip()}

    # ------------------------------------------------------------------
    # 消息契约
    # ------------------------------------------------------------------

    def _contract_row(self, contract_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM message_contracts WHERE contract_id=?", (contract_id,)
        ).fetchone()
        if row is None:
            raise NotFound("消息契约不存在")
        return row

    @staticmethod
    def _contract_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "contract_id": row["contract_id"],
            "message_kind": row["message_kind"],
            "source_domain": row["source_domain"],
            "target_domain": row["target_domain"],
            "required_fields": json.loads(row["required_fields_json"]),
            "allowed_versions": json.loads(row["allowed_versions_json"]),
            "ticket_ttl_seconds": row["ticket_ttl_seconds"],
            "state": row["state"],
            "revision": row["revision"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "frozen_by": row["frozen_by"],
            "frozen_at": row["frozen_at"],
            "revoked_by": row["revoked_by"],
            "revoked_at": row["revoked_at"],
            "revoke_reason": row["revoke_reason"],
        }

    def create_contract(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "contract.write")
        draft = ContractDraft.from_dict(raw)
        for domain_id in (draft.source_domain, draft.target_domain):
            domain = self.connection.execute(
                "SELECT domain_id FROM exchange_domains WHERE domain_id=?", (domain_id,)
            ).fetchone()
            if domain is None:
                raise NotFound(f"计算域不存在: {domain_id}")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO message_contracts(contract_id,message_kind,source_domain,target_domain,"
                    "required_fields_json,allowed_versions_json,ticket_ttl_seconds,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        draft.contract_id,
                        draft.message_kind,
                        draft.source_domain,
                        draft.target_domain,
                        canonical_json(list(draft.required_fields)),
                        canonical_json(list(draft.allowed_versions)),
                        draft.ticket_ttl_seconds,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "contract",
                    draft.contract_id,
                    "contract.created",
                    actor_id,
                    {
                        "message_kind": draft.message_kind,
                        "source_domain": draft.source_domain,
                        "target_domain": draft.target_domain,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("契约编号已经存在") from exc
        return self._contract_view(self._contract_row(draft.contract_id))

    def get_contract(self, actor_id: str, contract_id: str) -> dict[str, Any]:
        self._user(actor_id)
        return self._contract_view(self._contract_row(contract_id))

    def freeze_contract(self, actor_id: str, contract_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "contract.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE message_contracts SET state='frozen',revision=revision+1,frozen_by=?,frozen_at=? "
                "WHERE contract_id=? AND state='draft' AND revision=?",
                (actor_id, self._now(), contract_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("契约不是当前草稿版本")
            self._audit("contract", contract_id, "contract.frozen", actor_id, {"from_revision": expected_revision})
        return self._contract_view(self._contract_row(contract_id))

    def revoke_contract(
        self, actor_id: str, contract_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "contract.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE message_contracts SET state='revoked',revision=revision+1,"
                "revoked_by=?,revoked_at=?,revoke_reason=? "
                "WHERE contract_id=? AND state='frozen' AND revision=?",
                (actor_id, self._now(), reason.strip(), contract_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有已冻结契约的当前版本可以撤回")
            blocked = self.connection.execute(
                "SELECT transfer_id,revision FROM transfer_tickets WHERE contract_id=? AND state='issued'",
                (contract_id,),
            ).fetchall()
            for ticket in blocked:
                self.connection.execute(
                    "UPDATE transfer_tickets SET state='blocked',revision=revision+1 "
                    "WHERE transfer_id=? AND state='issued'",
                    (ticket["transfer_id"],),
                )
                self._audit(
                    "transfer",
                    ticket["transfer_id"],
                    "transfer.blocked",
                    actor_id,
                    {"contract_id": contract_id, "reason": reason.strip()},
                )
            self._audit(
                "contract",
                contract_id,
                "contract.revoked",
                actor_id,
                {"reason": reason.strip(), "blocked_transfers": len(blocked)},
            )
        view = self._contract_view(self._contract_row(contract_id))
        view["blocked_transfers"] = len(blocked)
        return view

    # ------------------------------------------------------------------
    # 传递票据
    # ------------------------------------------------------------------

    @staticmethod
    def _ticket_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "transfer_id": row["transfer_id"],
            "contract_id": row["contract_id"],
            "contract_revision": row["contract_revision"],
            "message_kind": row["message_kind"],
            "source_domain": row["source_domain"],
            "target_domain": row["target_domain"],
            "payload_version": row["payload_version"],
            "content_sha256": row["content_sha256"],
            "declared_fields": json.loads(row["declared_fields_json"]),
            "state": row["state"],
            "revision": row["revision"],
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _submission_digest(submission: TransferSubmission) -> str:
        return digest({
            "transfer_id": submission.transfer_id,
            "contract_id": submission.contract_id,
            "payload_version": submission.payload_version,
            "content_sha256": submission.content_sha256,
            "fields": list(submission.fields),
        })

    def _stored_response(self, idempotency_key: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT response_json FROM gateway_idempotency WHERE scope=? AND idempotency_key=?",
            (SUBMIT_SCOPE, idempotency_key),
        ).fetchone()
        if row is None:
            raise NotFound("原始提交记录不存在")
        return json.loads(row["response_json"])

    def submit_transfer(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "transfer.submit")
        submission = TransferSubmission.from_dict(raw)
        request_digest = self._submission_digest(submission)
        stored = self.connection.execute(
            "SELECT request_sha256 FROM gateway_idempotency WHERE scope=? AND idempotency_key=?",
            (SUBMIT_SCOPE, submission.idempotency_key),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("同一幂等键对应不同提交内容")
            return self._stored_response(submission.idempotency_key)
        existing = self.connection.execute(
            "SELECT * FROM transfer_tickets WHERE transfer_id=?", (submission.transfer_id,)
        ).fetchone()
        if existing is not None:
            if existing["request_sha256"] != request_digest:
                raise Conflict("同一编号承载不同内容")
            response = self._stored_response(existing["idempotency_key"])
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO gateway_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (SUBMIT_SCOPE, submission.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
            return response
        contract = self._contract_row(submission.contract_id)
        if contract["state"] != "frozen":
            raise InvalidState("契约未冻结，不能签发传递票据")
        if user["domain_id"] != contract["source_domain"]:
            raise Forbidden("发送方域身份与契约源域不符")
        allowed_versions = json.loads(contract["allowed_versions_json"])
        if submission.payload_version not in allowed_versions:
            raise ValidationFailed("payload_version 不在契约有效版本范围内")
        missing = sorted(set(json.loads(contract["required_fields_json"])) - set(submission.fields))
        if missing:
            raise ValidationFailed(f"内容缺少契约字段最小集: {missing}")
        now = self.clock.now()
        expires_at = utc_text(now + timedelta(seconds=contract["ticket_ttl_seconds"]))
        response = {
            "transfer_id": submission.transfer_id,
            "contract_id": contract["contract_id"],
            "contract_revision": contract["revision"],
            "message_kind": contract["message_kind"],
            "source_domain": contract["source_domain"],
            "target_domain": contract["target_domain"],
            "payload_version": submission.payload_version,
            "state": "issued",
            "revision": 1,
            "expires_at": expires_at,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO transfer_tickets(transfer_id,contract_id,contract_revision,message_kind,"
                    "source_domain,target_domain,payload_version,content_sha256,declared_fields_json,"
                    "idempotency_key,request_sha256,submitted_by,submitted_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        submission.transfer_id,
                        contract["contract_id"],
                        contract["revision"],
                        contract["message_kind"],
                        contract["source_domain"],
                        contract["target_domain"],
                        submission.payload_version,
                        submission.content_sha256,
                        canonical_json(list(submission.fields)),
                        submission.idempotency_key,
                        request_digest,
                        actor_id,
                        self._now(),
                        expires_at,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO gateway_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (SUBMIT_SCOPE, submission.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "transfer",
                    submission.transfer_id,
                    "transfer.submitted",
                    actor_id,
                    {
                        "contract_id": contract["contract_id"],
                        "payload_version": submission.payload_version,
                        "content_sha256": submission.content_sha256,
                        "expires_at": expires_at,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("传递编号或幂等键冲突") from exc
        return response

    def _ticket_row(self, transfer_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM transfer_tickets WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if row is None:
            raise NotFound("传递票据不存在")
        return row

    def _expire_due(self) -> list[str]:
        now = self._now()
        due = self.connection.execute(
            "SELECT transfer_id FROM transfer_tickets WHERE state='issued' AND expires_at<=? "
            "ORDER BY transfer_id",
            (now,),
        ).fetchall()
        for ticket in due:
            self.connection.execute(
                "UPDATE transfer_tickets SET state='expired',revision=revision+1 "
                "WHERE transfer_id=? AND state='issued'",
                (ticket["transfer_id"],),
            )
            self._audit("transfer", ticket["transfer_id"], "transfer.expired", "system", {})
        return [ticket["transfer_id"] for ticket in due]

    def confirm_consumption(
        self, actor_id: str, transfer_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        user = self._require(actor_id, "transfer.confirm")
        confirmation = Confirmation.from_dict(raw)
        if user["domain_id"] is None or user["domain_id"] != confirmation.domain_id:
            raise Forbidden("接收方域身份与登记信息不符")
        with transaction(self.connection, immediate=True):
            self._expire_due()
            ticket = self._ticket_row(transfer_id)
            contract = self._contract_row(ticket["contract_id"])
            if ticket["state"] == "consumed":
                fact = self.connection.execute(
                    "SELECT * FROM consumption_facts WHERE transfer_id=?", (transfer_id,)
                ).fetchone()
                if (
                    fact["content_sha256"] == confirmation.content_sha256
                    and fact["target_domain"] == confirmation.domain_id
                ):
                    return {
                        "transfer_id": transfer_id,
                        "state": "consumed",
                        "revision": ticket["revision"],
                        "consumption_id": fact["consumption_id"],
                        "adopted_version": fact["payload_version"],
                        "confirmed_by": fact["confirmed_by"],
                        "confirmed_at": fact["confirmed_at"],
                        "replayed": True,
                    }
                raise Conflict("回执内容与已登记的消费事实不符")
            if ticket["state"] != "issued":
                raise InvalidState("票据已过期或已被阻止，乱序回执不能恢复消费")
            if contract["state"] != "frozen":
                raise InvalidState("契约已被撤回，未完成传递立即终止")
            if (
                contract["revision"] != ticket["contract_revision"]
                or contract["revision"] != confirmation.expected_contract_revision
            ):
                raise InvalidState("契约已变化，票据不再有效")
            if (
                confirmation.domain_id != ticket["target_domain"]
                or contract["target_domain"] != ticket["target_domain"]
                or contract["source_domain"] != ticket["source_domain"]
            ):
                raise Forbidden("接收方域身份与票据目标域不符")
            if ticket["revision"] != confirmation.expected_ticket_revision:
                raise InvalidState("票据版本已变化")
            if ticket["expires_at"] <= self._now():
                raise InvalidState("票据已经过期")
            if ticket["content_sha256"] != confirmation.content_sha256:
                raise Conflict("回执内容摘要与票据登记不一致")
            now = self._now()
            cursor = self.connection.execute(
                "UPDATE transfer_tickets SET state='consumed',revision=revision+1 "
                "WHERE transfer_id=? AND state='issued' AND revision=?",
                (transfer_id, confirmation.expected_ticket_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("票据状态已变化")
            insert = self.connection.execute(
                "INSERT INTO consumption_facts(transfer_id,contract_id,contract_revision,payload_version,"
                "content_sha256,source_domain,target_domain,confirmed_by,confirmed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    ticket["contract_id"],
                    ticket["contract_revision"],
                    ticket["payload_version"],
                    ticket["content_sha256"],
                    ticket["source_domain"],
                    ticket["target_domain"],
                    actor_id,
                    now,
                ),
            )
            consumption_id = int(insert.lastrowid)
            self._audit(
                "transfer",
                transfer_id,
                "transfer.consumed",
                actor_id,
                {
                    "contract_id": ticket["contract_id"],
                    "adopted_version": ticket["payload_version"],
                    "consumption_id": consumption_id,
                },
            )
        return {
            "transfer_id": transfer_id,
            "state": "consumed",
            "revision": confirmation.expected_ticket_revision + 1,
            "consumption_id": consumption_id,
            "adopted_version": ticket["payload_version"],
            "confirmed_by": actor_id,
            "confirmed_at": now,
            "replayed": False,
        }

    def get_transfer(self, actor_id: str, transfer_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "transfer.read")
        with transaction(self.connection, immediate=True):
            self._expire_due()
            ticket = self._ticket_row(transfer_id)
        if user["role"] == "gateway_sender" and user["domain_id"] != ticket["source_domain"]:
            raise Forbidden("发送方只能读取本域发出的传递")
        if user["role"] == "gateway_receiver" and user["domain_id"] != ticket["target_domain"]:
            raise Forbidden("接收方只能读取发往本域的传递")
        return self._ticket_view(ticket)

    def pending_transfers(self, actor_id: str, domain_id: str | None = None) -> dict[str, Any]:
        user = self._require(actor_id, "transfer.read")
        with transaction(self.connection, immediate=True):
            self._expire_due()
            if user["role"] == "auditor":
                if domain_id is None:
                    rows = self.connection.execute(
                        "SELECT * FROM transfer_tickets WHERE state='issued' ORDER BY expires_at,transfer_id"
                    ).fetchall()
                else:
                    rows = self.connection.execute(
                        "SELECT * FROM transfer_tickets WHERE state='issued' AND "
                        "(source_domain=? OR target_domain=?) ORDER BY expires_at,transfer_id",
                        (domain_id, domain_id),
                    ).fetchall()
            elif user["role"] == "gateway_sender":
                rows = self.connection.execute(
                    "SELECT * FROM transfer_tickets WHERE state='issued' AND source_domain=? "
                    "ORDER BY expires_at,transfer_id",
                    (user["domain_id"],),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM transfer_tickets WHERE state='issued' AND target_domain=? "
                    "ORDER BY expires_at,transfer_id",
                    (user["domain_id"],),
                ).fetchall()
        return {"pending": [self._ticket_view(row) for row in rows], "count": len(rows)}

    # ------------------------------------------------------------------
    # 审计还原
    # ------------------------------------------------------------------

    def trace_transfer(self, actor_id: str, transfer_id: str) -> dict[str, Any]:
        self._require(actor_id, "trace.read")
        ticket = self._ticket_row(transfer_id)
        contract = self._contract_row(ticket["contract_id"])
        fact = self.connection.execute(
            "SELECT * FROM consumption_facts WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM gateway_audit_events "
            "WHERE (entity_type='transfer' AND entity_id=?) OR (entity_type='contract' AND entity_id=?) "
            "ORDER BY event_id",
            (transfer_id, ticket["contract_id"]),
        ).fetchall()
        consumption = None
        if fact is not None:
            consumption = {
                "consumption_id": fact["consumption_id"],
                "confirmed_by": fact["confirmed_by"],
                "confirmed_at": fact["confirmed_at"],
                "adopted_version": fact["payload_version"],
                "contract_revision": fact["contract_revision"],
                "content_sha256": fact["content_sha256"],
            }
        return {
            "transfer_id": transfer_id,
            "message_kind": ticket["message_kind"],
            "direction": {
                "source_domain": ticket["source_domain"],
                "target_domain": ticket["target_domain"],
            },
            "contract": self._contract_view(contract),
            "ticket": self._ticket_view(ticket),
            "released_by": contract["frozen_by"],
            "consumption": consumption,
            "adopted_version": None if fact is None else fact["payload_version"],
            "events": [
                {
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in events
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM gateway_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
