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
4. **Plain request/response HTTP only — no WebSockets, no held
   connections.** Every exchange with the server is a short-lived HTTP
   call; "push" is emulated by piggybacking state on responses and by
   adaptive polling.
5. **Offline is a first-class state.** Events queue and flush later; failures
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

Server → client updates — **polling only, no persistent connections**:

- Primary channel: piggyback on the review cadence. Each batch flush
  response carries the current shared state (e.g. raid boss HP, opponent
  turn ready). Near-real-time feel with **zero extra requests** while the
  player is actually reviewing.
- Secondary channel: adaptive background polling of `GET /v1/state` for the
  idle case (player has Anki open but isn't answering cards) — e.g. every
  30 s while a raid/match is active, backing off to minutes when nothing is
  active, stopped entirely when the player has no live multiplayer session.
- Cheap by construction: `GET /v1/state` supports `If-None-Match`/ETag (or a
  `since` cursor) so idle polls are 304s costing a few hundred bytes.

WebSockets and SSE are **out of scope by decision** — the deployment model
for the Go API won't support held connections, so nothing in the protocol
may assume one. If lower latency is ever needed, the knob is the poll
interval, not a transport change.

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
3. **"Live" duels — reframe, don't build on push.** With both players in an
   active review session, the piggyback channel already delivers opponent
   turns within one card-answer of each other — that's as "live" as the
   medium gets, and it needs no new transport. True lockstep realtime is out
   of scope (see the no-held-connections decision above).

## Go API sketch

- Stack: `net/http` + chi (or gin), Postgres (SQLite fine for beta), JSON,
  versioned under `/v1`. Stateless handlers; shared state in the DB.
- Auth: reuse the leaderboard credential model (username + API key →
  `Authorization: Bearer`), same key provisioning flow the leaderboard uses.
- Core endpoints:
  - `POST /v1/events:batch` — idempotent batch ingest; response embeds the
    caller's active multiplayer state (raid/boss snapshot, pending PvP turns).
  - `GET /v1/state` — the same state snapshot on its own, ETag/cursor-aware,
    for idle polling.
  - `POST /v1/raids` / `POST /v1/raids/{id}/join` / `GET /v1/raids/{id}`
  - `POST /v1/matches` (matchmaking), `POST /v1/matches/{id}/turns`,
    `GET /v1/matches/{id}`
- Anti-cheat = server-side sanity caps, not simulation: max reviews/minute,
  damage bounded by reported level/stats, monotonic timestamps, idempotency
  keys to make client retries safe.

## Balancing

The unit of power in Ankimon is the answered card, and player populations
differ on three axes: review volume per day, review quality (grades), and
Pokémon strength (level/team). Balance means no axis dominates and none can
be gamed.

One hard rule up front: **local battle turns are not a balance input.**
`battle.cards_per_round` is a client setting (`src/Ankimon/__init__.py:984`,
default 2, user-editable), and move choice can be manual
(`controls.allow_to_choose_moves`). All multiplayer math derives from
*answered-card events* and is computed **server-side**.

### Co-op raid boss

- **Contribution formula (server-side), per answered card:**
  `contribution = base × grade_weight × level_factor`
  - `grade_weight`: reuse the established weights from
    `ankimon_tracker.calc_multiply_card_rating` — easy 1.0, good 0.5,
    hard 0.25, again 0. Quality matters, exactly as it already does locally.
  - `level_factor`: sub-linear, e.g. `1 + log2(main_level) / 4` — a level-60
    Pokémon hits ~2.5× a level-1, not 60×. Progression is felt but grinding
    Pokémon levels can't trivialize raids.
- **Soft daily cap with diminishing returns.** Full value up to a personal
  daily target (the player's trailing 7-day median reviews, clamped to
  e.g. 50–300), square-root taper beyond it. This stops one whale from
  soloing the boss, keeps low-volume players relevant, and deliberately
  *doesn't* reward junk-review grinding — incentives stay aligned with
  actually studying.
- **Boss HP is fit to the party, not fixed.**
  `boss_hp = target_days × Σ expected_daily_contribution(participants)`,
  computed from each member's trailing averages at raid start. A 3-person
  casual lobby and a 10-person hardcore lobby both get a ~5-day raid.
  Mid-raid joins either rescale HP proportionally or are locked out —
  pick one and keep it simple (lockout recommended for v1).
- **Rewards by personal participation, not leaderboard rank.** Tiers keyed
  to each player's own expected contribution (e.g. ≥60% of your baseline =
  full reward), prorated by boss % killed if the timer expires (the boss
  "flees" — raids fail soft, never punish). A single cosmetic for top
  contributor is fine; ranked *rewards* are not.

### PvP

- **Decouple power from volume: turn tokens.** Answering X cards charges one
  turn token; a player banks at most K tokens (e.g. 3). A match advances in
  rounds — a round resolves only when *both* players have committed a turn.
  Review volume controls how *fast* you play, never how *much* you hit.
- **Normalize stats in ranked: league format.** Both teams are scaled to a
  reference level (e.g. 50) for ranked queues, so team composition and move
  choice are the skill expression. Offer an unscaled "open" queue for
  players who want their grind to show.
- **Review quality = bounded edge.** Map the existing 0–1 tracker multiplier
  for the cards behind each turn to a small bonus (≤ ±15% damage, or
  crit/priority chance). Good studying gives an edge, never a steamroll.
- **Deterministic peer-verified resolution (no Go port of the engine).**
  The server issues a per-round RNG seed. Both clients run poke_engine with
  identical inputs (both serialized teams, both committed moves, the seed)
  and report the resulting state hash. Hashes match → outcome accepted;
  mismatch → round flagged and replayed. The opponent is the validator; the
  Go server only compares hashes and enforces caps.
- **Match pacing.** Matchmake on two axes: rating (Glicko-2) *and* activity
  cadence (pair similar daily review counts so rounds don't stall for days).
  Turn timer of 24–48 h, then an auto-move (server picks the first usable
  move); forfeit after several consecutive timeouts.
- **Commit-then-reveal move order.** Both players submit moves before either
  sees the other's choice (server holds them until the round closes), so the
  second mover gains nothing.

### Between the two modes

- **One event stream powers both.** Every answered card contributes to the
  active raid *and* charges the PvP turn meter simultaneously — players never
  choose between modes, and the review flow stays the only loop.
- **Parallel, non-stacking rewards.** Raids pay out items/cosmetics; PvP pays
  rating and seasonal cosmetics. Neither grants XP or currency the other
  needs, so neither mode becomes strictly optimal.
- **Shared integrity caps.** Server-side, per account: sustained review rate
  cap (~1 card / 2 s), monotonic timestamps, and grade-distribution sanity
  (100% "easy" at 1 s/card gets flagged) — protecting raid and PvP with the
  same checks.

## Rollout plan

1. **Phase 0 — network layer:** add `api_client` + `outbox`; port the
   leaderboard `sync_data_to_leaderboard` onto it (fixes the existing
   main-thread, no-timeout POST as a side benefit and proves the pattern).
2. **Phase 1 — raid boss:** Go service with events:batch + raids; raid bar in
   the reviewer iframe; lobby UI as a normal menu window via
   `create_menu_actions` (multiplayer UI lives outside the review loop).
3. **Phase 2 — async PvP** on the same event/turn ledger.
4. **Phase 3 (optional) — polish the live feel** within plain HTTP: tune the
   adaptive poll intervals, add the ETag/cursor fast path, and surface
   presence ("opponent is reviewing now") from recent-activity timestamps the
   server already has from batch ingests.
