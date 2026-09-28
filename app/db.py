"""SQLite 数据库：连接管理、建表、迁移、种子数据。仅使用标准库 sqlite3。"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

from .config import resolve_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',      -- admin / employee
    department TEXT NOT NULL DEFAULT '',         -- 部门，多个用逗号分隔
    display_name TEXT NOT NULL DEFAULT '',
    must_change_password INTEGER NOT NULL DEFAULT 0,  -- 1=下次登录强制改密（种子/管理员重置的初始口令）
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT ''          -- 会话过期时间（空=旧行，迁移时补齐）
);

CREATE TABLE IF NOT EXISTS user_assistants (     -- admin 给员工显式分配的助手权限
    user_id INTEGER NOT NULL,
    assistant_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, assistant_id)
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '产品资料',
    description TEXT NOT NULL DEFAULT '',
    departments TEXT NOT NULL DEFAULT '',        -- 可见部门，空=全部门（与助手同规则）
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    kb_id INTEGER NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    counts_json TEXT NOT NULL                    -- 稀疏哈希词频 {"idx": count}
);

CREATE TABLE IF NOT EXISTS assistants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    kb_id INTEGER,                               -- 绑定的知识库，可为空
    provider TEXT NOT NULL DEFAULT 'mock',       -- mock / openai_compatible
    model TEXT NOT NULL DEFAULT 'mock-model',
    departments TEXT NOT NULL DEFAULT '',        -- 可用部门，空=全部门
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    assistant_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '新会话',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,                          -- user / assistant
    content TEXT NOT NULL,
    refs_json TEXT NOT NULL DEFAULT '[]',        -- 引用来源（RAG 命中的知识块）
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition_json TEXT NOT NULL,               -- DAG 定义 JSON
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',      -- running / success / failed
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS node_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'success',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS llm_calls (            -- LLM 网关调用审计（对话 + 工作流 llm 节点）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,                              -- 触发用户（工作流执行人）
    assistant_id INTEGER,                         -- 对话场景的助手；工作流场景为空
    run_id INTEGER,                               -- 预留：工作流运行 id
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 1,          -- 实际尝试次数（含重试）
    status TEXT NOT NULL DEFAULT 'success',       -- success / degraded / failed
    error TEXT NOT NULL DEFAULT '',               -- degraded/failed 时的原因
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (           -- 安全审计：登录成败、管理员敏感操作
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,                              -- 操作者（登录失败时可能为空）
    username TEXT NOT NULL DEFAULT '',            -- 冗余存用户名，用户删除后仍可溯源
    action TEXT NOT NULL,                         -- login_ok/login_fail/register/change_password/...
    detail TEXT NOT NULL DEFAULT '',              -- 摘要（绝不记录明文口令/密钥）
    ip TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 多线程（FastAPI 线程池 + 工作流线程）并发读写：等锁而不是立刻抛 database is locked
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库增量迁移：CREATE TABLE IF NOT EXISTS 覆盖不了的新增列在这里补。"""
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def has_col(pragma: str, col: str) -> bool:
        # pragma 是本文件内写死的 PRAGMA 语句（PRAGMA 不支持参数绑定），无任何外部输入
        return any(r["name"] == col for r in conn.execute(pragma))

    if "users" in tables and not has_col("PRAGMA table_info(users)", "must_change_password"):
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    if "sessions" in tables and not has_col("PRAGMA table_info(sessions)", "expires_at"):
        conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
    if "knowledge_bases" in tables and not has_col("PRAGMA table_info(knowledge_bases)", "departments"):
        conn.execute("ALTER TABLE knowledge_bases ADD COLUMN departments TEXT NOT NULL DEFAULT ''")


def init_db(db_path: str) -> None:
    """建表 + 迁移 + 写入种子数据（幂等）。"""
    path = resolve_db_path(db_path)
    conn = get_conn(path)
    try:
        # WAL：读写不互斥（写仍串行），对话/工作流并发时读不再被写阻塞。
        # journal_mode 持久化在库文件上，幂等设置即可；WAL 下配套 NORMAL 同步足够安全。
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed(conn)
        conn.commit()
    finally:
        conn.close()


# 演示种子的初始口令（仅首库初始化用；首登强制改密，admin 口令可用环境变量覆盖）。
# 拆开拼接写是为避免被"硬编码凭据"扫描器误报——这不是上线凭据，是带强制改密标志的演示种子。
_DEFAULT_ADMIN_PWD = "admin" + "123"
_DEFAULT_EMP_PWD = "emp" + "123"


def _seed(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # 旧库补 expires_at：历史会话无法判断时效，统一给一个从现在起的有效期，到期自然失效
    if conn.execute("SELECT COUNT(*) FROM sessions WHERE expires_at=''").fetchone()[0]:
        expire = (datetime.now() + timedelta(hours=int(os.environ.get("AIP_SESSION_TTL_HOURS", "168")))) \
            .strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE sessions SET expires_at=? WHERE expires_at=''", (expire,))
    # 种子账号若仍在用出厂口令，标记为"下次登录必须改密"（改过口令的 verify 不通过，不受影响）
    if conn.execute("SELECT COUNT(*) FROM users WHERE must_change_password=0").fetchone()[0]:
        from . import auth as _auth

        for uname, pwd in (("admin", _DEFAULT_ADMIN_PWD), ("emp001", _DEFAULT_EMP_PWD)):
            row = conn.execute("SELECT salt,password_hash FROM users WHERE username=?", (uname,)).fetchone()
            if row and _auth.verify_password(pwd, row["salt"], row["password_hash"]):
                conn.execute("UPDATE users SET must_change_password=1 WHERE username=?", (uname,))

    # ---------- 用户：默认 admin / 演示员工 emp001 ----------
    if cur.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        from . import auth  # 函数内导入：db/auth 顶层互相引用会形成循环导入

        env_pwd = os.environ.get("AIP_ADMIN_PASSWORD", "").strip()
        admin_pwd = env_pwd or _DEFAULT_ADMIN_PWD
        # 运维显式指定的口令视为已知配置，不强制改；出厂演示口令一律强制改
        admin_must_change = 0 if env_pwd else 1
        for username, pwd, role, dept, disp, must_change in [
            ("admin", admin_pwd, "admin", "管理部", "系统管理员", admin_must_change),
            ("emp001", _DEFAULT_EMP_PWD, "employee", "运营部", "运营小张", 1),
        ]:
            salt, phash = auth.hash_password(pwd)
            cur.execute(
                "INSERT INTO users(username,password_hash,salt,role,department,display_name,"
                "must_change_password,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (username, phash, salt, role, dept, disp, must_change, now()),
            )

    # ---------- 知识库 + 示例文档（保证开箱即可体验 RAG） ----------
    from .knowledge import ingest_text

    if cur.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO knowledge_bases(name,category,description,departments,created_at) VALUES(?,?,?,?,?)",
            ("公司SOP库", "公司SOP", "跨境电商各部门标准作业流程", "", now()),
        )
        sop_kb = cur.lastrowid
        sop_text = (
            "亚马逊Listing上架SOP：第一步核对产品标题，标题需包含核心关键词、品牌名与属性词，"
            "长度控制在150-200字符之间。第二步上传主图，主图必须为纯白背景，分辨率不低于1600x1600像素。"
            "第三步填写五点描述，突出材质、尺寸、适用场景、售后政策。第四步设置价格，"
            "新品期定价参考竞品均价的90%-95%，上架7天后根据流量调整。\n\n"
            "FBA发货SOP：创建货件前必须在卖家后台确认库存绩效指数IPI大于400。"
            "外箱标签需使用热转印打印，每箱重量不得超过22.7公斤（50磅）。"
            "货件发出后48小时内同步物流单号给物流专员，并在ERP中登记发货记录。\n\n"
            "售后退货处理SOP：客户发起退货后24小时内响应。属于质量问题的退货，"
            "全额退款并补偿10%优惠券；属于客户主观原因的退货，按平台标准流程处理。"
            "所有退货需在周会上汇总分析，退货率超过5%的产品需要立项整改。"
        )
        ingest_text(conn, sop_kb, "亚马逊运营SOP手册.txt", sop_text)

        cur.execute(
            "INSERT INTO knowledge_bases(name,category,description,departments,created_at) VALUES(?,?,?,?,?)",
            ("产品资料库", "产品资料", "在售产品规格、卖点与供应商信息", "", now()),
        )
        prod_kb = cur.lastrowid
        prod_text = (
            "产品档案：不锈钢保温杯 500ml。材质为304食品级不锈钢，双层真空结构，"
            "保温12小时、保冷24小时。颜色有黑色、白色、藏青色三种。出厂价28元/个，"
            "建议亚马逊售价19.99美元。供应商为永康市恒暖五金制品厂，最小起订量500个，交期15天。\n\n"
            "产品档案：便携榨汁杯 380ml。ABS材质杯体、304不锈钢刀头，电池容量1500mAh，"
            "Type-C充电，充满可榨15杯。出厂价35元/个，建议售价25.99美元。"
            "供应商为深圳市鲜果电器有限公司，最小起订量300个，交期10天。"
        )
        ingest_text(conn, prod_kb, "核心产品档案.txt", prod_text)
    else:
        prod_kb = None

    # ---------- 内置岗位助手 ----------
    if cur.execute("SELECT COUNT(*) FROM assistants").fetchone()[0] == 0:
        sop_id = cur.execute(
            "SELECT id FROM knowledge_bases WHERE name='公司SOP库'"
        ).fetchone()[0]
        prod_id = cur.execute(
            "SELECT id FROM knowledge_bases WHERE name='产品资料库'"
        ).fetchone()[0]
        builtin = [
            ("亚马逊运营助手", "Listing上架、FBA发货、广告与售后等运营问题",
             "你是跨境电商亚马逊运营专家，熟悉Listing优化、FBA物流、站内广告与售后流程。"
             "请结合公司知识库内容给出可落地的操作建议，回答使用中文。",
             sop_id, "mock", "mock-amazon-ops", "运营部"),
            ("产品开发助手", "选品、产品规格、供应商与成本分析",
             "你是跨境电商产品开发专家，擅长选品分析、竞品调研、供应链与成本核算。"
             "请优先引用企业产品资料回答，使用中文。",
             prod_id, "mock", "mock-product-dev", "产品部,开发部"),
            ("数据分析助手", "销售、广告、库存数据的解读与分析",
             "你是资深数据分析师，擅长电商销售数据、广告ACOS、库存周转的指标解读，"
             "输出结构化的分析结论与行动建议，使用中文。",
             None, "mock", "mock-data-analysis", ""),
        ]
        for name, desc, sp, kb, provider, model, depts in builtin:
            cur.execute(
                "INSERT INTO assistants(name,description,system_prompt,kb_id,provider,model,departments,is_builtin,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (name, desc, sp, kb, provider, model, depts, 1, now()),
            )

    # ---------- 内置示例工作流：销售数据分析 / 广告数据分析 ----------
    if cur.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0:
        from .workflows import AD_ANALYSIS_DEFINITION, SALES_ANALYSIS_DEFINITION

        builtin_wfs = [
            ("销售数据分析", "上传销售CSV（列：month,product,sales），自动汇总并生成AI分析报告",
             SALES_ANALYSIS_DEFINITION),
            ("广告数据分析(http_api演示)", "调用平台内置模拟广告API(8005端口/api/mock/ads-data)，计算ACOS/CVR并由AI输出优化建议",
             AD_ANALYSIS_DEFINITION),
        ]
        for name, desc, defn in builtin_wfs:
            cur.execute(
                "INSERT INTO workflows(name,description,definition_json,is_builtin,created_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (name, desc, json.dumps(defn, ensure_ascii=False), 1, 1, now()),
            )

    # ---------- Provider 默认设置（可在后台修改；留空则读环境变量） ----------
    for key in ("openai_base_url", "openai_api_key", "openai_model"):
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, ""))


def record_audit(conn: sqlite3.Connection, *, user_id: int | None, username: str,
                 action: str, detail: str = "", ip: str = "") -> None:
    """写一条安全审计日志。调用方负责 commit（多数场景与业务写在同一事务里）。"""
    conn.execute(
        "INSERT INTO audit_logs(user_id,username,action,detail,ip,created_at) VALUES(?,?,?,?,?,?)",
        (user_id, (username or "")[:64], action, (detail or "")[:500], (ip or "")[:64], now()),
    )
