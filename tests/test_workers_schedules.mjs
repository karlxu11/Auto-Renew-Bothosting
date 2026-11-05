import assert from "node:assert/strict";
import test from "node:test";
import worker from "../workers.js";

const PREFIX = "renewal-account:";
const KEY = "test-view-key";
const future = () => new Date(Date.now() + 60 * 60 * 1000).toISOString();

function fixture(values = {}, pages) {
  const calls = { reads: [], lists: [], writes: [] };
  const env = {
    AUTH_KEY: KEY,
    BOT_HOSTING_ACCOUNT_IDS: "account_1,account_2,account_3",
    RENEWAL_STATE: {
      async list(options) {
        calls.lists.push(options);
        if (pages) return pages[calls.lists.length - 1];
        return {
          keys: Object.keys(values).filter((key) => key.startsWith(options.prefix)).map((name) => ({ name })),
          list_complete: true,
        };
      },
      async get(key, type) {
        calls.reads.push(key);
        const value = values[key] ?? null;
        return type === "json" && value !== null ? JSON.parse(value) : value;
      },
      async put(key, value) {
        calls.writes.push([key, value]);
      },
    },
  };
  return { env, calls };
}

const request = (path = `/schedules?key=${KEY}`, method = "GET") => new Request(`https://worker.example.invalid${path}`, { method });
const rowFor = (html, id) => [...html.matchAll(/<tr data-account-id="([^"]*)">([\s\S]*?)<\/tr>/g)].find((row) => row[1] === id)?.[2];

test("schedules requires the existing AUTH_KEY before any KV reads", async () => {
  const { env, calls } = fixture();
  for (const path of ["/schedules", "/schedules?key=wrong"]) {
    assert.equal((await worker.fetch(request(path), env)).status, 401);
  }
  assert.equal(calls.reads.length, 0);
  assert.equal(calls.lists.length, 0);
});

test("lists configured accounts, historical KV accounts, and missing records without side effects", async (t) => {
  const outbound = t.mock.method(globalThis, "fetch", () => { throw new Error("Read-only page must not make outbound requests"); });
  const { env, calls } = fixture({
    [`${PREFIX}account_1`]: JSON.stringify({ status: "success", next_run_at: future() }),
    [`${PREFIX}retired`]: JSON.stringify({ status: "failed", next_run_at: "2000-01-01T00:00:00Z" }),
    "other-application:secret": "must-not-be-displayed",
  });
  const response = await worker.fetch(request(), env);
  const html = await response.text();
  assert.equal(response.status, 200);
  assert.match(rowFor(html, "account_1"), />karlxu28<\/strong>/);
  assert.match(rowFor(html, "account_2"), />karlxu0<\/strong>/);
  assert.match(rowFor(html, "account_3"), />karlzhe<\/strong>/);
  for (const id of ["account_1", "account_2", "account_3"]) {
    assert.ok(rowFor(html, id).includes(`<span class="subtext">${id}</span>`));
  }
  assert.match(rowFor(html, "account_1"), /等待中/);
  assert.match(rowFor(html, "account_2"), /待初始化/);
  assert.match(rowFor(html, "account_3"), /待初始化/);
  assert.match(rowFor(html, "retired"), /未配置/);
  assert.match(html, /仅保留在 KV，未参与调度/);
  assert.doesNotMatch(html, /must-not-be-displayed/);
  assert.equal(new Set(calls.reads).size, 4);
  assert.equal(calls.writes.length, 0);
  assert.equal(outbound.mock.callCount(), 0);
});

test("KV pagination continues through empty pages", async () => {
  const pages = [
    { keys: [], list_complete: false, cursor: "first" },
    { keys: [{ name: `${PREFIX}old-account` }], list_complete: false, cursor: "second" },
    { keys: [], list_complete: true },
  ];
  const { env, calls } = fixture({ [`${PREFIX}old-account`]: "{}" }, pages);
  const response = await worker.fetch(request(), env);
  assert.match(await response.text(), /old-account/);
  assert.deepEqual(calls.lists, [
    { prefix: PREFIX },
    { prefix: PREFIX, cursor: "first" },
    { prefix: PREFIX, cursor: "second" },
  ]);
});

test("all stored time fields are displayed in Beijing time", async () => {
  const { env } = fixture({
    [`${PREFIX}account_1`]: JSON.stringify({
      status: "not_due",
      next_run_at: "2026-11-04T17:30:00+08:00",
      updated_at: "2026-11-04T01:20:00-07:00",
      in_flight_until: "2026-11-04T10:00:00Z",
    }),
  });
  const html = await (await worker.fetch(request(), env)).text();
  const row = rowFor(html, "account_1");
  assert.match(row, /datetime="2026-11-04T09:30:00.000Z">2026-11-04 17:30:00 北京时间<\/time>/);
  assert.match(row, />2026-11-04 16:20:00 北京时间<\/time>/);
  assert.match(row, />2026-11-04 18:00:00 北京时间<\/time>/);
  assert.match(html, /北京时间（UTC\+8）/);
  assert.doesNotMatch(html, /所有时间均为 UTC|\(UTC\)/);
});

test("Beijing time conversion handles midnight and year rollover", async () => {
  const { env } = fixture({
    [`${PREFIX}account_1`]: JSON.stringify({
      next_run_at: "2026-12-31T16:00:00Z",
      updated_at: "2026-11-04T15:59:59Z",
      in_flight_until: "2026-11-04T16:00:00Z",
    }),
  });
  const html = await (await worker.fetch(request(), env)).text();
  const row = rowFor(html, "account_1");
  assert.match(row, />2027-01-01 00:00:00 北京时间<\/time>/);
  assert.match(row, />2026-11-04 23:59:59 北京时间<\/time>/);
  assert.match(row, />2026-11-05 00:00:00 北京时间<\/time>/);
});

test("active leases take precedence over due dates, expired leases do not", async () => {
  const { env } = fixture({
    [`${PREFIX}account_1`]: JSON.stringify({ status: "failed", next_run_at: "2000-01-01T00:00:00Z", in_flight_until: future() }),
    [`${PREFIX}account_2`]: JSON.stringify({ status: "success", next_run_at: "2000-01-01T00:00:00Z", in_flight_until: "2000-01-01T00:00:00Z" }),
  });
  const html = await (await worker.fetch(request(), env)).text();
  assert.match(rowFor(html, "account_1"), /badge active">占用中/);
  assert.match(rowFor(html, "account_2"), /badge due">待检查/);
});

test("a corrupt record does not hide healthy accounts", async () => {
  const { env } = fixture({
    [`${PREFIX}account_1`]: "not-json",
    [`${PREFIX}account_2`]: JSON.stringify({ next_run_at: "invalid-date" }),
    [`${PREFIX}account_3`]: JSON.stringify({ status: "success", next_run_at: future() }),
  });
  const response = await worker.fetch(request(), env);
  const html = await response.text();
  assert.equal(response.status, 200);
  assert.match(rowFor(html, "account_1"), /记录异常/);
  assert.match(rowFor(html, "account_2"), /无效时间/);
  assert.match(rowFor(html, "account_3"), /等待中/);
});

test("KV content is escaped and neither credentials nor the auth key appear in HTML", async () => {
  const { env } = fixture({
    [`${PREFIX}<img src=x onerror=alert(1)>`]: JSON.stringify({ status: '<script>alert("x")</script>' }),
  });
  env.GH_PAT = "test-github-token";
  env.SCHEDULER_AUTH_KEY = "test-scheduler-key";
  const response = await worker.fetch(request(), env);
  const html = await response.text();
  assert.doesNotMatch(html, /<img|<script>/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;script&gt;/);
  for (const secret of [KEY, env.GH_PAT, env.SCHEDULER_AUTH_KEY]) assert.ok(!html.includes(secret));
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
});

test("malformed field types and non-object JSON records do not break the page", async () => {
  const { env } = fixture({
    [`${PREFIX}account_1`]: JSON.stringify({ status: { toString: null }, next_run_at: { toString: null }, in_flight_until: [] }),
    [`${PREFIX}account_2`]: "[]",
    [`${PREFIX}account_3`]: "null",
  });
  const response = await worker.fetch(request(), env);
  const html = await response.text();
  assert.equal(response.status, 200);
  assert.match(rowFor(html, "account_1"), /未知结果/);
  assert.match(rowFor(html, "account_1"), /无效时间/);
  assert.match(rowFor(html, "account_2"), /记录异常/);
  assert.match(rowFor(html, "account_3"), /记录异常/);
});

test("the trailing slash and HEAD are read-only; unsupported methods are rejected", async (t) => {
  const outbound = t.mock.method(globalThis, "fetch", () => { throw new Error("Must not dispatch"); });
  const { env, calls } = fixture();
  assert.equal((await worker.fetch(request(`/schedules/?key=${KEY}`), env)).status, 200);
  const head = await worker.fetch(request(undefined, "HEAD"), env);
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
  for (const method of ["POST", "PUT", "DELETE"]) {
    const response = await worker.fetch(request(undefined, method), env);
    assert.equal(response.status, 405);
    assert.equal(response.headers.get("allow"), "GET, HEAD");
  }
  assert.equal(calls.writes.length, 0);
  assert.equal(outbound.mock.callCount(), 0);
});

test("missing KV and list failures return an error without dispatching", async (t) => {
  t.mock.method(console, "error", () => {});
  const { env } = fixture();
  assert.equal((await worker.fetch(request(), { ...env, RENEWAL_STATE: undefined })).status, 503);
  env.RENEWAL_STATE.list = async () => { throw new Error("KV unavailable"); };
  assert.equal((await worker.fetch(request(), env)).status, 500);
});

test("the existing root URL still dispatches the requested account", async (t) => {
  const outbound = t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 204 }));
  const { env, calls } = fixture();
  Object.assign(env, { GH_PAT: "test-only-token", GH_USER: "example", GH_REPO: "example" });
  const response = await worker.fetch(request(`/?key=${KEY}&account=account_2`), env);
  assert.equal(response.status, 200);
  assert.equal(outbound.mock.callCount(), 1);
  const payload = JSON.parse(outbound.mock.calls[0].arguments[1].body);
  assert.deepEqual(payload.client_payload.account_ids, ["account_2"]);
  assert.equal(calls.writes[0][0], `${PREFIX}account_2`);
});
