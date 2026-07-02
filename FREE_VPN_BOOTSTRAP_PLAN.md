# Server task: bootstrap "free VPN for Telegram login" endpoint

## Why
The Clavis mobile app added a censorship **escape hatch**: in the quick-setup
wizard, when a user says Telegram is *not* reachable, the app temporarily
connects a free VPN just long enough to open Telegram and log in, then
force-disconnects after 5 minutes. The app needs a server endpoint that hands
out a working VPN config on demand, rate-limited per device.

**The client side is already implemented and shipped.** This document is the
server contract it expects, plus a concrete implementation plan. Do not change
the request/response shape below — match it.

---

## 1. The exact client contract (must match)

Client code: `_fetchBootstrapConfig()` in the **clavis-app** repo
(`flutter_app/lib/src/login/telegram_bootstrap.dart`) — shared by both the
quick-setup wizard and «Войти через Telegram».

- **Request:** `GET {base}/{install_id}` — plain HTTP GET, **no auth header**.
  - `base` is a constant the app team sets (`kBootstrapConfigBase`); you only
    own everything after it, i.e. the `/{install_id}` path segment.
  - `install_id` is the app's per-install identifier: a 32-char **base64url**
    string (charset `A-Za-z0-9-_`, no padding). It is generated on first launch
    and is the **same id already sent to `/device/register`** — you can
    correlate device records by it. Validate charset/length (e.g. 16–64 chars);
    reject anything else with 400.
  - Must respond within ~10s (client timeout: 8s connect, 10s total).
- **Success → HTTP 200**, body is EITHER:
  - JSON `{"link": "vless://..."}` (recommended), or
  - the bare link as the response body (`vless://...`).
  - The link must be a single, working server config on the **Free** group.
    Supported protocols (app engine): `vless`, `vmess`, `trojan`, `shadowsocks`,
    `clavis`. Prefer a DPI-resistant one (vless+REALITY or the clavis transport).
- **Rate-limited → HTTP 429** with body `{"error": "rate_limited"}` (recommended;
  the client treats either a 429 status OR an `{"error":"rate_limited"}` body as
  throttled and shows "попробуйте позже").
- **Any other non-200** → the client shows a generic "сервер недоступен (code)".
- **Empty link** → the client shows "пустая конфигурация" — so never return 200
  with an empty `link`.

Client behavior after it gets the link (for context, no server action needed):
`importServer(link)` → connect → wait until `telegram.org` resolves through the
tunnel → open Telegram → **disconnect + delete the imported server after 5 min**.
So the issued key is used for ≈1–5 minutes and then abandoned by the client.

---

## 2. What to build

A new public route on the subscription FastAPI server
(`subscription/router.py`), e.g.:

```
GET /app/free-vpn/{install_id}
```

(so the app's `_kBootstrapConfigBase` = `https://<host>/app/free-vpn`).

It must:

1. **Validate** `install_id` (charset/length) → 400 on bad input.
2. **Rate-limit by `install_id`** (see §3). If over the limit → `429
   {"error":"rate_limited"}`.
3. **Issue a free key** on the `FREE_GROUP_NAME` ("Free") server group for this
   install, short-lived (see §4).
4. **Return** `200 {"link": "<one vless:// link to a Free server>"}`.

### Reuse these existing pieces (file:line, clavis-vpn-bot2)
- **Closest template — the (disabled) free trial:** `subscription/router.py:987`
  `@router.get("/trial")` / `:994 /trial_disabled` — "Free 48h VPN trial,
  rate-limited per IP". Mirror its structure, but key on `install_id` instead of
  IP and return a single link instead of an HTML page.
- **Free sub + key creation:** `subscription/router.py:709` `@router.get(
  "/invite/{code}")` — creates a guest `User`, a `Subscription(plan_type='free',
  device_limit=1, expires_at=now+REFERRAL_SUBSCRIPTION_DAYS)`, then
  `KeyService.ensure_keys_exist(db, subscription, <client_id>)`. This is the
  proven "free key on the Free group" flow. Copy it; shorten the TTL (§4).
- **Build a single `vless://` link from a sub's keys:** the `/raw/{token}`
  (`subscription/router.py:277`) and `/json/{token}` (`:401`) handlers already
  render per-server `vless://` URIs (see the `uri.startswith("vless://")` checks
  at `:342` and `:429`). Extract/reuse that to get the first Free-server link
  for the new sub. Return that one link.
- **Free group + duration constants:** `config/settings.py` — `FREE_GROUP_NAME =
  "Free"` (line ~94/102), `REFERRAL_SUBSCRIPTION_DAYS = 3` (line ~103),
  `DEVICE_LIMIT` (~117).
- **Key/server selection already filters to Free for `plan_type='free'`:**
  `services/key_service.py` → `select_servers_for_user()` (the `if plan_type ==
  'free': ... FREE_GROUP_NAME` branch) and `ensure_keys_exist(db, sub,
  client_id)`; `_client_id_for_sub(sub)` builds the client id
  (`app_{account_id}` / telegram_id). For a no-account install use a stable
  synthetic client id derived from `install_id`, e.g. `app_boot_{install_id}`.
- **429 precedent:** `subscription/api/router.py:242` /:395 →
  `raise HTTPException(status_code=429, detail="rate_limited")`. Make the body
  `{"error":"rate_limited"}` to match the client's JSON parse.
- **DB session:** `with get_db_session() as db:` (used throughout router.py).
- **Subscription model fields:** `database/models.py:53` `class Subscription`
  (`user_id`, `account_id`, `plan_type`, `is_test`, `is_active`, `expires_at`,
  `device_limit`, `token`).

---

## 3. Rate-limiting (per install_id)

The app's 5-minute timer is only a *soft* cap; **the real limit is here.**

- Add a small table, e.g. `BootstrapGrant(install_id PK, first_seen,
  last_issued_at, count_today, day)` — or reuse an existing throttle table if one
  fits. (The `/trial` route limited per IP; here key on `install_id`.)
- Suggested policy (tune): **max 1 issuance per install_id per 10 minutes** and
  **max 3 per rolling 24h**. A legit user needs ~1 per login attempt.
- Over the limit → `429 {"error":"rate_limited"}`.
- Banning auto-generated install_ids (people scripting fake ids) is a separate
  moderation concern, handled server-side out of band — **not** this endpoint's
  job, but keep the table so such ids are observable.

---

## 4. Lifetime / cleanup

The client uses the key for ≈1–5 min then drops it. Don't let throwaway free
subs/keys accumulate:

- Issue with a **short TTL** — e.g. `expires_at = now + 1 hour` (not 3 days like
  referral).
- Add/extend a reaper to delete expired bootstrap subs + their x-ui clients
  (mirror whatever reaps expired subs today).
- **Reuse over re-issue:** if this install_id already has a live, non-expired
  bootstrap key, return the existing link instead of minting a new one (cheaper,
  and naturally cooperates with the rate limit).

Open choice for the implementing agent:
- **(Recommended) Per-install short-TTL key** — revocable, attributable,
  bandwidth-limitable per device; matches `/invite`. Needs the reaper above.
- **Shared rotating key** — one pre-provisioned key per Free server, endpoint
  just rate-limits and returns it. Simpler, but a single key extracted from app
  traffic is trivially shared/abused and a single block point. Only pick this if
  per-install key churn is a real operational problem.

---

## 5. Reachability (critical)

This endpoint is fetched over the user's **raw, possibly-censored network before
any tunnel exists** (chicken-and-egg). If the censor blocks it, the whole
feature is dead. So:
- Host it on a **hard-to-block** address (resilient IP / not a burned domain;
  consider domain-fronting or the same resilient ingress used for `/sub`).
- The **Free server** it hands out (§2) must likewise be reachable + DPI-
  resistant in censored networks (vless+REALITY / clavis transport), otherwise
  the user connects but still can't reach Telegram.

---

## 6. Test checklist
- `GET /app/free-vpn/<valid-id>` first call → `200 {"link":"vless://..."}`, link
  points to a Free-group server and actually connects.
- Immediate second call (same id) → `429 {"error":"rate_limited"}` (or the live
  link reused, per §4) — verify the cooldown.
- Bad id (`/app/free-vpn/!!!`) → 400.
- Issued sub/key has the short TTL and is reaped after expiry.
- Response is JSON with a non-empty `link`; never 200-with-empty.
- End-to-end: point a debug build's `_kBootstrapConfigBase` at staging, run the
  wizard "Нет → подключить бесплатный VPN", confirm it connects, opens Telegram,
  and auto-disconnects at 5 min.

---

## 7. App-side change (owned by the app team — for coordination only)
Set `kBootstrapConfigBase` in
`flutter_app/lib/src/login/telegram_bootstrap.dart` (currently `''`) to
`https://<host>/app/free-vpn` and rebuild. Until then the button shows
"Бесплатный VPN для входа сейчас недоступен". Deploy this endpoint **before**
shipping an app build with the base URL set.
