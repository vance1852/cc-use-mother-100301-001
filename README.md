# 极地科考站协作服务：基础服务与越冬物资配额装载决策

本仓库包含两个共用同一 SQLite 数据库与审计边界的服务包：

- `src/polar_station_foundation/`：基础服务。登记科考机构、站点、操作者和结构化业务资料，提供角色权限、请求幂等、SQLite 事务与哈希串联审计。
- `src/polar_station_logistics/`：越冬物资配额与装载决策。面向封舱前载荷会审，管理航次舱位、关键储备、机构额度、科研申报、装载决策、紧急预留、装载确认与稳定转配。

## 越冬物资配额与装载决策规则

- **截止固化**：航次到达截止时刻后冻结，快照固化货舱重量、体积、关键储备、批次效期、危险等级与科研优先级（`log_flight_snapshots`），冻结后普通申报关闭。
- **申报关系留痕**：申报之间的替代（substitute）、拆分（split）、顺延（defer）关系逐条按 `relation_version` 留痕；拆分的父单被取代、拆分部分总量受父单数量约束；顺延在目标航次自动生成承接申报并回链。
- **机构额度**：额度按（航次，机构）核算全部在配份额，同一机构拆单不能绕过上限。
- **关键储备护栏**：批准非关键类别时必须为食品、应急氧气等类别留足保底空间，预留量从航次总量中先行扣减。
- **紧急需求双人限时批准**：截止后的紧急需求只能动用预留量，需两名不同的授权人员（admin/operator）在限时窗口内批准，发起人不能自批，窗口过期自动失效。
- **幂等与并发**：所有写操作按 request_id 回执去重；装载确认、领用使用条件更新（状态 + 版本号），并发确认只有一方成功，请求重放不会重复扣减。
- **稳定转配**：航班取消、部分到货、物资失效、临时换装时，只释放尚未装载和领用的份额，并按冻结时的同一套规则（优先级、提交时刻、编号顺序）重新安置；已装载/已领用份额不受影响。
- **可解释与守恒**：`explain_declaration` 给出每份申请获批、候补或拒绝的原因链与审计轨迹；`conservation_report` 证明各舱位容量、机构额度与关键储备始终守恒；系统重启后从 SQLite 接续未完成的装载确认。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/polar_station_logistics/：航次与舱位建模、申报与关系、决策引擎、紧急批准、装载确认、转配、守恒报告、HTTP 路由和离线验收；
- tests/：基础规则、决策规则、紧急批准、装载与转配、接口路由、重启接续、并发与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance
    PYTHONPATH=src python3 -m polar_station_logistics.acceptance

两个验收命令都会在临时 SQLite 数据库中执行完整业务链路：基础服务验收登记机构、操作者、站点和资料并核对幂等回执与审计链；物流验收演绎会审全流程（重复申报、冻结、决策、双人紧急批准、幂等确认、部分到货转配、重启接续、封舱守恒），成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m polar_station_logistics.api --database logistics.sqlite3 --host 127.0.0.1 --port 8081

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。物流服务在 `/logistics/*` 下提供物资目录、批次、航次、舱位、储备、额度、申报、关系、冻结、决策、紧急需求、批准、装载确认、领用、封舱、转配等接口，其余路径回退到基础服务路由；典型查询：

- `GET /logistics/flights/{id}`：航次全貌（舱位、储备、额度、申报与份额计数）；
- `GET /logistics/flights/{id}/snapshot`：截止时刻的冻结快照；
- `GET /logistics/flights/{id}/conservation`：舱位容量与关键储备守恒证明；
- `GET /logistics/flights/{id}/pending-loads`：重启后待接续的装载确认；
- `GET /logistics/flights/{id}/decisions`：本航次全部决策记录；
- `GET /logistics/declarations/{id}/explain`：单份申请的获批/候补/拒绝原因链；
- `GET /logistics/emergency-requests/{id}`：紧急需求状态与批准记录。
