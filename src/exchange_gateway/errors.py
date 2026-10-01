"""隔离交换闸口服务向 API 和验收暴露的稳定错误。"""


class GatewayError(RuntimeError):
    code = "gateway_error"
    status = 400


class NotFound(GatewayError):
    code = "not_found"
    status = 404


class Conflict(GatewayError):
    code = "conflict"
    status = 409


class Forbidden(GatewayError):
    code = "forbidden"
    status = 403


class InvalidState(GatewayError):
    code = "invalid_state"
    status = 409


class ValidationFailed(GatewayError):
    code = "validation_failed"
    status = 422
