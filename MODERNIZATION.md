# Outline Panel — Re-architecture Review (Stages 1–4)

Written 2026-08-23 against `claude/pensive-pascal-ucmisd` at `3569493`.
Baseline: **306 tests green in 118 s**, ~9,900 lines of Python + ~2,900 lines of
frontend. Prerequisites: `REFACTOR_PLAN.md` (completed), `ROADMAP.md` (partly done).

Stage 5 (code generation) is **not started** and will not start without sign-off
on this document. The decisions that need your answer are collected at the end.

---

## 0. A correction to the brief, before anything else

The brief assumes a legacy system to be rebuilt clean-slate under a strict
Hexagonal layering and a 150-line-per-file cap. That is the wrong default here,
and saying so is cheaper now than after a rewrite:

* This code is **not legacy**. It was fully reviewed a month ago, its security
  holes were closed, and the fixes are covered by tests that fail against the
  old code (`REFACTOR_PLAN.md`).
* `ROADMAP.md` already contains an explicit *do-not-build* list — no React
  rewrite, no Postgres, no microservices, no GraphQL, no Redis — each with a
  reason that still holds. A clean-slate rebuild would silently reverse four
  decisions you have already made and paid for.
* A 150-line cap on this codebase would turn `core/db.py` (1,138 lines of
  cohesive storage) into ~9 files that must all be opened together. The cap
  optimises a metric, not comprehension.

What archaeology *did* find is a single **wrong aggregate boundary** at the
centre of the domain, which produces a confirmed, revenue-affecting defect
(§1.3, **A1**). That one boundary is worth re-architecting properly. Everything
else in this document is scoped around it.

So: all five stages are executed below, in full, honestly. Stage 5 is proposed
as a **targeted re-architecture of the domain core plus a mechanical frontend
modernisation** — not a rebuild. If you want the literal clean-slate version
instead, say so at the gate and I will cost it separately.

---

# STAGE 1 — CODEBASE ARCHAEOLOGY & DOMAIN EXTRACTION

## 1.1 Dependency & topology map

### Process model

Three entry points share **one SQLite file** and one set of singletons built in
`web/deps.py` (`db`, `reg`, `settings`, `botmgr`):

| Entry point | Runs | Notes |
|---|---|---|
| `outline-panel` (`web/run.py` → `web/app.py`) | FastAPI + optional in-process aiogram bot + scheduler | `ENABLE_SCHEDULER` still gates whether the loop starts, though the loop itself is now lease-coordinated |
| `outline-panel-bot` (`bot/run.py`) | Same `BotManager` + its own scheduler loop | Deliberately reuses `web.deps` so there is one wiring, not two |
| `outline-panel-admin` (`cli.py`) | Password reset / status | Opens the DB directly |

Coordination between processes is the `locks` table (`db.acquire_lease`), so
exactly one process schedules regardless of worker count. Rate limiting moved
into `rate_events` for the same reason. Both are correct.

### Layer graph (as-built)

```
                    ┌──────────────────────────────────────────┐
   static/*.html ──▶│ web/app.py  (4 middlewares, 12 routers)   │
   (vanilla JS)     │  profile_host_guard → observability →     │
                    │  audit → security_headers                 │
                    └───────┬──────────────────────────────────┘
                            │ web/deps.py ── singletons + auth + scope
        ┌───────────────────┼────────────────────┬─────────────────┐
        ▼                   ▼                    ▼                 ▼
  routers/keys.py     routers/subscription  routers/miniapp   routers/{auth,
   (965 lines —        (public, no auth)    (Telegram HMAC)     admins,servers,
    the real domain)          │                   │             settings,…}
        │                     │                   │
        └────────┬────────────┴───────────────────┘
                 ▼
        core/  db.py · outline_api.py · scheduler.py · settings.py
               security.py · rights.py · backup.py · metrics.py · errors.py
                 │                              │
                 ▼                              ▼
            SQLite (WAL)              Outline Management API (httpx)
                 ▲
        bot/dispatcher.py ── bot/manager.py ── (same core, same rights)
```

**The important structural fact:** `core/` is genuinely framework-free and
`core/rights.py` is a single shared authority used by the dashboard, the bot and
the Mini App. That is the part of the architecture that is already right, and it
is why the panel and Telegram can no longer drift apart in permissions.

**The important structural flaw:** the *business logic* does not live in `core/`.
It lives in `web/routers/keys.py` — creation, purchase, reversal, renewal,
rotation, mirroring, deletion, bulk membership. `core/scheduler.py` re-implements
parts of it (monthly reset, expiry) against the same tables. `bot/dispatcher.py`
re-implements a third variant of extend (`cb_extend`, +30 days inline). So the
rules that decide what a customer gets exist in three places with three
implementations, and only the HTTP one is guarded by the purchase logic.

### Data flows

| Flow | Path | Notes |
|---|---|---|
| Sell a key | Browser → `POST /api/servers/{sid}/keys` → `idempotency.begin` → `_buy` (charge) → `create_key_for` → Outline → `db.add_key` → `_add_extra_servers` → mirror | Charge lands before Outline; failure reverses; retry replays |
| Customer fetches config | VPN client → `GET /sub/{token}` → rate-limit → `_collect` (20 s cache) → per-member `get_key` + `get_transfer_metrics` | No auth; token is the secret |
| Expiry / quota | `expiry_loop` (lease holder only) → per-server `get_transfer_metrics` → activate / reset / warn / disable + prune + backup + reconcile | One pass does eight unrelated jobs |
| Telegram | Update → `dispatcher.gate_key` → `core.rights` → `db`/`registry`, or `deps.create_key_as` → the HTTP route function | Mini App re-enters the same route functions with a synthetic auth |

### External integrations

* **Outline Management API** over httpx, TLS pinned by `certSha256` or
  `verify=False`. One `OutlineAPI` per server, rebuilt only when url/cert move.
* **Telegram Bot API** (aiogram v3, long polling) + **Mini App `initData`** HMAC.
* **Prometheus** text exposition at `/metrics`, bearer- or owner-gated.
* No payment gateway, no message broker, no cache server, no object store.

## 1.2 Domain invariants register

Extracted from code, comments and tests. "Enforced at" is where it actually
binds — not where it is documented. This table is the acceptance contract for
Stage 5: every row must still hold afterwards.

| # | Invariant | Enforced at | Tested | Risk if lost |
|---|---|---|---|---|
| **Money** ||||
| M1 | Credit can never go negative | `db.charge` — `WHERE credit >= ?` is the write itself | ✅ | Overdraft; concurrent tabs both sell |
| M2 | `admins.credit` always equals `SUM(credit_ledger.delta)` | Nothing at write time; `db.credit_drift` detects, scheduler alerts | ✅ | Silent money drift |
| M3 | Credit moves only through `charge`/`credit_admin`; `update_admin` refuses the `credit` column | `db.update_admin` allowlist | ✅ | Untraced balance edits |
| M4 | A retried purchase charges once | `web/idempotency.py` + `idempotency` PK | ✅ | Double charge, double key |
| M5 | A failed sale is reversed in full | `keys._reverse` + `guard.abandon()` | ✅ | Reseller pays for nothing |
| M6 | Ledger snapshots `package_name`/`price`; never joins back to `packages` | `_LEDGER_SCHEMA`, `packages.edit_package` | ✅ | History rewritten by a price edit |
| M7 | A credit admin gets **no** time or volume outside the price list | `keys.deny_free` on limit/monthly/reset; `_buy` on create/extend; `dispatcher.refuse_free` | ✅ | Free product (was the S3 leak) |
| M8 | Mirroring onto another server is the **one** deliberate free top-up | `sub_add_server` omits `deny_free` | ✅ | — (intentional) |
| **Access** ||||
| A-1 | Empty server allowlist means **no** servers, not all | `rights.can_see` | ✅ | Blank field = full panel access |
| A-2 | A sub-admin sees and edits only keys they own (`owner_admin_id`) | `rights.owns` + `keys_for_server` filter + `enforce_scope` | ✅ | Resellers see each other's customers |
| A-3 | `owner_admin_id IS NULL` means the panel owner's | `rights.owns` | ✅ | Legacy keys orphaned |
| A-4 | Telegram and the dashboard apply *identical* rules | `core/rights.py` shared; `dispatcher.gate_key`; `miniapp._may` | ✅ | Telegram becomes the lax back door |
| A-5 | Backup/restore, admin management and audit reading are never delegatable | `CAPS` tuple omits them; `require_owner` | ✅ | Privilege escalation to owner |
| A-6 | A subscription must be **wholly** the caller's | `keys.sub_or_404` | ✅ | Mint/unlink on a rival's customer |
| A-7 | Revocation is instant — the admin row is re-read every request | `deps.current_admin` | ✅ | Disabled admin keeps working |
| A-8 | One Telegram id ↔ one admin | `admins._check_telegram`, validated **before** insert | ✅ | Wrong rights handed out silently |
| **Time & quota** ||||
| T1 | Validity starts on first connection unless `start_now` | `create_key_for` + `scheduler` activation pass | ✅ | Unused key burns its term |
| T2 | `limit_bytes` is a **cumulative ceiling**, never a plan size | `_apply_package`, `reset_usage`, scheduler reset | ✅ | Quota compounds 10→20→40 GB |
| T3 | A "reset" means `used + monthly_bytes`; only `monthly_bytes` may be the base | `reset_usage` | ✅ | Compounding allowance |
| T4 | Monthly reset advances from `reset_ts`, not from now | `scheduler` `while nxt <= now: nxt += cycle` | ✅ | Cycle drift |
| T5 | Expiry disables (limit→0), never deletes | `scheduler` step 5 | ✅ | Data loss on expiry |
| T6 | A pending plan reports `pendingDays`, never "No expiry" | `subscription._collect_fresh` | ✅ | Customer told 30 days = forever |
| T7 | Rotation carries the clock and the *remaining* allowance | `rotate_key` | ✅ | Free data on every rotation |
| **Integrity** ||||
| I1 | An orphan Outline key is never left behind on a persist failure | `create_key_for`, `mirror_onto`, `rotate_key` compensations | ✅ | Unbilled keys accumulate |
| I2 | Restore is all-or-nothing and must carry every wiped table | `db.import_all` single transaction; `restore_backup` field check | ✅ | Panel wiped by a bad file |
| I3 | Migrations are ordered, numbered and each re-runnable | `db._migrate` + `PRAGMA user_version` | ✅ | Half-applied schema |
| I4 | Exactly one scheduler runs, whatever the worker count | `locks` lease | ✅ | Double reset / double expiry |
| I5 | Adoption verifies the key exists upstream before creating a row | `ensure_local` | ✅ | Invented keys mirrored for real |
| I6 | Subscription membership changes invalidate the cache | `subscription.invalidate` at every mutation site | ✅ | Removed server keeps serving |
| **Public surface** ||||
| P1 | The profile host serves the profile and nothing else | `profile_host_guard` + `_RESERVED` denylist | ✅ | Customer link reveals the panel |
| P2 | `/sub/{token}` is rate-limited per IP | `subscription._rate_limit` | ✅ | Amplifier onto the whole fleet |
| P3 | Forwarded headers are trusted only behind `TRUST_PROXY` | `_client_ip` in auth/audit/subscription | ✅ | Spoofed IP evades rate limits |
| P4 | No API response is cacheable | `security_headers` middleware | ✅ | `ss://` keys cached by a proxy |
| **Observability** ||||
| O1 | Every mutating `/api/` request is audited, even failed ones | `audit_middleware` | ✅ | Gaps exactly where it matters |
| O2 | Secrets are never written into the audit detail | `_SECRET_KEYS`, `_NO_BODY` | ✅ | Passwords in the log |
| O3 | Audit/health/rate/idempotency tables are pruned | scheduler housekeeping | ✅ | Unbounded growth |

**Undeclared invariant that the code does *not* hold — see A1 below:**
*"A customer is one subscription; suspending, renewing or deleting them applies
to every server they are on."* The UI, the pricing model and `mirror_onto`'s own
docstring all assume this. Nothing enforces it.

## 1.3 Anti-pattern, bottleneck & defect audit

Severity: 🔴 correctness/revenue · 🟠 performance/scale · 🟡 maintainability.

### A1 🔴 **The aggregate boundary is wrong — CONFIRMED DEFECT**

A customer's identity is the `sub_token`. Every write endpoint is keyed by
`(server_id, key_id)` — one row of that customer. `mirror_onto` creates the
extra rows; nothing keeps them in step afterwards.

Probed against the real app with two servers and a mirrored subscription:

```
MEMBERS:                [('s1','1',exp=1790099417,dis=0), ('s2','1',exp=1790099417,dis=0)]

POST /api/servers/s1/keys/1/disable   -> 200
AFTER DISABLE:          [('s1','1',dis=1), ('s2','1',dis=0)]
outline limits  s1={'1': 0}   s2={'1': 10737418240}      ← still fully live

POST /api/servers/s1/keys/1/extend {days:30} -> 200
AFTER EXTEND:           s1 exp=1792691417   s2 exp=1790099417  ← mirror not renewed

DELETE /api/servers/s1/keys/1         -> 200
SUB MEMBERS AFTER DELETING PRIMARY:  [('s2','1')]
s2 outline keys still live:          ['1']
public sub still serves:             200  [{'server':'S2', ... 'disabled': False}]
```

Three consequences, all live today:

1. **Suspension does not suspend.** A non-paying or abusive customer keeps a
   working config on every mirror. Only the primary is cut.
2. **Renewal does not renew.** A customer who paid for another 30 days is
   disabled by the scheduler on every mirror at the old date.
3. **Deletion does not delete.** Removing the customer leaves live keys on the
   mirrors and the public subscription link keeps handing them out — with no row
   left in the panel to find them by.

`reset_usage`, `set_key_limit`, `set_key_monthly` and `rotate_key` share the
defect. `test_features.py:269` proves the mirror *inherits* state at creation;
nothing tests that it *tracks* state afterwards. Neither `REFACTOR_PLAN.md` nor
`ROADMAP.md` mentions this.

Root cause is structural, not a missing line: `keys.py` treats the Outline key as
the aggregate root when the subscription is.

### A2 🟠 Settings de-caching traded staleness for an unbounded read fan-out

`SettingsStore` reads through to SQLite on every call — correct, and the right
fix for the stale-cache bug. But there are **41 call sites in `web/` alone**, and
several sit inside per-server loops: `_conn_info` reads `metrics_ttl` once per
server (`keys.py:43`), `keys_for_server` reads `profile_base` once per server
(`keys.py:73`). `current_admin` adds `session_max_age` plus a `reg.sync()` SELECT
to *every* authenticated request. A 10-server panel pays ~25 extra SELECTs per
`/api/keys` — polled every 30 s per open tab. Correct answer is a
request-scoped snapshot (or a version-stamped cache), not the old process dict.

### A3 🟠 Missing indexes on the two hottest lookups

`keys` has only `PRIMARY KEY (server_id, key_id)`. Both
`get_key_by_sub_token` and `get_keys_by_sub_token` full-scan the table — and
that is the **public, unauthenticated** subscription path. `owner_admin_id` is
also unindexed, so a reseller's key list is filtered in Python after loading
every row of each server. Two indexes fix both.

### A4 🟠 `bulk_server_membership` is sequential and unbounded — **fixed**

Up to 500 keys, each doing `ensure_local` → `sub_or_404` → `mirror_onto` →
one Outline `create_key` — serially, inside one HTTP request, with no progress.
500 × ~300 ms is ~2.5 minutes behind a proxy that will time out first.

*Fixed without making it a job.* Three phases: authorise every row sequentially
(the security boundary, and DB-only for a key the panel already knows), then do
the upstream work **once per subscription** a few at a time, then report in the
order asked. Measured over 120 customers at a 50 ms round trip: 6.46 s → 0.89 s
at the default concurrency of 8, 0.37 s at 24, and `bulk_concurrency: 1`
reproduces the old timing exactly.

Once per *subscription* rather than once per row is not an optimisation — a
selection can name two members of the same customer, and mirroring them
separately puts two keys on the destination for one person, the second unbilled
and invisible. Sequential code got away with it because `mirror_onto` checks
membership first; concurrent code would not. There is a test for it.

An async job was the original proposal and would have been the wrong trade here:
it changes the response shape the panel already renders, and sub-second is not a
problem that needs a job queue.

### A5 🟠 `_collect_fresh` is N+1 against upstream on a public route

Per member: one `get_key`. With a 4-server subscription that is four sequential
round-trips to four different hosts, on the path a VPN client hits every time
its `Profile-Update-Interval` fires. The 20 s cache and the per-IP limit shield
the fleet, but the tail latency is unnecessary — `list_keys` is already fetched
elsewhere and these could be gathered.

### A6 🟠 scrypt runs on the event loop

`verify_password` is a synchronous 55 ms scrypt (measured) called from async
`verify_login`. Every login attempt blocks the whole worker for 55 ms; the global
rate limit caps this at ~1.65 s of blocked loop per window. Bounded, but it is
free to fix with `run_in_executor`, and it is the reason the test suite takes
118 s.

### A7 🟡 Domain logic lives in the HTTP layer

`web/routers/keys.py` is 965 lines and is the only complete implementation of
the selling rules. `core/scheduler.py` re-implements reset/expiry;
`bot/dispatcher.py:cb_extend` re-implements extend as an inline +30 days that
skips `_apply_package`, skips the ledger and skips mirroring. `web/deps.py` has
to lazily import a router to let the bot create a key, and `miniapp.py` calls
route *functions* directly, which is why every one of its routes needs a manual
`_may()` — FastAPI dependencies never fire on that path. That is three
work-arounds for one misplaced layer.

### A8 🟡 No response contracts at all

Zero `response_model` in the codebase. Every response is a hand-built dict
literal; `keys_for_server` alone emits 21 keys. The frontend consumes them by
convention. Nothing detects a renamed field until the UI blanks.
`/openapi.json` and `/docs` are served and describe request bodies only.

### A9 🟡 Manual XSS discipline in ~1,500 lines of string-built HTML

`esc()` is applied consistently — I found no unescaped injection of
server-controlled data, and attributes are uniformly double-quoted (`esc()` does
not escape `'`). But the safety is one forgotten call away, in a file that grows,
with no lint rule and no test. This is a latent risk, not a current bug.

### A10 🟡 Backup claims more than it carries

`export_all` writes `"version": 1` and `import_all` never reads it; unknown
columns are dropped silently by the `[c for c in COLS if c in row]` filter, so a
newer backup restored into older code loses data quietly. `server_health` is not
exported at all, though the README says backup covers "everything".

### A11 🟡 One scheduler pass does eight unrelated jobs

Activation, monthly reset, notifications, health probing, four kinds of pruning,
backup, and credit reconciliation all run on the same interval in one
`_check_once`. A slow health probe delays expiry enforcement; the interval that
suits alerting (60 s) is absurd for backups (24 h) and is worked around with
timestamps in `settings`.

### A12 🟡 `/docs` and `/openapi.json` are unauthenticated on the panel host

They leak the full route surface of an internet-exposed admin panel. Free to
disable in production.

### Deliberately **not** flagged

`_wants_html` heuristic (self-documented, has `?format=raw`), the single-flight
metrics cache (removing it means hammering every server), one SQLite file, one
process, vanilla JS. All are correct calls for this system.

---

# STAGE 2 — ARCHITECTURAL SYNTHESIS & INNOVATION MATRIX

## 2.1 Target architecture

Hexagonal, but **calibrated**: three layers, ports only where a second
implementation is real or imminent. Ports invented for symmetry are cost with no
return.

```
┌── domain/ ─────────────────────────────────── pure, no I/O, no framework ──┐
│  subscription.py   Subscription aggregate: members, allowance, clock,      │
│                    suspension. All state transitions are methods that      │
│                    return a list of Commands — they never touch a DB.      │
│  pricing.py        price_for, discount, package → grant  (from rights.py)  │
│  rights.py         unchanged — already correct                             │
│  policy.py         deny_free, on_credit, mirror-is-free                    │
│  events.py         KeyCreated, Suspended, Renewed, Rotated, Removed        │
└─────────────────────────────────────────────────────────────────────────────┘
┌── application/ ──────────────────── use cases; orchestrate, don't decide ──┐
│  sell_key.py  renew.py  suspend.py  rotate.py  remove.py  mirror.py        │
│  Each: load aggregate → call domain → charge (port) → dispatch commands    │
│  through the node port → persist → emit events. Idempotency wraps here,    │
│  not in the router, so bot / Mini App / HTTP all get it.                   │
└─────────────────────────────────────────────────────────────────────────────┘
┌── ports/ ──────────────────────────────────────── the only abstractions ───┐
│  NodePort      list/create/delete/rename/set_limit/usage/info   ← REAL:    │
│                Outline today, Xray/sing-box the day Shadowsocks is blocked │
│  LedgerPort    charge / credit / statement                                 │
│  ClockPort     now()  ← makes every time invariant testable without sleeps │
│  NotifierPort  already exists in spirit (scheduler's notifier)             │
└─────────────────────────────────────────────────────────────────────────────┘
┌── infrastructure/ ─────────────────────────────────────────────────────────┐
│  sqlite/ (db.py split by table group)  outline/ (current outline_api.py)   │
│  telegram/  http/ (FastAPI routers become thin: parse → use case → model)  │
│  jobs/ (the scheduler, split — see F1)                                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

Two rules make this worth doing rather than decorative:

1. **`web/routers/*` may not import `core/db` directly.** Enforced by a test
   that walks the import graph, not by discipline.
2. **A state change is expressed once**, as a method on the aggregate. The
   scheduler, the bot and HTTP all call the same use case. A1 becomes
   unrepresentable: `Subscription.suspend()` returns a command per member.

**What I am not doing:** no repository interface per table, no DTO layer between
application and domain, no dependency-injection container, no CQRS. Each would
add a file per concept and buy nothing at this size.

## 2.2 Innovation matrix

### F1 — Convergence loop (event-driven reconciliation) · `M` · impact 🔴 high

**What.** Every domain state change writes to an `outbox` table
(`subscription_token, member, intent, payload, attempts, next_try_ts`) inside the
same transaction as the DB write. A worker drains it against the node port with
exponential backoff. A periodic *reconciler* compares desired state (panel) with
actual state (each server) per subscription and enqueues the difference.

**Why it is the keystone.** It is the correct fix for A1 *and* for the
half-applied-mutation class generally: today "Outline said 502" means the panel
and the server disagree until a human notices. It also makes A4 (bulk) trivial —
enqueue 500 intents, return immediately.

**Trade-offs.** Writes become eventually consistent: the UI must show
"suspending…" rather than "suspended", which is a real UX change and the honest
one. Adds one table, one worker, and a reconciler that can *fight* a human who
edits a key in Outline Manager directly — so adoption (`ensure_local`) must stay
the reconciler's rule for unknown keys, never deletion. Cost: ~350 lines +
tests. **Recommended, first.**

### F2 — Node port + a second backend · `L` · impact 🔴 existential

**What.** `ROADMAP.md` 4.1, with a concrete recommendation attached. `OutlineAPI`
goes behind `NodePort`; a second adapter (Xray/sing-box, VLESS+Reality) follows.

**Why now specifically.** The roadmap says "do the abstraction later, decide
now", and that was right *then*. A1 forces `keys.py` to be rewritten anyway.
Introducing the port during that rewrite is nearly free; introducing it
afterwards means touching the same 965 lines twice. The window is open exactly
once.

**Trade-offs.** It changes the product's identity from "Outline panel" to "VPN
panel" — repo name, docs, `sub.html`'s app table, and the `ss://`-shaped
assumptions in `_ss_with_label`. The port is only honest if the second adapter
is actually written; a one-implementation interface is YAGNI and the roadmap
says so. **Recommended only if you answer "yes" to Q1.** If the answer is no,
skip the port entirely and keep `OutlineAPI` concrete — do not build it "just in
case".

### F3 — SSE live updates, replacing 5-second polling · `M` · impact 🟠 high

**What.** One `GET /api/stream` (Server-Sent Events, owner/admin-scoped) pushing
`stats`, `key-changed` and `server-health` events. The frontend's `setInterval`
and its two signature hashes (`_sigStats`, `_sigKeys`) disappear.

**Why.** Today every open tab fans out to every Outline server every 5 s, and
`/api/keys` every 30 s. Ten tabs on a 10-server panel is 120 upstream requests a
minute for data that changes once a minute (Outline refreshes bandwidth ~60 s —
the code already knows this and dedupes by `bwTs`). SSE moves the fan-out to
*one* server-side sampler, which is also the natural home for the reconciler's
output. Directly serves the Stage 3 latency budget: no poll-driven re-renders.

**Trade-offs.** SSE needs a proxy that does not buffer (Caddy is fine; a naive
nginx config is not) and one connection per tab — irrelevant at this scale.
Needs a polling fallback for the Mini App inside older Telegram webviews.
WebSockets rejected: bidirectional is not needed and costs a protocol upgrade
path. Cost: ~200 lines server, ~120 removed client-side. **Recommended.**

### F4 — Optimistic UI with a durable command queue · `S/M` · impact 🟠 medium-high

**What.** The panel already mints `Idempotency-Key` per purchase *intent* — the
hard half is done. Extend it: every mutation is queued in `localStorage` with its
key, applied optimistically to the local state, and replayed on reconnect. The
server's idempotency table already guarantees replay is safe.

**Why.** Resellers work from phones on the same censored, lossy networks the
product exists to route around. Today a lost response on "create user" leaves
them staring at a spinner, unsure whether they were charged.

**Trade-offs.** Rollback UX on genuine failure is fiddly and must never
optimistically show *money* as spent. Restricted to key operations; balance
always comes from the server. Pairs naturally with F1's "pending" state, so build
it after F1 or the two will disagree about what "applied" means.

### F5 — Optional: read-only MCP surface for the owner · `S` · impact 🟡 low-medium

**What.** A small MCP server exposing read-only queries ("who expires this
week", "which resellers are below X credit", "which servers flapped").

**Trade-offs.** Genuinely useful for an owner who lives in chat, and cheap on top
of the typed contracts from Stage 4. But it is a *new authentication boundary on
a system that holds every customer's `ss://` key*, and it would be the only
surface not covered by the existing session/TOTP/audit story. If built:
read-only, owner-token-only, audited like everything else, and never on the
profile host. **Not recommended for this cycle** — it is the least valuable item
here and the most likely to become a hole.

### Deliberately rejected (extending the roadmap's list)

| Proposal | Why not |
|---|---|
| Event sourcing the subscription | `credit_ledger` already gives append-only where money is; F1's outbox gives convergence. Full ES adds replay, snapshots and versioning for no question anyone is asking |
| CQRS read models | The read path is one SQLite file and a 20 s cache. There is no read/write asymmetry to exploit |
| Postgres / Redis | Unchanged from `ROADMAP.md`. WAL SQLite is far from its limit and a second service is a second thing to lose |
| Frontend framework | Three pages. The real problem (§3) is layout strategy and render granularity, both fixable in vanilla |
| gRPC / GraphQL | Single first-party consumer. FastAPI already emits OpenAPI — generate a typed client from it instead (§2.3) |

## 2.3 Integration overhaul

| Concern | Today | Target | Why |
|---|---|---|---|
| Panel ↔ browser, reads | REST + 5 s polling | REST + **SSE** (F3) | Kills the poll fan-out and the poll-driven re-render |
| Panel ↔ browser, writes | REST + `Idempotency-Key` | Unchanged, extended to every mutation | Already the right primitive |
| Type safety across the wire | None (A8) | `response_model` on every route → `/openapi.json` → **generated TS types** consumed by the vanilla frontend as JSDoc | Contract enforced at build time with no framework and no bundler |
| Panel ↔ Outline | Direct httpx per call site | `NodePort` behind F1's outbox | Retries, backoff and convergence in one place |
| Panel ↔ Telegram | Long polling | Unchanged | Webhooks need a stable public HTTPS endpoint many operators do not have |
| Mini App auth | `initData` HMAC, routes call route functions directly | Same HMAC, routes call **use cases** | Removes the `_may()` work-around class entirely |

---

# STAGE 3 — FLUID & ULTRA-RESPONSIVE UI/UX SPECIFICATION

## 3.0 What is actually wrong today

Measured against the current `index.html`:

* **Layout is decided in JavaScript.** `const narrow = innerWidth < 720` and
  `wide = W >= 1100` pick between three hand-written `style.cssText` strings
  (`renderApp`). One `@media` rule exists in the whole file (line 98).
  *(Correction, made while fixing this: there **is** a resize listener — the
  review said there was not. It re-renders the whole app, debounced by 200 ms,
  when the window crosses 720px. That is worse than no listener in one respect:
  a rotation shows the wrong layout for a fifth of a second and destroys any
  open dialog. It is gone now.)*
* **Every update is a full `innerHTML` swap.** `renderList()` rebuilds the entire
  table; focus, scroll position, text selection and any open `<details>` are
  destroyed. The poll already guards this with `keysSig()`, which is a clever
  patch over the wrong granularity.
* **No `clamp()` anywhere.** Every size is a fixed px literal, several
  interpolated from JS (`max-width:${innerWidth<720?'150px':'230px'}`).
* **CLS is unbounded on load** — KPI cards, the list and the rail all mount into
  zero-height containers after their fetches resolve.
* **Accessibility is thin**: 7 `aria-*`/`role` attributes in 1,537 lines. Rows
  are `<div data-act="detail">` — not focusable, not keyboard-activatable,
  not announced. There is a command palette and `?` help, so keyboard users are
  clearly in scope; the list itself just was not finished.
* **No `prefers-reduced-motion`, no `prefers-color-scheme`** in any of the three
  pages. Seven keyframe animations run unconditionally.
* RTL is handled well (`i18n.js`, the `[dir="rtl"]` block is genuinely
  thoughtful) — that part stays.

## 3.1 Container-driven responsiveness (mandate)

1. **Zero layout decisions in JavaScript.** `innerWidth` may not appear in a
   layout expression. Enforced by a grep test in CI.
2. Every panel that can appear in more than one context declares
   `container-type: inline-size` and sizes itself with `@container`, not
   `@media`. The key list must lay out correctly at 320 px inside a Telegram
   webview, at 380 px in the rail, and at 1400 px full-width — with one
   stylesheet.
3. **Fluid scale.** A token set replaces the px literals:
   `--step-0: clamp(0.875rem, 0.83rem + 0.22vw, 1rem)` and siblings for
   `-1…3`; spacing on the same ratio. Component CSS references tokens only.
4. **Intrinsic sizing** for the grids that are currently hand-branched:
   `grid-template-columns: repeat(auto-fit, minmax(clamp(16rem, 40cqi, 22rem), 1fr))`
   replaces the wide/mid/narrow triple.
5. The table→cards switch becomes a container query, not a JS branch — and stops
   being a user-visible "view" toggle at sizes where only one fits.
6. Styles move out of inline `style="…"` strings into a real stylesheet, so a
   Content-Security-Policy without `unsafe-inline` becomes possible. That is a
   security win the current architecture cannot have.

## 3.2 Reactivity & interaction performance (budget)

| Metric | Target | How |
|---|---|---|
| INP | **< 150 ms** p75, < 100 ms typical | Keyed DOM patching (`data-key` per row, update in place) instead of `innerHTML`; SSE replaces polling so idle costs zero DOM work |
| CLS | **0** | Every async region reserves its box: KPI cards render skeletons at final height; the rail and list have `min-block-size` from first paint; QR canvases get fixed `aspect-ratio` |
| LCP | < 1.5 s on a cold 3G-class link | Already no CDN font and no framework — keep it that way. Inline critical CSS, defer `qrcodejs` until a QR is actually opened (it is loaded on every page load today) |
| Long tasks | none > 50 ms | Sort/filter over >500 keys moves off the render path; the SSE handler coalesces bursts with one `requestAnimationFrame` |
| Idle network | 0 requests when hidden | Already partly done (`document.hidden` check); SSE closes on `visibilitychange` and resumes with `Last-Event-ID` |

Rendering rule: **state → diff → patch**. A ~60-line keyed reconciler over
`data-key` rows, no virtual DOM, no build step. Everything that is a full
re-render today becomes a patch except navigation between screens.

## 3.3 Device & accessibility invariants

* Every actionable row is a real `<button>` or has `role="button"` +
  `tabindex="0"` + Enter/Space handling. Full keyboard path: list → row →
  drawer → close, with focus returned to the invoking row.
* Focus is trapped in modals and drawers; `Esc` closes; `:focus-visible` ring is
  already defined and stays.
* Touch targets ≥ 44×44 CSS px. Several icon buttons are 27 px today.
* `prefers-reduced-motion: reduce` disables all seven keyframe animations and
  the pulse dot.
* `prefers-color-scheme: light` gets a real palette. The panel is dark-only, and
  it is used in daylight on phones.
* Live regions: toast is `role="status"`, destructive confirmations move from
  `confirm()` to an accessible dialog (the `ask()` helper is already the single
  choke point — one change, whole app).
* Safe-area insets are already handled; keep and extend to the drawer.
* RTL: keep `i18n.js` as-is; migrate the remaining physical properties
  (`left`, `right`, `margin-left`) to logical ones (`inline-start`, etc.).

---

# STAGE 4 — EXECUTABLE CONTRACTS & VERIFICATION HARNESS

## 4.1 Strict type contracts

**Python.** Pydantic v2 models for every response, `response_model=` on every
route, plus:

```toml
[tool.mypy]        # new
strict = true
files = ["src/outline_panel/domain", "src/outline_panel/application", "src/outline_panel/ports"]
# infrastructure and web are added a package at a time, never with `ignore_errors`
```

Domain types stop being `dict`:

```python
@dataclass(frozen=True, slots=True)
class Member:
    server_id: ServerId
    key_id: KeyId
    limit_bytes: int | None          # cumulative ceiling — invariant T2
    monthly_bytes: int | None
    activated_ts: int | None
    expiry_ts: int | None
    disabled: bool

@dataclass(frozen=True, slots=True)
class Subscription:
    token: SubToken
    owner_admin_id: AdminId | None   # None = the panel owner — invariant A-3
    members: tuple[Member, ...]

    def suspend(self) -> tuple[Command, ...]: ...
    def renew(self, grant: Grant, now: int) -> tuple[Command, ...]: ...
    def remove(self) -> tuple[Command, ...]: ...
```

Every one of those returns commands **for all members**, which is how A1 stops
being expressible.

**Wire.** `/openapi.json` (already generated) becomes the source of truth; a
CI step generates `static/types.d.ts` and the frontend annotates with JSDoc
`@type` imports. Typecheck via `tsc --checkJs --noEmit` — no bundler, no
`node_modules` at runtime, the pages stay hand-editable HTML.

**Config.** The `KNOBS` table already carries its own spec (min/max/label/help)
and generates the settings UI — that design is good and stays. It gains a
Pydantic model so a knob's type is checked once, at definition.

## 4.2 Characterization tests (golden master)

The 306 existing tests are the asset that makes this refactor safe. The harness
adds three layers on top:

**(a) API golden master.** A seeded fixture DB (2 servers, 3 admins, 12 keys
across every state: pending, active, disabled, monthly, mirrored, adopted,
rotated) is replayed against ~40 endpoints; responses are snapshotted to
`tests/golden/*.json` with volatile fields (timestamps, ids, tokens)
normalised. Any field renamed, added or dropped during the refactor shows up as
a diff that a human must approve. This is what protects the undocumented wire
contract (A8) while it is being formalised.

**(b) Invariant suite — one test per row of §1.2.** Most exist; they get moved
into `tests/invariants/` and named after their id (`test_M1_credit_never_negative`)
so a deleted guarantee is visible as a deleted test. Each is mutation-checked:
break the guard, confirm the test fails. `REFACTOR_PLAN.md` already established
this practice for the security fixes.

**(c) New-behaviour tests, written before the code:**

| id | Test | Currently |
|---|---|---|
| A1-1 | Suspending a 2-server subscription sets limit 0 on **both** | ❌ fails |
| A1-2 | Renewing moves `expiry_ts` on **every** member | ❌ fails |
| A1-3 | Deleting removes every member's key upstream and leaves no live sub | ❌ fails |
| A1-4 | Rotating a mirrored member keeps the other members' clocks | ❌ fails |
| A1-5 | `reset_usage` / `set_limit` apply to every member | ❌ fails |
| F1-1 | An outbox intent whose node call 502s is retried and converges | new |
| F1-2 | The reconciler never deletes an unknown upstream key (it adopts) | new |
| F1-3 | Draining is idempotent — a replayed intent is a no-op | new |
| F3-1 | SSE emits on change and nothing while idle | new |
| UI-1 | No `innerWidth` in a layout expression (grep gate) | new |
| UI-2 | Axe-core clean on all three pages (Playwright, already available) | new |
| UI-3 | CLS = 0 and INP < 150 ms on a scripted list interaction | new |

**(d) Deterministic gates in CI** (`ci.yml` currently runs `pytest -q` only):

```
ruff check --no-fix .        →  zero warnings
mypy (scoped, strict)        →  zero errors
pytest -q                    →  306 + new, all green
tsc --checkJs --noEmit       →  zero errors
import-graph test            →  web/* must not import core.db
playwright: axe + CLS/INP    →  budgets in §3.2 enforced
```

Speed note: moving scrypt off the event loop (A6) and lowering the scrypt cost
factor **in tests only** takes the suite from 118 s to a few seconds, which is
what makes a gate people actually wait for.

---

# STAGE 5 — EXECUTION (approved 2026-08-23)

Decisions taken at the gate: **Q1 yes** (second protocol is coming, build the
port), **Q2 targeted** (steps 0–7, not a clean-slate rebuild), **Q3 converged**
(outbox + reconciler), **Q4 by seam** (file sizes follow the seams that exist).

| # | Step | Status |
|---|---|---|
| 0 | Harness: golden master, A1 targets, structural gates, CI | ✅ `6148e44` |
| 1 | A3 indexes · A6 executor · A2 request-scoped settings · A12 docs off | ✅ `e2daf29` |
| 2 | `domain/` + `application/` extracted; routers thin | ✅ `a3277f4` |
| 3 | **A1 fixed** — subscription aggregate | ✅ `a3277f4` |
| 4 | Outbox drained; reconciler + drift report | ✅ `d8e3b31` |
| 8 | Node port + conformance suite | ✅ (adapter #2 pending — see below) |
| 5 | Response models — 83 of 92 operations; the other 9 are not JSON | ✅ |
| 6 | Frontend: tokens, container queries, keyed patching, a11y | ✅ |
| 7 | SSE; polling removed | ✅ |

354 tests green, ruff clean, and **every golden-master snapshot is
byte-identical** from step 0 to here — the domain moved, the wire did not.

## What landed, against what was planned

**A1 is closed**, and it was worse than the review described. Three separate
propagation failures (suspend, renew, delete) plus a fourth found while fixing
them: `mirror_onto` never carried `monthly_bytes`, so a mirrored member was
invisible to the scheduler's monthly reset and kept the ceiling it was born with
for good, while the customer's own page showed a quota that refreshed.

**The bot was the third implementation**, as §1.3 A7 said. `cb_extend` was an
inline +30 days that skipped the aggregate, the ledger and the mirrors
entirely; `cb_disable`, `cb_enable`, `cb_del` and `step_set_limit` each had
their own copy. All five now call the same use cases, so Telegram cannot
reintroduce the bug the panel just stopped having. A structural test enforces
it: no router or bot handler may call the per-member writes.

**The converged design needed one rule the review did not anticipate.** Writing
desired state and queuing the upstream half turned out to conflict with an
existing guarantee — `test_extend_does_not_commit_before_outline` asserts that a
502 must leave the expiry alone, or an admin's retry stacks the days twice. Both
are right, and the resolution is better than either extreme:

* *some* members reached their server → partial success, stragglers queued,
  `pending` in the response;
* *no* member reached any server → nothing written, nothing queued, 502.

So a single-server panel behaves exactly as it always did, and a multi-server
one stops being held hostage by its worst node. Commands with no upstream half
(`SetExpiry`, `SetDuration`, a plain `SetMonthly`) are excluded from that count,
or clearing a quota while a server is down would look like total failure.

**Grill §6's migration risk is handled, not deferred.** `GET
/api/convergence/drift` is a read-only report of what each server actually
enforces versus what the panel believes; applying it is a separate call and is
off by default (`reconcile_enabled`). The first real pass on a panel that has
been running with A1 will cut off customers who have been connected for months,
and that should be something an operator reads first and then decides.
Reconciliation only ever queues *suspensions* — a mismatched ceiling can be a
hand edit in Outline Manager, and a key upstream with no panel row is never
touched, because adoption is `ensure_local`'s job and a reconciler that deleted
those would be a data-loss bug on a timer.

**Step 8 is deliberately three-quarters done.** `ports/node.py` names the
surface, `OutlineAPI` satisfies it structurally without having changed, and
`tests/test_node_port.py` is the conformance suite a second adapter is developed
against. What is *not* here is the Xray/sing-box adapter itself: VLESS+Reality
needs a real node to develop against, and an adapter written blind would be
worse than no adapter — it would look finished. Writing the port did its job
immediately, though: it caught two ways `FakeOutline` had drifted from the real
client, both of which were making tests lie.

## Two corrections to this document

1. **§4.2(d) claimed lowering the scrypt cost in tests would take the suite from
   118 s to seconds.** It would save roughly 35 s of the 120, and the only way
   to do it is an env var that weakens password hashing — read by production
   code, one misconfiguration away from being real. Not worth it; dropped. The
   suite is ~140 s and the time is spread across ~350 fixture setups that each
   re-import the package, not concentrated anywhere a knob could reach.
2. **§4.2(b) proposed moving every invariant test into `tests/invariants/`.**
   Moving 300 passing tests is churn with real risk and no behavioural payoff.
   `tests/INVARIANTS.md` maps each guarantee to the test that holds it and
   `test_invariant_map.py` asserts the mapping resolves, which buys the property
   that mattered — a deleted guarantee shows up as a broken map rather than as
   one fewer green dot — for none of the risk.

## What remains

Steps 5, 6 and 7 are each a session's work and are independent of everything
above. Recommended order, and why:

* **7 is done.** One server-side sampler feeds every tab; measured in Chromium,
  the dashboard makes zero `/api/stats` or `/api/keys` requests while idle,
  against one every five seconds before. Two things worth recording for whoever
  reads this next: httpx's `ASGITransport` buffers a streaming response, so the
  stream tests drive ASGI directly rather than through the test client — the
  endpoint was correct long before the tests agreed it was. And a stream is a
  long-lived connection, so it re-reads the admin row every tick and closes with
  a `bye` frame when access is revoked; otherwise disabling someone would leave
  them watching for as long as their tab stayed open.
* **6 is done.** Nine `innerWidth` layout branches and the debounced `resize`
  handler that kept them honest are gone; `test_architecture.py` now asserts
  zero rather than ratcheting down. The table and the cards were two templates
  with two sets of fields — they are one row now, and the container decides how
  it reads, so the "view" toggle is a preference rather than the only thing
  between a phone and an unusable grid. `renderList` patches keyed rows instead
  of rebuilding the table, which is what makes a live stream survivable:
  measured in Chromium, focus, caret position and scroll position all survive a
  snapshot arriving while you type. `tests/test_ui.py` holds those checks; it is
  run by hand rather than in CI, since a browser install per run costs more than
  it returns for checks that only move when `static/` does.

* **5 is done.** Every route that returns JSON declares what it returns — 83 of
  92 operations. The nine that do not are a named allowlist in
  `test_architecture.py`, not a threshold: they return a file, a stream, plain
  text, or the whole database as a download, and a response model on any of them
  would be a lie about the content type. Adding a route without a model now
  fails that test and has to be argued for by name.

  The order mattered more than the models did. 36 new snapshots went in first —
  the entire Mini App surface, convergence, and every admin, package, server and
  settings write had no snapshot at all — taking the golden master from 57 to
  93. Only then were the models attached. All 93 came back byte-identical
  afterwards, which is the evidence that 71 newly typed routes dropped no field.
  `response_model_exclude_unset=True` is on every one of them: without it FastAPI
  materialises absent optional fields as `null`, which is itself a wire change.

* **A4 is done** — see the finding above, which now records the fix.

* **The second node adapter** is gated on having a VLESS node to develop
  against, not on any of this.

# "GRILL ME" — where this plan is weak

**1. F1 makes the panel eventually consistent, and that is a UX regression
before it is an improvement.** "Suspend" currently returns 200 meaning done. It
would return 202 meaning queued. On a healthy fleet the difference is
milliseconds; on a flapping server, an operator sees "pending" and does not know
whether to worry. Mitigation: per-member status in the drawer and an explicit
"out of sync" badge. If you would rather keep synchronous writes, the smaller
fix for A1 is "iterate the members inline and fail the whole request if any
member fails" — simpler, but it means one unreachable server blocks suspending a
customer on the servers that *are* reachable, which is arguably worse for the
thing you most need to work.

**2. Step 2 (extracting the domain) is the highest-risk step and buys the least
on its own.** It touches every route while changing no behaviour. If it slips, we
have paid the cost and banked nothing. The golden master exists precisely to make
this safe — but if you want value first, we can do step 3 (fix A1) *inside*
`keys.py` and extract afterwards. I recommend against it: the extraction is what
makes the aggregate fix natural rather than five more careful loops, and doing it
after means editing the same code twice.

**3. F2 is a product decision I cannot make for you, and it gates everything
downstream.** `ROADMAP.md` 2.1 (customer identity) locks the data model around
Outline keys. If VLESS is coming, the port must precede customer identity. If it
is not, building the port is waste. This is Q1 and it is genuinely blocking.

**4. The Stage 3 rewrite touches the one thing users see, with no visual
regression test today.** Adding Playwright screenshot baselines is cheap
(Chromium is already available here) but they are noisy across renderers. I would
rather ship §3 in two passes — tokens + container queries first (mechanical,
verifiable), keyed patching second (behavioural, needs the interaction tests) —
than one big-bang restyle.

**5. My own bias to declare:** I found A1 and I am proposing an architecture
shaped around it. A cheaper reading is "add five loops to `keys.py`, add five
tests, done in a day". That is a legitimate choice and it fixes the bug. What it
does not fix is the reason the bug existed — three copies of the selling rules
and no place where a customer is one object — so the next feature that touches
membership reintroduces it. I think the extraction is right, but the cheap fix is
not wrong, and it is your money.

**6. Migration risk I have not fully costed:** F1's outbox and the reconciler
need to run against panels whose SQLite already disagrees with their servers —
that is the *normal* state after A1 has been live. The first reconciler pass on
a real panel will find mirrored keys that should have been suspended months ago
and will suspend them. That is correct, and it will also be the day several
customers who were quietly getting free service stop. That needs to be a
deliberate, announced switch with a dry-run mode, not a silent upgrade.

---

# Approval gate — answered 2026-08-23

| # | Question | Answer |
|---|---|---|
| Q1 | Second protocol (VLESS/Reality)? | **Yes** — port built, adapter pending a node to develop against |
| Q2 | Targeted re-architecture or clean-slate rebuild? | **Targeted**, steps 0–7 |
| Q3 | A1 converged/async or synchronous? | **Converged** — outbox + reconciler |
| Q4 | 150-line cap literal or by seam? | **By seam** |

Steps 0–4 and 8 are done. Steps 5, 6 and 7 remain, in the order argued above.
