"""隔离交换闸口的输入契约与校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
MESSAGE_KINDS = {"target_trajectory", "environment_summary", "execution_receipt"}
MAX_FIELDS = 64
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


def domain_name(value: object, field: str = "domain") -> str:
    return identifier(value, field)


def sha256_text(value: object, field: str = "content_sha256") -> str:
    result = required_text(value, field, 64).lower()
    if not SHA256_HEX.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是 64 位小写 SHA-256 摘要")
    return result


def positive_integer(value: object, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    if maximum is not None and value > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return value


def field_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed(f"{field} 必须是非空字段数组")
    if len(value) > MAX_FIELDS:
        raise ValidationFailed(f"{field} 不能超过 {MAX_FIELDS} 个字段")
    names: list[str] = []
    for item in value:
        name = required_text(item, f"{field} 元素", 64)
        if not FIELD_NAME.fullmatch(name):
            raise ValidationFailed(f"{field} 元素 {name} 格式不正确")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValidationFailed(f"{field} 存在重复字段")
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class ContractDraft:
    """架构人员登记的消息契约草稿，冻结后不可更改。"""

    contract_id: str
    version: int
    message_kind: str
    source_domain: str
    target_domain: str
    field_set: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContractDraft":
        message_kind = required_text(raw.get("message_kind"), "message_kind", 32)
        if message_kind not in MESSAGE_KINDS:
            raise ValidationFailed("message_kind 必须是 target_trajectory、environment_summary 或 execution_receipt")
        source_domain = domain_name(raw.get("source_domain"), "source_domain")
        target_domain = domain_name(raw.get("target_domain"), "target_domain")
        if source_domain == target_domain:
            raise ValidationFailed("源计算域与目标计算域不能相同")
        return cls(
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            version=positive_integer(raw.get("version"), "version"),
            message_kind=message_kind,
            source_domain=source_domain,
            target_domain=target_domain,
            field_set=field_list(raw.get("field_set"), "field_set"),
        )


@dataclass(frozen=True, slots=True)
class TicketRequest:
    """发送方提交内容摘要后申请的有期限传递票据。"""

    ticket_id: str
    contract_id: str
    contract_version: int
    fields: tuple[str, ...]
    content_sha256: str
    ttl_seconds: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TicketRequest":
        return cls(
            ticket_id=identifier(raw.get("ticket_id"), "ticket_id"),
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            contract_version=positive_integer(raw.get("contract_version"), "contract_version"),
            fields=field_list(raw.get("fields"), "fields"),
            content_sha256=sha256_text(raw.get("content_sha256")),
            ttl_seconds=positive_integer(raw.get("ttl_seconds"), "ttl_seconds", MAX_TTL_SECONDS),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
