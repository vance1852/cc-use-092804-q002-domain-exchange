# 建立控制域与 AI 域隔离交换闸口基础平台

本项目是一套可离线运行的 Python 服务端平台，服务于具身智能机器人控制域、AI 计算域和国产电子部件质量域。平台管理控制计算节点、实时控制总线、资源时隙、机器人构建、验证测量、分析租约、部件批次与质量决定，业务状态、幂等结果和审计事件保存在 SQLite 中。

## 目录

- `src/robot_control/`：控制节点、实时总线、资源批次、时隙申请、容量分配和架构情景；
- `src/embodied_ai/`：机器人构建、验证协议、测量导入、排除复核、分析任务和准入决定；
- `src/component_qualification/`：国产电子部件批次、信号测量、统计分析、账号权限和质量审批；
- `src/exchange_gateway/`：控制域与 AI 域之间的隔离交换闸口，冻结消息契约、签发有期限传递票据、确认消费并保留审计链；
- `fixtures/`：离线验收使用的验证协议与结构化测量；
- `tests/`：领域规则、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m robot_control.acceptance --workspace .
PYTHONPATH=src python3 -m embodied_ai.acceptance --workspace .
PYTHONPATH=src python3 -m component_qualification.acceptance
PYTHONPATH=src python3 -m exchange_gateway.acceptance
```

四条命令会在临时 SQLite 数据库中完成控制资源分配、AI 验证分析、国产电子部件质量流程和跨域隔离交换，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_control.api --database robot-control.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m embodied_ai.api --database embodied-ai.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_qualification.api --database component.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m exchange_gateway.api --database exchange-gateway.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 隔离交换闸口

闸口用于证明某份 AI 结果是否经过允许的通道进入控制域：

1. 架构人员（`architect`）登记并冻结消息契约：方向（源域、目标域）、消息类型（目标轨迹、环境摘要、执行回执）、字段最小集和有效版本，冻结后内容不可更改；
2. 发送方（`sender`，域身份必须匹配契约源域）提交内容摘要，闸口按契约签发有期限的传递票据，字段超出最小集即拒绝；
3. 接收方（`receiver`，域身份必须匹配票据目标域）只有在契约仍冻结、域身份一致、票据摘要与版本未变化且未过期时才能确认消费；
4. 安全规则撤回立即把该契约版本下未完成的传递置为受阻，已消费事实继续可追溯；
5. 重复投递按幂等键返回原票据，同一编号承载不同内容会冲突，过期票据不能被乱序回执复活；
6. 审计人员（`auditor`）通过 `GET /tickets/{id}/trace` 还原一次 AI 决策如何跨域、由谁放行、控制侧实际采用了哪个契约版本，并用 `GET /audit/chain` 校验哈希链；
7. 未决交接保存在 SQLite 中，服务重启后接收方可通过 `GET /tickets/pending` 继续处理。
