# 极地科考站越冬物资配额与装载决策服务

本项目在极地科考站协作基础能力（组织、操作者、站点、幂等、审计链、SQLite 事务）之上，
提供**越冬物资配额固化、优先级决策、紧急双签与稳定转配**的完整服务端，解决封舱会审中
燃料滤芯、低温电池被重复申报，以及航班变动时无法确定替代品与释放顺序的问题。

## 业务规则

### 1. 截止时刻固化（freeze）

- 每个航次在创建时登记干货舱/危险品舱的**重量与体积容量**、从容量中物理切出的**紧急预留池**、
  申报截止时刻和越冬结束时刻。
- 截止前可登记物料（单件重量/体积、危险等级）、关键储备、机构配额、申报与替代/拆分/顺延关系；
  截止后基线冻结，不可再改。
- `freeze` 只允许在截止时刻之后执行，产出：
  - 不可变的**冻结清单快照**（舱容、关键储备、配额、申报、修订、关系、决策）及其 SHA-256 摘要；
  - 每份申报的 `approved / waitlisted / rejected` 决策与**结构化原因**；
  - 配载台账（allocation ledger）初始事件。
- 决策规则：
  - 先满足**关键储备**（储备量不可达成时整体拒绝冻结并回滚）；
  - 剩余舱容按**科研优先级分数**（同分时按提交时间）竞争，重量与体积双重约束；
  - 批次效期必须覆盖整个越冬期，已过期或越冬期内到期的批次直接拒绝；
  - 同物料被多单重复申报且高优先级者已占舱时，低优先级单标注 `duplicate_material` 原因候补。

### 2. 机构上限不可拆单绕过

配额按 `(航次, 机构, 物料)` 聚合计量：新增申报和修订都会把该机构该物料的全部未撤销申报
累加后与配额比较，超限直接拒绝，无法用多张小额申报绕过数量或重量上限。

### 3. 截止后紧急需求：两名授权人限时会签

- 紧急申请只在冻结后受理，自动生成优先级 100 的应急申报，动用量来自**独立预留池**而非普通舱容。
- 必须由**两名不同**的授权角色（admin/operator）在限时窗口（默认 60 分钟）内先后批准；
  同一人重复会签被拒，窗口过期惰性置为 `expired` 且预留量不被动用。
- 预留池有独立余量校验，超池的双签在第二次批准时回滚。

### 4. 并发确认与请求重放不重复扣减

- 所有写操作携带 `request_id`：首次执行落幂等收据，重放返回原始回执且**不再扣减**。
- 全部写事务使用 `BEGIN IMMEDIATE` 串行化；装载确认在事务内做条件数量校验
  （`loaded + delta <= allocated`），真并发下超额请求一方失败回滚。
- 装载确认逐条持久化；服务重启后 `pending-loading` 可查出所有未完成确认并接续。

### 5. 航班取消 / 部分到货 / 物资失效 / 临时换装

- 部分到货（shortfall）、物资失效（expiry）只能释放**尚未装载且未领用**的份额；
  已装机份额不可移动。释放后按**冻结时排序**（rank 确定性）把份额稳定递补给同舱候补，
  不做任何随机性或“重新择优”。
- 临时换装（swap）必须基于**已登记且生效的替代关系版本**，受目标舱容量和目标申报余量约束。
- 航班取消时：已登记顺延关系且指向后续航次的份额确定性顺延（部分装机的残留保留在本航次）；
  其余份额释放后同样按冻结排序递补。
- 紧急份额（来自预留池）发生短少/失效时**退回预留池**，不流入普通递补。
- 上述任何事件之后若仍会击穿关键储备，操作整体回滚（应改走紧急双签补位），
  从而保证关键储备始终守恒。

### 6. 可解释性与守恒证明

- `application-explanation` 给出每份申报的修订版本、替代/拆分/顺延关系、决策原因、
  配载结果与完整台账流水，可回答“为何获批、候补或拒绝”。
- `conservation` 逐条校验：
  - 台账借贷余额 == 配载余额；
  - 装载 ≤ 配载、领用 ≤ 装载；
  - 普通用量 ≤ 可竞争舱容（总容量 − 预留池）、普通用量 + 预留占用 ≤ 舱容；
  - 预留池占用 == 紧急台账净量且不超池；
  - 关键储备覆盖（含顺延到后续航次的份额）；
  - 哈希审计链完整。

## 目录

- `src/polar_station_foundation/`
  - `cargo.py`：航次、物料、储备、配额、申报、关系、冻结决策、紧急双签、装载领用、稳定转配、守恒校验；
  - `storage.py`：SQLite schema 与事务边界；
  - `service.py` / `audit.py`：基础登记、角色权限、幂等收据、哈希串联审计；
  - `api.py`：仅依赖标准库的 HTTP/JSON 边界；
  - `acceptance.py`：离线端到端验收。
- `tests/`：44 个单元 / 集成 / 路由 / 验收测试，含真并发与重启恢复用例。

## 环境

- Linux，Python 3.11+
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance

验收脚本在临时 SQLite 库中跑完基础登记链和完整越冬决策链（拆单拦截、冻结固化、
双签会签、重放不重复扣减、短少递补、物资失效、临时换装、航班顺延、守恒校验），
成功时输出一行 `status` 为 `ok`、`cargo_status` 为 `ok` 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

- 健康检查：`GET /health`
- 写接口通过 `X-Actor-Id`（或 body 顶层 `actor_id`）标识操作者，幂等键为 body 中的 `request_id`。
- 主要接口：`/voyages`、`/materials`、`/critical-reserves`、`/org-quotas`、
  `/applications`、`/relations`、`/voyages/freeze`、`/freeze-manifest`、
  `/emergency-releases`、`/emergency-approvals`、`/loading/confirm`、`/issues/confirm`、
  `/loading/pending`、`/cargo/shortfall`、`/cargo/expiry`、`/cargo/flight-cancellation`、
  `/cargo/swap`、`/allocation-ledger`、`/application-explanation`、`/conservation`。
- 服务重启后 SQLite 中的航次、冻结快照、配载、台账和审计历史全部保留。
