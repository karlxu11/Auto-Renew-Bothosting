export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerGithubAction(env, "Cloudflare Cron"));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/favicon.ico") {
      return new Response(null, { status: 204 });
    }

    const authKey = env.AUTH_KEY;
    const clientKey = url.searchParams.get("key");

    if (!authKey || clientKey !== authKey) {
      return new Response("Unauthorized: use ?key=YOUR_AUTH_KEY", { status: 401 });
    }

    const message = await triggerGithubAction(env, "Manual URL");
    return new Response(stripHtml(message), {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};

async function triggerGithubAction(env, source) {
  const {
    GH_PAT,
    GH_USER,
    GH_REPO,
    TG_BOT_TOKEN,
    TG_CHAT_ID,
  } = env;
  const eventType = env.GH_EVENT_TYPE || "cf_timer";
  const now = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });

  if (!GH_PAT || !GH_USER || !GH_REPO) {
    return "Missing GH_PAT / GH_USER / GH_REPO";
  }

  const githubUrl = `https://api.github.com/repos/${GH_USER}/${GH_REPO}/dispatches`;

  try {
    const response = await fetch(githubUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${GH_PAT}`,
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "Cloudflare-Worker-Action-Trigger",
      },
      body: JSON.stringify({ event_type: eventType }),
    });

    let message;
    if (response.ok) {
      message = [
        "✅ <b>GitHub Actions 已触发</b>",
        "",
        `<b>仓库:</b> ${GH_USER}/${GH_REPO}`,
        `<b>事件:</b> ${eventType}`,
        `<b>来源:</b> ${source}`,
        `<b>时间:</b> ${now}`,
      ].join("\n");
    } else {
      const errorText = await response.text();
      message = [
        "❌ <b>GitHub Actions 触发失败</b>",
        "",
        `<b>仓库:</b> ${GH_USER}/${GH_REPO}`,
        `<b>状态:</b> ${response.status}`,
        `<b>原因:</b> ${errorText.slice(0, 300)}`,
      ].join("\n");
    }

    await sendTelegram(env, message, TG_BOT_TOKEN, TG_CHAT_ID);
    return message;
  } catch (error) {
    const message = `Worker 内部错误: ${error.message}`;
    await sendTelegram(env, message, TG_BOT_TOKEN, TG_CHAT_ID);
    return message;
  }
}

async function sendTelegram(env, message, token, chatId) {
  if (!token || !chatId) return;

  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text: message,
      parse_mode: "HTML",
    }),
  });
}

function stripHtml(text) {
  return text.replace(/<[^>]*>/g, "");
}
