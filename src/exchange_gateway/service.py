"""隔离交换闸口：契约冻结、传递票据、消费确认与审计还原的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, digest
from .models import ContractDraft, TicketRequest, domain_name, required_text, sha256_text
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "architect": {"contract.write", "contract.freeze", "contract.revoke"},
    "sender": {"ticket.issue"},
    "receiver": {"ticket.read", "transfer.consume"},
    "auditor": {"ticket.read", "trace.read", "audit.read"},
}

SYSTEM_ACTOR = "system"


class GatewayService:
    """在单个 SQLite 连接上提供跨域隔离交换的全部业务操作。"""

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

    def create_user(
        self, user_id: str, display_name: str, role: str, domain: str = ""
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        domain = domain.strip()
        if role in {"sender", "receiver"}:
            domain = domain_name(domain, "domain")
        elif domain:
            domain = domain_name(domain, "domain")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO gateway_users(user_id,display_name,role,domain,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, domain, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "domain": domain}

    @staticmethod
    def _contract_entity(contract_id: str, version: int) -> str:
        return f"{contract_id}@{version}"

    def create_contract(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "contract.write")
        draft = ContractDraft.from_dict(raw)
        field_set = list(draft.field_set)
        field_set_sha256 = digest(field_set)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO message_contracts(contract_id,version,message_kind,source_domain,target_domain,"
                    "field_set_json,field_set_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        draft.contract_id,
                        draft.version,
                        draft.message_kind,
                        draft.source_domain,
                        draft.target_domain,
                        canonical_json(field_set),
                        field_set_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "contract",
                    self._contract_entity(draft.contract_id, draft.version),
                    "contract.created",
                    actor_id,
                    {
                        "message_kind": draft.message_kind,
                        "source_domain": draft.source_domain,
                        "target_domain": draft.target_domain,
                        "field_set": field_set,
                        "field_set_sha256": field_set_sha256,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("契约版本已经存在") from exc
        return {
            "contract_id": draft.contract_id,
            "version": draft.version,
            "state": "draft",
            "revision": 1,
            "field_set_sha256": field_set_sha256,
        }

    def _contract(self, contract_id: str, version: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM message_contracts WHERE contract_id=? AND version=?",
            (contract_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("消息契约版本不存在")
        return row

    def get_contract(self, actor_id: str, contract_id: str, version: int) -> dict[str, Any]:
        self._require(actor_id, "ticket.read")
        return self._contract_view(self._contract(contract_id, version))

    @staticmethod
    def _contract_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "contract_id": row["contract_id"],
            "version": row["version"],
            "message_kind": row["message_kind"],
            "source_domain": row["source_domain"],
            "target_domain": row["target_domain"],
            "field_set": json.loads(row["field_set_json"]),
            "field_set_sha256": row["field_set_sha256"],
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

    def freeze_contract(
        self, actor_id: str, contract_id: str, version: int, expected_revision: int
    ) -> dict[str, Any]:
        self._require(actor_id, "contract.freeze")
        self._contract(contract_id, version)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE message_contracts SET state='frozen',revision=revision+1,frozen_by=?,frozen_at=? "
                "WHERE contract_id=? AND version=? AND state='draft' AND revision=?",
                (actor_id, self._now(), contract_id, version, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("契约不是当前草稿版本")
            self._audit(
                "contract",
                self._contract_entity(contract_id, version),
                "contract.frozen",
                actor_id,
                {"from_revision": expected_revision},
            )
        return {
            "contract_id": contract_id,
            "version": version,
            "state": "frozen",
            "revision": expected_revision + 1,
        }

    def revoke_contract(
        self, actor_id: str, contract_id: str, version: int, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "contract.revoke")
        self._contract(contract_id, version)
        reason = required_text(reason, "reason", 256)
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE message_contracts SET state='revoked',revision=revision+1,"
                "revoked_by=?,revoked_at=?,revoke_reason=? "
                "WHERE contract_id=? AND version=? AND state='frozen'",
                (actor_id, now, reason, contract_id, version),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有已冻结契约可以撤回")
            pending = self.connection.execute(
                "SELECT ticket_id FROM transfer_tickets "
                "WHERE contract_id=? AND contract_version=? AND state='issued' ORDER BY ticket_id",
                (contract_id, version),
            ).fetchall()
            blocked: list[str] = []
            for row in pending:
                updated = self.connection.execute(
                    "UPDATE transfer_tickets SET state='blocked',revision=revision+1,blocked_reason=? "
                    "WHERE ticket_id=? AND state='issued'",
                    (f"契约撤回: {reason}", row["ticket_id"]),
                )
                if updated.rowcount == 1:
                    blocked.append(row["ticket_id"])
                    self._audit(
                        "ticket",
                        row["ticket_id"],
                        "ticket.blocked",
                        actor_id,
                        {"contract_id": contract_id, "contract_version": version, "reason": reason},
                    )
            self._audit(
                "contract",
                self._contract_entity(contract_id, version),
                "contract.revoked",
                actor_id,
                {"reason": reason, "blocked_tickets": blocked},
            )
        return {
            "contract_id": contract_id,
            "version": version,
            "state": "revoked",
            "blocked_tickets": blocked,
        }

    def _ticket(self, ticket_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM transfer_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise NotFound("传递票据不存在")
        return row

    @staticmethod
    def _ticket_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ticket_id": row["ticket_id"],
            "contract_id": row["contract_id"],
            "contract_version": row["contract_version"],
            "message_kind": row["message_kind"],
            "source_domain": row["source_domain"],
            "target_domain": row["target_domain"],
            "fields": json.loads(row["fields_json"]),
            "content_sha256": row["content_sha256"],
            "state": row["state"],
            "revision": row["revision"],
            "issued_by": row["issued_by"],
            "issued_at": row["issued_at"],
            "expires_at": row["expires_at"],
            "consumed_at": row["consumed_at"],
            "blocked_reason": row["blocked_reason"],
        }

    def _materialize_expiry(self, ticket: sqlite3.Row) -> sqlite3.Row:
        """把已到期的签发中票据落为 expired；乱序回执只能看到终态，不能复活。"""

        if ticket["state"] == "issued" and ticket["expires_at"] <= self._now():
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE transfer_tickets SET state='expired',revision=revision+1 "
                    "WHERE ticket_id=? AND state='issued'",
                    (ticket["ticket_id"],),
                )
                if cursor.rowcount == 1:
                    self._audit(
                        "ticket",
                        ticket["ticket_id"],
                        "ticket.expired",
                        SYSTEM_ACTOR,
                        {"expires_at": ticket["expires_at"]},
                    )
            return self._ticket(ticket["ticket_id"])
        return ticket

    def issue_ticket(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "ticket.issue")
        request = TicketRequest.from_dict(raw)
        request_digest = digest(
            {
                "ticket_id": request.ticket_id,
                "contract_id": request.contract_id,
                "contract_version": request.contract_version,
                "fields": list(request.fields),
                "content_sha256": request.content_sha256,
                "ttl_seconds": request.ttl_seconds,
            }
        )
        existing = self.connection.execute(
            "SELECT * FROM transfer_tickets WHERE idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["request_sha256"] != request_digest:
                raise Conflict("同一幂等键对应不同传递内容")
            return self._ticket_view(existing) | {"replayed": True}
        contract = self._contract(request.contract_id, request.contract_version)
        if contract["state"] != "frozen":
            raise InvalidState("契约未冻结或已撤回，不能签发传递票据")
        if user["domain"] != contract["source_domain"]:
            raise Forbidden("发送方域身份与契约方向不符")
        allowed = set(json.loads(contract["field_set_json"]))
        unknown = [name for name in request.fields if name not in allowed]
        if unknown:
            raise ValidationFailed(f"字段超出契约字段最小集: {', '.join(unknown)}")
        now = self.clock.now()
        issued_at = utc_text(now)
        expires_at = utc_text(now + timedelta(seconds=request.ttl_seconds))
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO transfer_tickets(ticket_id,contract_id,contract_version,message_kind,"
                    "source_domain,target_domain,fields_json,content_sha256,request_sha256,idempotency_key,"
                    "issued_by,issued_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.ticket_id,
                        request.contract_id,
                        request.contract_version,
                        contract["message_kind"],
                        contract["source_domain"],
                        contract["target_domain"],
                        canonical_json(list(request.fields)),
                        request.content_sha256,
                        request_digest,
                        request.idempotency_key,
                        actor_id,
                        issued_at,
                        expires_at,
                    ),
                )
                self._audit(
                    "ticket",
                    request.ticket_id,
                    "ticket.issued",
                    actor_id,
                    {
                        "contract_id": request.contract_id,
                        "contract_version": request.contract_version,
                        "content_sha256": request.content_sha256,
                        "fields": list(request.fields),
                        "expires_at": expires_at,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("票据编号或幂等键冲突，同一编号不能承载不同内容") from exc
        return self._ticket_view(self._ticket(request.ticket_id)) | {"replayed": False}

    def get_ticket(self, actor_id: str, ticket_id: str) -> dict[str, Any]:
        self._require(actor_id, "ticket.read")
        return self._ticket_view(self._materialize_expiry(self._ticket(ticket_id)))

    def pending_tickets(self, actor_id: str) -> dict[str, Any]:
        """接收方查看本域仍可消费的未决交接，重启后据此继续处理。"""

        user = self._require(actor_id, "transfer.consume")
        rows = self.connection.execute(
            "SELECT * FROM transfer_tickets WHERE target_domain=? AND state='issued' "
            "ORDER BY expires_at,ticket_id",
            (user["domain"],),
        ).fetchall()
        tickets = []
        for row in rows:
            row = self._materialize_expiry(row)
            if row["state"] == "issued":
                tickets.append(self._ticket_view(row))
        return {"target_domain": user["domain"], "tickets": tickets}

    def _receipt_view(self, receipt: sqlite3.Row, replayed: bool) -> dict[str, Any]:
        return {
            "receipt_id": receipt["receipt_id"],
            "ticket_id": receipt["ticket_id"],
            "state": "consumed",
            "contract_id": receipt["contract_id"],
            "contract_version": receipt["contract_version"],
            "content_sha256": receipt["content_sha256"],
            "confirmed_by": receipt["confirmed_by"],
            "confirmed_at": receipt["confirmed_at"],
            "replayed": replayed,
        }

    def confirm_consumption(
        self,
        actor_id: str,
        ticket_id: str,
        content_sha256: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        user = self._require(actor_id, "transfer.consume")
        content_sha256 = sha256_text(content_sha256)
        ticket = self._materialize_expiry(self._ticket(ticket_id))
        if user["domain"] != ticket["target_domain"]:
            raise Forbidden("接收方域身份与票据目标域不符")
        if ticket["state"] == "consumed":
            if ticket["content_sha256"] != content_sha256:
                raise Conflict("同一编号承载不同内容")
            receipt = self.connection.execute(
                "SELECT * FROM consumption_receipts WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            return self._receipt_view(receipt, replayed=True)
        if ticket["state"] == "expired":
            raise InvalidState("传递票据已过期，乱序回执不能复活")
        if ticket["state"] == "blocked":
            raise InvalidState("传递票据已因安全规则撤回被阻止")
        contract = self._contract(ticket["contract_id"], ticket["contract_version"])
        if contract["state"] != "frozen":
            raise InvalidState("契约已撤回，不能确认消费")
        if ticket["content_sha256"] != content_sha256:
            raise Conflict("确认内容与票据摘要不符")
        if ticket["revision"] != expected_revision:
            raise InvalidState("票据版本已变化")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE transfer_tickets SET state='consumed',consumed_at=?,revision=revision+1 "
                "WHERE ticket_id=? AND state='issued' AND revision=?",
                (now, ticket_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("票据状态已变化")
            receipt_cursor = self.connection.execute(
                "INSERT INTO consumption_receipts(ticket_id,contract_id,contract_version,content_sha256,"
                "ticket_revision,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    ticket["contract_id"],
                    ticket["contract_version"],
                    ticket["content_sha256"],
                    expected_revision,
                    actor_id,
                    now,
                ),
            )
            self._audit(
                "ticket",
                ticket_id,
                "transfer.consumed",
                actor_id,
                {
                    "receipt_id": receipt_cursor.lastrowid,
                    "contract_id": ticket["contract_id"],
                    "contract_version": ticket["contract_version"],
                    "content_sha256": ticket["content_sha256"],
                    "target_domain": ticket["target_domain"],
                },
            )
        receipt = self.connection.execute(
            "SELECT * FROM consumption_receipts WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        return self._receipt_view(receipt, replayed=False)

    def transfer_trace(self, actor_id: str, ticket_id: str) -> dict[str, Any]:
        """审计还原：一次跨域传递的契约、放行人、接收确认与实际采用版本。"""

        self._require(actor_id, "trace.read")
        ticket = self._materialize_expiry(self._ticket(ticket_id))
        contract = self._contract(ticket["contract_id"], ticket["contract_version"])
        receipt = self.connection.execute(
            "SELECT * FROM consumption_receipts WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        contract_entity = self._contract_entity(contract["contract_id"], contract["version"])
        events = self.connection.execute(
            "SELECT * FROM gateway_audit_events WHERE "
            "(entity_type='ticket' AND entity_id=?) OR (entity_type='contract' AND entity_id=?) "
            "ORDER BY event_id",
            (ticket_id, contract_entity),
        ).fetchall()
        return {
            "ticket": self._ticket_view(ticket),
            "contract": self._contract_view(contract),
            "receipt": None if receipt is None else self._receipt_view(receipt, replayed=False),
            "crossing": {
                "message_kind": ticket["message_kind"],
                "source_domain": ticket["source_domain"],
                "target_domain": ticket["target_domain"],
                "content_sha256": ticket["content_sha256"],
                "contract_frozen_by": contract["frozen_by"],
                "ticket_issued_by": ticket["issued_by"],
                "consumed_by": None if receipt is None else receipt["confirmed_by"],
                "adopted_contract_version": None if receipt is None else receipt["contract_version"],
            },
            "events": [
                dict(row) | {"payload": json.loads(row["payload_json"])} for row in events
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
