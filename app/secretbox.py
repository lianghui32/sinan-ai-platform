"""settings 表中 API Key 的静态加密：HMAC-SHA256-CTR + HMAC 校验标签，纯标准库。

设计取舍：
- 没有引入 cryptography 依赖（项目保持"轻依赖"定位），用 HMAC-SHA256 作流密码与 MAC，
  原语标准、实现短小、可审计；密文带 "enc1:" 前缀与版本号，便于将来换算法。
- 主密钥来源：环境变量 AIP_SECRET_KEY（32+ 字符）；未设置时自动生成 data/.secret_key
  （随机 32 字节 hex，POSIX 下 chmod 600）。数据库文件与密钥文件分离，拖库不等于拿到明文 Key。
- 这是"静态加密"而非 HSM 级防护：拿到密钥文件的人仍可解密。目的是让"只拖走 platform.db"
  的场景（备份泄漏、误提交）拿不到 API Key。
"""
import base64
import hashlib
import os
import secrets
from pathlib import Path

_PREFIX = "enc1:"     # 密文版本前缀：v1 = nonce(16) + ct + tag(16)
_NONCE_LEN = 16
_TAG_LEN = 16


class SecretBox:
    def __init__(self, master_key: bytes):
        # 从主密钥派生出加密/校验两把子密钥（HMAC 作 KDF，domain separation）
        self._enc = hmac_sha256(master_key, b"aip-enc-v1")
        self._mac = hmac_sha256(master_key, b"aip-mac-v1")

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(_NONCE_LEN)
        data = plaintext.encode("utf-8")
        ks = _keystream(self._enc, nonce, len(data))
        ct = bytes(a ^ b for a, b in zip(data, ks))
        tag = hmac_sha256(self._mac, nonce + ct)[:_TAG_LEN]
        return _PREFIX + base64.b64encode(nonce + ct + tag).decode("ascii")

    def decrypt(self, token: str) -> str:
        raw = base64.b64decode(token[len(_PREFIX):].encode("ascii"))
        nonce, ct, tag = raw[:_NONCE_LEN], raw[_NONCE_LEN:-_TAG_LEN], raw[-_TAG_LEN:]
        expect = hmac_sha256(self._mac, nonce + ct)[:_TAG_LEN]
        if not secrets.compare_digest(tag, expect):
            raise ValueError("密文校验失败（密钥不匹配或数据被篡改）")
        ks = _keystream(self._enc, nonce, len(ct))
        return bytes(a ^ b for a, b in zip(ct, ks)).decode("utf-8")

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return isinstance(value, str) and value.startswith(_PREFIX)


def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    import hmac as _hmac

    return _hmac.new(key, msg, hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256-CTR：counter 从 0 起，每块 32 字节。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac_sha256(key, nonce + counter.to_bytes(8, "big")))
        counter += 1
    return bytes(out[:length])


def load_or_create_key(data_dir: Path) -> bytes:
    """主密钥：优先 AIP_SECRET_KEY 环境变量；否则使用/生成 data/.secret_key。"""
    from .config import SECRET_KEY_ENV

    env_key = os.environ.get(SECRET_KEY_ENV, "").strip()
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    key_file = data_dir / ".secret_key"
    if key_file.exists():
        return hashlib.sha256(key_file.read_text(encoding="utf-8").strip().encode("utf-8")).digest()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)   # Windows 上等效收紧 ACL，失败不影响功能
    except OSError:
        pass
    return hashlib.sha256(key_file.read_text(encoding="utf-8").strip().encode("utf-8")).digest()


_BOXES: dict[str, SecretBox] = {}


def get_box(key_dir: str) -> SecretBox:
    """按密钥目录取 SecretBox（缓存键 = 目录）。

    注意 key_dir 是目录而不是库文件路径：加密（admin_router）与解密（providers）两侧
    必须解析到同一个 .secret_key，否则无 AIP_SECRET_KEY 时会各建一把钥匙互相解不开。
    """
    dir_path = str(Path(key_dir))
    if dir_path not in _BOXES:
        _BOXES[dir_path] = SecretBox(load_or_create_key(Path(dir_path)))
    return _BOXES[dir_path]
