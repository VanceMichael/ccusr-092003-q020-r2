# 东北亚算力候选方案评估系统

面向东北亚的算力项目申报中，低电价、变电容量、工业数据授权和跨境服务意向来自不同时间点。
本系统通过**每轮评审锁定证据版本**，保证只把“锁定时点已落实”的资源计为建设条件，并据此
展示逐维度缺口；容量锁定原子预留、不超承诺；跨境不足可降级为不出域比较；获批后冻结判断
依据，后续变化单独呈现。全程只保存受控引用与 sha256 摘要，决策人员看不到企业原始数据。

## 核心规则

1. **证据版本锁定**：六个维度（电力窗口、机房阶段、数据可用范围、驻留限制、科研合作授权、
   服务区域）的证据分版本（`draft/issued/superseded/withdrawn`）并带生效窗口。每轮每方案
   每维度只能锁定一个锁定时点已 `issued` 且生效中的版本，快照复制、轮次内不可更换。
2. **缺口展示**：快照按锁定版本判定，给出逐维度缺口码（草案/未生效/阶段未就绪/类别未覆盖/
   电价超限/仅有意向等），见 `contracts/entities.json`。
3. **容量并发**：容量锁定在单事务内完成，触发器保证并发 `held` 总量不越过当时承诺值。
   节点容量被占用只影响尚未锁定的方案；已锁定/已获批方案不回溯；驳回释放预留。
4. **不出域比较**：跨境仅卡在驻留或服务区域时，自动按境内（数据不出域）口径比较与批准；
   存在硬性缺口则两种口径都不成立。
5. **隐私**：入口拒绝任何原始材料字段，`fact` 仅接受白名单结构化事实。
6. **历史可追溯**：获批/驳回时冻结完整依据；事后证据被取代撤回、承诺值调整、容量被占只
   追加到历史，与原判断严格分开。

## 接口速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/evidence` | 登记证据条目 |
| POST | `/evidence/{id}/revisions` | 登记证据版本（状态、生效窗口、引用、摘要、fact） |
| POST | `/evidence/{id}/revisions/transition` | 版本状态流转（发布/取代/撤回） |
| GET | `/evidence/{id}` | 证据及全部版本 |
| POST | `/grid-nodes` | 登记变电节点 |
| POST | `/grid-nodes/{ref}/commitments` | 登记承诺值版本 |
| GET | `/grid-nodes/{ref}` | 当前承诺值、已锁总量、剩余容量、占用明细 |
| POST | `/proposals` | 登记候选方案 |
| GET | `/proposals[/{ref}]` | 方案列表/详情 |
| POST | `/rounds` | 开启评审轮次 |
| POST | `/rounds/{id}/proposals/{ref}/locks` | 锁定某维度证据版本 |
| POST | `/rounds/{id}/proposals/{ref}/capacity` | 原子锁定变电容量（幂等） |
| GET | `/rounds/{id}/proposals/{ref}/snapshot` | 六维缺口 + 容量状态快照 |
| GET | `/rounds/{id}/comparison?mode=auto\|cross_border\|domestic` | 同口径方案比较 |
| POST | `/rounds/{id}/proposals/{ref}/decision` | 批准/驳回（批准即冻结依据） |
| GET | `/decisions/{id}` | 冻结依据 + 后续变化历史 |
| POST | `/decisions/{id}/notes` | 追加人工历史附注 |

完整报文示例见 `fixtures/example.json`，字段与缺口码约定见 `contracts/entities.json`，
领域规则说明见 `docs/domain.md`。

## 本地开发

- `make migrate`：初始化/升级数据文件（服务启动时对新库也会自动迁移）
- `make test`：执行自动化检查（含并发超售、版本锁定、历史分离等端到端用例）
- `make run`：启动服务（`PORT` 指定端口，`DATABASE_PATH` 指定数据文件）

也可以 `docker compose up --build` 在隔离容器中运行，宿主机端口由 `APP_PORT` 调整。

快速验证：

```bash
make test
make run &
curl -s http://localhost:8080/ | python -m json.tool
```
