# Multiplayer via a Go API — Integration Design

Goal: add multiplayer to Ankimon backed by a Go API, without ever making the
review loop wait on the network.

## Where the player flow lives today

- The whole game loop runs **synchronously on the Qt main thread** inside
  `gui_hooks.reviewer_did_answer_card` → `on_review_card`
  (`src/Ankimon/__init__.py:765`). A battle turn only resolves every
  `_get_cards_per_round()` answered cards; the turn is simulated locally by
  `simulate_battle_with_poke_engine` (`src/Ankimon/__init__.py:597`,
  poke_engine is a bundled Python engine).
- Feedback is non-blocking by design: `tooltipWithColour` toasts and the
  reviewer-iframe life bar (`reviewer_obj.update_life_bar`). No dialogs appear
  mid-review except the optional move chooser.
- Existing HTTP is synchronous `requests` on the main thread — e.g.
  `sync_data_to_leaderboard` (`src/Ankimon/pyobj/ankimon_leaderboard.py:98`)
  has **no timeout** and can freeze the UI. The one correct async pattern in
  the codebase is `QueryOp(...).run_in_background()`
  (`src/Ankimon/__init__.py:327`); Anki also provides `mw.taskman`.
- Auth precedent exists: username + `api_key` stored at
  `user_path_credentials`, already used against
  `leaderboard-api.ankimon.com`.

## Design principles

1. **A card answer never waits on the network.** All multiplayer I/O happens
   off the main thread; the hook only appends an event to an in-memory queue
   (microseconds).
2. **The local battle stays local.** The poke_engine simulation cannot be
   re-run in Go cheaply; the server is an authoritative *ledger of outcomes*
   (damage contributed, turns taken), not a battle simulator.
3. **Asynchronous multiplayer modes only (at first).** Review pacing is
   unpredictable (seconds to minutes per card), so real-time synchronous
   duels fight the medium. Raid bosses and async PvP fit it naturally.
4. **Offline is a first-class state.** Events queue and flush later; failures
   are logged (`mw.logger`), never surfaced as dialogs mid-review.

## Client architecture (addon side)

New module `src/Ankimon/net/api_client.py`:

- One `requests.Session` (keep-alive/HTTP2 via connection reuse), base URL
  from settings, `(connect=2s, read=5s)` timeouts on every call.
- All calls dispatched with `mw.taskman.run_in_background(...)`; UI updates
  marshalled back with `mw.taskman.run_on_main(...)` (or `QueryOp` for
  one-shots like fetching a lobby list).
- Auth header from the existing credentials file; on 401, mark multiplayer
  inactive and show one tooltip — never a modal.

New module `src/Ankimon/net/outbox.py` (event queue):

- `on_review_card` calls `outbox.push({type, ts, payload})` — that is the
  *only* multiplayer code in the hook path.
- A background flusher (QTimer on main thread that *dispatches* a background
  task, or a worker thread) batches events every ~15 s or 20 events into one
  `POST /v1/events:batch` call with an `Idempotency-Key` header, retrying
  with exponential backoff.
- Queue persists to a JSON/SQLite file in the user files dir so a crash or
  offline session syncs next time; flush also on `profile_will_close` and
  `sync_did_finish` (hooks already wired in `hooks.py` / `__init__.py:936`).

Server → client updates: piggyback on the review cadence. Each batch flush
response carries the current shared state (e.g. raid boss HP, opponent turn
ready). That gives near-real-time feel with **zero extra requests**. A
WebSocket/SSE channel is a later optimization, not a v1 requirement
(PyQt6.QtWebSockets is available in Anki's bundled Qt if needed).

## Multiplayer modes, ranked by fit

1. **Co-op raid boss (build first).** Server owns boss HP; clients batch-post
   damage contributions; the flush response returns boss HP + top
   contributors. UI: a raid health bar in the existing reviewer iframe and
   tooltips ("Your guild dealt 1,240 dmg — boss at 62%"). No turn coupling
   between players, so nothing can ever block.
2. **Async PvP.** Matchmaking pairs two players; each player's *reviews* fill
   an energy/turn meter; turns are summaries (move id, damage rolled locally,
   validated server-side against level/stat caps), applied to a
   server-authoritative HP ledger. Opponent turns arrive via the flush
   response and render as tooltips + life-bar updates between cards.
3. **Real-time duels — defer.** Only worth it with both players in an active
   session; requires WebSockets, presence, and turn timers. Revisit after 1–2.

## Go API sketch

- Stack: `net/http` + chi (or gin), Postgres (SQLite fine for beta), JSON,
  versioned under `/v1`. Stateless handlers; shared state in the DB.
- Auth: reuse the leaderboard credential model (username + API key →
  `Authorization: Bearer`), same key provisioning flow the leaderboard uses.
- Core endpoints:
  - `POST /v1/events:batch` — idempotent batch ingest; response embeds the
    caller's active multiplayer state (raid/boss snapshot, pending PvP turns).
  - `POST /v1/raids` / `POST /v1/raids/{id}/join` / `GET /v1/raids/{id}`
  - `POST /v1/matches` (matchmaking), `POST /v1/matches/{id}/turns`,
    `GET /v1/matches/{id}`
- Anti-cheat = server-side sanity caps, not simulation: max reviews/minute,
  damage bounded by reported level/stats, monotonic timestamps, idempotency
  keys to make client retries safe.

## Rollout plan

1. **Phase 0 — network layer:** add `api_client` + `outbox`; port the
   leaderboard `sync_data_to_leaderboard` onto it (fixes the existing
   main-thread, no-timeout POST as a side benefit and proves the pattern).
2. **Phase 1 — raid boss:** Go service with events:batch + raids; raid bar in
   the reviewer iframe; lobby UI as a normal menu window via
   `create_menu_actions` (multiplayer UI lives outside the review loop).
3. **Phase 2 — async PvP** on the same event/turn ledger.
4. **Phase 3 (optional) — realtime/presence** over WebSocket once the async
   modes prove retention.
