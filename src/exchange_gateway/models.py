"""隔离交换闸口的输入契约：消息契约草稿、传递提交和消费确认。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MESSAGE_KINDS = {"target_trajectory", "environment_summary", "execution_receipt"}
MAX_TTL_SECONDS = 86400


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def sha256_text(value: object, field: str) -> str:
    result = required_text(value, field, 64).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制 SHA-256 摘要")
    return result


def _text_sequence(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationFailed(f"{field} 必须是字符串数组")
    return tuple(required_text(item, f"{field} 元素", 64) for item in value)


def field_set(value: object, field: str) -> tuple[str, ...]:
    items = _text_sequence(value, field)
    if not items:
        raise ValidationFailed(f"{field} 不能为空数组")
    for item in items:
        if not FIELD_NAME.fullmatch(item):
            raise ValidationFailed(f"{field} 含非法字段名 {item}")
    if len(set(items)) != len(items):
        raise ValidationFailed(f"{field} 不能重复")
    return items


def version_set(value: object, field: str) -> tuple[str, ...]:
    items = _text_sequence(value, field)
    if not items:
        raise ValidationFailed(f"{field} 不能为空数组")
    for item in items:
        if not IDENTIFIER.fullmatch(item):
            raise ValidationFailed(f"{field} 含非法版本号 {item}")
    if len(set(items)) != len(items):
        raise ValidationFailed(f"{field} 不能重复")
    return items


def positive_revision(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


@dataclass(frozen=True, slots=True)
class ContractDraft:
    """架构人员登记的消息契约草稿，冻结后内容不可再变。"""

    contract_id: str
    message_kind: str
    source_domain: str
    target_domain: str
    required_fields: tuple[str, ...]
    allowed_versions: tuple[str, ...]
    ticket_ttl_seconds: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContractDraft":
        message_kind = required_text(raw.get("message_kind"), "message_kind", 32)
        if message_kind not in MESSAGE_KINDS:
            raise ValidationFailed("message_kind 必须是 target_trajectory、environment_summary 或 execution_receipt")
        source_domain = identifier(raw.get("source_domain"), "source_domain")
        target_domain = identifier(raw.get("target_domain"), "target_domain")
        if source_domain == target_domain:
            raise ValidationFailed("消息契约的源域和目标域不能相同")
        ttl = raw.get("ticket_ttl_seconds")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= MAX_TTL_SECONDS:
            raise ValidationFailed(f"ticket_ttl_seconds 必须是 1 到 {MAX_TTL_SECONDS} 的整数")
        return cls(
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            message_kind=message_kind,
            source_domain=source_domain,
            target_domain=target_domain,
            required_fields=field_set(raw.get("required_fields"), "required_fields"),
            allowed_versions=version_set(raw.get("allowed_versions"), "allowed_versions"),
            ticket_ttl_seconds=ttl,
        )


@dataclass(frozen=True, slots=True)
class TransferSubmission:
    """发送方提交的内容摘要，闸口据以签发有期限的传递票据。"""

    transfer_id: str
    contract_id: str
    payload_version: str
    content_sha256: str
    fields: tuple[str, ...]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TransferSubmission":
        return cls(
            transfer_id=identifier(raw.get("transfer_id"), "transfer_id"),
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            payload_version=identifier(raw.get("payload_version"), "payload_version"),
            content_sha256=sha256_text(raw.get("content_sha256"), "content_sha256"),
            fields=field_set(raw.get("fields"), "fields"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class Confirmation:
    """接收方确认消费时出示的契约、域身份和票据版本。"""

    domain_id: str
    expected_contract_revision: int
    expected_ticket_revision: int
    content_sha256: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Confirmation":
        return cls(
            domain_id=identifier(raw.get("domain_id"), "domain_id"),
            expected_contract_revision=positive_revision(
                raw.get("expected_contract_revision"), "expected_contract_revision"
            ),
            expected_ticket_revision=positive_revision(
                raw.get("expected_ticket_revision"), "expected_ticket_revision"
            ),
            content_sha256=sha256_text(raw.get("content_sha256"), "content_sha256"),
        )
