// timer/worker.js: dispatches tick.yml, retries twice, throws on final failure; every request gets 404.
import test from "node:test";
import assert from "node:assert/strict";
import worker from "../timer/worker.js";

const env = { REPO: "owner/repo", GITHUB_TOKEN: "test-token" };
const controller = { scheduledTime: Date.UTC(2026, 10, 24, 9, 57), cron: "27,57 * * * *" };

function stub(t, replies) {
  const calls = [];
  t.mock.method(globalThis, "setTimeout", (fn) => { fn(); return 0; });
  t.mock.method(console, "log", () => {});
  t.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, init });
    const reply = replies.shift();
    if (reply instanceof Error) throw reply;
    return new Response(reply === 204 ? null : "busy", { status: reply });
  });
  return calls;
}

test("one POST to the tick.yml dispatches endpoint with the token, the API headers and ref main", async (t) => {
  const calls = stub(t, [204]);
  await worker.scheduled(controller, env);
  assert.equal(calls.length, 1);
  const { url, init } = calls[0];
  assert.equal(url, "https://api.github.com/repos/owner/repo/actions/workflows/tick.yml/dispatches");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, "Bearer test-token");
  assert.equal(init.headers.Accept, "application/vnd.github+json");
  assert.equal(init.headers["X-GitHub-Api-Version"], "2022-11-28");
  assert.ok(init.headers["User-Agent"]);
  assert.deepEqual(JSON.parse(init.body), { ref: "main" });
});

test("a failed dispatch is tried twice more", async (t) => {
  const calls = stub(t, [500, new TypeError("network down"), 204]);
  await worker.scheduled(controller, env);
  assert.equal(calls.length, 3);
});

test("three failures throw, naming the last one", async (t) => {
  const calls = stub(t, [500, 502, 422]);
  await assert.rejects(worker.scheduled(controller, env), /dispatch failed after 3 attempts: HTTP 422/);
  assert.equal(calls.length, 3);
});

test("any request to the Worker gets 404", async () => {
  const res = await worker.fetch(new Request("https://example.invalid/anything"), env);
  assert.equal(res.status, 404);
});
