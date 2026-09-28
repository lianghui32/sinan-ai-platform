/* 司南AI中台（Sinan）前端逻辑（原生JS，无框架）
 * 交互约定：
 *  - 所有异步错误一律页面内 toast，禁用原生 alert / confirm（危险操作用自绘确认弹窗）
 *  - 401 自动回到登录页并提示
 *  - 长耗时操作（发消息 / 跑工作流 / 上传）期间按钮 disabled，防重复提交
 *  - 工作流执行有实时状态：提交后轮询运行详情，逐节点刷新，失败节点高亮
 *  - 后台表格统一分页 + 横向滚动容器，窄屏不破版
 */
"use strict";
const $ = (id) => document.getElementById(id);
const API = "";
let TOKEN = localStorage.getItem("aip_token") || "";
let ME = null;

let assistants = [], curAssistant = null, conversations = [], curConv = null;
let kbs = [], curKb = null;
let wfs = [], curWf = null;
let sending = false;            // 对话进行中
let runWatch = null;            // 工作流轮询定时器
let toastTimer = null;

/* ============================================================ 基础请求 */
const FIELD_CN = {
  username: "用户名", password: "密码", content: "消息内容", assistant_id: "助手",
  name: "名称", description: "描述", system_prompt: "系统提示词", kb_id: "知识库",
  provider: "模型渠道", model: "模型名", departments: "可用部门", department: "部门",
  display_name: "姓名", role: "角色", category: "分类", definition: "工作流定义",
  body: "请求体", query: "参数", file: "文件", top_k: "返回条数", q: "检索词",
};
function friendlyField(loc) {
  const last = (loc || []).filter((x) => typeof x === "string").pop();
  return FIELD_CN[last] || last || "字段";
}
/* pydantic 的 422 detail 是数组，直接抛给 UI 会显示成 [object Object] */
function friendlyDetail(data, status) {
  const d = data && data.detail;
  if (d == null) return "请求失败（HTTP " + status + "）";
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    const msg = d.slice(0, 3).map((e) => {
      const f = friendlyField(e.loc);
      const ctx = e.ctx || {};
      if (e.type === "string_too_short") return `${f}至少 ${ctx.min_length} 个字符`;
      if (e.type === "string_too_long") return `${f}最多 ${ctx.max_length} 个字符`;
      if (e.type === "int_parsing") return `${f}必须是整数`;
      if (e.type === "missing") return `缺少必填项「${f}」`;
      if (e.type === "model_attributes_type" || e.type === "dict_type") return `${f}格式不对（需为对象）`;
      return `${f}：${e.msg || "格式不正确"}`;
    }).join("；");
    return d.length > 3 ? msg + ` 等 ${d.length} 处` : msg;
  }
  return String(d);
}

async function req(method, url, body, isForm) {
  const headers = {};
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  let payload = body;
  if (body && !isForm) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const resp = await fetch(API + url, { method, headers, body: payload });
  let data = {};
  if (resp.status !== 204) data = await resp.json().catch(() => ({}));
  if (resp.status === 401) {
    logoutLocal();
    throw new Error("登录状态已失效，请重新登录");
  }
  if (!resp.ok) throw new Error(friendlyDetail(data, resp.status));
  return data;
}
const get = (u) => req("GET", u);
const post = (u, b, f) => req("POST", u, b, f);
const put = (u, b) => req("PUT", u, b);
const del = (u) => req("DELETE", u);

/* 统一异步错误处理：失败 → toast，返回 null，调用方 if (!x) return; */
async function guard(fn, label) {
  try { return await fn(); }
  catch (e) { toast((label ? label + "：" : "") + e.message, "err"); return null; }
}

function toast(msg, kind) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast show" + (kind ? " " + kind : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), kind === "err" ? 4200 : 2400);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function emptyBox(text, hint) {
  return `<div class="empty">${esc(text)}${hint ? `<div class="empty-hint">${esc(hint)}</div>` : ""}</div>`;
}
function busy(btnId, on, text, text2) {
  const b = $(btnId);
  if (!b) return;
  b.disabled = on;
  if (text) b.textContent = on ? text : (text2 || text);
}

/* 同一操作在请求返回前只允许跑一次：表格行内按钮没有 id，用「动作:目标id」做键。
   重复点击直接返回 null（调用方的 `if (r === null) return;` 已把它当"未执行"处理）。 */
const INFLIGHT = new Set();
async function withLock(key, run) {
  if (INFLIGHT.has(key)) { toast("上一条操作还在处理中，请稍候", "err"); return null; }
  INFLIGHT.add(key);
  try { return await run(); }
  finally { INFLIGHT.delete(key); }
}

/* ============================================================ 弹窗 / 确认 */
let modalOnOk = null;
let modalLocked = false;
let confirmResolve = null;
function openModal(opts) {
  closeModal();
  const title = esc(opts.title || "");
  modalLocked = !!opts.locked;   // 锁定弹窗（强制改密）：遮罩点击 / ESC 均不可关闭
  const mask = document.createElement("div");
  mask.className = "modal-mask"; mask.id = "modal-mask";
  mask.innerHTML = `<div class="modal${opts.wide ? " wide" : ""}" role="dialog" aria-modal="true">
    <h3>${title}</h3>
    <div class="modal-body">${opts.body || ""}</div>
    <div class="btns">
      ${opts.locked ? "" : `<button class="btn-ghost" onclick="closeModal()">${esc(opts.cancelText || "取消")}</button>`}
      <button class="${opts.danger ? "btn-danger-solid" : "btn-primary"}" id="modal-ok">${esc(opts.okText || "确定")}</button>
    </div></div>`;
  document.body.appendChild(mask);
  // locked 弹窗（如强制改密）不允许点遮罩关闭
  if (!opts.locked) mask.addEventListener("mousedown", (e) => { if (e.target === mask) closeModal(); });
  modalOnOk = opts.onOk || null;
  $("modal-ok").onclick = async () => {
    if (!modalOnOk) return closeModal();
    const btn = $("modal-ok");
    btn.disabled = true;
    try { await modalOnOk(); }
    catch (e) { toast(e.message, "err"); btn.disabled = false; return; }
    btn.disabled = false;
  };
  const first = mask.querySelector("input, textarea, select");
  if (first) setTimeout(() => first.focus(), 30);
}
function closeModal() {
  $("modal-mask")?.remove();
  modalOnOk = null;
  modalLocked = false;
  if (confirmResolve) { const r = confirmResolve; confirmResolve = null; r(false); }
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!modalLocked) closeModal();
    closeThemeMenu();
  }
});

/* ============================================================ 主题切换
 * 色板定义在 style.css 底部（html[data-theme=...] 变量块），这里只负责切换与记忆。
 * 首帧防闪白：index.html <head> 内联脚本在渲染前恢复 localStorage 里的主题。
 */
const THEMES = [
  { id: "light", name: "清爽蓝（默认）", accent: "#5b8def", bg: "#f3f5f9" },
  { id: "codex", name: "Codex 暖白", accent: "#1a1a19", bg: "#f7f5f1" },
  { id: "github-dark", name: "GitHub Dark", accent: "#4493f8", bg: "#0d1117" },
  { id: "dracula", name: "Dracula 紫", accent: "#bd93f9", bg: "#282a36" },
  { id: "nord", name: "Nord 极地", accent: "#88c0d0", bg: "#2e3440" },
];
function currentTheme() {
  return document.documentElement.dataset.theme || "light";
}
function setTheme(id) {
  if (!THEMES.some((t) => t.id === id)) return;
  document.documentElement.dataset.theme = id;
  try { localStorage.setItem("aip_theme", id); } catch (e) { /* 隐私模式等场景忽略 */ }
  renderThemeMenu();
}
function renderThemeMenu() {
  const menu = $("theme-menu");
  if (!menu) return;
  const cur = currentTheme();
  menu.innerHTML = THEMES.map((t) => `
    <div class="theme-item ${t.id === cur ? "active" : ""}" role="menuitemradio" aria-checked="${t.id === cur}"
         onclick="setTheme('${t.id}')">
      <span class="theme-dot" style="--theme-dot-accent:${t.accent};--theme-dot-bg:${t.bg}"></span>
      ${esc(t.name)}${t.id === cur ? '<span class="theme-check">✓</span>' : ""}
    </div>`).join("");
}
function toggleThemeMenu(ev, source) {
  const menu = $("theme-menu");
  if (!menu) return;
  if (menu.style.display === "none") {
    // 菜单跟随入口位置：顶栏按钮 → 顶栏下方；悬浮球 → 按钮上方
    if (source === "topbar") {
      menu.style.top = "52px"; menu.style.right = "12px"; menu.style.bottom = "auto";
    } else {
      menu.style.top = ""; menu.style.right = ""; menu.style.bottom = "";
    }
    renderThemeMenu();
    menu.style.display = "";
  } else {
    menu.style.display = "none";
  }
  if (ev) ev.stopPropagation();
}
function closeThemeMenu() { const m = $("theme-menu"); if (m) m.style.display = "none"; }
document.addEventListener("mousedown", (e) => {
  const menu = $("theme-menu"), fab = $("theme-fab"), topBtn = $("topbar-theme-btn");
  if (menu && menu.style.display !== "none"
      && !menu.contains(e.target) && e.target !== fab && !fab?.contains(e.target)
      && e.target !== topBtn && !topBtn?.contains(e.target)) {
    menu.style.display = "none";
  }
});

/* 危险操作二次确认（替代原生 confirm，返回 Promise<boolean>） */
function confirmModal(opts) {
  if (confirmResolve) { const r = confirmResolve; confirmResolve = null; r(false); }
  return new Promise((resolve) => {
    openModal({
      title: opts.title || "请确认",
      danger: opts.danger !== false,
      okText: opts.okText || "确认执行",
      body: `<p class="confirm-text">${esc(opts.text || "")}</p>`,
      onOk: async () => {
        const r = confirmResolve; confirmResolve = null; closeModal();
        if (r) r(true); else resolve(true);
      },
    });
    confirmResolve = (v) => { confirmResolve = null; resolve(v); };
  });
}

/* ============================================================ 分页表格 */
const TBL = {};
function mountTable(key, mountId, columns, rows, opts) {
  const o = opts || {};
  TBL[key] = { mountId, columns, rows: rows || [], page: 1, size: o.size || 8, empty: o.empty || "暂无数据" };
  drawTable(key);
}
function drawTable(key) {
  const t = TBL[key], el = $(t.mountId);
  if (!t || !el) return;
  const total = t.rows.length;
  const pages = Math.max(1, Math.ceil(total / t.size));
  if (t.page > pages) t.page = pages;
  const from = (t.page - 1) * t.size;
  const slice = t.rows.slice(from, from + t.size);
  const head = `<tr>${t.columns.map((c) => `<th>${c.label}</th>`).join("")}</tr>`;
  const body = slice.length
    ? slice.map((r) => `<tr>${t.columns.map((c) => `<td>${c.render(r)}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${t.columns.length}"><div class="empty">${esc(t.empty)}</div></td></tr>`;
  el.innerHTML = `<div class="tbl-wrap"><table class="tbl">${head}${body}</table></div>` + pagerHtml(key, total, pages, from, slice.length);
}
function pagerHtml(key, total, pages, from, shown) {
  if (!total) return "";
  const t = TBL[key];
  let btns = "";
  const win = 5;
  let start = Math.max(1, Math.min(t.page - 2, pages - win + 1));
  for (let p = start; p < start + win && p <= pages; p++) {
    btns += `<button class="pg ${p === t.page ? "on" : ""}" onclick="pagerGo('${key}',${p})">${p}</button>`;
  }
  return `<div class="pager">
    <span class="pinfo">第 ${from + 1}-${from + shown} 条 / 共 ${total} 条</span>
    <span class="pgroup">
      <button class="pg" onclick="pagerGo('${key}',${t.page - 1})" ${t.page <= 1 ? "disabled" : ""}>上一页</button>
      ${pages > 1 ? btns : ""}
      <button class="pg" onclick="pagerGo('${key}',${t.page + 1})" ${t.page >= pages ? "disabled" : ""}>下一页</button>
    </span></div>`;
}
function pagerGo(key, p) {
  const t = TBL[key];
  if (!t) return;
  t.page = Math.max(1, p);
  drawTable(key);
}

/* ============================================================ 登录 / 注册 */
function switchAuthTab(which) {
  $("tab-login").classList.toggle("active", which === "login");
  $("tab-register").classList.toggle("active", which === "register");
  $("form-login").style.display = which === "login" ? "" : "none";
  $("form-register").style.display = which === "register" ? "" : "none";
  $("login-error").textContent = "";
}
function authKeydown(e, which) {
  if (e.key !== "Enter") return;
  e.preventDefault();
  which === "login" ? doLogin() : doRegister();
}
async function doLogin() {
  if ($("login-btn").disabled) return;
  busy("login-btn", true, "登录中…");
  $("login-error").textContent = "";
  try {
    const pwd = $("login-password").value;
    const d = await post("/api/auth/login", {
      username: $("login-username").value.trim(), password: pwd,
    });
    TOKEN = d.token; localStorage.setItem("aip_token", TOKEN); ME = d.user;
    await enterMain();
    toast("欢迎回来，" + (ME.display_name || ME.username), "ok");
    // 初始口令（种子/管理员代设）必须先改密才能继续使用
    if (ME.must_change_password) await forceChangePassword(pwd);
  } catch (e) {
    $("login-error").textContent = e.message;
    if (/重新登录/.test(e.message)) { /* 401 已回登录页 */ }
  } finally {
    busy("login-btn", false, "登录中…", "登 录");
  }
}

/* 强制改密：种子/管理员重置的初始口令登录后必须修改，弹窗不可关闭 */
function forceChangePassword(prefillOld) {
  return new Promise((resolve) => {
    openModal({
      title: "首次登录：请修改初始密码",
      locked: true,
      okText: "确认修改",
      body: `
        <label>当前密码</label><input id="m-cp-old" type="password" value="${esc(prefillOld || "")}">
        <label>新密码（至少 6 位）</label><input id="m-cp-new" type="password">
        <label>确认新密码</label><input id="m-cp-new2" type="password">
        <p class="hint left">检测到该账号仍在使用初始口令（出厂演示口令或管理员代设口令），修改后才能继续使用平台。</p>`,
      onOk: async () => {
        const oldPwd = $("m-cp-old").value, p1 = $("m-cp-new").value, p2 = $("m-cp-new2").value;
        if (p1.length < 6) throw new Error("新密码至少 6 位");
        if (p1 !== p2) throw new Error("两次输入的新密码不一致");
        if (p1 === oldPwd) throw new Error("新密码不能与当前密码相同");
        await post("/api/auth/change-password", { old_password: oldPwd, new_password: p1 });
        ME = await get("/api/auth/me");      // 刷新 must_change_password 标记
        closeModal();
        toast("密码已修改，当前设备保持登录", "ok");
        resolve(true);
      },
    });
    // 首次打开自动聚焦旧密码框（预填时聚焦新密码框）
    setTimeout(() => { (prefillOld ? $("m-cp-new") : $("m-cp-old"))?.focus(); }, 30);
  });
}
async function doRegister() {
  if ($("reg-btn").disabled) return;
  busy("reg-btn", true, "提交中…");
  $("login-error").textContent = "";
  try {
    await post("/api/auth/register", {
      username: $("reg-username").value.trim(), password: $("reg-password").value,
      department: $("reg-department").value.trim(), display_name: $("reg-display").value.trim(),
    });
    switchAuthTab("login");
    $("login-username").value = $("reg-username").value.trim();
    $("login-password").focus();
    toast("注册成功，请输入密码登录", "ok");
  } catch (e) { $("login-error").textContent = e.message; }
  finally { busy("reg-btn", false, "提交中…", "注 册"); }
}
function logoutLocal() {
  TOKEN = ""; ME = null; localStorage.removeItem("aip_token");
  stopRunWatch();
  sending = false;
  // 清空上一位用户的界面残留：选中项 + 列表 + 聊天气泡（切换账号后不能看到别人的会话）
  assistants = []; curAssistant = null; conversations = []; curConv = null;
  kbs = []; curKb = null; wfs = []; curWf = null;
  const setHtml = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
  setHtml("messages", "");
  setHtml("assistant-list", "");
  setHtml("conversation-list", "");
  setHtml("kb-list", "");
  setHtml("kb-docs", "");
  setHtml("kb-search-result", "");
  setHtml("wf-list", "");
  setHtml("wf-runs", "");
  setHtml("wf-detail", "选择左侧工作流查看详情并执行");
  setHtml("wf-run-detail", "");
  setHtml("assistant-banner", "请选择左侧助手开始对话");
  ["wf-live", "wf-run-detail"].forEach((id) => { const el = $(id); if (el) el.style.display = "none"; });
  $("login-password") && ($("login-password").value = "");
  $("reg-password") && ($("reg-password").value = "");
  $("view-main").style.display = "none"; $("view-login").style.display = "";
}
async function doLogout() {
  try { await post("/api/auth/logout"); } catch (e) { /* 已失效也无妨 */ }
  logoutLocal();
  toast("已退出登录");
}

/* ============================================================ 主界面 */
async function enterMain() {
  ME = await get("/api/auth/me");
  $("view-login").style.display = "none";
  $("view-main").style.display = "";
  $("user-info").textContent = `${ME.display_name || ME.username}（${ME.role === "admin" ? "管理员" : "员工"} · ${ME.department || "未分配部门"}）`;
  $("nav-admin").style.display = ME.role === "admin" ? "" : "none";
  $("kb-add-btn").style.display = ME.role === "admin" ? "" : "none";
  switchView("chat");
}
function switchView(v) {
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  ["chat", "kb", "wf", "admin"].forEach((p) => { $("page-" + p).style.display = p === v ? "" : "none"; });
  if (v === "chat") guard(() => loadAssistants(), "加载助手失败");
  if (v === "kb") guard(() => loadKbs(), "加载知识库失败");
  if (v === "wf") guard(() => loadWorkflows(), "加载工作流失败");
  if (v === "admin") switchAdminTab(document.querySelector(".list-item[data-atab].active")?.dataset.atab || "users");
}

/* ============================================================ 对话 */
async function loadAssistants() {
  assistants = await get("/api/assistants");
  if (curAssistant && !assistants.some((a) => a.id === curAssistant.id)) {
    curAssistant = null; curConv = null;          // 助手被删除或收回授权：不留下上一次的选中
  }
  $("assistant-list").innerHTML = assistants.length ? assistants.map((a) =>
    `<div class="list-item ${curAssistant && curAssistant.id === a.id ? "active" : ""}" onclick="selectAssistant(${a.id})">
      ${esc(a.name)}<span class="li-sub">${esc(a.description || "")}</span>
      <span class="li-sub">模型：${esc(a.provider)} / ${esc(a.model)}${a.kb_name ? " · 知识库：" + esc(a.kb_name) : ""}</span>
    </div>`).join("") : emptyBox("暂无可用助手", "请联系管理员按部门分配助手");
  await loadConversations();
  // 与知识库/工作流列表一致：进入页面即选中第一个可用助手（内部自动建会话并进入），少一步操作
  if (!curAssistant && assistants.length) await selectAssistant(assistants[0].id);
}
async function selectAssistant(id) {
  curAssistant = assistants.find((a) => a.id === id) || null;
  if (!curAssistant) return;
  curConv = null;
  renderBanner();
  renderMessages([], { empty: "会话已就绪，输入问题即可开始" });
  await guard(() => loadAssistants(), "加载助手失败");
  // 生成→使用自动衔接：该助手下没有会话就直接建一个并进入，不多一步
  if (!conversations.length) { await newConversation(true); }
  else { openConversation(conversations[0].id); }
}
function renderBanner() {
  const b = $("assistant-banner");
  if (!curAssistant) { b.innerHTML = "请选择左侧助手开始对话"; return; }
  b.innerHTML = `<b>${esc(curAssistant.name)}</b> — ${esc(curAssistant.description || "")}
    <span class="tag">${esc(curAssistant.provider)}/${esc(curAssistant.model)}</span>
    ${curAssistant.kb_name ? `<span class="tag green">知识库：${esc(curAssistant.kb_name)}</span>` : ""}
    ${curConv ? `<button class="btn-ghost mini right" id="del-conv-btn" onclick="deleteCurrentConversation()">删除当前会话</button>
       <span class="tag gray">${esc(curConv.title)}</span>` : ""}`;
}
async function loadConversations() {
  if (!curAssistant) { conversations = []; $("conversation-list").innerHTML = emptyBox("先选择助手"); return; }
  conversations = await get("/api/conversations?assistant_id=" + curAssistant.id);
  $("conversation-list").innerHTML = conversations.length ? conversations.map((c) =>
    `<div class="list-item ${curConv && curConv.id === c.id ? "active" : ""}" onclick="openConversation(${c.id})">
      ${esc(c.title)}<span class="li-sub">${esc(c.updated_at)}</span></div>`).join("")
    : emptyBox("还没有会话", "点上方「＋新会话」开始");
}
async function newConversation(silent) {
  if (!curAssistant) return toast("请先选择助手", "err");
  const aid = curAssistant.id;
  const c = await withLock("newconv:" + aid, async () => {
    busy("new-conv-btn", true, "建立中…");
    try { return await guard(() => post("/api/conversations", { assistant_id: aid }), "新建会话失败"); }
    finally { busy("new-conv-btn", false, "建立中…", "＋新会话"); }
  });
  if (!c) return;
  curConv = { id: c.id, title: c.title, assistant_id: c.assistant_id };
  await guard(() => loadConversations(), "刷新会话列表失败");
  renderMessages([], { empty: "会话已创建，输入问题即可开始" });
  renderBanner();
  setChatEnabled(true);
  if (!silent) toast("已新建会话并进入", "ok");
  $("chat-input").focus();
}
async function openConversation(id) {
  const c = conversations.find((x) => x.id === id);
  if (!c) return;
  await withLock("openconv:" + id, async () => {
    curConv = c;
    renderBanner();
    await guard(() => loadConversations(), "刷新会话列表失败");
    renderMessages(null);   // loading 占位
    const msgs = await guard(() => get(`/api/conversations/${id}/messages`), "加载消息失败");
    if (msgs) renderMessages(msgs);
    setChatEnabled(true);
  });
}
async function deleteCurrentConversation() {
  if (!curConv) return;
  const ok = await confirmModal({
    title: "删除会话", danger: true, okText: "确认删除",
    text: `将删除「${curConv.title}」及其全部消息记录，此操作不可恢复。`,
  });
  if (!ok) return;
  const id = curConv.id;
  const r = await withLock("delconv:" + id, async () => {
    busy("del-conv-btn", true, "删除中…");
    try { return await guard(() => del("/api/conversations/" + id), "删除失败"); }
    finally { busy("del-conv-btn", false, "删除中…", "删除当前会话"); }
  });
  if (r === null) return;
  curConv = null;
  toast("会话已删除", "ok");
  setChatEnabled(false);
  renderMessages([], { empty: "会话已删除，点左侧「＋新会话」继续" });
  await guard(() => loadConversations(), "刷新会话列表失败");
  renderBanner();
}
function setChatEnabled(on) {
  const locked = !on || sending;
  $("send-btn").disabled = locked;
  $("chat-input").disabled = locked;
  $("send-btn").textContent = sending ? "发送中…" : "发送";
  $("chat-input").placeholder = !on ? "请先在左侧选择助手（会自动新建会话）"
    : sending ? "AI 正在回复，请稍候…" : "输入问题，Enter 发送 / Shift+Enter 换行";
}
function refsHtml(refs) {
  if (!refs || !refs.length) return "";
  return `<div class="refs"><div class="refs-title">📎 引用来源 ${refs.length} 条（点击展开原文）</div>` +
    refs.map((r) => `<details class="ref">
      <summary><b>《${esc(r.filename)}》</b> 第${(r.seq ?? 0) + 1}段 · 相关度 ${esc(r.score)}${r.doc_id ? ` · 文档#${r.doc_id}` : ""}</summary>
      <div class="ref-text">${esc(r.text)}</div></details>`).join("") + "</div>";
}
function scrollBottom() {
  const m = $("messages");
  m.scrollTop = m.scrollHeight;
}
function renderMessages(msgs, opts) {
  const box = $("messages");
  const o = opts || {};
  if (msgs === null) { box.innerHTML = `<div class="msg bot pending">正在载入消息…</div>`; return; }
  box.innerHTML = (msgs || []).map((m) => {
    if (m.role === "user") return `<div class="msg user">${esc(m.content)}</div>`;
    return `<div class="msg bot">${esc(m.content)}${refsHtml(m.refs)}</div>`;
  }).join("") || emptyBox(o.empty || "开始提问吧", "绑定知识库的助手会在回复下附引用来源");
  scrollBottom();
}
function chatKeydown(e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
/* 解析 SSE 字节流为事件帧。返回 {event, data} 迭代所需的底层读取器封装 */
async function readSse(resp, onEvent) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let ev = "message", data = null;
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data = line.slice(5).trim();
      }
      if (data != null) {
        try { onEvent(ev, JSON.parse(data)); } catch (e) { /* 跳过坏帧 */ }
      }
    }
  }
}
function usageMeta(meta, done) {
  const bits = [];
  if (meta) bits.push(esc((meta.degraded ? "mock(已降级)" : meta.provider) + " / " + (meta.model || "")));
  if (done && done.usage) {
    bits.push(`tokens ↑${done.usage.prompt_tokens} ↓${done.usage.completion_tokens}`);
    bits.push(`耗时 ${done.latency_ms}ms${done.attempts > 1 ? ` · 重试${done.attempts - 1}次` : ""}`);
  }
  return `<div class="msg-meta">${bits.filter(Boolean).map(esc).join(" · ")}</div>`;
}
async function sendMessage() {
  if (sending) return toast("上一条消息还在回复中，请稍候", "err");
  if (!curConv) { toast("请先选择助手并新建会话", "err"); return; }
  const input = $("chat-input");
  const content = input.value.trim();
  if (!content) return;
  if (content.length > 8000) return toast("单条消息最多 8000 字", "err");

  input.value = "";
  sending = true;
  setChatEnabled(true);
  const box = $("messages");
  if (box.querySelector(".empty")) box.innerHTML = "";
  box.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(content)}</div>`);
  // 流式气泡：delta 直接写入 textContent（安全），完成后整体替换为富文本版本
  box.insertAdjacentHTML("beforeend",
    `<div class="msg bot streaming" id="msg-streaming"><span class="stream-text"></span><span class="caret"></span></div>`);
  scrollBottom();
  const bubble = $("msg-streaming");

  const cid = curConv.id;
  let meta = null, errMsg = null, text = "", donePayload = null;
  const finish = (ok) => {
    const refs = (meta && meta.refs) || [];
    let html = esc(text) + refsHtml(refs);
    if (meta && meta.degraded) {
      html += `<div class="fallback-tip">⚠ 模型调用失败已自动降级为本地模拟回复：${esc(meta.degrade_reason || "")}</div>`;
    }
    html += usageMeta(meta, ok);
    bubble.classList.remove("streaming");
    bubble.removeAttribute("id");   // 关键：只有"正在流式"的气泡才持有该 id，否则下一条消息会定位到旧气泡
    bubble.innerHTML = html;
    scrollBottom();
  };
  try {
    const headers = { "Content-Type": "application/json" };
    if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
    const resp = await fetch(API + `/api/conversations/${cid}/messages/stream`, {
      method: "POST", headers, body: JSON.stringify({ content }),
    });
    if (resp.status === 401) { logoutLocal(); throw new Error("登录状态已失效，请重新登录"); }
    if (!resp.ok || !resp.body) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(friendlyDetail(data, resp.status));
    }
    await readSse(resp, (ev, d) => {
      if (ev === "meta") { meta = d; }
      else if (ev === "delta") {
        text += d.text;
        const t = bubble.querySelector(".stream-text");
        if (t) t.textContent = text;
        scrollBottom();
      } else if (ev === "error") { errMsg = d.message; }
      else if (ev === "done") { donePayload = d; }
    });
    if (errMsg) throw new Error(errMsg);
    finish(donePayload);
  } catch (e) {
    $("msg-streaming")?.remove();
    // 服务端未落库：明确告知未发送成功，并把内容还给用户，避免"看着发了其实没发"
    box.insertAdjacentHTML("beforeend",
      `<div class="msg bot err-bubble"><b>发送失败（本条未保存）</b>：${esc(e.message)}
        <div class="fallback-tip">${/模型调用失败|未配置/.test(e.message)
          ? "该助手配置的模型不可用：可在 管理后台 → 助手配置 → 该行「模型」按钮 切回 mock，或补全 Base URL / API Key。"
          : /太频繁/.test(e.message) ? "平台对每用户消息频率有限流，稍等片刻再发。" : "请检查网络或登录状态后重试。"}</div></div>`);
    scrollBottom();
    if (!input.value.trim()) input.value = content;
    toast(e.message, "err");
  } finally {
    sending = false;
    setChatEnabled(true);
    input.focus();
    guard(() => loadConversations(), "刷新会话列表失败");   // 后台刷新，不占用输入锁
  }
}

/* ============================================================ 知识库 */
async function loadKbs() {
  kbs = await get("/api/kb");
  if (curKb) curKb = kbs.find((k) => k.id === curKb.id) || null;   // 重新指向最新一行，doc_count 才不会过期
  $("kb-list").innerHTML = kbs.length ? kbs.map((k) =>
    `<div class="list-item ${curKb && curKb.id === k.id ? "active" : ""}" onclick="selectKb(${k.id})">
      ${esc(k.name)}${k.departments ? '<span class="tag gray">限部门</span>' : '<span class="tag green">全部门</span>'}
      <span class="li-sub">${esc(k.category)} · ${k.doc_count}文档 · ${k.chunk_count}分块</span></div>`).join("")
    : emptyBox("还没有知识库", "管理员可点右上角「＋新建」");
  if (kbs.length && !curKb) await selectKb(kbs[0].id);
}
async function selectKb(id) {
  curKb = kbs.find((k) => k.id === id) || null;
  if (!curKb) return;
  $("kb-title-text").textContent = curKb.name + " · " + curKb.category
    + (curKb.departments ? "（限部门：" + curKb.departments + "）" : "（全部门可见）");
  $("kb-upload-card").style.display = ME.role === "admin" ? "" : "none";
  $("kb-del-btn").style.display = ME.role === "admin" ? "" : "none";
  $("kb-edit-btn").style.display = ME.role === "admin" ? "" : "none";
  await guard(() => loadKbs(), "加载知识库失败");
  const docs = await guard(() => get(`/api/kb/${id}/documents`), "加载文档失败");
  if (!docs) return;
  if (!docs.length) { $("kb-docs").innerHTML = emptyBox("该知识库还没有文档", "上传 txt / md / docx / csv 后自动分块建索引"); return; }
  mountTable("kbdocs", "kb-docs", [
    { label: "文件名", render: (d) => esc(d.filename) },
    { label: "字符数", render: (d) => d.char_count },
    { label: "分块", render: (d) => d.chunk_count },
    { label: "上传时间", render: (d) => esc(d.created_at) },
    { label: "操作", render: (d) => (ME.role === "admin"
        ? `<button class="btn-danger" onclick="deleteDoc(${d.id})">删除</button>` : "-") },
  ], docs, { size: 8, empty: "暂无文档" });
}
function createKb() {
  openModal({
    title: "新建知识库",
    body: `
      <label>名称</label><input id="m-kb-name" placeholder="如：供应链资料库">
      <label>分类</label><select id="m-kb-cat">${["产品资料", "公司SOP", "供应商资料", "运营经验", "质量标准"].map((c) => `<option>${c}</option>`).join("")}</select>
      <label>描述</label><input id="m-kb-desc" placeholder="一句话说明放什么内容">
      <label>可见部门（逗号分隔，留空=全部门可见）</label><input id="m-kb-depts" placeholder="运营部,产品部">`,
    okText: "创建",
    onOk: async () => {
      const d = await withLock("newkb", () => post("/api/kb", {
        name: $("m-kb-name").value.trim(), category: $("m-kb-cat").value,
        description: $("m-kb-desc").value.trim(), departments: $("m-kb-depts").value.trim(),
      }));
      if (d === null) throw new Error("上一次创建还在处理中，请稍候");
      closeModal(); toast("知识库已创建，已进入该库可直接上传文档", "ok");
      curKb = null;                                   // 让 loadKbs 不自动跳回第一个库
      await loadKbs();
      await selectKb(d.id);                           // 生成→使用：新建完就站在这个库里
    },
  });
}

async function editKb() {
  if (!curKb) return;
  openModal({
    title: `编辑知识库：${curKb.name}`,
    body: `
      <label>名称</label><input id="m-kb-name" value="${esc(curKb.name)}">
      <label>分类</label><select id="m-kb-cat">${["产品资料", "公司SOP", "供应商资料", "运营经验", "质量标准"].map((c) =>
        `<option ${c === curKb.category ? "selected" : ""}>${c}</option>`).join("")}</select>
      <label>描述</label><input id="m-kb-desc" value="${esc(curKb.description || "")}">
      <label>可见部门（逗号分隔，留空=全部门可见）</label><input id="m-kb-depts" value="${esc(curKb.departments || "")}">`,
    okText: "保存",
    onOk: async () => {
      const kid = curKb.id;
      await withLock("editkb:" + kid, () => put("/api/kb/" + kid, {
        name: $("m-kb-name").value.trim(), category: $("m-kb-cat").value,
        description: $("m-kb-desc").value.trim(), departments: $("m-kb-depts").value.trim(),
      }));
      closeModal(); toast("知识库已更新", "ok");
      curKb = null;
      await loadKbs();
      await selectKb(kid);   // 编辑完留在该库，不跳回第一个
    },
  });
}
async function deleteKb() {
  if (!curKb) return;
  if (curKb.doc_count > 0) {
    toast("该知识库还有 " + curKb.doc_count + " 个文档，请先删除文档", "err");
    return;
  }
  const ok = await confirmModal({
    title: "删除知识库", okText: "确认删除",
    text: `确认删除知识库「${curKb.name}」？被助手绑定的知识库需要先解绑。`,
  });
  if (!ok) return;
  const kid = curKb.id;
  const r = await withLock("delkb:" + kid, async () => {
    busy("kb-del-btn", true, "删除中…");
    try { return await guard(() => del("/api/kb/" + kid), "删除失败"); }
    finally { busy("kb-del-btn", false, "删除中…", "删除该知识库"); }
  });
  if (r === null) return;
  toast("知识库已删除", "ok");
  curKb = null;
  guard(() => loadKbs(), "加载知识库失败");
}
async function uploadDoc() {
  if (!curKb) return toast("请选择知识库", "err");
  const f = $("kb-file").files[0];
  if (!f) return toast("请先选择文件", "err");
  if (f.size > 2 * 1024 * 1024) return toast("文件超过 2MB 上限，请拆分后再传", "err");
  const fd = new FormData(); fd.append("file", f);
  const kbId = curKb.id;
  busy("kb-upload-btn", true, "解析建索引中…");
  const d = await withLock(`upload:${kbId}:${f.name}:${f.size}`, async () => {
    try { return await guard(() => post(`/api/kb/${kbId}/documents`, fd, true), "上传失败"); }
    finally { busy("kb-upload-btn", false, "解析建索引中…", "上传并建索引"); }
  });
  if (!d) return;
  $("kb-file").value = "";
  toast(`《${f.name}》已入库，生成 ${d.chunk_count} 个知识分块`, "ok");
  await guard(() => selectKb(kbId), "刷新文档失败");
}
async function deleteDoc(id) {
  if (!curKb) return;
  const kid = curKb.id;
  const ok = await confirmModal({
    title: "删除文档", okText: "确认删除",
    text: "删除后该文档的所有知识分块索引一并移除，正在进行的 RAG 回答不再引用它。",
  });
  if (!ok) return;
  const r = await withLock(`deldoc:${kid}:${id}`, () =>
    guard(() => del(`/api/kb/${kid}/documents/${id}`), "删除失败"));
  if (r === null) return;
  toast("文档已删除", "ok");
  guard(() => selectKb(kid), "刷新文档失败");
}
async function searchKb() {
  if (!curKb) return toast("请选择知识库", "err");
  const q = $("kb-search-input").value.trim();
  if (!q) { $("kb-search-result").innerHTML = ""; return toast("请输入检索词", "err"); }
  const mode = ($("kb-search-mode") || {}).value || "hybrid";
  busy("kb-search-btn", true, "检索中…");
  const d = await guard(() => get(`/api/kb/${curKb.id}/search?q=${encodeURIComponent(q)}&top_k=3&mode=${mode}`), "检索失败");
  busy("kb-search-btn", false, "检索中…", "检索");
  if (!d) return;
  const modeLabel = { tfidf: "TF-IDF 余弦", bm25: "BM25", hybrid: "混合检索 RRF" }[mode] || mode;
  $("kb-search-result").innerHTML = `<div class="card"><h3>检索结果 Top${d.results.length}（${modeLabel}）</h3>` +
    (d.results.length ? d.results.map((r) => `<div class="node-card">
        <div class="ntitle">《${esc(r.filename)}》第${r.seq + 1}段 <span class="tag">相关度 ${esc(r.score)}</span></div>
        <div class="kb-text">${esc(r.text)}</div></div>`).join("")
      : emptyBox("未检索到相关内容", "换个说法，或先上传相关文档")) + "</div>";
}

/* ============================================================ 工作流 */
async function loadWorkflows() {
  wfs = await get("/api/workflows");
  $("wf-list").innerHTML = wfs.length ? wfs.map((w) =>
    `<div class="list-item ${curWf && curWf.id === w.id ? "active" : ""}" onclick="selectWf(${w.id})">
      ${esc(w.name)}${w.is_builtin ? '<span class="tag green" style="margin-left:4px">内置</span>' : ""}
      <span class="li-sub">${w.node_count}个节点 · ${esc((w.node_types || []).join("/"))}</span></div>`).join("")
    : emptyBox("还没有工作流", "管理员可在后台「工作流配置」新建，或恢复内置示例");
  if (!wfs.length) {                                  // 全被删除：别留下一个能点执行的旧面板
    curWf = null; stopRunWatch();
    $("wf-detail").innerHTML = emptyBox("没有可执行的工作流");
    $("wf-live").style.display = "none";
    $("wf-run-detail").style.display = "none";
    $("wf-runs").innerHTML = "";
    return;
  }
  if (!curWf || !wfs.some((w) => w.id === curWf.id)) await selectWf(wfs[0].id);
}
async function selectWf(id) {
  const wf = await guard(() => get("/api/workflows/" + id), "加载工作流失败");
  if (!wf) return;
  curWf = wf;
  stopRunWatch();
  $("wf-live").style.display = "none";
  $("wf-run-detail").style.display = "none";
  await guard(() => loadWorkflows(), "加载工作流失败");
  const d = curWf.definition;
  const needCsv = (d.nodes || []).some((n) => n.type === "condition" && /rows/.test(JSON.stringify(n.config || {})));
  $("wf-detail").innerHTML = `<h3>${esc(curWf.name)} <span class="tag gray">${esc(curWf.description || "无描述")}</span></h3>
    <div class="flow">${(d.nodes || []).map((n) => `<span class="tag flow-node">${esc(n.name || n.id)} <i>[${esc(n.type)}]</i></span>`).join('<span class="arrow">→</span>')}</div>
    <div class="row"><input type="file" id="wf-file" accept=".csv">
      <input id="wf-params" placeholder='参数JSON(可选) 如 {"campaign":"双11大促"}' style="flex:1;min-width:180px">
      <button class="btn-primary" id="wf-run-btn" onclick="runWf()">▶ 执行工作流</button></div>
    <div class="hint left">${needCsv ? "该流程含数据校验节点：上传 CSV（首行表头，如 month,product,sales）；不传或空文件会走「无数据」分支。" : "无 CSV 也可执行；"}参数 JSON 会作为 input.params 注入模板，定义里写成 {{input.params.x|默认值}} 的参数不传也能跑。</div>`;
  loadRuns();
}
async function loadRuns() {
  if (!curWf) return;
  const runs = await guard(() => get(`/api/workflows/${curWf.id}/runs`), "加载执行历史失败");
  if (!runs) return;
  if (!runs.length) { $("wf-runs").innerHTML = emptyBox("还没有执行记录", "点上方「▶ 执行工作流」跑一次"); return; }
  mountTable("wfruns", "wf-runs", [
    { label: "#", render: (r) => r.id },
    { label: "状态", render: (r) => `<span class="tag ${r.status === "success" ? "green" : (r.status === "running" ? "blue" : "red")}">${esc(r.status)}</span>` },
    { label: "执行人", render: (r) => esc(r.username) },
    { label: "开始时间", render: (r) => esc(r.started_at) },
    { label: "耗时", render: (r) => r.finished_at ? esc(shortSpan(r.started_at, r.finished_at)) : "执行中…" },
    { label: "操作", render: (r) => `<button class="btn-ghost" onclick="showRunDetail(${r.id})">节点详情</button>` },
  ], runs, { size: 6, empty: "暂无执行记录" });
}
function shortSpan(a, b) {
  const t1 = new Date(String(a).replace(" ", "T")), t2 = new Date(String(b).replace(" ", "T"));
  const s = Math.round((t2 - t1) / 1000);
  return isNaN(s) ? "-" : (s < 60 ? s + "秒" : Math.floor(s / 60) + "分" + (s % 60) + "秒");
}
async function runWf() {
  if (!curWf) return toast("请先选择工作流", "err");
  if (runWatch) return toast("上一个执行还在进行中", "err");
  const fd = new FormData();
  const f = $("wf-file").files[0];
  if (f) {
    if (f.size > 5 * 1024 * 1024) return toast("CSV 超过 5MB 上限", "err");
    fd.append("file", f);
  }
  const ptxt = $("wf-params").value.trim() || "{}";
  try { JSON.parse(ptxt); } catch (e) { return toast("参数不是合法 JSON", "err"); }
  fd.append("params", ptxt);

  const wfId = curWf.id, wfName = curWf.name, defNodes = (curWf.definition.nodes || []);
  busy("wf-run-btn", true, "执行中…");
  let topId = 0;
  const before = await guard(() => get(`/api/workflows/${wfId}/runs`));
  if (before && before.length) topId = before[0].id;

  $("wf-live").style.display = "";
  renderLive({ status: "running", nodes: [] }, defNodes, wfName, "已提交，等待节点执行…");
  runWatch = setInterval(() => pollRun(wfId, topId, defNodes, wfName), 900);
  pollRun(wfId, topId, defNodes, wfName);

  try {
    const d = await post(`/api/workflows/${wfId}/run`, fd, true);
    const det = await guard(() => get(`/api/workflows/runs/${d.run_id}/detail`), "加载运行详情失败");
    if (det) {
      drawRunDetail(det);
      renderLive(det, defNodes, wfName,
        det.status === "success" ? "执行完成，全部节点已成功" : null);
    } else {
      renderLive({ status: d.status, nodes: [], error: d.error }, defNodes, wfName,
        d.status === "success" ? "执行完成" : "执行失败：" + d.error);
    }
    toast(d.status === "success" ? `「${wfName}」执行成功` : "执行失败，见失败节点高亮",
      d.status === "success" ? "ok" : "err");
    await loadRuns();
  } catch (e) {
    renderLive({ status: "failed", nodes: [], error: e.message }, defNodes, wfName, "执行请求失败");
    toast("执行失败：" + e.message, "err");
  } finally {
    stopRunWatch();
    busy("wf-run-btn", false, "执行中…", "▶ 执行工作流");
  }
}
async function pollRun(wfId, topId, defNodes, wfName) {
  if (!runWatch) return;
  if (!curWf || curWf.id !== wfId) return stopRunWatch();
  try {
    const runs = await get(`/api/workflows/${wfId}/runs`);
    if (!runWatch) return;                       // 主流程已结束，别覆盖最终状态
    const fresh = (runs || []).filter((r) => r.id > topId);
    if (!fresh.length) return;                      // 运行行尚未提交
    const det = await get(`/api/workflows/runs/${fresh[0].id}/detail`);
    if (!runWatch) return;
    renderLive(det, defNodes, wfName);
  } catch (e) { /* 轮询期间偶发失败忽略，主流程负责收尾 */ }
}
function stopRunWatch() { if (runWatch) { clearInterval(runWatch); runWatch = null; } }
function renderLive(det, defNodes, wfName, note) {
  const box = $("wf-live");
  if (!box) return;
  const done = {};
  (det.nodes || []).forEach((n) => { done[n.node_id] = n; });
  const total = (defNodes || []).length || Object.keys(done).length;
  const failed = (defNodes || []).filter((n) => done[n.id] && done[n.id].status === "failed").map((n) => n.id);
  const pct = total ? Math.round((Object.keys(done).length / total) * 100) : 0;
  box.innerHTML = `<h3>实时执行 · ${esc(wfName || "")}
      <span class="tag ${det.status === "success" ? "green" : det.status === "failed" ? "red" : "blue"}">${esc(det.status)}</span>
      <span class="tag gray">已执行 ${Object.keys(done).length}/${total}</span></h3>
    <div class="prog"><div class="prog-fill ${det.status === "failed" ? "bad" : ""}" style="width:${det.status === "success" ? 100 : pct}%"></div></div>
    ${note ? `<div class="hint left">${esc(note)}</div>` : ""}
    ${failed.length ? `<div class="fail-tip">失败节点：${failed.map((f) => esc(f)).join("、")}（下方红色高亮）</div>` : ""}
    <div class="node-strip">${(defNodes || []).map((n) => {
      const st = done[n.id] ? done[n.id].status : (det.status === "failed" ? "skipped" : "pending");
      return `<div class="node-chip ${st}"><span class="dot"></span>${esc(n.name || n.id)}<em>${esc(n.type)}</em></div>`;
    }).join("")}</div>`;
  box.scrollIntoView({ block: "nearest" });
}
async function showRunDetail(runId) {
  const d = await guard(() => get(`/api/workflows/runs/${runId}/detail`), "加载运行详情失败");
  if (d) drawRunDetail(d);
}
function drawRunDetail(d) {
  const box = $("wf-run-detail");
  if (!box) return;
  box.style.display = "";
  const failedIds = d.nodes.filter((n) => n.status === "failed").map((n) => n.node_id);
  box.innerHTML = `<h3>运行 #${d.id} <span class="tag ${d.status === "success" ? "green" : "red"}">${esc(d.status)}</span>
      <span class="tag gray">${esc(d.started_at)} → ${esc(d.finished_at || "进行中")}</span>
      ${d.error ? `<div class="fail-tip">${esc(d.error)}</div>` : ""}</h3>
    ${d.nodes.length ? d.nodes.map((n) => `<div class="node-card ${n.status}">
      <div class="ntitle">节点 ${esc(n.node_id)} <span class="tag gray">${esc(n.node_type)}</span>
        <span class="tag ${n.status === "success" ? "green" : "red"}">${esc(n.status)}</span>
        ${n.status === "failed" ? '<span class="tag red">⚠ 失败原因见下</span>' : ""}
        <span class="nspan">${esc(n.started_at)} → ${esc(n.finished_at || "-")}</span></div>
      ${n.error ? `<div class="nerr">${esc(n.error)}</div>` : ""}
      <details><summary>输入 / 输出</summary>
        <pre class="code">输入: ${esc(JSON.stringify(n.input, null, 1))}

输出: ${esc(JSON.stringify(n.output, null, 1))}</pre></details></div>`).join("")
      : emptyBox("该运行没有节点记录", "可能提交后即失败")}
    ${failedIds.length ? `<div class="hint left">共 ${failedIds.length} 个节点失败，工作流已停止推进。</div>` : ""}
    <h3 style="margin-top:10px">最终输出</h3><pre class="code">${esc(JSON.stringify(d.output, null, 2))}</pre>`;
  if (failedIds.length) box.querySelector(".node-card.failed")?.scrollIntoView({ block: "nearest" });
}

/* ============================================================ 管理后台 */
function switchAdminTab(tab) {
  document.querySelectorAll(".list-item[data-atab]").forEach((b) => b.classList.toggle("active", b.dataset.atab === tab));
  document.querySelectorAll(".atab").forEach((p) => { p.style.display = "none"; });
  $("atab-" + tab).style.display = "";
  const jobs = {
    users: loadUsers, assistants: loadAdminAssistants, wfs: loadAdminWfs,
    stats: loadStats, convs: loadAdminConvs, settings: loadSettings,
  };
  if (jobs[tab]) guard(() => jobs[tab](), "加载失败");
}

async function loadUsers() {
  const data = await Promise.all([get("/api/admin/users"), get("/api/admin/assistants")]);
  const [users, assts] = data;
  $("atab-users").innerHTML = `<div class="card">
    <h3>用户与权限 <button class="btn-mini" onclick="editUser(null)">＋新建员工</button></h3>
    <div id="tw-users"></div>
    <div class="hint left">助手可见性规则：助手 departments 为空 = 全部门可用；否则按员工部门匹配；也可在上表为员工显式分配。</div></div>`;
  mountTable("users", "tw-users", [
    { label: "ID", render: (u) => u.id },
    { label: "用户名", render: (u) => esc(u.username) },
    { label: "姓名", render: (u) => esc(u.display_name || "-") },
    { label: "角色", render: (u) => `<span class="tag ${u.role === "admin" ? "" : "gray"}">${esc(u.role)}</span>` },
    { label: "部门", render: (u) => esc(u.department || "-") },
    { label: "显式分配助手", render: (u) => (u.assistant_ids.length ? u.assistant_ids.map((id) =>
        `<span class="tag">${esc((assts.find((a) => a.id === id) || {}).name || "已删除#" + id)}</span>`).join("") : "-") },
    { label: "提问数", render: (u) => u.message_count },
    { label: "操作", render: (u) => `<button class="btn-ghost" data-u='${esc(JSON.stringify(u))}' onclick="editUser(JSON.parse(this.dataset.u))">编辑</button>
        ${u.id !== ME.id ? `<button class="btn-danger" data-deluser="${u.id}" onclick="deleteUser(+this.dataset.deluser)">删除</button>` : '<span class="tag gray">本人</span>'}` },
  ], users, { size: 10, empty: "暂无用户" });
}
function editUser(u) {
  get("/api/admin/assistants").then((assts) => {
    const isEdit = !!u;
    openModal({
      title: isEdit ? `编辑用户：${u.username}` : "新建员工账号",
      wide: true,
      body: `
      ${isEdit ? "" : '<label>用户名</label><input id="m-u-name"><label>初始密码（至少6位，用户首次登录会被要求修改）</label><input id="m-u-pwd" placeholder="设置一个初始口令">'}
      ${isEdit ? '<label>重置密码（留空不修改，至少6位）</label><input id="m-u-pwd">' : ""}
      <label>角色</label><select id="m-u-role"><option value="employee" ${u && u.role === "employee" ? "selected" : ""}>employee 员工</option><option value="admin" ${u && u.role === "admin" ? "selected" : ""}>admin 管理员</option></select>
      <label>部门（多个用英文逗号分隔）</label><input id="m-u-dept" value="${esc(u ? u.department : "")}" placeholder="运营部">
      <label>姓名</label><input id="m-u-disp" value="${esc(u ? u.display_name : "")}">
      <label>显式分配助手（部门规则之外的补充授权）</label>
      <div class="grant-box">${assts.map((a) => `<label class="grant">
        <input type="checkbox" value="${a.id}" ${u && u.assistant_ids.includes(a.id) ? "checked" : ""}> ${esc(a.name)}</label>`).join("") || '<span class="hint left">暂无助手</span>'}</div>`,
      okText: "保存",
      onOk: async () => {
        const grants = [...document.querySelectorAll(".grant-box input:checked")].map((i) => +i.value);
        const pwd = $("m-u-pwd") ? $("m-u-pwd").value : "";
        if (isEdit) {
          await put("/api/admin/users/" + u.id, {
            role: $("m-u-role").value, department: $("m-u-dept").value,
            display_name: $("m-u-disp").value, password: pwd || null, assistant_ids: grants,
          });
        } else {
          if (!pwd) throw new Error("必须设置初始密码（至少 6 位）");
          if (pwd.length < 6) throw new Error("初始密码至少 6 位");
          await post("/api/admin/users", {
            username: $("m-u-name").value.trim(), password: pwd,
            role: $("m-u-role").value, department: $("m-u-dept").value,
            display_name: $("m-u-disp").value, assistant_ids: grants,
          });
        }
        closeModal(); toast("已保存", "ok"); guard(() => loadUsers(), "刷新用户失败");
      },
    });
  }).catch((e) => toast("加载助手失败：" + e.message, "err"));
}
async function deleteUser(id) {
  // 用户名等展示字段从已挂载的表格数据里取：用户输入绝不拼进 onclick 的 JS 字符串
  const u = (TBL.users && TBL.users.rows || []).find((x) => x.id === id);
  const username = u ? u.username : ("#" + id);
  const ok = await confirmModal({
    title: "删除用户", okText: "确认删除",
    text: `将删除用户「${username}」的账号、全部会话与消息记录，不可恢复。`,
  });
  if (!ok) return;
  const r = await withLock("deluser:" + id, () => guard(() => del("/api/admin/users/" + id), "删除失败"));
  if (r === null) return;
  toast("用户已删除", "ok"); guard(() => loadUsers(), "刷新用户失败");
}

function assistantColumns(kbs2) {
  return [
    { label: "名称", render: (a) => `${esc(a.name)}${a.is_builtin ? '<span class="tag green">内置</span>' : ""}
        <div class="li-sub">${esc(a.description)}</div>` },
    { label: "系统提示词", render: (a) => `<div class="clamp2">${esc(a.system_prompt)}</div>` },
    { label: "知识库", render: (a) => a.kb_id ? esc((kbs2.find((k) => k.id === a.kb_id) || {}).name || "已删除#" + a.kb_id) : "-" },
    { label: "Provider / 模型", render: (a) => `<span class="tag ${a.provider === "mock" ? "gray" : "blue"}">${esc(a.provider)}</span><span class="tag gray">${esc(a.model)}</span>` },
    { label: "可用部门", render: (a) => esc(a.departments || "全部门") },
    { label: "操作", render: (a) => `<button class="btn-primary tiny" onclick="quickModelConfig(${a.id})">模型</button>
        <button class="btn-ghost" data-a='${esc(JSON.stringify(a))}' onclick="editAssistant(JSON.parse(this.dataset.a))">编辑</button>
        <button class="btn-danger" data-delasst="${a.id}" onclick="deleteAssistant(+this.dataset.delasst)">删除</button>` },
  ];
}
async function loadAdminAssistants() {
  const [assts, kbs2] = await Promise.all([get("/api/admin/assistants"), get("/api/kb")]);
  $("atab-assistants").innerHTML = `<div class="card">
    <h3>岗位助手配置 <button class="btn-mini" onclick="editAssistant(null)">＋新建助手</button></h3>
    <div id="tw-assts"></div>
    <div class="hint left">新增岗位助手（如供应链助手、财务助手）= 新增一条配置：名称 + 系统提示词 + 绑定知识库 + 选择模型；点行内「模型」可就地换 Provider / 模型名。</div></div>`;
  mountTable("assts", "tw-assts", assistantColumns(kbs2), assts, { size: 6, empty: "暂无助手" });
}
function editAssistant(a) {
  get("/api/kb").then((kbs2) => {
    const isEdit = !!a;
    openModal({
      title: isEdit ? `编辑助手：${a.name}` : "新建岗位助手",
      wide: true,
      body: `
      <label>名称</label><input id="m-a-name" value="${esc(a ? a.name : "")}">
      <label>描述</label><input id="m-a-desc" value="${esc(a ? a.description : "")}">
      <label>系统提示词</label><textarea id="m-a-sp" rows="4">${esc(a ? a.system_prompt : "")}</textarea>
      <label>绑定知识库（可空）</label><select id="m-a-kb"><option value="">不绑定</option>
        ${kbs2.map((k) => `<option value="${k.id}" ${a && a.kb_id === k.id ? "selected" : ""}>${esc(k.name)}（${esc(k.category)}）</option>`).join("")}</select>
      <div class="row2">
        <div><label>Provider</label><select id="m-a-prov">
          <option value="mock" ${a && a.provider === "mock" ? "selected" : ""}>mock（本地确定性模拟）</option>
          <option value="openai_compatible" ${a && a.provider === "openai_compatible" ? "selected" : ""}>openai_compatible</option></select></div>
        <div><label>模型名</label><input id="m-a-model" value="${esc(a ? a.model : "mock-model")}" placeholder="deepseek-chat"></div>
      </div>
      <label>可用部门（逗号分隔，留空=全部门）</label><input id="m-a-depts" value="${esc(a ? a.departments : "")}">`,
      okText: "保存",
      onOk: async () => {
        const body = {
          name: $("m-a-name").value.trim(), description: $("m-a-desc").value, system_prompt: $("m-a-sp").value,
          kb_id: $("m-a-kb").value ? +$("m-a-kb").value : null, provider: $("m-a-prov").value,
          model: $("m-a-model").value.trim() || "mock-model", departments: $("m-a-depts").value,
        };
        if (!body.name) throw new Error("助手名称不能为空");
        if (isEdit) await put("/api/admin/assistants/" + a.id, body);
        else await post("/api/admin/assistants", body);
        closeModal(); toast("已保存", "ok");
        guard(() => loadAdminAssistants(), "刷新助手失败");
      },
    });
  }).catch((e) => toast("加载知识库失败：" + e.message, "err"));
}
async function deleteAssistant(id) {
  const a = (TBL.assts && TBL.assts.rows || []).find((x) => x.id === id);
  const name = a ? a.name : ("#" + id);
  const ok = await confirmModal({
    title: "删除助手", okText: "确认删除",
    text: `将删除助手「${name}」及其全部会话记录（员工端将不再看到该助手）。`,
  });
  if (!ok) return;
  const r = await withLock("delasst:" + id, () => guard(() => del("/api/admin/assistants/" + id), "删除失败"));
  if (r === null) return;
  toast("助手已删除", "ok");
  if (curAssistant && curAssistant.id === id) { curAssistant = null; curConv = null; }
  guard(() => loadAdminAssistants(), "刷新助手失败");
  if ($("page-chat").style.display !== "none") guard(() => loadAssistants(), "刷新助手列表失败");
}

/* 内联模型配置：入口挂在助手行旁，不再要求跳到大面板逐项翻 */
async function quickModelConfig(id) {
  const res = await guard(() => Promise.all([get("/api/assistants/" + id), get("/api/admin/settings")]), "加载模型配置失败");
  if (!res) return;
  const [a, s] = res;
  const readyTip = s.openai_base_url && s.openai_api_key_set;
  openModal({
    title: `模型配置 · ${a.name}`,
    body: `<div id="qmc-box">
      <label>Provider</label>
      <select id="qmc-prov">
        <option value="mock" ${a.provider === "mock" ? "selected" : ""}>mock（离线确定性模拟回复）</option>
        <option value="openai_compatible" ${a.provider === "openai_compatible" ? "selected" : ""}>openai_compatible（OpenAI / DeepSeek / 通义 / vLLM / Ollama…）</option>
      </select>
      <label>模型名</label><input id="qmc-model" value="${esc(a.model)}" placeholder="mock-model 或 deepseek-chat">
      <div id="qmc-global" class="sub-card">
        <div class="sub-title">全局 OpenAI 兼容端点（保存时一并写入）</div>
        <label>Base URL</label><input id="qmc-url" value="${esc(s.openai_base_url)}" placeholder="https://api.deepseek.com/v1">
        <label>API Key${s.openai_api_key_set ? "（已配置，留空=保持不变）" : "（未配置）"}</label>
        <input id="qmc-key" type="password" placeholder="sk-...">
      </div>
      <div class="hint left" id="qmc-tip"></div>
    </div>`,
    okText: "保存",
    onOk: async () => {
      const provider = $("qmc-prov").value;
      const model = $("qmc-model").value.trim() || (provider === "mock" ? "mock-model" : (s.openai_model || "gpt-4o-mini"));
      await put("/api/admin/assistants/" + a.id, {
        name: a.name, description: a.description, system_prompt: a.system_prompt,
        kb_id: a.kb_id, provider, model, departments: a.departments,
      });
      const url = $("qmc-url").value.trim(), key = $("qmc-key").value.trim();
      if (url !== (s.openai_base_url || "") || key) {
        await put("/api/admin/settings", { openai_base_url: url, ...(key ? { openai_api_key: key } : {}) });
      }
      closeModal(); toast("模型配置已保存", "ok");
      if ($("atab-assistants").style.display !== "none") guard(() => loadAdminAssistants(), "刷新助手失败");
      if ($("atab-settings").style.display !== "none") guard(() => loadSettings(), "刷新设置失败");
      if ($("page-chat").style.display !== "none") guard(() => loadAssistants(), "刷新助手列表失败");
    },
  });
  const tip = $("qmc-tip"), glob = $("qmc-global"), prov = $("qmc-prov");
  const sync = () => {
    const isOpen = prov.value === "openai_compatible";
    glob.style.display = isOpen ? "" : "none";
    tip.innerHTML = isOpen
      ? (readyTip || $("qmc-url").value.trim() && $("qmc-key").value.trim()
        ? "端点已配置：保存后该助手将真实调用外部模型（失败会返回 502 提示，不影响其它助手）。"
        : "⚠ 当前 Base URL / API Key 尚未配置完整，保存后与该助手对话会返回 502「模型调用失败」，可随时切回 mock。")
      : "mock 为本地确定性回复，用于离线演示与测试，不产生任何 API 费用。";
  };
  prov.addEventListener("change", sync);
  $("qmc-url").addEventListener("input", sync);
  $("qmc-key").addEventListener("input", sync);
  sync();
}

/* ============================================================ 后台：工作流配置 */
// 新建时的骨架：本身就用了 {{路径|默认值}}，改改就能跑（复制即用）
const WF_SKELETON = JSON.stringify({
  nodes: [
    { id: "start", type: "start", name: "开始" },
    { id: "api", type: "http_api", name: "取数(内置模拟API)", config: {
        method: "GET", url: "http://127.0.0.1:8005/api/mock/ads-data",
        params: { campaign: "{{input.params.campaign|双11大促}}" } } },
    { id: "calc", type: "code", name: "指标计算", config: {
        vars: { spend: "{{api.data.spend}}", sales: "{{api.data.sales}}" },
        expression: "{'acos_pct': round(spend / sales * 100, 2)}" } },
    { id: "report", type: "llm", name: "AI分析", config: {
        provider: "mock", model: "mock-data-analysis",
        prompt: "指标：{{calc.result}}，请给出结论。" } },
    { id: "end", type: "end", name: "输出", config: {
        output: { metrics: "{{api.data}}", report: "{{report.text}}" } } },
  ],
  edges: [
    { from: "start", to: "api" }, { from: "api", to: "calc" },
    { from: "calc", to: "report" }, { from: "report", to: "end" },
  ],
}, null, 1);

const WF_SYNTAX_TIP = `节点类型：<b>start / llm / knowledge_search / http_api / code / condition / end</b>；
  边可带 <code>condition</code>（true/false 匹配 condition 节点结果，或写安全表达式）。
  配置里的字符串支持模板：<code>{{节点id.路径}}</code> 取上游输出、<code>{{input.params.x}}</code> 取运行参数、
  <code>{{input.params.x|默认值}}</code> 在参数未传时用默认值（默认值支持 <code>5 / true / null / [] / "文本"</code>，
  不写默认值且路径缺失会让节点失败而不是静默传空）。
  保存时即校验 DAG 与 code/condition 表达式安全性，非法定义会返回 400 并点名具体节点。`;

async function loadAdminWfs() {
  const list = await guard(() => get("/api/workflows"), "加载工作流失败");
  if (!list) return;
  $("atab-wfs").innerHTML = `<div class="card">
    <h3>工作流定义 <button class="btn-mini" id="wf-new-btn" onclick="editWf(null)">＋新建工作流</button></h3>
    <div id="tw-adminwfs"></div>
    <div class="hint left">${WF_SYNTAX_TIP}</div></div>`;
  mountTable("adminwfs", "tw-adminwfs", [
    { label: "ID", render: (w) => w.id },
    { label: "名称", render: (w) => `${esc(w.name)}${w.is_builtin ? '<span class="tag green">内置</span>' : ""}
        <div class="li-sub">${esc(w.description || "无描述")}</div>` },
    { label: "节点", render: (w) => w.node_count },
    { label: "节点类型", render: (w) => (w.node_types || []).map((t) => `<span class="tag gray">${esc(t)}</span>`).join("") },
    { label: "创建时间", render: (w) => esc(w.created_at) },
    { label: "操作", render: (w) => `<button class="btn-ghost" data-w='${esc(JSON.stringify(w))}' onclick="editWf(JSON.parse(this.dataset.w))">编辑</button>
        <button class="btn-danger" data-delwf="${w.id}" onclick="deleteWf(+this.dataset.delwf)">删除</button>` },
  ], list, { size: 6, empty: "还没有工作流，点右上「＋新建工作流」" });
}

async function editWf(w) {
  const isEdit = !!w;
  let defnText = WF_SKELETON, desc = "";
  if (isEdit) {
    const full = await guard(() => get("/api/workflows/" + w.id), "加载定义失败");
    if (!full) return;
    defnText = JSON.stringify(full.definition, null, 1);
    desc = full.description || "";
  }
  openModal({
    title: isEdit ? `编辑工作流：${w.name}` : "新建工作流",
    wide: true,
    body: `
      <label>名称</label><input id="m-w-name" value="${esc(isEdit ? w.name : "")}" placeholder="如：库存周转分析">
      <label>描述（员工在工作流页可见）</label><input id="m-w-desc" value="${esc(desc)}">
      <label>DAG 定义（JSON）</label>
      <textarea id="m-w-def" rows="14" class="mono" spellcheck="false">${esc(defnText)}</textarea>
      <div class="hint left" style="margin-top:8px">${WF_SYNTAX_TIP}</div>`,
    okText: "保存",
    onOk: async () => {
      const name = $("m-w-name").value.trim();
      if (!name) throw new Error("工作流名称不能为空");
      let definition;
      try { definition = JSON.parse($("m-w-def").value); }
      catch (e) { throw new Error("定义不是合法 JSON：" + e.message); }
      const body = { name, description: $("m-w-desc").value.trim(), definition };
      busy("modal-ok", true, "保存中…");
      let saved;
      try {
        // 不用 guard：让后端的 400 细节（哪个节点/哪条边非法）原样冒泡到弹窗提示，且弹窗保持打开
        saved = await withLock(isEdit ? "wfcfg:" + w.id : "wfnew",
          () => (isEdit ? put("/api/workflows/" + w.id, body) : post("/api/workflows", body)));
      } finally {
        busy("modal-ok", false, "保存中…", "保存");
      }
      if (saved === null) throw new Error("上一次保存还在处理中，请稍候");
      closeModal(); toast(isEdit ? "工作流已更新" : "工作流已创建", "ok");
      guard(() => loadAdminWfs(), "刷新工作流失败");
      if ($("page-wf").style.display !== "none") guard(() => loadWorkflows(), "刷新工作流失败");
    },
  });
}

async function deleteWf(id) {
  const w = (TBL.adminwfs && TBL.adminwfs.rows || []).find((x) => x.id === id);
  const name = w ? w.name : ("#" + id);
  const builtin = w ? !!w.is_builtin : false;
  const ok = await confirmModal({
    title: "删除工作流", okText: "确认删除",
    text: `将删除「${name}」及其全部执行记录，不可恢复。`
      + (builtin ? "注意：这是内置示例，删除后需重新初始化数据库才会回来。" : ""),
  });
  if (!ok) return;
  const r = await withLock("delwf:" + id, () => guard(() => del("/api/workflows/" + id), "删除失败"));
  if (r === null) return;
  toast("工作流已删除", "ok");
  if (curWf && curWf.id === id) curWf = null;
  guard(() => loadAdminWfs(), "刷新工作流失败");
  if ($("page-wf").style.display !== "none") guard(() => loadWorkflows(), "刷新工作流失败");
}

function barRows(items, labelKey, valKey) {
  if (!items.length) return emptyBox("暂无数据");
  const max = Math.max(1, ...items.map((i) => i[valKey]));
  return items.map((i) => `<div class="bar-row"><div class="bar-label" title="${esc(i[labelKey])}">${esc(i[labelKey])}</div>
    <div class="bar-track"><div class="bar-fill" style="width:${Math.round(i[valKey] / max * 100)}%"></div></div>
    <div class="bar-val">${esc(i[valKey])}</div></div>`).join("");
}
async function loadStats() {
  const s = await guard(() => get("/api/admin/stats"), "加载统计失败");
  if (!s) return;
  const llm = s.llm || {};
  const llmCards = [
    ["LLM 调用", llm.calls || 0],
    ["成功 / 降级 / 失败", `${llm.success || 0} / ${llm.degraded || 0} / ${llm.failed || 0}`],
    ["tokens ↑" + (llm.prompt_tokens || 0) + " ↓" + (llm.completion_tokens || 0), `平均延迟 ${llm.avg_latency_ms || 0}ms`],
    ["平均尝试次数", llm.avg_attempts ?? 1],
  ];
  $("atab-stats").innerHTML = `
    <div class="card"><h3>总览</h3><div class="stat-cards">
      ${[["用户数", s.totals.users], ["助手数", s.totals.assistants], ["会话数", s.totals.conversations],
        ["消息数", s.totals.messages], ["知识文档", s.totals.documents], ["工作流执行", s.totals.workflow_runs]]
        .map(([l, n]) => `<div class="stat-card"><div class="num">${esc(n)}</div><div class="lbl">${l}</div></div>`).join("")}
    </div></div>
    <div class="card"><h3>模型网关用量（重试 / 降级 / token 计量）</h3><div class="stat-cards">
      ${llmCards.map(([l, n]) => `<div class="stat-card"><div class="num">${esc(n)}</div><div class="lbl">${l}</div></div>`).join("")}
    </div>
    ${llm.by_assistant && llm.by_assistant.length
      ? `<div class="scroll-box">${barRows(llm.by_assistant.map((a) => ({ k: `${a.name}（降级${a.degraded}次）`, v: a.calls })), "k", "v")}</div>`
      : '<div class="hint left">还没有模型调用记录</div>'}</div>
    <div class="card"><h3>每用户消息数（含提问与回复）</h3><div class="scroll-box">${barRows(s.per_user.map((u) => ({ k: `${u.username}(${u.department || "-"})`, v: u.message_count })), "k", "v")}</div></div>
    <div class="card"><h3>每助手消息数</h3><div class="scroll-box">${barRows(s.per_assistant.map((a) => ({ k: a.name, v: a.message_count })), "k", "v")}</div></div>
    <div class="card"><h3>近7日活跃</h3><div class="scroll-box">
      ${barRows(s.daily_active_7d.map((d) => ({ k: d.date + " 活跃用户", v: d.active_users })), "k", "v")}
      ${barRows(s.daily_active_7d.map((d) => ({ k: d.date + " 消息数", v: d.messages })), "k", "v")}
    </div></div>`;
}

async function loadAdminConvs() {
  const convs = await guard(() => get("/api/admin/conversations"), "加载对话记录失败");
  if (!convs) return;
  $("atab-convs").innerHTML = `<div class="card"><h3>全部对话记录（所有用户，最多 200 条）</h3>
    <div id="tw-convs"></div><div id="conv-detail"></div></div>`;
  mountTable("convs", "tw-convs", [
    { label: "ID", render: (c) => c.id },
    { label: "用户", render: (c) => esc(c.username) },
    { label: "助手", render: (c) => esc(c.assistant_name) },
    { label: "标题", render: (c) => esc(c.title) },
    { label: "消息数", render: (c) => c.message_count },
    { label: "更新时间", render: (c) => esc(c.updated_at) },
    { label: "操作", render: (c) => `<button class="btn-ghost" onclick="viewConv(${c.id})">查看</button>` },
  ], convs, { size: 10, empty: "还没有人产生对话" });
}
async function viewConv(id) {
  const msgs = await guard(() => get(`/api/admin/conversations/${id}/messages`), "加载消息失败");
  if (!msgs) return;
  $("conv-detail").innerHTML = `<h3 style="margin-top:10px">会话 #${id} 消息明细</h3>` +
    (msgs.length ? msgs.map((m) => `<div class="msg ${m.role === "user" ? "user" : "bot"}" style="max-width:100%">
        ${esc(m.content)}${refsHtml(m.refs)}
        <div class="msg-meta">${esc(m.created_at)}</div></div>`).join("")
      : emptyBox("该会话还没有消息"));
  $("conv-detail").scrollIntoView({ block: "nearest" });
}

async function loadSettings() {
  const [s, assts] = await guard(() => Promise.all([get("/api/admin/settings"), get("/api/admin/assistants")]), "加载模型设置失败");
  if (!s) return;
  $("atab-settings").innerHTML = `<div class="card">
    <h3>全局模型端点（OpenAI 兼容）</h3>
    <div class="hint left">平台内置两类 Provider：<b>mock</b>（确定性模拟，演示/测试用）与 <b>openai_compatible</b>（任意 OpenAI 兼容端点）。
    单个助手用哪个模型，请直接到下方助手行点「模型」就地配置。当前配置来源：${s.from_env ? "环境变量" : "数据库"}。</div>
    <label>Base URL（如 https://api.deepseek.com/v1）</label><input id="m-s-url" value="${esc(s.openai_base_url)}">
    <label>API Key（${s.openai_api_key_set ? "已配置" : "未配置"}；本框留空并保存 = 保持原值不覆盖）</label>
    <input id="m-s-key" type="password" placeholder="${s.openai_api_key_set ? "已配置，如需更换请输入新值" : "sk-..."}">
    <label>默认模型名（助手未填模型名时使用）</label><input id="m-s-model" value="${esc(s.openai_model)}">
    <div class="row" style="margin-top:10px">
      <button class="btn-primary" id="s-save-btn" onclick="saveSettings()">保存设置</button>
      <button class="btn-ghost" onclick="reloadSettings()">放弃修改</button>
    </div></div>
    <div class="card"><h3>各助手当前模型</h3><div id="tw-asstmodel"></div>
      <div class="hint left">切到 openai_compatible 但 Base URL / API Key 未配置时，对话会返回明确的 502 提示而不是崩溃。</div></div>`;
  mountTable("asstmodel", "tw-asstmodel", [
    { label: "助手", render: (a) => esc(a.name) },
    { label: "Provider", render: (a) => `<span class="tag ${a.provider === "mock" ? "gray" : "blue"}">${esc(a.provider)}</span>` },
    { label: "模型名", render: (a) => `<span class="tag gray">${esc(a.model)}</span>` },
    { label: "可用部门", render: (a) => esc(a.departments || "全部门") },
    { label: "操作", render: (a) => `<button class="btn-primary tiny" onclick="quickModelConfig(${a.id})">配置模型</button>` },
  ], assts, { size: 8, empty: "暂无助手" });
}
function reloadSettings() { guard(() => loadSettings(), "刷新设置失败"); }
async function saveSettings() {
  busy("s-save-btn", true, "保存中…");
  const key = $("m-s-key").value.trim();
  const r = await guard(() => put("/api/admin/settings", {
    openai_base_url: $("m-s-url").value.trim(),
    ...(key ? { openai_api_key: key } : {}),        // 留空不覆盖已配置的 Key
    openai_model: $("m-s-model").value.trim(),
  }), "保存失败");
  busy("s-save-btn", false, "保存中…", "保存设置");
  if (r === null) return;
  toast("模型设置已保存", "ok");
  guard(() => loadSettings(), "刷新设置失败");
}

/* ============================================================ 启动 */
(async function init() {
  if (TOKEN) {
    // 校验本地 token 期间先藏住登录页：否则会闪出一个"可点"的登录表单，
    // 用户再点一次就会并行跑两条 enterMain 链路，把助手/会话选中态写乱。
    $("view-login").style.display = "none";
    try {
      await enterMain();
      if (ME.must_change_password) await forceChangePassword();   // 恢复登录态也要先改密
      return;
    }
    catch (e) { logoutLocal(); toast("登录状态已失效，请重新登录", "err"); }
  }
  $("view-login").style.display = "";
  setChatEnabled(false);
})();
