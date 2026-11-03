"use strict";
const $ = (s, root = document) => root.querySelector(s);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const paths = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  chat: "M21 11a8 8 0 0 1-8 8H7l-4 3V5a2 2 0 0 1 2-2h8a8 8 0 0 1 8 8Z M7 8h10 M7 12h6",
  tasks: "M9 5h11 M9 12h11 M9 19h11 M3 5l1 1 2-3 M3 12l1 1 2-3 M3 19l1 1 2-3",
  memory: "M5 5h14v16H5z M8 2v6 M12 2v6 M16 2v6 M8 12h8 M8 16h5",
  clock: "M12 8v5l3 2 M22 12A10 10 0 1 1 2 12a10 10 0 0 1 20 0Z",
  link: "m10 13 4-4 M8 16l-2 2a4 4 0 0 1-5-5l5-5a4 4 0 0 1 5 0 M13 8l2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-5 0",
  evolve: "M4 16 10 10l4 4 7-9 M15 5h6v6 M3 21h18",
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.grid}"/></svg>`;
document.querySelectorAll("[data-icon]").forEach((el) => {
  el.innerHTML = icon(el.dataset.icon);
});
const labels = {
  overview: "概览",
  chat: "与助手对话",
  tasks: "任务",
  memory: "长期记忆",
  schedules: "定时任务",
  channels: "消息渠道",
  evolution: "进化记录",
};
const statusLabels = {
  queued: "排队中",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  cancelling: "取消中",
  cancelled: "已取消",
  interrupted: "已中断",
  waiting: "待回复",
  sending: "回复中",
  sent: "已回复",
  uncertain: "状态待确认",
  pending: "待验证",
  validating: "验证中",
  merged: "已合并",
  integrated: "已集成",
  rejected: "已拒绝",
  conflict: "冲突",
  active: "已启用",
  disabled: "未启用",
};
const badge = (status) =>
  `<span class="pill ${esc(status)}">${esc(statusLabels[status] || status)}</span>`;
const fmtDate = (ts) =>
  ts
    ? new Date(ts * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const empty = (title, hint, name = "tasks") =>
  `<div class="empty"><span class="empty-icon">${icon(name)}</span><b>${esc(title)}</b>${esc(hint)}</div>`;
const head = (name, subtitle, action = "") =>
  `<div class="page-head"><div><p class="eyebrow">${name === "overview" ? "WORKSPACE OVERVIEW" : "EVOMESH / " + name.toUpperCase()}</p><h1>${labels[name]}</h1><p>${subtitle}</p></div><div class="head-actions">${action}</div></div>`;
const panel = (title, body, link = "") =>
  `<section class="panel"><div class="panel-title"><h2>${title}</h2>${link}</div>${body}</section>`;
const channelNames = {
  feishu: "飞书 / Lark",
  dingtalk: "钉钉",
  telegram: "Telegram",
};
const channelIcons = { feishu: "飞", dingtalk: "钉", telegram: "↗" };
const channelIcon = (kind) =>
  `<span class="channel-icon ${esc(kind)}">${channelIcons[kind] || "↗"}</span>`;
let token = sessionStorage.getItem("evomesh.token") || "";
let page = "overview",
  generation = 0,
  currentStatus = {},
  session = sessionStorage.getItem("evomesh.session") || "main";
let activeTask = null,
  pendingPrompt = "",
  chatHistory = [],
  pollBusy = false,
  toastTimer;
let cachedTasks = [],
  cachedProposals = [];

async function api(path, data) {
  const response = await fetch(path, {
    method: data === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      ...(data === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  const body = await response.json();
  if (response.status === 401) {
    disconnect();
    throw new Error("令牌无效或已失效，请重新连接。");
  }
  if (!response.ok)
    throw new Error(body.error || `请求失败 (${response.status})`);
  return body;
}
function toast(text) {
  clearTimeout(toastTimer);
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("#toast").hidden = true;
  }, 4500);
}
function disconnect() {
  activeTask = null;
  pendingPrompt = "";
  chatHistory = [];
  token = "";
  sessionStorage.removeItem("evomesh.token");
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#detail").close();
  generation++;
}
async function connect() {
  currentStatus = await api("/status");
  sessionStorage.setItem("evomesh.token", token);
  $("#login").hidden = true;
  $("#shell").hidden = false;
  $("#token").value = "";
  $("#project").textContent = currentStatus.project;
  $("#owner").textContent = currentStatus.owner;
  $("#version").textContent = "v" + currentStatus.version;
  await render();
}
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  token = $("#token").value.trim();
  $("#login-error").textContent = "";
  try {
    await connect();
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("#logout").addEventListener("click", disconnect);
$("#refresh").addEventListener("click", () => render());
$("#close-detail").addEventListener("click", () => $("#detail").close());
window.addEventListener("hashchange", () => {
  if (token) render();
});
function showDetail(title, html) {
  $("#detail-title").textContent = title;
  $("#detail-body").innerHTML = html;
  if (!$("#detail").open) $("#detail").showModal();
}

function taskTable(tasks, compact = false) {
  if (!tasks.length)
    return empty("还没有任务", "发起一次对话，执行记录就会出现在这里。");
  return `<div class="table-wrap"><table><thead><tr><th>任务 / 会话</th><th>状态</th>${compact ? "" : "<th>执行次数</th>"}<th>创建时间</th></tr></thead><tbody>${tasks.map((t) => `<tr><td><button class="task-link" data-task="${esc(t.id)}">${esc(t.prompt)}</button><small>${esc(t.session).slice(0, 34)}</small></td><td>${badge(t.status)}</td>${compact ? "" : `<td>${t.attempts}</td>`}<td><small>${fmtDate(t.created_at)}</small></td></tr>`).join("")}</tbody></table></div>`;
}
async function render(quiet = false) {
  if (!token) return;
  const next = location.hash.slice(1);
  page = labels[next] ? next : "overview";
  const id = ++generation;
  document.querySelectorAll("[data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === page);
    if (a.dataset.page === page) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  $("#breadcrumb").textContent = labels[page];
  document.title = `${labels[page]} · EvoMesh`;
  if (!quiet)
    $("#main").innerHTML = '<div class="loading">正在读取工作空间…</div>';
  try {
    let html;
    if (page === "overview") html = await overview();
    if (page === "tasks") {
      cachedTasks = await api("/tasks");
      html =
        head(
          "tasks",
          "任务持久化在共享数据库中，可查看执行过程、取消或手动重试。",
          '<button class="primary" data-action="new-task">＋ 新建任务</button>',
        ) + panel("最近 100 个任务", taskTable(cachedTasks));
    }
    if (page === "memory") html = await memory();
    if (page === "schedules") html = await schedules();
    if (page === "channels") html = await channels();
    if (page === "evolution") html = await evolution();
    if (page === "chat") html = await chat();
    if (id !== generation || !token) return;
    $("#main").innerHTML = html;
    if (page === "chat") {
      drawMessages();
      if (activeTask) updateRunStatus("正在等待执行…");
    }
    $("#connection").innerHTML = '<i class="dot"></i>已连接';
  } catch (error) {
    if (id !== generation) return;
    $("#connection").textContent = "连接异常";
    if (!quiet) $("#main").innerHTML = empty("暂时无法读取", error.message);
    toast(error.message);
  }
}
async function overview() {
  const [status, tasks, channels, evo] = await Promise.all([
    api("/status"),
    api("/tasks"),
    api("/channels"),
    api("/evolution"),
  ]);
  currentStatus = status;
  const total = Object.values(status.tasks).reduce((sum, n) => sum + n, 0),
    enabled = channels.filter((c) => c.enabled && c.ready).length;
  const countType = (type) =>
    evo.proposals.filter((p) => p.target === type).length;
  const bars = ["skill", "tool", "soul", "loop", "code"]
    .map(
      (type) =>
        `<div class="bar-row"><div><span>${{ skill: "技能 Skill", tool: "工具 Tool", soul: "人格 Soul", loop: "推理循环 Loop", code: "核心代码 Code" }[type]}</span><small>${countType(type)} 项</small></div><progress aria-label="${type} 提案数" max="${Math.max(1, evo.proposals.length)}" value="${countType(type)}"></progress></div>`,
    )
    .join("");
  const connections = channels.length
    ? channels
        .map(
          (c) =>
            `<div class="connection-row">${channelIcon(c.kind)}<div><strong>${esc(c.name)}</strong><small>${esc(channelNames[c.kind] || c.kind)}</small></div>${badge(c.enabled && c.ready ? "active" : "disabled")}</div>`,
        )
        .join("")
    : empty("连接你习惯的消息应用", "支持飞书、钉钉与 Telegram。", "link");
  return (
    head(
      "overview",
      "把执行、连接与进化，放在同一个工作空间。",
      `<a href="#chat" class="primary small">＋ 开始对话</a>`,
    ) +
    `<section class="hero"><div><p class="eyebrow">GROW THROUGH EVERY TASK</p><h2>每一次执行，<br>都是下一次进化的起点。</h2><p>从技能到工具，从人格到代码。让多个实例共享经验，持续改进。</p><a href="#evolution" class="primary">查看进化记录 ↗</a></div><div class="hero-art" aria-hidden="true"><span>e•</span><i></i></div></section>` +
    `<div class="metrics">${[
      [
        total,
        "累计任务",
        `${status.tasks.succeeded || 0} 个任务已完成`,
        "tasks",
      ],
      [
        (status.tasks.queued || 0) + (status.tasks.running || 0),
        "进行中的任务",
        `${status.tasks.queued || 0} 个排队 · ${status.tasks.running || 0} 个执行中`,
        "clock",
      ],
      [enabled, "就绪的消息渠道", `${channels.length} 个已配置账号`, "link"],
      [
        evo.proposals.length,
        "近期进化提案",
        `${evo.observations.length} 条近期体验记录`,
        "evolve",
      ],
    ]
      .map(
        ([n, title, sub, ico]) =>
          `<div class="metric"><div class="metric-label">${title}${icon(ico)}</div><strong>${n}</strong><small>${sub}</small></div>`,
      )
      .join("")}</div>` +
    `${status.demo ? '<div class="notice">当前为演示模式：助手会列出工作区文件。配置真实模型后即可进行自由对话和工具调用。</div>' : ""}` +
    `<div class="columns"><div>${panel("最近任务", taskTable(tasks.slice(0, 5), true), '<a href="#tasks">查看全部 ↗</a>')}${panel("消息渠道", connections, '<a href="#channels">管理连接 ↗</a>')}</div><div>${panel("全栈进化分布", `<div class="panel-body">${bars}<div class="stack-tags"><span>EXPERIENCE → PROPOSAL → VALIDATION</span></div></div>`, '<a href="#evolution">↗</a>')}${panel("运行环境", `<div class="panel-body"><div class="bar-row"><div><span>模型</span><small>${esc(status.model || "未配置")}</small></div></div><div class="bar-row"><div><span>共享存储</span><small>${esc(status.database.toUpperCase())}</small></div></div><div class="bar-row"><div><span>当前实例</span><small>${esc(status.instance)}</small></div></div><div class="bar-row"><div><span>内置工作进程</span><small>${status.embedded_worker ? "运行中" : "未启动 / 使用外部 worker"}</small></div></div>${status.worker_error ? `<p class="danger">${esc(status.worker_error)}</p>` : ""}</div>`)}</div></div>`
  );
}
async function memory() {
  const rows = await api("/memory");
  return (
    head(
      "memory",
      "保留偏好、约定与上下文；同一 owner 的助手会共享这些记忆。",
    ) +
    panel(
      "添加或更新记忆",
      `<form id="memory-form" class="panel-body form-grid"><div><label for="memory-key">记忆名称</label><input id="memory-key" name="key" maxlength="128" placeholder="例如：回复偏好" required></div><div><label for="memory-content">内容</label><textarea id="memory-content" name="content" maxlength="12000" placeholder="希望助手长期记住什么？" required></textarea></div><button class="primary">保存记忆</button></form>`,
    ) +
    (rows.length
      ? `<div class="memory-grid">${rows.map((row) => `<article class="memory-card"><h3>${esc(row.key)}<button data-forget="${esc(row.key)}" aria-label="删除记忆 ${esc(row.key)}">删除</button></h3><p>${esc(row.content)}</p></article>`).join("")}</div>`
      : empty(
          "从一条偏好开始",
          "保存后，助手会在后续任务中使用这些记忆。",
          "memory",
        ))
  );
}
async function schedules() {
  const rows = await api("/schedules");
  return (
    head(
      "schedules",
      "按固定间隔创建任务。工作进程会自动领取，错过的周期合并为一次。",
    ) +
    panel(
      "创建定时任务",
      `<form id="schedule-form" class="panel-body form-grid"><div><label for="interval">间隔（分钟）</label><input id="interval" name="minutes" type="number" min="1" step="1" value="60" required></div><div><label for="schedule-prompt">要执行的任务</label><input id="schedule-prompt" name="task" maxlength="12000" placeholder="例如：整理工作区中的新文件" required></div><button class="primary">创建计划</button></form>`,
    ) +
    panel(
      "全部计划",
      rows.length
        ? `<div class="table-wrap"><table><thead><tr><th>任务</th><th>间隔</th><th>下次执行</th><th>状态</th><th></th></tr></thead><tbody>${rows.map((row) => `<tr><td>${esc(row.prompt)}</td><td>${Math.round(row.interval_seconds / 60)} 分钟</td><td>${row.enabled ? fmtDate(row.next_run) : "—"}</td><td>${badge(row.enabled ? "active" : "disabled")}</td><td>${row.enabled ? `<button class="small" data-disable="${esc(row.id)}">停用</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`
        : empty("把重复工作交给助手", "创建第一个周期任务。", "clock"),
    )
  );
}
const setup = {
  feishu: `[evomesh.channels.feishu]\nkind = "feishu"\napp_id_env = "FEISHU_APP_ID"\napp_secret_env = "FEISHU_APP_SECRET"\nverification_token_env = "FEISHU_VERIFICATION_TOKEN"\nencrypt_key_env = "FEISHU_ENCRYPT_KEY"\nallowed_senders = ["ou_your_open_id"]\nallowed_groups = []`,
  dingtalk: `[evomesh.channels.dingtalk]\nkind = "dingtalk"\napp_secret_env = "DINGTALK_APP_SECRET"\nallowed_senders = ["your_senderId"]\nallowed_groups = []`,
  telegram: `[evomesh.channels.telegram]\nkind = "telegram"\nbot_token_env = "TELEGRAM_BOT_TOKEN"\nwebhook_secret_env = "TELEGRAM_WEBHOOK_SECRET"\nallowed_senders = ["your_numeric_user_id"]\nallowed_groups = []`,
};
const setupSteps = {
  feishu:
    '在飞书开放平台创建企业自建应用并启用机器人，订阅 im.message.receive_v1，授予消息接收和以应用身份发送消息权限。在事件订阅中设置 Verification Token 和 Encrypt Key，将请求地址设为下面的 HTTPS 回调地址。Lark 国际版添加 region = "lark"。',
  dingtalk:
    "创建钉钉企业内部应用机器人，选择 HTTP 消息接收模式，将消息接收地址设为下面的 HTTPS 回调地址。环境变量使用应用 AppSecret。allowed_senders 填回调中的 senderId（不是 senderStaffId）。",
  telegram:
    "通过 BotFather 创建机器人，把 Bot Token 写入环境变量。调用 Telegram setWebhook 接口，url 为下面的 HTTPS 回调地址，secret_token 与 TELEGRAM_WEBHOOK_SECRET 一致；仅订阅 message 更新。",
};
async function channels() {
  const [accounts, deliveries] = await Promise.all([
    api("/channels"),
    api("/deliveries"),
  ]);
  return (
    head(
      "channels",
      "把助手连接到日常沟通工具。所有渠道共用任务系统和进化记录。",
    ) +
    `<div class="notice">仅接收明确允许的发送者；群聊还需单独加入允许名单。同一工作空间的渠道共享 owner 记忆和工具权限，适合你本人及可信成员。</div><div class="channel-grid">${Object.keys(
      channelNames,
    )
      .map((kind) => {
        const matches = accounts.filter((c) => c.kind === kind);
        return `<article class="channel-card"><div>${channelIcon(kind)}<span class="pill">TEXT · WEBHOOK</span></div><h2>${channelNames[kind]}</h2><p>${{ feishu: "加密事件校验 · 原消息回复", dingtalk: "企业机器人 · 会话 Webhook 回复", telegram: "Bot API · 私聊与群聊" }[kind]}</p>${matches.length ? matches.map((c) => `<div class="account"><strong>${esc(c.name)}</strong> ${badge(c.enabled && c.ready ? "active" : "disabled")}<br><code>${esc(c.webhook_path)}</code><br><small>${c.allowed_senders.length} 位可信发送者 · ${c.allowed_groups.length} 个群聊</small>${c.missing_env.length ? `<p class="danger">缺少环境变量：${esc(c.missing_env.join(", "))}</p>` : ""}${!c.allowed_senders.length ? '<p class="danger">请先配置发送者允许名单</p>' : ""}</div>`).join("") : '<div class="account muted">尚未配置账号</div>'}<button class="small" data-setup="${kind}">查看接入配置 ↗</button></article>`;
      })
      .join("")}</div>` +
    panel(
      "最近 100 条消息回复",
      deliveries.length
        ? `<div class="table-wrap"><table><thead><tr><th>渠道 / 任务</th><th>回复状态</th><th>接收时间</th><th>操作</th></tr></thead><tbody>${deliveries.map((d) => `<tr><td>${esc(d.channel)}<button class="task-link" data-task="${esc(d.task_id)}"><small>${esc(d.task_id).slice(0, 16)} ↗</small></button></td><td>${badge(d.status)}${d.error ? `<small>${esc(d.error)}</small>` : ""}</td><td>${fmtDate(d.created_at)}</td><td>${["failed", "uncertain"].includes(d.status) ? `<button class="small" data-reply-retry="${esc(d.id)}">重试回复</button>` : "—"}</td></tr>`).join("")}</tbody></table></div>`
        : empty(
            "等待第一条消息",
            "完成平台配置后，接收与回复记录会自动出现在这里。",
            "link",
          ),
    )
  );
}
async function evolution() {
  const data = await api("/evolution");
  cachedProposals = data.proposals;
  return (
    head(
      "evolution",
      "记录执行中的不顺畅，让技能、工具、人格、推理循环与代码都能进化。",
    ) +
    panel(
      "记下一次可以更好的体验",
      `<form id="feedback-form" class="panel-body feedback-row"><textarea name="detail" aria-label="体验反馈" placeholder="哪一步不够流畅？期望的行为是什么？" maxlength="16000" required></textarea><button class="primary">记录反馈</button></form>`,
    ) +
    `<div class="notice">记录和提案按 project 共享。代码变更由集成进程在独立工作树中验证，合并到 evolution/main 后再显式升级实例。</div>` +
    panel(
      "最近 100 个进化提案",
      data.proposals.length
        ? `<div class="table-wrap"><table><thead><tr><th>提案</th><th>类型</th><th>状态</th><th>来源实例</th></tr></thead><tbody>${data.proposals.map((p) => `<tr><td><button class="task-link" data-proposal="${esc(p.id)}">${esc(p.rationale || p.id)}</button><small>${fmtDate(p.created_at)}</small></td><td><span class="pill">${esc(p.target || "code")}</span></td><td>${badge(p.status)}</td><td>${esc(p.instance)}</td></tr>`).join("")}</tbody></table></div>`
        : empty(
            "经验先积累，改进再验证",
            "记录一次体验，或让真实模型在执行受阻后生成提案。",
            "evolve",
          ),
    ) +
    panel(
      "最近 100 条体验记录",
      data.observations.length
        ? data.observations
            .map(
              (o) =>
                `<article class="observation"><div><span>${esc(o.kind)} · ${esc(o.instance)}</span><time>${fmtDate(o.created_at)}</time></div><p>${esc(o.detail)}</p></article>`,
            )
            .join("")
        : empty(
            "还没有体验记录",
            "可以手动记录反馈，也会自动记录执行中遇到的问题。",
            "evolve",
          ),
    )
  );
}
async function chat() {
  const [sessions, history, status] = await Promise.all([
    api("/sessions"),
    api("/sessions/" + encodeURIComponent(session)),
    api("/status"),
  ]);
  chatHistory = history || [];
  currentStatus = status;
  return (
    head(
      "chat",
      "一个持续记住上下文、可以调用工具的助手。",
      '<button class="small" data-action="new-session">＋ 新对话</button>',
    ) +
    `<div class="chat-layout"><aside class="chat-list"><button data-action="new-session">＋ 新建对话</button><p class="nav-label">最近会话</p>${sessions.map((s) => `<button class="session-button ${s.name === session ? "active" : ""}" data-session="${esc(s.name)}">${esc(s.name)}<small>${fmtDate(s.updated_at)}</small></button>`).join("")}${!sessions.length ? '<p class="empty">还没有历史对话</p>' : ""}</aside><section class="chat-main"><div class="chat-header"><span>会话 <strong>${esc(session.slice(0, 30))}</strong></span><span>${esc(status.model || "模型未配置")} ${status.demo ? "· 演示模式" : ""}</span></div><div id="messages" class="messages" aria-live="polite"></div><div id="run-status" class="run-status" hidden></div><form id="chat-form" class="composer"><div><textarea id="prompt" name="task" maxlength="12000" aria-label="发送给助手的消息" placeholder="告诉助手，你想完成什么…" required></textarea><button class="primary" type="submit" ${activeTask ? "disabled" : ""}>发送 ↑</button></div><small>${status.demo ? "演示模型仅列出文件；配置真实模型后可自由对话。" : "Enter 发送 · Shift + Enter 换行 · 工具能力取决于当前配置"}</small></form></section></div>`
  );
}
function drawMessages() {
  const node = $("#messages");
  if (!node) return;
  const messages = chatHistory.filter(
    (m) =>
      ["user", "assistant"].includes(m.role) &&
      typeof m.content === "string" &&
      m.content,
  );
  node.innerHTML =
    messages.length || pendingPrompt
      ? messages.map((m) => messageHTML(m)).join("") +
        (pendingPrompt
          ? messageHTML({ role: "user", content: pendingPrompt })
          : "")
      : '<div class="chat-welcome"><span class="mark">e<span>•</span></span><h2>今天，一起完成什么？</h2><p>从一个小任务开始，让助手逐步了解你的工作方式。<br>上下文保存在当前会话中，下次可以继续。</p><div class="suggestions"><button data-suggestion="列出工作区文件">列出工作区文件 ↗</button><button data-suggestion="总结你现在可以使用的工具">了解助手的能力 ↗</button></div></div>';
  node.scrollTop = node.scrollHeight;
}
function messageHTML(m) {
  return `<article class="message ${m.role}"><span class="avatar">${m.role === "user" ? "你" : "e•"}</span><div class="message-body"><strong>${m.role === "user" ? "你" : "EvoMesh"}</strong><p>${esc(m.content)}</p></div></article>`;
}
function updateRunStatus(text) {
  const node = $("#run-status");
  if (node) {
    node.hidden = false;
    node.innerHTML = `<span><i class="dot"></i>${esc(text)}</span>${activeTask ? `<button data-task-action="cancel" data-id="${esc(activeTask)}">取消执行</button>` : ""}`;
  }
}
async function taskDetail(id) {
  const [t, events] = await Promise.all([
    api("/tasks/" + encodeURIComponent(id)),
    api("/tasks/" + encodeURIComponent(id) + "/events"),
  ]);
  showDetail(
    "任务详情",
    `<div class="meta">${badge(t.status)} · ${fmtDate(t.created_at)} · 会话 ${esc(t.session)}</div><h3>请求</h3><pre>${esc(t.prompt)}</pre>${t.result ? `<h3>结果</h3><pre>${esc(t.result)}</pre>` : ""}${t.error ? `<p class="danger">${esc(t.error)}</p>` : ""}<div class="actions">${["queued", "running", "cancelling"].includes(t.status) ? `<button data-task-action="cancel" data-id="${esc(t.id)}">取消任务</button>` : ""}${["failed", "interrupted", "cancelled"].includes(t.status) ? `<button data-task-action="retry" data-id="${esc(t.id)}">重新执行</button>` : ""}<button data-task="${esc(t.id)}">刷新详情</button></div><h3>执行轨迹</h3>${events.length ? events.map((e) => `<div class="event-row"><strong>${esc(e.kind)}</strong><small>${fmtDate(e.created_at)}</small><pre>${esc(typeof e.detail === "object" ? JSON.stringify(e.detail, null, 2) : e.detail)}</pre></div>`).join("") : empty("还没有执行事件", "任务被工作进程领取后会开始记录。")}`,
  );
}
function newSession() {
  if (activeTask) {
    toast("请先等待当前任务结束，或取消执行。");
    return;
  }
  session =
    "chat-" +
    new Date()
      .toISOString()
      .replace(/[:.TZ]/g, "-")
      .slice(0, 19);
  sessionStorage.setItem("evomesh.session", session);
  pendingPrompt = "";
  if (page === "chat") render();
  else location.hash = "chat";
}

// Delegated handlers also cover dialogs; untrusted content always passes through esc().
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.task) await taskDetail(button.dataset.task);
    if (button.dataset.action === "new-task")
      showDetail(
        "新建任务",
        '<form id="task-form" class="form-stack"><div><label for="task-prompt">任务描述</label><textarea id="task-prompt" name="task" maxlength="12000" required placeholder="希望助手完成什么？"></textarea></div><div><label for="task-session">会话名（可选）</label><input id="task-session" name="session" maxlength="128" placeholder="留空创建独立会话"></div><button class="primary">加入任务队列</button></form>',
      );
    if (button.dataset.action === "new-session") newSession();
    if (button.dataset.session) {
      if (activeTask) {
        toast("请先等待当前任务结束，或取消执行。");
        return;
      }
      session = button.dataset.session;
      sessionStorage.setItem("evomesh.session", session);
      pendingPrompt = "";
      await render();
    }
    if (button.dataset.suggestion) {
      $("#prompt").value = button.dataset.suggestion;
      $("#prompt").focus();
    }
    if (
      button.dataset.forget &&
      confirm(`删除记忆“${button.dataset.forget}”？`)
    ) {
      await api("/memory/delete", { key: button.dataset.forget });
      toast("记忆已删除");
      await render();
    }
    if (button.dataset.disable) {
      await api(
        "/schedules/" + encodeURIComponent(button.dataset.disable) + "/disable",
        {},
      );
      toast("计划已停用");
      await render();
    }
    if (button.dataset.taskAction) {
      const action = button.dataset.taskAction;
      if (
        action === "retry" &&
        !confirm("重新执行可能重复此前已经完成的工具操作，确认继续？")
      )
        return;
      const result = await api(
        "/tasks/" + encodeURIComponent(button.dataset.id) + "/" + action,
        {},
      );
      toast(result.changed ? "请求已提交" : "任务状态已变化，请刷新");
      if ($("#detail").open) await taskDetail(button.dataset.id);
      else if (page !== "chat") await render();
    }
    if (
      button.dataset.replyRetry &&
      confirm("如果上一条回复实际已送达，重试可能产生重复消息。确认重试？")
    ) {
      await api(
        "/deliveries/" +
          encodeURIComponent(button.dataset.replyRetry) +
          "/retry",
        { acknowledge_duplicate_risk: true },
      );
      toast("回复已重新排队");
      await render();
    }
    if (button.dataset.setup) {
      const kind = button.dataset.setup;
      showDetail(
        "连接 " + channelNames[kind],
        `<p>${setupSteps[kind]}</p><h3>1. 在 evomesh.toml 中添加</h3><pre>${esc(setup[kind])}</pre><h3>2. 配置环境变量并重启</h3><p class="muted">将上面 *_env 对应的真实密钥写入服务端环境变量。页面不读取或保存密钥值。API 实例与工作进程使用相同配置。</p><h3>3. 在平台配置回调地址</h3><pre>https://你的域名/webhooks/${kind}</pre><p class="muted">按需设置 allowed_groups；默认仅允许名单中的私聊。渠道名称可以自定义，回调路径使用同名账号。</p>`,
      );
    }
    if (button.dataset.proposal) {
      const row = cachedProposals.find((p) => p.id === button.dataset.proposal);
      if (row)
        showDetail(
          "进化提案",
          `<div class="notice">验证、合并与升级由 CLI 集成流程执行。这里展示共享提案的实际状态。</div><pre>${esc(JSON.stringify(row, null, 2))}</pre>`,
        );
    }
  } catch (error) {
    toast(error.message);
  }
});
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (
    ![
      "chat-form",
      "task-form",
      "memory-form",
      "schedule-form",
      "feedback-form",
    ].includes(form.id)
  )
    return;
  event.preventDefault();
  const button =
    event.submitter || form.querySelector('button[type="submit"],button');
  button.disabled = true;
  const data = Object.fromEntries(new FormData(form));
  try {
    if (form.id === "chat-form") {
      if (activeTask) return;
      const prompt = data.task.trim();
      if (!prompt) return;
      const result = await api("/tasks", {
        task: prompt,
        session,
        key: crypto.randomUUID(),
      });
      activeTask = result.id;
      pendingPrompt = prompt;
      $("#prompt").value = "";
      drawMessages();
      updateRunStatus("任务已入队，等待工作进程…");
    } else if (form.id === "task-form") {
      const result = await api("/tasks", {
        task: data.task,
        ...(data.session ? { session: data.session } : {}),
        key: crypto.randomUUID(),
      });
      $("#detail").close();
      toast("任务已加入队列");
      await render();
      await taskDetail(result.id);
    } else if (form.id === "memory-form") {
      await api("/memory", data);
      toast("记忆已保存");
      await render();
    } else if (form.id === "schedule-form") {
      await api("/schedules", {
        task: data.task,
        interval_seconds: Number(data.minutes) * 60,
      });
      toast("定时任务已创建");
      await render();
    } else if (form.id === "feedback-form") {
      await api("/observations", data);
      toast("反馈已记录到共享进化表");
      await render();
    }
  } catch (error) {
    toast(error.message);
  } finally {
    if (form.id !== "chat-form" || !activeTask) button.disabled = false;
  }
});
document.addEventListener("keydown", (event) => {
  if (
    event.target.id === "prompt" &&
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing
  ) {
    event.preventDefault();
    if (!activeTask) $("#chat-form").requestSubmit();
  }
});
async function poll() {
  if (!token || pollBusy || document.hidden) return;
  pollBusy = true;
  try {
    if (activeTask) {
      const task = await api("/tasks/" + encodeURIComponent(activeTask));
      if (
        ["succeeded", "failed", "interrupted", "cancelled"].includes(
          task.status,
        )
      ) {
        activeTask = null;
        pendingPrompt = "";
        chatHistory = await api("/sessions/" + encodeURIComponent(session));
        if (page === "chat") {
          drawMessages();
          updateRunStatus(
            task.status === "succeeded"
              ? "任务已完成"
              : `任务${statusLabels[task.status]}${task.error ? " · " + task.error : ""}`,
          );
          const button = $("#chat-form button");
          if (button) button.disabled = false;
        }
        if (task.status !== "succeeded")
          toast(
            "任务" + statusLabels[task.status] + "，可在任务页面查看和重试。",
          );
      } else {
        const events = await api(
          "/tasks/" + encodeURIComponent(activeTask) + "/events",
        );
        const latest = events.at(-1);
        updateRunStatus(
          task.status === "queued"
            ? "任务排队中 · 请确保内置或独立工作进程正在运行"
            : `${statusLabels[task.status]}${latest ? " · " + latest.kind : ""}`,
        );
      }
    }
    if (
      ["overview", "tasks", "channels"].includes(page) &&
      !$("#detail").open &&
      !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)
    )
      await render(true);
  } catch (error) {
    $("#connection").textContent = "连接异常";
  } finally {
    pollBusy = false;
  }
}
setInterval(poll, 3000);
if (token)
  connect().catch((error) => {
    disconnect();
    $("#login-error").textContent = error.message;
  });
