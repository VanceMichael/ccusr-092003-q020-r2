-- 候选方案评估系统：证据版本锁定、容量预留、决策快照

-- 候选方案（外部主体只用引用编号，不含真实身份）
CREATE TABLE IF NOT EXISTS proposals (
    proposal_ref TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    service_mode TEXT NOT NULL CHECK (service_mode IN ('IN_DOMAIN', 'CROSS_BORDER')),
    capacity_mw REAL NOT NULL CHECK (capacity_mw > 0),
    submitted_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 证据版本：每条证据按维度登记、不可修改；新版本是新行
-- dimension 取值：
--   power_window          园区电力窗口（低电价/供电窗口）
--   datacenter_stage      机房建设阶段
--   data_scope            工业数据授权与可用范围
--   residency             数据驻留限制
--   research_authorization 科研合作授权
--   service_area          服务区域与跨境服务意向
CREATE TABLE IF NOT EXISTS evidence_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_ref TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN (
        'power_window', 'datacenter_stage', 'data_scope',
        'residency', 'research_authorization', 'service_area'
    )),
    version_label TEXT NOT NULL,
    -- 受控材料引用与摘要；原始材料绝不入库
    source_ref TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    -- 规范化后的白名单属性（仅布尔/枚举/数值，不含企业原始数据）
    attributes_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (evidence_ref, version_label)
);

-- 评审轮次
CREATE TABLE IF NOT EXISTS review_rounds (
    round_id TEXT PRIMARY KEY,
    sequence_no INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);

-- 每轮、每方案、每维度锁定的具体证据版本
CREATE TABLE IF NOT EXISTS evidence_locks (
    round_id TEXT NOT NULL REFERENCES review_rounds(round_id),
    proposal_ref TEXT NOT NULL REFERENCES proposals(proposal_ref),
    dimension TEXT NOT NULL,
    revision_id INTEGER NOT NULL REFERENCES evidence_revisions(revision_id),
    locked_at TEXT NOT NULL,
    PRIMARY KEY (round_id, proposal_ref, dimension)
);

-- 变电节点承诺容量（评审期间采用的承诺值；变化保留在历史中）
CREATE TABLE IF NOT EXISTS grid_capacity_commitments (
    grid_node_ref TEXT PRIMARY KEY,
    committed_mw REAL NOT NULL CHECK (committed_mw >= 0),
    updated_at TEXT NOT NULL
);

-- 容量预留：方案锁定电力窗口时同步预留；总量在同一事务内校验
CREATE TABLE IF NOT EXISTS capacity_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL REFERENCES review_rounds(round_id),
    grid_node_ref TEXT NOT NULL,
    proposal_ref TEXT NOT NULL REFERENCES proposals(proposal_ref),
    reserved_mw REAL NOT NULL CHECK (reserved_mw >= 0),
    status TEXT NOT NULL CHECK (status IN ('held', 'released')),
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reservations_node
    ON capacity_reservations(grid_node_ref, status);

-- 获批/驳回决策：冻结当时判断（证据版本、缺口、容量口径）
CREATE TABLE IF NOT EXISTS decisions (
    proposal_ref TEXT PRIMARY KEY REFERENCES proposals(proposal_ref),
    round_id TEXT NOT NULL REFERENCES review_rounds(round_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('APPROVED', 'REJECTED')),
    service_mode TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    rationale_ref TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

-- 获批之后发生的变化，与原判断分列保存，不回写快照
CREATE TABLE IF NOT EXISTS post_decision_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_ref TEXT NOT NULL REFERENCES decisions(proposal_ref),
    changed_at TEXT NOT NULL,
    dimension TEXT NOT NULL,
    summary TEXT NOT NULL,
    new_evidence_ref TEXT,
    new_source_sha256 TEXT
);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_domain');
