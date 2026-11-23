// Draws the link-preview card (qa/og/card.html) and saves it as site/og.png, 1200 x 630.
//
//   node qa/og/make.mjs                 from the project root
//   node qa/og/make.mjs --out FILE      somewhere else
//
// The curve is the cumulative volume in site/data/volume_daily.json (cumulative against dates), downsampled
// to 120 points and scaled to 0..1, so the card carries no figures. The page is served from memory under
// http://localhost:8900 (the local site's origin) with the site's own tokens.css and fonts read from site/,
// so the dev server need not be running. Exits 1 if a face falls back or the card fails to draw.
// Browser: the installed Google Chrome (Playwright's bundled Chromium is not installed here).

import { readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve, extname, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..', '..');
const SITE = join(ROOT, 'site');
const require = createRequire(join(ROOT, 'qa', 'package.json'));
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const outAt = args.indexOf('--out');
const OUT = outAt >= 0 ? resolve(args[outAt + 1]) : join(SITE, 'og.png');
const N = 120;
const ORIGIN = 'http://localhost:8900';

// the curve
const vol = JSON.parse(readFileSync(join(SITE, 'data', 'volume_daily.json'), 'utf8'));
const { dates, cumulative } = vol;
if (!Array.isArray(dates) || !Array.isArray(cumulative) || dates.length !== cumulative.length || dates.length < 2) {
  throw new Error('volume_daily.json: dates and cumulative must be aligned arrays');
}
const rows = cumulative.map((v, i) => [i, v]).filter(([, v]) => Number.isFinite(v));
const last = dates.length - 1;
const pick = [];
for (let k = 0; k < N; k++) pick.push(rows[Math.round((k * (rows.length - 1)) / (N - 1))]);
const lo = Math.min(...pick.map(([, v]) => v)), hi = Math.max(...pick.map(([, v]) => v));
const points = pick.map(([i, v]) => [+(i / last).toFixed(4), +((v - lo) / (hi - lo)).toFixed(4)]);

// the page: the mark comes from the site's own lockup, so the two stay identical
const index = readFileSync(join(SITE, 'index.html'), 'utf8');
const mark = index.match(/<svg class="mark"[\s\S]*?<\/svg>/);
if (!mark) throw new Error('site/index.html: no <svg class="mark"> found');
const html = readFileSync(join(HERE, 'card.html'), 'utf8')
  .replace('<!--MARK-->', mark[0])
  .replace('/*POINTS*/null', JSON.stringify(points));

const TYPES = { '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' };

const browser = await chromium.launch({ channel: 'chrome' });
let failed = false;
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('requestfailed', (r) => errors.push(`failed ${r.url()}`));

  await page.route(`${ORIGIN}/**`, (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/__og/card.html') return route.fulfill({ status: 200, contentType: 'text/html', body: html });
    const file = normalize(join(SITE, decodeURIComponent(path)));
    if (!file.startsWith(SITE + '/')) return route.fulfill({ status: 404, body: '' });
    try {
      return route.fulfill({ status: 200, contentType: TYPES[extname(file)] || 'application/octet-stream', body: readFileSync(file) });
    } catch {
      return route.fulfill({ status: 404, body: '' });
    }
  });

  await page.goto(`${ORIGIN}/__og/card.html`);
  await page.waitForFunction(() => document.documentElement.classList.contains('is-ready') || window.__cardError, null, { timeout: 15000 });
  const err = await page.evaluate(() => window.__cardError);
  if (err) throw new Error(`card: ${err}`);
  const card = await page.evaluate(() => window.__card);

  // every text must render in its own face, not a fallback: ask the renderer which font it actually used
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('DOM.enable');
  await cdp.send('CSS.enable');
  const { root } = await cdp.send('DOM.getDocument');
  const want = { '.lk > span:not(.mo)': 'Zalando Sans Expanded', '.lk .mo': 'Zalando Sans Expanded', '#line': 'Inter', '#url': 'Geist Mono', '#by': 'Inter', '#by span': 'Inter' };
  const used = {};
  for (const [sel, family] of Object.entries(want)) {
    const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: sel });
    const { fonts } = await cdp.send('CSS.getPlatformFontsForNode', { nodeId });
    used[sel] = fonts.map((f) => `${f.familyName}${f.isCustomFont ? '' : ' (system)'} x${f.glyphCount}`).join(', ');
    if (!fonts.length || fonts.some((f) => f.familyName !== family || !f.isCustomFont)) failed = true;
  }

  await page.screenshot({ path: OUT, type: 'png', clip: { x: 0, y: 0, width: 1200, height: 630 } });

  const kb = (statSync(OUT).size / 1024).toFixed(1);
  console.log(`wrote ${OUT} (${kb} KB, 1200 x 630) from ${points.length} points, ${dates[0]} to ${dates[last]}`);
  console.log(`wordmark ${card.wordmarkPx}px for a 48px cap height`);
  for (const [sel, f] of Object.entries(used)) console.log(`  ${sel.padEnd(22)} ${f}`);
  for (const f of card.fonts) if (f.status !== 'loaded') { failed = true; console.log(`  face ${f.family}: ${f.status}`); }
  if (errors.length) { failed = true; console.log(errors.map((e) => `  error: ${e}`).join('\n')); }
  if (failed) console.log('FAILED: a face fell back or the page reported an error');
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
