"""企业知识库：库管理(admin)、文档上传(txt/md/docx/csv)、检索。

访问控制：知识库带 departments 配置（空=全部门），列表/文档/检索按用户部门过滤，
与助手可见性同一套规则（见 access.py）。员工越权访问不存在的/无权的库一律 403，
避免借错误码差异探测库是否存在。
"""
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, Field

from ..access import check_departments_value, kb_accessible
from ..config import KB_CATEGORIES, KB_UPLOAD_MAX_BYTES
from ..db import get_conn, now, record_audit
from ..knowledge import delete_document, ingest_text, parse_upload, search_kb
from ..uploads import read_async


class KBReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    category: str = "产品资料"
    description: str = Field(default="", max_length=256)
    departments: str = Field(default="", max_length=256)


def build_router(deps: dict) -> APIRouter:
    router = APIRouter(prefix="/api/kb", tags=["knowledge"])
    get_db_path = deps["get_db_path"]
    user_dep = deps["user_dep"]
    admin_dep = deps["admin_dep"]

    def _load_kb(conn, kb_id: int, user: dict):
        """加载知识库并校验可见性；admin 之外的越权访问统一 403（不区分存在/无权，防探测）。"""
        row = conn.execute("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone()
        if row is None or not kb_accessible(conn, row, user):
            raise HTTPException(403, "无权访问该知识库")
        return row

    @router.get("/categories")
    def categories(user: dict = Depends(user_dep)):
        return KB_CATEGORIES

    @router.get("")
    def list_kbs(user: dict = Depends(user_dep)):
        conn = get_conn(get_db_path())
        try:
            rows = conn.execute(
                "SELECT k.*, (SELECT COUNT(*) FROM documents d WHERE d.kb_id=k.id) AS doc_count,"
                " (SELECT COUNT(*) FROM chunks c WHERE c.kb_id=k.id) AS chunk_count "
                "FROM knowledge_bases k ORDER BY k.id").fetchall()
            return [dict(r) for r in rows if kb_accessible(conn, r, user)]
        finally:
            conn.close()

    @router.post("")
    def create_kb(req: KBReq, request: Request, admin: dict = Depends(admin_dep)):
        if req.category not in KB_CATEGORIES:
            raise HTTPException(400, f"分类必须是: {'/'.join(KB_CATEGORIES)}")
        try:
            departments = check_departments_value(req.departments)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        conn = get_conn(get_db_path())
        try:
            cur = conn.execute(
                "INSERT INTO knowledge_bases(name,category,description,departments,created_at) VALUES(?,?,?,?,?)",
                (req.name.strip(), req.category, req.description.strip(), departments, now()))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="kb_create",
                         detail=f"kb={cur.lastrowid} name={req.name.strip()} departments={departments or '全部门'}",
                         ip=request.client.host if request.client else "")
            conn.commit()
            return {"id": cur.lastrowid}
        finally:
            conn.close()

    @router.put("/{kb_id}")
    def update_kb(kb_id: int, req: KBReq, request: Request, admin: dict = Depends(admin_dep)):
        if req.category not in KB_CATEGORIES:
            raise HTTPException(400, f"分类必须是: {'/'.join(KB_CATEGORIES)}")
        try:
            departments = check_departments_value(req.departments)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        conn = get_conn(get_db_path())
        try:
            if conn.execute("SELECT id FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone() is None:
                raise HTTPException(404, "知识库不存在")
            conn.execute(
                "UPDATE knowledge_bases SET name=?,category=?,description=?,departments=? WHERE id=?",
                (req.name.strip(), req.category, req.description.strip(), departments, kb_id))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="kb_update",
                         detail=f"kb={kb_id} departments={departments or '全部门'}",
                         ip=request.client.host if request.client else "")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @router.delete("/{kb_id}")
    def delete_kb(kb_id: int, request: Request, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            used = conn.execute("SELECT COUNT(*) FROM assistants WHERE kb_id=?", (kb_id,)).fetchone()[0]
            if used:
                raise HTTPException(400, "该知识库已被助手绑定，请先解绑")
            for d in conn.execute("SELECT id FROM documents WHERE kb_id=?", (kb_id,)).fetchall():
                delete_document(conn, d["id"])
            conn.execute("DELETE FROM knowledge_bases WHERE id=?", (kb_id,))
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="kb_delete",
                         detail=f"kb={kb_id}", ip=request.client.host if request.client else "")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @router.get("/{kb_id}/documents")
    def list_documents(kb_id: int, user: dict = Depends(user_dep)):
        conn = get_conn(get_db_path())
        try:
            _load_kb(conn, kb_id, user)
            rows = conn.execute(
                "SELECT * FROM documents WHERE kb_id=? ORDER BY id DESC", (kb_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @router.post("/{kb_id}/documents")
    async def upload_document(kb_id: int, request: Request, file: UploadFile = File(...),
                              admin: dict = Depends(admin_dep)):
        # 分块限量读取：先读完再校验会让超大请求把文件整体拉进内存
        try:
            raw = await read_async(file, KB_UPLOAD_MAX_BYTES)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not raw:
            raise HTTPException(400, "文件内容为空")
        conn = get_conn(get_db_path())
        try:
            if conn.execute("SELECT id FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone() is None:
                raise HTTPException(404, "知识库不存在")
            try:
                text = parse_upload(file.filename or "unknown.txt", raw)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            if not text.strip():
                raise HTTPException(400, "解析后文档内容为空，无法建立索引")
            info = ingest_text(conn, kb_id, file.filename or "unknown.txt", text)
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="kb_upload",
                         detail=f"kb={kb_id} doc={info['doc_id']} file={file.filename}",
                         ip=request.client.host if request.client else "")
            conn.commit()
            return info
        finally:
            conn.close()

    @router.delete("/{kb_id}/documents/{doc_id}")
    def remove_document(kb_id: int, doc_id: int, request: Request, admin: dict = Depends(admin_dep)):
        conn = get_conn(get_db_path())
        try:
            row = conn.execute("SELECT * FROM documents WHERE id=? AND kb_id=?", (doc_id, kb_id)).fetchone()
            if row is None:
                raise HTTPException(404, "文档不存在")
            delete_document(conn, doc_id)
            record_audit(conn, user_id=admin["id"], username=admin["username"], action="kb_doc_delete",
                         detail=f"kb={kb_id} doc={doc_id}", ip=request.client.host if request.client else "")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    @router.get("/{kb_id}/search")
    def search(kb_id: int, request: Request, q: str, top_k: int = 3, mode: str = "hybrid",
               user: dict = Depends(user_dep)):
        # 查询串钳制：GET 不走 body 上限，超长 q 会让分词/检索白耗 CPU
        q = (q or "")[:2000]
        conn = get_conn(get_db_path())
        try:
            _load_kb(conn, kb_id, user)
            if mode not in ("tfidf", "bm25", "hybrid"):
                raise HTTPException(400, "mode 必须是 tfidf / bm25 / hybrid")
            return {"query": q, "mode": mode,
                    "results": search_kb(conn, kb_id, q, top_k=min(max(top_k, 1), 10), mode=mode)}
        finally:
            conn.close()

    return router
