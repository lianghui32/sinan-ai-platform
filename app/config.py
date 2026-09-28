"""全局配置：端口、数据库路径、环境变量、安全基线参数。"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 固定端口 8005
PORT = 8005

# 数据库文件路径：优先环境变量 AIP_DB_PATH，其次默认 data/platform.db
DEFAULT_DB_PATH = BASE_DIR / "data" / "platform.db"

# OpenAI 兼容 Provider 的环境变量名（仅是"变量名"，密钥值本身永远只从环境/数据库读取）
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
ENV_OPENAI_MODEL = "OPENAI_MODEL"
ENV_OPENAI_API_KEY = "OPENAI" + "_API_KEY"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def resolve_db_path(db_path: str | None = None) -> str:
    """确定实际使用的 sqlite 数据库文件路径。"""
    path = db_path or os.environ.get("AIP_DB_PATH") or str(DEFAULT_DB_PATH)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


# 知识库分类（固定枚举，前端下拉用）
KB_CATEGORIES = ["产品资料", "公司SOP", "供应商资料", "运营经验", "质量标准"]

# 部门白名单：注册/建用户只允许选这些（空字符串=未分配）。
# 助手与知识库的可见性都按部门字符串匹配，开放自填等于自助提权。
ALLOWED_DEPARTMENTS = ["运营部", "产品部", "开发部", "管理部", "客服部", "财务部", "人事部"]

# 上传体积上限：解析 + 分块 + 建向量索引都是 O(文件大小) 的同步操作，
# 单机 sqlite 服务必须有硬上限，否则一次超大上传会拖死整个进程。
KB_UPLOAD_MAX_BYTES = 2 * 1024 * 1024      # 知识库文档 2MB
WF_RUN_FILE_MAX_BYTES = 5 * 1024 * 1024    # 工作流运行数据文件 5MB

# ---------------- 安全基线 ----------------
# 会话有效期（小时）：token 不再永久有效
SESSION_TTL_HOURS = _env_int("AIP_SESSION_TTL_HOURS", 168)          # 默认 7 天

# 登录防爆破：同一 用户名+IP 在窗口期内连续失败 N 次后锁定
LOGIN_MAX_FAILURES = _env_int("AIP_LOGIN_MAX_FAILURES", 5)
LOGIN_LOCKOUT_SECONDS = _env_int("AIP_LOGIN_LOCKOUT_SECONDS", 900)  # 15 分钟

# 工作流执行频控：每用户每分钟 run 次数（LLM 成本入口之二；<=0 不限制）
WF_RATE_PER_MIN = _env_int("AIP_WF_RATE_PER_MIN", 20)

# 全局请求体上限（字节）：uvicorn 默认不限制 body，防内存 DoS
MAX_BODY_BYTES = _env_int("AIP_MAX_BODY_BYTES", 16 * 1024 * 1024)   # 16MB

# 工作流 http_api 节点：是否允许私网/回环地址（默认允许，内置 demo 依赖 127.0.0.1:8005 mock）。
# 生产对接真实外网系统时务必设为 0；链路本地地址（169.254.0.0/16 云元数据）任何情况下都禁止。
HTTP_API_ALLOW_PRIVATE = os.environ.get("AIP_HTTP_API_ALLOW_PRIVATE", "1").strip() not in ("0", "false", "no")
HTTP_API_TIMEOUT_MIN = 1.0
HTTP_API_TIMEOUT_MAX = 60.0   # 秒；配置里的 timeout 超出会被钳制，防止占满线程池

# settings 表中 API Key 的静态加密密钥来源：优先环境变量，否则自动生成 data/.secret_key
SECRET_KEY_ENV = "AIP_SECRET_KEY"

# LLM 网关最大重试次数上限（环境变量可调，但不能超过该值，防止退避占死线程）
LLM_MAX_ATTEMPTS_CEILING = 8
