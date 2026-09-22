# 东北算力项目边界评估

面向东北亚的算力候选方案评审系统。园区电力窗口、机房阶段、工业数据授权、数据驻留、
科研合作授权与跨境服务意向取自不同日期与主体；系统按**评审轮次锁定每个维度的具体证据版本**，
据此展示建设条件缺口，并对变电节点容量做并发安全的预留，避免把尚未落实的资源误判为已具备条件。

## 它保证什么

- **证据版本固定**：每轮评审把六个维度分别锁到具体证据版本；之后登记的新版本不影响在评方案，
  证据本身不可覆盖（新版本是新记录）。
- **缺口可见**：评估按锁定版本逐项核对（窗口是否生效、机房是否设备就绪且容量足够、
  数据/科研授权是否 GRANTED 且未过期、驻留是否匹配服务模式、跨境意向是否存在）。
- **容量不超额**：锁电力窗口时在同一事务内预留变电节点容量，`BEGIN IMMEDIATE` 保证并发
  预留总量不越过节点承诺值。节点容量被别的项目占用（承诺下调）只挡住**尚未锁定**的方案，
  已持有预留不受影响；驳回/解锁释放容量。
- **不出域方案可比**：跨境条件不足时，可用 `service_mode=IN_DOMAIN` 只比较境内不出域方案。
- **原始数据不入库、不可见**：证据只保存受控 `source_ref` 与 `source_sha256`，
  属性按维度白名单校验（仅枚举/数值/布尔/时间/受控标签），未声明字段在接口边界即被拒绝。
- **获批历史可解释且不可篡改观感**：获批时冻结决策快照；历史页面把"当时为何可行"
  （`original_judgement`）与"后来变化"（`later_changes_recorded`、
  `current_state_vs_snapshot`）严格分列，后来变化不回写原判断。

本服务通过 HTTP 接口交换业务记录，使用 SQLite 文件保存状态。`PORT` 指定监听端口，
`DATABASE_PATH` 指定数据文件（服务启动时自动迁移）。字段约定见 `contracts/entities.json`，
领域规则见 `docs/domain.md`，不含真实身份的请求示例见 `fixtures/example.json`。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/proposals` | 登记候选方案（IN_DOMAIN / CROSS_BORDER） |
| POST | `/evidence` | 登记证据新版本（不可变，重复版本返回 409） |
| PUT | `/grid-nodes/{ref}/commitment` | 登记/调整节点承诺容量 |
| POST | `/rounds` · `POST /rounds/{id}/close` | 开启 / 关闭评审轮次 |
| PUT/GET/DELETE | `/rounds/{id}/locks[/{proposal}/{dimension}]` | 按维度锁版本、查锁、解锁 |
| GET | `/rounds/{id}/evaluations/{proposal}?as_of=` | 六维度缺口评估 |
| GET | `/rounds/{id}/comparison?service_mode=` | 候选方案横向比较（可只看出域） |
| GET | `/reservations?grid_node_ref=` | 容量预留台账 |
| POST/GET | `/proposals/{ref}/decision` | 获批/驳回（有缺口不可获批）并冻结快照 |
| POST | `/proposals/{ref}/changes` | 登记决策后的外部变化 |
| GET | `/proposals/{ref}/history` | 原判断与后来变化分列的历史页 |

## 本地开发

```sh
make migrate   # 初始化/升级数据文件
make test      # 运行自动化检查（含并发容量与 HTTP 端到端）
make run       # 启动服务（默认 8080）
```

也可以 `docker compose up --build` 在隔离容器中运行，宿主机端口由 `APP_PORT` 调整。
