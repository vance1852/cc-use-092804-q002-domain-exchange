# 建立控制域与 AI 域隔离交换闸口基础平台

本项目是一套可离线运行的 Python 服务端平台，服务于具身智能机器人控制域、AI 计算域和国产电子部件质量域。平台管理控制计算节点、实时控制总线、资源时隙、机器人构建、验证测量、分析租约、部件批次与质量决定，业务状态、幂等结果和审计事件保存在 SQLite 中。

## 目录

- `src/robot_control/`：控制节点、实时总线、资源批次、时隙申请、容量分配和架构情景；
- `src/embodied_ai/`：机器人构建、验证协议、测量导入、排除复核、分析任务和准入决定；
- `src/component_qualification/`：国产电子部件批次、信号测量、统计分析、账号权限和质量审批；
- `src/exchange_gateway/`：控制域与 AI 域之间的隔离交换闸口，负责消息契约冻结、有期限传递票据、消费确认、安全规则撤回和跨域审计还原；
- `fixtures/`：离线验收使用的验证协议与结构化测量；
- `tests/`：领域规则、事务、权限、HTTP API 和命令行验收测试。

## 隔离交换闸口

运动控制与 AI 感知部署在隔离计算域中，目标轨迹、环境摘要和执行回执只能经过闸口跨域：

1. 架构人员登记计算域并创建消息契约（方向、字段最小集、有效版本、票据有效期），契约冻结后内容不可再变；
2. 发送方提交内容摘要（SHA-256）和实际字段清单，闸口校验契约已冻结、发送方域身份与契约源域一致、版本在有效范围内、字段覆盖最小集后，签发有期限的传递票据；
3. 接收方确认消费时必须出示未变化的契约版本、域身份和票据版本，且内容摘要与票据登记一致，闸口才登记消费事实；
4. 安全规则撤回在同一事务内阻止全部未完成传递，已消费事实继续可追溯；
5. 重复投递返回原结果，同一编号承载不同内容必须冲突，过期票据进入终态，乱序回执不能复活；
6. 审计人员通过 `GET /transfers/{id}/trace` 还原一次 AI 决策如何跨域、由谁放行、控制侧实际采用了哪个版本，审计事件为哈希链结构；
7. 全部状态保存在 SQLite 中，服务重启后可通过 `GET /transfers/pending` 继续处理未决交接。

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
PYTHONPATH=src python3 -m exchange_gateway.acceptance --workspace .
```

四条命令会在临时 SQLite 数据库中完成控制资源分配、AI 验证分析、国产电子部件质量流程和跨域隔离交换（含重启续办未决交接），不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_control.api --database robot-control.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m embodied_ai.api --database embodied-ai.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_qualification.api --database component.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m exchange_gateway.api --database exchange-gateway.sqlite3 --host 127.0.0.1 --port 8083
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
