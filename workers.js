const STATE_PREFIX = "renewal-account:";
const ACTION_LEASE_MS = 7 * 60 * 60 * 1000;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchDueAccounts(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/favicon.ico") {
      return new Response(null, { status: 204 });
    }

    if (url.pathname === "/api/schedule") {
      return saveAccountSchedule(request, env);
    }

    const authKey = env.AUTH_KEY;
    const clientKey = url.searchParams.get("key");
    if (!authKey || clientKey !== authKey) {
      return new Response("Unauthorized: use ?key=YOUR_AUTH_KEY", { status: 401 });
    }

    if (url.pathname === "/schedules" || url.pathname === "/schedules/") {
      if (request.method === "POST") {
        return triggerScheduleAccount(request, env);
      }
      return showSchedules(request, env);
    }

    try {
      const accountIds = getAccountIds(env);
      const requestedAccountId = url.searchParams.get("account");
      const selectedIds = requestedAccountId ? [requestedAccountId] : accountIds;
      if (selectedIds.some((id) => !accountIds.includes(id))) {
        return new Response("Unknown account ID", { status: 400 });
      }
      const result = await dispatchManualAccounts(env, selectedIds, "Manual URL");
      return new Response(stripHtml(result.message), {
        status: result.status,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    } catch (error) {
      return new Response(error.message, { status: 500 });
    }
  },
};

const SCHEDULE_HEADERS = {
  "Content-Type": "text/html; charset=utf-8",
  "Cache-Control": "no-store",
  // 保留同源表单的 Origin；外部链接仍不会收到带 key 的 Referer。
  "Referrer-Policy": "same-origin",
  "X-Content-Type-Options": "nosniff",
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
};

const SCHEDULE_ACCOUNT_NAMES = {
  account_1: "karlxu28",
  account_2: "karlxu0",
  account_3: "karlzhe",
};
const SCHEDULE_TIME_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
  hourCycle: "h23",
});

async function showSchedules(request, env) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("仅支持 GET、HEAD 和 POST 请求", {
      status: 405,
      headers: { ...SCHEDULE_HEADERS, Allow: "GET, HEAD, POST", "Content-Type": "text/plain; charset=utf-8" },
    });
  }
  if (!env.RENEWAL_STATE) {
    return new Response("请先为 Worker 添加名为 RENEWAL_STATE 的 KV binding。", {
      status: 503,
      headers: { ...SCHEDULE_HEADERS, "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  try {
    const configuredIds = getAccountIds(env);
    const configured = new Set(configuredIds);
    const storedIds = new Set();
    let cursor;
    do {
      const page = await env.RENEWAL_STATE.list({ prefix: STATE_PREFIX, ...(cursor ? { cursor } : {}) });
      for (const key of page.keys) {
        storedIds.add(key.name.slice(STATE_PREFIX.length));
      }
      if (page.list_complete) break;
      if (!page.cursor || page.cursor === cursor) throw new Error("KV 列表分页缺少有效游标");
      cursor = page.cursor;
    } while (true);

    const accountIds = [...configuredIds, ...[...storedIds].filter((id) => !configured.has(id)).sort()];
    const records = [];
    // 分批读取，避免账号较多时一次创建过多并发请求。
    for (let offset = 0; offset < accountIds.length; offset += 25) {
      const batch = await Promise.all(accountIds.slice(offset, offset + 25).map(async (id) => {
        const raw = await env.RENEWAL_STATE.get(stateKey(id));
        let state = null;
        let invalid = false;
        if (raw !== null) {
          try {
            state = JSON.parse(raw);
            invalid = !state || typeof state !== "object" || Array.isArray(state);
          } catch {
            invalid = true;
          }
        }
        return { id, configured: configured.has(id), state: invalid ? null : state, invalid };
      }));
      records.push(...batch);
    }

    const notice = scheduleTriggerNotice(new URL(request.url), configured);
    const actionsUrl = env.GH_USER && env.GH_REPO
      ? `https://github.com/${encodeURIComponent(env.GH_USER)}/${encodeURIComponent(env.GH_REPO)}/actions`
      : null;
    const html = renderSchedules(records, new Date(), notice, actionsUrl);
    return new Response(request.method === "HEAD" ? null : html, { headers: SCHEDULE_HEADERS });
  } catch (error) {
    console.error("读取账号计划失败：", error.message);
    return new Response("读取账号计划失败，请检查 KV binding 和 BOT_HOSTING_ACCOUNT_IDS 配置后重试。", {
      status: 500,
      headers: { ...SCHEDULE_HEADERS, "Content-Type": "text/plain; charset=utf-8" },
    });
  }
}

async function triggerScheduleAccount(request, env) {
  const url = new URL(request.url);
  // 页面按钮只接受同源 POST；浏览、刷新和链接预取都不会启动任务。
  if (request.headers.get("Origin") !== url.origin) {
    return new Response("仅允许从账号计划页触发续期。", { status: 403 });
  }

  let accountId;
  try {
    const form = await request.formData();
    const ids = form.getAll("account_id");
    if (ids.length !== 1 || typeof ids[0] !== "string") {
      return new Response("请选择一个账号。", { status: 400 });
    }
    accountId = ids[0];
  } catch {
    return new Response("无效的触发请求。", { status: 400 });
  }

  try {
    if (!getAccountIds(env).includes(accountId)) {
      return new Response("未知账号或账号已移出配置。", { status: 400 });
    }
    const result = await dispatchManualAccounts(env, [accountId], "Schedules page");
    url.searchParams.set("trigger", result.ok ? "sent" : result.status === 409 ? "busy" : "failed");
  } catch (error) {
    console.error("页面手动触发失败：", error.message);
    url.searchParams.set("trigger", "failed");
  }
  url.searchParams.set("account", accountId);
  // POST 后跳转回只读 GET，避免刷新页面重复提交。
  return new Response(null, {
    status: 303,
    headers: { Location: url.toString(), "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" },
  });
}

async function dispatchManualAccounts(env, accountIds, source) {
  if (!env.RENEWAL_STATE) throw new Error("Missing RENEWAL_STATE KV binding");
  const states = new Map();
  for (const accountId of accountIds) {
    const state = await readAccountState(env, accountId);
    const leaseUntil = Date.parse(state?.in_flight_until || "");
    if (Number.isFinite(leaseUntil) && leaseUntil > Date.now()) {
      return { ok: false, status: 409, message: `Account ${accountId} already has a running renewal` };
    }
    states.set(accountId, state);
  }
  const result = await triggerGithubAction(env, source, { account_ids: accountIds, manual: true });
  if (result.ok) await persistLeases(env, accountIds, states);
  return { ...result, status: result.ok ? 200 : 502 };
}

function scheduleTriggerNotice(url, configured) {
  const id = url.searchParams.get("account");
  if (!configured.has(id)) return null;
  const name = Object.hasOwn(SCHEDULE_ACCOUNT_NAMES, id) ? `${SCHEDULE_ACCOUNT_NAMES[id]}（${id}）` : id;
  const notices = {
    sent: { kind: "success", message: `已为 ${name} 发送续期请求。任务将由 GitHub Actions 启动，请稍后刷新查看结果。` },
    busy: { kind: "error", message: `${name} 已有任务占用，请等待完成后再触发。` },
    failed: { kind: "error", message: `${name} 触发失败。请检查 Worker 的 GitHub 配置和运行日志后重试。` },
  };
  const result = url.searchParams.get("trigger");
  return Object.hasOwn(notices, result) ? notices[result] : null;
}

function scheduleStatus(record, now) {
  if (!record.configured) return { label: "未配置", kind: "muted" };
  if (record.invalid) return { label: "记录异常", kind: "danger" };
  if (parseScheduleTime(record.state?.in_flight_until) > now) return { label: "占用中", kind: "active" };
  if (!record.state) return { label: "待初始化", kind: "due" };
  if (parseScheduleTime(record.state.next_run_at) > now) return { label: "等待中", kind: "waiting" };
  return { label: "待检查", kind: "due" };
}

function parseScheduleTime(value) {
  return typeof value === "string" ? Date.parse(value) : NaN;
}

function renderBeijingTime(value) {
  if (value === null || value === undefined || value === "") return '<span class="empty">—</span>';
  const timestamp = parseScheduleTime(value);
  if (!Number.isFinite(timestamp)) return '<span class="empty">无效时间</span>';
  const iso = new Date(timestamp).toISOString();
  const parts = Object.fromEntries(SCHEDULE_TIME_FORMAT.formatToParts(new Date(timestamp)).map(({ type, value }) => [type, value]));
  const label = `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} 北京时间`;
  return `<time datetime="${iso}">${label}</time>`;
}

function renderSchedules(records, now, notice = null, actionsUrl = null) {
  const results = { success: "续期成功", not_due: "尚未到期", failed: "续期失败" };
  const statuses = records.map((record) => scheduleStatus(record, now.getTime()));
  const due = statuses.filter((status) => status.kind === "due").length;
  const active = statuses.filter((status) => status.kind === "active").length;
  const rows = records.map((record, index) => {
    const name = Object.hasOwn(SCHEDULE_ACCOUNT_NAMES, record.id) ? SCHEDULE_ACCOUNT_NAMES[record.id] : record.id;
    const status = statuses[index];
    const result = record.state?.status;
    let resultLabel = "未回写";
    if (typeof result === "string" && result) {
      resultLabel = Object.hasOwn(results, result) ? results[result] : `未知（${result}）`;
    } else if (result !== undefined && result !== null) {
      resultLabel = "未知结果";
    }
    const unavailable = status.kind === "active" || record.invalid;
    const action = record.configured ? `<form method="post" class="trigger-form">
        <input type="hidden" name="account_id" value="${escapeHtml(record.id)}">
        <button class="trigger" type="submit" aria-label="立即触发 ${escapeHtml(name)}（${escapeHtml(record.id)}）"${unavailable ? " disabled" : ""}>${status.kind === "active" ? "任务占用中" : "立即触发"}</button>
      </form>` : '<span class="empty">—</span>';
    return `<tr data-account-id="${escapeHtml(record.id)}">
      <td><strong class="account">${escapeHtml(name)}</strong>${name !== record.id ? `<span class="subtext">${escapeHtml(record.id)}</span>` : ""}${!record.configured ? '<span class="subtext">仅保留在 KV，未参与调度</span>' : ""}</td>
      <td>${action}</td>
      <td><span class="badge ${status.kind}">${status.label}</span></td>
      <td>${escapeHtml(resultLabel)}</td>
      <td>${renderBeijingTime(record.state?.next_run_at)}</td>
      <td>${renderBeijingTime(record.state?.updated_at)}</td>
      <td>${renderBeijingTime(record.state?.in_flight_until)}</td>
    </tr>`;
  }).join("");

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>账号续期计划 · Bot-hosting</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #172b39; background: #f4f7f8; }
    * { box-sizing: border-box; }
    body { margin: 0; padding: 56px 28px; }
    main { max-width: 1280px; margin: auto; }
    .eyebrow { margin: 0 0 12px; color: #39756c; font-size: 12px; font-weight: 700; letter-spacing: .14em; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 30px; }
    h1 { margin: 0 0 10px; font-size: 30px; letter-spacing: -.03em; }
    .description, footer { color: #61727d; font-size: 14px; line-height: 1.8; }
    .description { margin: 0; }
    .refresh { flex-shrink: 0; padding: 10px 19px; border: 1px solid #cbd8dd; border-radius: 8px; color: #264c49; background: white; font-size: 14px; font-weight: 600; text-decoration: none; }
    .refresh:hover { background: #eaf3f0; border-color: #78a89a; }
    .refresh:focus-visible { outline: 3px solid #68ac99; outline-offset: 3px; }
    .trigger-form { margin: 0; }
    .trigger { border: 1px solid #39756c; border-radius: 6px; padding: 7px 12px; color: white; background: #39756c; font: inherit; font-weight: 600; cursor: pointer; white-space: nowrap; }
    .trigger:hover:not(:disabled) { background: #295c54; }
    .trigger:focus-visible { outline: 3px solid #68ac99; outline-offset: 3px; }
    .trigger:disabled { border-color: #dfe7ea; background: #f0f2f4; color: #7b858d; cursor: not-allowed; }
    .notice { margin: 0 0 24px; padding: 14px 18px; border: 1px solid; border-radius: 8px; font-size: 14px; line-height: 1.8; }
    .notice.success { background: #edf7f1; color: #216c52; border-color: #c5e0d1; }
    .notice.error { background: #fff3ed; color: #9c4826; border-color: #efd1bf; }
    .notice a { color: inherit; font-weight: 600; margin-left: 10px; }
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
    .stat { border: 1px solid #dfe7ea; border-radius: 12px; background: white; padding: 21px 24px; }
    .stat dt { color: #61727d; font-size: 13px; }
    .stat dd { margin: 9px 0 0; font-size: 30px; font-weight: 650; font-variant-numeric: tabular-nums; }
    .stat:nth-child(2) dd { color: #996716; }
    .stat:nth-child(3) dd { color: #216c62; }
    .table-panel { overflow: hidden; background: white; border: 1px solid #dfe7ea; border-radius: 12px; }
    .table-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 22px 24px; }
    h2 { font-size: 16px; margin: 0; }
    .zone { font-size: 12px; font-weight: 600; color: #39756c; background: #edf5f1; padding: 5px 9px; border-radius: 5px; }
    .table-scroll { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; text-align: left; white-space: nowrap; font-size: 13px; }
    th { color: #627780; font-size: 12px; font-weight: 600; background: #f7f9fa; }
    th, td { padding: 17px 20px; border-top: 1px solid #e8eef0; }
    th:first-child, td:first-child { padding-left: 24px; }
    tbody tr:hover { background: #fafcfb; }
    .account { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; }
    .subtext { display: block; color: #87949d; font-size: 11px; margin-top: 6px; }
    time { font-variant-numeric: tabular-nums; font-size: 12px; }
    .empty { color: #91a0a8; }
    .badge { display: inline-block; padding: 4px 9px; border-radius: 5px; font-size: 12px; font-weight: 500; }
    .waiting { background: #edf3fa; color: #3b6593; }
    .active { background: #e7f4ee; color: #257455; }
    .due { background: #fcf3de; color: #946516; }
    .danger { background: #fceceb; color: #b74d48; }
    .muted { background: #f0f2f4; color: #7b858d; }
    footer { margin-top: 22px; font-size: 12px; }
    footer p { margin: 5px 0; }
    @media (max-width: 600px) { body { padding: 28px 16px; } h1 { font-size: 25px; } header { align-items: flex-start; } .stats { gap: 10px; } .stat { padding: 17px 15px; } .stat dd { font-size: 26px; } .description time { display: block; } .table-heading { padding: 18px; } }
  </style>
</head>
<body>
  <main>
    <p class="eyebrow">BOT-HOSTING / SCHEDULES</p>
    <header>
      <div><h1>账号续期计划</h1><p class="description">查看各账号的下次检查时间。当前时间：${renderBeijingTime(now.toISOString())}</p></div>
      <a class="refresh" href="">刷新</a>
    </header>
    ${notice ? `<div class="notice ${notice.kind}" role="${notice.kind === "error" ? "alert" : "status"}">${escapeHtml(notice.message)}${actionsUrl ? `<a href="${escapeHtml(actionsUrl)}" target="_blank" rel="noreferrer">查看运行任务 ↗</a>` : ""}</div>` : ""}
    <dl class="stats" aria-label="计划概览">
      <div class="stat"><dt>账号总数</dt><dd>${records.length}</dd></div>
      <div class="stat"><dt>待检查 / 待初始化</dt><dd>${due}</dd></div>
      <div class="stat"><dt>占用中</dt><dd>${active}</dd></div>
    </dl>
    <section class="table-panel" aria-labelledby="schedule-heading">
      <div class="table-heading"><h2 id="schedule-heading">全部账号</h2><span class="zone">北京时间（UTC+8）</span></div>
      <div class="table-scroll" role="region" aria-label="账号计划表，可横向滚动" tabindex="0">
        <table><thead><tr><th scope="col">账号</th><th scope="col">操作</th><th scope="col">调度状态</th><th scope="col">上次结果</th><th scope="col">下次检查（北京时间）</th><th scope="col">最近更新（北京时间）</th><th scope="col">占用截止（北京时间）</th></tr></thead><tbody>${rows}</tbody></table>
      </div>
    </section>
    <footer><p>点击“立即触发”可马上启动该账号的续期检查，无需等待下次定时检查。实际续期结果以任务运行结果为准。</p><p>浏览和刷新页面不会触发任务；KV 更新可能有短暂延迟。“占用中”按调度占用期限判断，不代表 GitHub Actions 的实时状态。未配置的旧账号仅供查看。</p></footer>
  </main>
</body>
</html>`;
}

async function dispatchDueAccounts(env) {
  try {
    if (!env.RENEWAL_STATE) {
      throw new Error("Missing RENEWAL_STATE KV binding");
    }

    const accountIds = getAccountIds(env);
    const now = Date.now();
    const dueIds = [];
    const states = new Map();

    for (const accountId of accountIds) {
      const state = await readAccountState(env, accountId);
      states.set(accountId, state);

      const leaseUntil = Date.parse(state?.in_flight_until || "");
      if (Number.isFinite(leaseUntil) && leaseUntil > now) {
        continue;
      }

      const nextRun = Date.parse(state?.next_run_at || "");
      if (!Number.isFinite(nextRun) || nextRun <= now) {
        dueIds.push(accountId);
      }
    }

    if (!dueIds.length) {
      return;
    }

    const result = await triggerGithubAction(env, "Cloudflare Cron", {
      account_ids: dueIds,
      manual: false,
    });

    if (result.ok) {
      await persistLeases(env, dueIds, states);
    } else {
      console.error(`GitHub dispatch failed: ${result.message}`);
    }
  } catch (error) {
    console.error(`Scheduled dispatch failed: ${error.message}`);
    await sendTelegram(
      env,
      `❌ <b>续期调度失败</b>\n\n<b>原因:</b> ${escapeHtml(error.message)}`,
      env.TG_BOT_TOKEN,
      env.TG_CHAT_ID,
    );
  }
}

async function saveAccountSchedule(request, env) {
  if (request.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  const schedulerKey = env.SCHEDULER_AUTH_KEY;
  if (!schedulerKey || request.headers.get("Authorization") !== `Bearer ${schedulerKey}`) {
    return new Response("Unauthorized: SCHEDULER_AUTH_KEY is missing or does not match", { status: 401 });
  }
  if (!env.RENEWAL_STATE) {
    return new Response("Missing RENEWAL_STATE KV binding", { status: 500 });
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const { account_id: accountId, status, next_run_at: nextRunAt } = payload || {};
  let accountIds;
  try {
    accountIds = getAccountIds(env);
  } catch (error) {
    return new Response(error.message, { status: 500 });
  }

  if (typeof accountId !== "string" || !accountIds.includes(accountId)) {
    return new Response("Unknown account ID", { status: 400 });
  }
  if (!["success", "not_due", "failed"].includes(status)) {
    return new Response("Invalid status", { status: 400 });
  }

  const parsedNextRun = Date.parse(nextRunAt || "");
  if (!Number.isFinite(parsedNextRun) || parsedNextRun <= Date.now()) {
    return new Response("Invalid next_run_at", { status: 400 });
  }

  const savedAt = new Date().toISOString();
  try {
    await env.RENEWAL_STATE.put(
      stateKey(accountId),
      JSON.stringify({
        status,
        next_run_at: new Date(parsedNextRun).toISOString(),
        updated_at: savedAt,
        in_flight_until: null,
      }),
    );
    return Response.json({ ok: true, account_id: accountId, next_run_at: new Date(parsedNextRun).toISOString() });
  } catch (error) {
    return new Response(`Failed to save schedule: ${error.message}`, { status: 500 });
  }
}

async function readAccountState(env, accountId) {
  const rawState = await env.RENEWAL_STATE.get(stateKey(accountId), "json");
  return rawState && typeof rawState === "object" ? rawState : null;
}

async function persistLeases(env, accountIds, states) {
  const leaseUntil = new Date(Date.now() + ACTION_LEASE_MS).toISOString();
  for (const accountId of accountIds) {
    const state = states.get(accountId) || {};
    await env.RENEWAL_STATE.put(
      stateKey(accountId),
      JSON.stringify({ ...state, in_flight_until: leaseUntil }),
    );
  }
}

function getAccountIds(env) {
  const accountIds = (env.BOT_HOSTING_ACCOUNT_IDS || "")
    .split(",")
    .map((id) => id.trim())
    .filter(Boolean);

  if (!accountIds.length) {
    throw new Error("Missing BOT_HOSTING_ACCOUNT_IDS");
  }
  if (accountIds.some((id) => !/^[A-Za-z0-9_-]{1,64}$/.test(id))) {
    throw new Error("BOT_HOSTING_ACCOUNT_IDS contains an invalid ID");
  }
  if (new Set(accountIds).size !== accountIds.length) {
    throw new Error("BOT_HOSTING_ACCOUNT_IDS contains duplicate IDs");
  }
  return accountIds;
}

function stateKey(accountId) {
  return `${STATE_PREFIX}${accountId}`;
}

async function triggerGithubAction(env, source, payload) {
  const { GH_PAT, GH_USER, GH_REPO, TG_BOT_TOKEN, TG_CHAT_ID } = env;
  const eventType = env.GH_EVENT_TYPE || "cf_timer";
  const now = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });

  if (!GH_PAT || !GH_USER || !GH_REPO) {
    const message = "Missing GH_PAT / GH_USER / GH_REPO";
    await sendTelegram(env, `❌ <b>GitHub Actions 触发失败</b><br>${escapeHtml(message)}`, TG_BOT_TOKEN, TG_CHAT_ID);
    return { ok: false, message };
  }

  const githubUrl = `https://api.github.com/repos/${GH_USER}/${GH_REPO}/dispatches`;

  try {
    const response = await fetch(githubUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${GH_PAT}`,
        Accept: "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "Cloudflare-Worker-Action-Trigger",
      },
      body: JSON.stringify({
        event_type: eventType,
        client_payload: { ...payload, source },
      }),
    });

    let message;
    if (response.ok) {
      message = [
        "✅ <b>GitHub Actions 已触发</b>",
        "",
        `<b>仓库:</b> ${escapeHtml(`${GH_USER}/${GH_REPO}`)}`,
        `<b>事件:</b> ${escapeHtml(eventType)}`,
        `<b>来源:</b> ${escapeHtml(source)}`,
        `<b>账号数:</b> ${payload.account_ids.length}`,
        `<b>时间:</b> ${escapeHtml(now)}`,
      ].join("\n");
    } else {
      const errorText = await response.text();
      message = [
        "❌ <b>GitHub Actions 触发失败</b>",
        "",
        `<b>仓库:</b> ${escapeHtml(`${GH_USER}/${GH_REPO}`)}`,
        `<b>状态:</b> ${response.status}`,
        `<b>原因:</b> ${escapeHtml(errorText.slice(0, 300))}`,
      ].join("\n");
    }

    await sendTelegram(env, message, TG_BOT_TOKEN, TG_CHAT_ID);
    return { ok: response.ok, message };
  } catch (error) {
    const message = `Worker 内部错误: ${error.message}`;
    await sendTelegram(env, escapeHtml(message), TG_BOT_TOKEN, TG_CHAT_ID);
    return { ok: false, message };
  }
}

async function sendTelegram(env, message, token, chatId) {
  if (!token || !chatId) return;

  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text: message, parse_mode: "HTML" }),
    });
  } catch (error) {
    console.error(`Telegram notification failed: ${error.message}`);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function stripHtml(text) {
  return text.replace(/<[^>]*>/g, "");
}
