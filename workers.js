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

    try {
      const accountIds = getAccountIds(env);
      const requestedAccountId = url.searchParams.get("account");
      const selectedIds = requestedAccountId ? [requestedAccountId] : accountIds;
      if (selectedIds.some((id) => !accountIds.includes(id))) {
        return new Response("Unknown account ID", { status: 400 });
      }
      if (!env.RENEWAL_STATE) {
        return new Response("Missing RENEWAL_STATE KV binding", { status: 500 });
      }
      const states = new Map();
      for (const accountId of selectedIds) {
        const state = await readAccountState(env, accountId);
        const leaseUntil = Date.parse(state?.in_flight_until || "");
        if (Number.isFinite(leaseUntil) && leaseUntil > Date.now()) {
          return new Response(`Account ${accountId} already has a running renewal`, { status: 409 });
        }
        states.set(accountId, state);
      }

      const result = await triggerGithubAction(env, "Manual URL", {
        account_ids: selectedIds,
        manual: true,
      });
      if (result.ok) {
        await persistLeases(env, selectedIds, states);
      }
      return new Response(stripHtml(result.message), {
        status: result.ok ? 200 : 502,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    } catch (error) {
      return new Response(error.message, { status: 500 });
    }
  },
};

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
    return new Response("Unauthorized", { status: 401 });
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
