# 贡献指南

感谢关注司南（Sinan）！Issue 与 PR 均欢迎。

## 开发环境

```bash
git clone https://github.com/lianghui32/sinan-ai-platform.git
cd sinan-ai-platform
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8005
```

## 提交前检查

1. `python -m pytest tests -q` 全绿（当前 107 项）；
2. 涉及检索算法（`app/knowledge.py`）的改动，请附 `python scripts/eval_rag.py` 的**改动前后指标对比**（hit@1 / hit@3 / MRR）——CI 中有评估回归护栏，指标回退会直接挂；
3. 涉及工作流 `code`/`condition` 表达式能力的改动，请同时补充 `safe_eval` 白名单的用例（新增放行语法必须有对应的拒绝用例）；
4. 新增依赖请锁定版本并说明用途。

## 代码约定

- 后端同步风格 + 每路由独立连接（`get_conn`），SQLite 语句保持标准 SQL 方言；
- 错误处理：用户可见错误一律中文、带下一步建议；4xx 给出原因，不静默吞异常；
- 前端错误一律页面内 toast，禁用原生 `alert/confirm`；颜色一律走 CSS 变量（新增主题参考 style.css 底部色板块）；
- 注释只写"代码本身说不出来的约束"（为什么这样做、边界在哪），不写流水账。

## 分支与提交

- `main` 保持可运行；功能分支 → PR；
- 提交信息建议格式：`模块: 动作`（如 `gateway: 增加并发信号量`、`retrieval: 修复 BM25 空查询除零`）。
