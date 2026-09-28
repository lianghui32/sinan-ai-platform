"""冒烟验证脚本：对 127.0.0.1:8005 的运行实例逐项验证安全修复点。"""
import httpx

BASE = "http://127.0.0.1:8005"
c = httpx.Client(base_url=BASE, trust_env=False, timeout=10)


def show(name, ok, extra=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {extra}")
    return ok


results = []

# 1. 注册新员工（运营部）
r = c.post("/api/auth/register", json={"username": "smokeemp2", "password": "pass123456",
                                       "department": "运营部"})
results.append(show("注册运营部员工", r.status_code == 200, r.text if r.status_code != 200 else ""))
etok = c.post("/api/auth/login", json={"username": "smokeemp2", "password": "pass123456"}).json()["token"]
atok = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
E = {"Authorization": f"Bearer {etok}"}
A = {"Authorization": f"Bearer {atok}"}

# 2. KB 部门隔离
kb = c.post("/api/kb", headers=A, json={"name": "冒烟受限库", "category": "供应商资料",
                                        "departments": "产品部"}).json()["id"]
code = c.get(f"/api/kb/{kb}/search", params={"q": "x"}, headers=E).status_code
results.append(show("员工检索受限KB→403", code == 403, f"got {code}"))
names = [k["name"] for k in c.get("/api/kb", headers=E).json()]
results.append(show("员工KB列表不含受限库", "冒烟受限库" not in names, str(names)))
code = c.post("/api/kb", headers=A, json={"name": "x库", "category": "产品资料",
                                          "departments": "黑客部"}).status_code
results.append(show("非法部门配置→400", code == 400, f"got {code}"))

# 3. 工作流脱敏 + SSRF
defn = {
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "api", "type": "http_api", "config": {
            "method": "GET", "url": "http://127.0.0.1:8005/api/mock/ads-data",
            "headers": {"Authorization": "Bearer TOPSECRETTOKEN"}, "timeout": 3}},
        {"id": "end", "type": "end", "config": {"output": {}}},
    ],
    "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "end"}],
}
wf = c.post("/api/workflows", headers=A, json={"name": "冒烟脱敏流", "definition": defn}).json()["id"]
emp_def = c.get(f"/api/workflows/{wf}", headers=E).json()["definition"]
cfg = next(n for n in emp_def["nodes"] if n["id"] == "api")["config"]
results.append(show("员工视角headers脱敏", cfg["headers"]["Authorization"] == "***", str(cfg["headers"])))
run = c.post(f"/api/workflows/{wf}/run", headers=E).json()
results.append(show("回环mock借内部密钥执行成功", run["status"] == "success", run.get("error", "")[:80]))
detail = c.get(f"/api/workflows/runs/{run['run_id']}/detail", headers=E).json()
node_in = str(next(n for n in detail["nodes"] if n["node_id"] == "api")["input"])
results.append(show("node_runs记录脱敏", "TOPSECRETTOKEN" not in node_in))
ssrf = {
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "api", "type": "http_api", "config": {
            "method": "GET", "url": "http://169.254.169.254/latest/meta-data/", "timeout": 2}},
        {"id": "end", "type": "end", "config": {"output": {}}},
    ],
    "edges": [{"from": "start", "to": "api"}, {"from": "api", "to": "end"}],
}
wf2 = c.post("/api/workflows", headers=A, json={"name": "SSRF探针", "definition": ssrf}).json()["id"]
run2 = c.post(f"/api/workflows/{wf2}/run", headers=E).json()
results.append(show("SSRF元数据地址被拦截", run2["status"] == "failed" and "链路本地" in run2["error"],
                    run2["error"][:80]))

# 4. 审计
audit = c.get("/api/admin/audit", headers=A).json()
actions = {r["action"] for r in audit}
results.append(show("审计日志有记录", {"login_ok", "register", "kb_create", "workflow_run"} <= actions,
                    str(sorted(actions))[:100]))

# 5. 前端页面 + 无默认口令提示
html = c.get("/").text
results.append(show("登录页已移除默认口令提示", "admin123" not in html and "emp123" not in html))

print("\n" + ("=" * 40))
print(f"冒烟结果: {sum(results)}/{len(results)} 通过")
if not all(results):
    raise SystemExit(1)
