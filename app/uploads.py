"""上传文件限量读取：一次性 file.read() 会把整个请求体拉进内存（校验在读完之后才发生），
攻击者可以用超大文件把进程内存打爆。这里按 1MB 分块累计，超限立刻中止。"""
CHUNK_SIZE = 1 << 20  # 1MB


def _mb(n: int) -> str:
    return f"{max(1, n // 1024 // 1024)}MB"


def read_sync(fp, max_bytes: int) -> bytes:
    """同步 spool（UploadFile.file / Starlette TemporaryFile）限量读取。超限抛 ValueError。"""
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = fp.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"文件过大，上限 {_mb(max_bytes)}")
        parts.append(chunk)
    return b"".join(parts)


async def read_async(file, max_bytes: int) -> bytes:
    """异步 UploadFile 限量读取。超限抛 ValueError。"""
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"文件过大，上限 {_mb(max_bytes)}")
        parts.append(chunk)
    return b"".join(parts)
