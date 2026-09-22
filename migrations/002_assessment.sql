-- 候选方案评估系统：证据版本锁定、容量并发预留、决策冻结
-- 所有时间以带偏移量的 ISO 8601 字符串入库，统一归一化到 UTC(+00:00)。
-- 原始材料只保存受控引用 payload_ref 与 payload_sha256，概不保存内容本身。

-- 证据条目：六个评估维度之一的受控证据
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    dimension   TEXT NOT NULL CHECK (dimension IN (
                    'power_window',      -- 园区电力窗口（含低电价档位）
                    'datacenter_stage',  -- 机房建设阶段
                    'data_scope',        -- 工业数据授权可用范围
                    'residency',         -- 数据驻留/出境限制
                    'research_grant',    -- 科研合作授权
                    'service_region'     -- 服务区域
                 )),
    subject_ref TEXT NOT NULL,           -- 受控主体引用（园区/数据集合/科研项目编号），不含真实身份
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 证据版本：状态与生效窗口按版本冻结；锁定时复制快照而非引用活值
CREATE TABLE IF NOT EXISTS evidence_revision (
    evidence_id    TEXT NOT NULL REFERENCES evidence(evidence_id),
    revision       INTEGER NOT NULL CHECK (revision > 0),
    status         TEXT NOT NULL CHECK (status IN
                     ('draft', 'issued', 'superseded', 'withdrawn')),
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    payload_ref    TEXT NOT NULL,        -- 受控材料库引用编号
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    fact           TEXT NOT NULL DEFAULT '{}',  -- 各维度白名单结构化事实
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (evidence_id, revision)
);

CREATE TABLE IF NOT EXISTS evidence_event (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    event       TEXT NOT NULL,           -- registered / transitioned
    from_status TEXT,
    to_status   TEXT,
    note        TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (evidence_id, revision)
        REFERENCES evidence_revision(evidence_id, revision)
);

-- 变电节点
CREATE TABLE IF NOT EXISTS grid_node (
    grid_node_ref TEXT PRIMARY KEY,
    site_ref      TEXT NOT NULL,
    label         TEXT NOT NULL
);

-- 变电节点承诺值本身也是分版本证据：锁定时只能对当时生效版本负责
CREATE TABLE IF NOT EXISTS grid_commitment (
    grid_node_ref  TEXT NOT NULL REFERENCES grid_node(grid_node_ref),
    revision       INTEGER NOT NULL CHECK (revision > 0),
    committed_mw   TEXT NOT NULL,        -- 数值按契约以 TEXT 保存
    effective_from TEXT NOT NULL,
    recorded_at    TEXT NOT NULL,
    note           TEXT,
    PRIMARY KEY (grid_node_ref, revision)
);

-- 候选方案
CREATE TABLE IF NOT EXISTS proposal (
    proposal_ref             TEXT PRIMARY KEY,
    site_ref                 TEXT NOT NULL,
    grid_node_ref            TEXT NOT NULL REFERENCES grid_node(grid_node_ref),
    data_collection_ref      TEXT NOT NULL, -- 工业数据集合的受控引用
    research_grant_ref       TEXT NOT NULL, -- 科研合作项目的受控引用
    demand_mw                TEXT NOT NULL,
    max_price_cny_per_kwh    TEXT,          -- 可接受电价上限（可选）
    required_data_categories TEXT NOT NULL DEFAULT '[]',
    target_mode              TEXT NOT NULL CHECK (target_mode IN
                               ('cross_border', 'domestic')),
    created_at               TEXT NOT NULL
);

-- 评审轮次
CREATE TABLE IF NOT EXISTS review_round (
    round_id  TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL,
    note      TEXT
);

-- 维度证据锁：每轮每方案每维度一行，复制版本快照
CREATE TABLE IF NOT EXISTS round_lock (
    round_id               TEXT NOT NULL REFERENCES review_round(round_id),
    proposal_ref           TEXT NOT NULL REFERENCES proposal(proposal_ref),
    dimension              TEXT NOT NULL,
    evidence_id            TEXT NOT NULL,
    revision               INTEGER NOT NULL,
    status_snapshot        TEXT NOT NULL,
    effective_from_snapshot TEXT NOT NULL,
    effective_to_snapshot  TEXT,
    payload_ref_snapshot   TEXT NOT NULL,
    payload_sha256_snapshot TEXT NOT NULL,
    fact_snapshot          TEXT NOT NULL,
    locked_at              TEXT NOT NULL,
    PRIMARY KEY (round_id, proposal_ref, dimension),
    FOREIGN KEY (evidence_id, revision)
        REFERENCES evidence_revision(evidence_id, revision)
);

-- 容量预留：锁定即预留，获批后持续占用，驳回/重锁时释放
CREATE TABLE IF NOT EXISTS capacity_reservation (
    reservation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id           TEXT NOT NULL,
    proposal_ref       TEXT NOT NULL,
    grid_node_ref      TEXT NOT NULL,
    commitment_revision INTEGER NOT NULL,
    mw                 TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('held', 'released')),
    locked_at          TEXT NOT NULL,
    released_at        TEXT,
    release_reason     TEXT,
    FOREIGN KEY (grid_node_ref, commitment_revision)
        REFERENCES grid_commitment(grid_node_ref, revision),
    FOREIGN KEY (round_id) REFERENCES review_round(round_id),
    FOREIGN KEY (proposal_ref) REFERENCES proposal(proposal_ref)
);
-- 同一方案同时只有一份有效预留
CREATE UNIQUE INDEX IF NOT EXISTS ux_held_reservation_per_proposal
    ON capacity_reservation(proposal_ref) WHERE status = 'held';
CREATE INDEX IF NOT EXISTS ix_capacity_node_held
    ON capacity_reservation(grid_node_ref) WHERE status = 'held';

-- 防超售触发器：新增 held 预留后，该节点 held 总量不得超过锁定所依据承诺版本的承诺值
CREATE TRIGGER IF NOT EXISTS trg_capacity_oversell_insert
BEFORE INSERT ON capacity_reservation
WHEN NEW.status = 'held'
BEGIN
    SELECT CASE
        WHEN (
            COALESCE((
                SELECT SUM(CAST(mw AS REAL))
                FROM capacity_reservation
                WHERE grid_node_ref = NEW.grid_node_ref AND status = 'held'
            ), 0) + CAST(NEW.mw AS REAL)
        ) > (
            SELECT CAST(committed_mw AS REAL)
            FROM grid_commitment
            WHERE grid_node_ref = NEW.grid_node_ref
              AND revision = NEW.commitment_revision
        )
        THEN RAISE(ABORT, 'capacity exceeds committed value')
    END;
END;

-- 决策：冻结当时全部判定依据，行不可变（服务层不提供 UPDATE/DELETE）
CREATE TABLE IF NOT EXISTS decision (
    decision_id    TEXT PRIMARY KEY,
    round_id       TEXT NOT NULL,
    proposal_ref   TEXT NOT NULL,
    grid_node_ref  TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('approved', 'rejected')),
    evaluated_mode TEXT NOT NULL CHECK (evaluated_mode IN
                     ('cross_border', 'domestic')),
    decided_at     TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    frozen_snapshot TEXT NOT NULL,       -- 六维版本快照 + 承诺值/占用 + 缺口
    UNIQUE (round_id, proposal_ref)
);

-- 决策引用的证据版本：证据被取代/撤回时据此挂后续变化，绝不回写冻结判断
CREATE TABLE IF NOT EXISTS decision_lock_ref (
    decision_id TEXT NOT NULL REFERENCES decision(decision_id),
    dimension   TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    PRIMARY KEY (decision_id, dimension)
);

-- 决策后的后续变化：只追加，绝不回写冻结判断
CREATE TABLE IF NOT EXISTS decision_note (
    note_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL REFERENCES decision(decision_id),
    kind        TEXT NOT NULL CHECK (kind IN (
                    'commitment_changed',   -- 变电承诺值调整
                    'capacity_occupied',    -- 容量被其他项目占用
                    'evidence_superseded',  -- 证据版本被新版本取代
                    'evidence_withdrawn',   -- 证据撤回
                    'window_changed',       -- 电力窗口变化
                    'general'
                )),
    note        TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_note ON decision_note(decision_id);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_assessment');
