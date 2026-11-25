# AGENTS.md: Variational Monitor

Read this before changing anything. It describes the system as it runs on 25 Nov 2026. `README.md` is the public
description of the site; this file is for whoever changes the code.

## What is live

- https://variationalmonitor.github.io/ is the second version of the site, live since 23 Nov 2026.
- `site/` is the page (plain ES modules, ECharts, cards driven by `site/js/catalog.js`). `pipeline/` builds the data
  files. `qa/gate.py` checks them and the rendered page. `ci/` holds the logic of the jobs.
- `public/`, `tools/`, `config/` and `data/` belong to the first version. Nothing live reads them. Do not build on them.
- Branch `main` is the code. Branch `data` is the store (raw readings, sealed days, source caches) and is written
  only by the jobs.

## The jobs

- `tick.yml`: the collector, one short run per reading, started by a timer outside GitHub (see "The collector").
- `capture.yml`: the old collector, a loop that stayed alive for hours and restarted itself. Retired and disabled on
  25 Nov 2026; kept only for going back.
- `publish.yml`: builds the data files, runs the gate, deploys. It runs after every on-the-hour reading and on a push.
  If the gate fails nothing is deployed, the last good site stays up, and an issue labelled `qa-gate` opens.
  An issue labelled `report-alert` opens when a biweekly update is overdue or could not be fully read.

## Rules that must not be broken

1. A push to `main` that touches `site/`, `pipeline/`, `qa/`, `ci/` or `publish.yml` builds, gates and deploys in
   about two minutes. Run both test suites first. Never weaken a gate check to get a pass.
2. Never commit to `data` by hand, and never rewrite history on `main`.
3. Only the collector calls the Omni stats API (`ci/capture.py`). Do not call it from tests, local scripts or new
   jobs: tests use stored readings and stubs. Never use Omni's private API and never work around its bot check.
4. No figures, series or names taken from another analytics provider's dashboard. Rebuild from primary sources.
5. No job that stays alive for hours or restarts itself, and no new `schedule:` trigger. A tick run waits about
   three minutes for its reading and exits. GitHub's own schedule fires about 2% of this repository's slots, so it
   is not a timer anyway.
6. Never write a token, key, account id or email address into the repository.
7. History of volume and open interest comes from DefiLlama. CoinGecko is for token market data once the token trades.

## Rules for anything a reader sees

- Minimum words. Explanations live only in the info popover of a card. No source lines on cards.
- A figure or chart has one home. Only Overview may repeat what another page shows.
- Never the middle dot character. No colons in card titles.
- "Gross revenue", not "spreads paid"; the popover says once that Variational reports it as spreads paid.
- Never a time axis with empty slots. Bars start at zero; line charts scale to the data.
- The site credit is "by Ponyo", with "Independent analytics, not affiliated with Variational" in the footer.

## The collector (short runs since 25 Nov 2026)

Why: one job per reading is smaller and simpler than a job that never ends, and a failed run costs one reading,
not hours. It also uses about 3.5 minutes of runner time per reading in place of a runner held around the clock.

How it works:
- A Cloudflare Worker named `variational-monitor-timer` (`timer/worker.js`, `timer/wrangler.toml`) fires at minutes
  27 and 57 of every hour and asks GitHub to start `tick.yml`.
- The run waits for :00 or :30, takes one reading with the same code the loop uses (`ci/capture.py once`), commits
  it to `data`, asks `publish.yml` to run after an on-the-hour reading, and exits.
- The Worker holds one secret, `GITHUB_TOKEN`: a fine-grained token limited to this organisation with one
  permission, Actions read and write (start, stop and read workflow runs). It expires on 24 Nov 2027 and must be
  renewed before then. The owner sets it in the Cloudflare dashboard; it is not in this repository or on any disk
  here.
- The Worker has no public address. Its schedule and code are in `timer/`; `wrangler deploy` from that folder
  updates it. Its runs and errors show in the Cloudflare dashboard under Workers & Pages.
- A reading whose push to `data` fails is pushed again for up to 25 minutes after its boundary (`settle` in
  `ci/capture.py`). If it still is not on the branch the run ends red and that reading is lost.
- A tick run captures only when the repository variable `VM_CAPTURE` is exactly `tick` and no run of `capture.yml`
  is queued or in progress (`ci/tick_mode.sh`). Otherwise it is a shadow: it waits like a real run, calls nothing,
  writes nothing, and prints one line with its timing.

State: switched on 25 Nov 2026 at 03:00 UTC, after 28 shadow runs had matched the loop's timing. `VM_CAPTURE` is
`tick` and `capture.yml` is disabled. The loop took its last reading at 03:00 UTC; ticks have taken every reading
since 03:30 UTC and dispatch the hourly publish.

Going back to the loop: `gh variable delete VM_CAPTURE`; `gh workflow enable capture.yml`;
`gh workflow run capture.yml`, and only when no tick run is active.

`tick.yml`, `ci/capture.py`, `ci/datastore.py`, `ci/tick_mode.sh`, `timer/` and the repository variables
`VM_PUBLISH` and `VM_CAPTURE` decide whether readings are taken, and a missed reading is lost for good. Change them
only with tests and a rehearsal against a copy of the `data` branch, and say so in the commit message.

## Changes on 24 Nov 2026

- `f16dd38` A market gets its page file from its first half-hourly reading (a new listing had stopped the build).
- `013b3c6` Research Library links the published Four Pillars piece.
- `93d2bc8` Report alert: gate warning and one `report-alert` issue for an incomplete or overdue biweekly update.
- `dbde4ff` A report read on the day it is posted no longer adds an unfinished day to open interest.
- `413e6c1`, `3b5680a` A listing missing from `pipeline/config/classification.json` is classified and dated at
  build time; a post older than two days before its first reading does not date it.
- `e543f2c`, `93895e7`, `5e2efca`, `5a2d291` `tick.yml`, `ci/capture.py once`, the timer Worker; when two jobs save
  the same reading the first one is kept.

## Changes on 25 Nov 2026

- `7e0c772` `ci/capture.py once` pushes a reading again until it is on the branch, else the run fails. Found in
  rehearsal: GitHub refused the loop's 00:30 UTC push and the reading went out with the next one, which a short run
  cannot do.
- `d1607b1` A rejected push logs every line git and the remote gave, not only the last one.
- The collector switched from the loop to ticks at 03:00 UTC.

## Checks before a push

    /usr/bin/python3 -m unittest discover -s tests -p "test_*.py"
    node --test tests/*.test.mjs

To rehearse a build against real data without touching anything live: clone the `data` branch to a scratch folder,
`. ci/env.sh <clone>`, set `VM_SITE_DATA` to a scratch folder, run `pipeline/build.py --offline`, then
`ci/live.py fetch --out <prev>` and `qa/gate.py --previous <prev> --render`.

## Working together

Two coding agents work on this repository from separate folders. Pull `main` before starting; a clone that still
shows `public/` as the site is on the first version and must be updated first. Keep commits small and say in the
message what changed and why. If a deploy is held, read the first failing lines of the build step in the gate
issue: a long list of missing files is usually one early failure.
