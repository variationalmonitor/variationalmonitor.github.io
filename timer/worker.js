// The capture timer: a Cloudflare Worker that dispatches .github/workflows/tick.yml at :27 and :57 UTC, about three
// minutes before every :00 and :30 capture. GitHub's own cron fires too few slots to be relied on.
//
// Config (wrangler.toml): REPO, a plain variable. Secret: GITHUB_TOKEN, set outside the repository with
// `wrangler secret put GITHUB_TOKEN` (a fine-grained token for this one repository with Actions: read and write).
// A failed dispatch is tried twice more after a short pause; then the handler throws, so the failure is in the
// Worker's logs. Every other request gets 404.

const ATTEMPTS = 3;
const PAUSE_MS = 5000;

async function dispatch(env) {
  const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/tick.yml/dispatches`;
  let last = "";
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "capture-timer",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      });
      if (res.status === 204) return attempt;
      last = `HTTP ${res.status} ${(await res.text()).slice(0, 200)}`;
    } catch (err) {
      last = String(err);
    }
    if (attempt < ATTEMPTS) await new Promise((resolve) => setTimeout(resolve, PAUSE_MS));
  }
  throw new Error(`tick.yml dispatch failed after ${ATTEMPTS} attempts: ${last}`);
}

export default {
  async scheduled(controller, env) {
    const attempt = await dispatch(env);
    console.log(`tick.yml dispatched for ${new Date(controller.scheduledTime).toISOString()} (attempt ${attempt})`);
  },
  async fetch() {
    return new Response("Not found", { status: 404 });
  },
};
