# Phase D — peer-verified poke_engine PvP resolution

Status: implemented except the flag flip. The four
determinism items (D1-D4), the server protocol, the client's resolution
poller and its UI states have landed; what remains is turning
`PVP_HUMAN_ENABLED` on, under the condition at the end of this document.

Phase 2 shipped async PvP on a **placeholder** resolver: the Go server
derives damage in `[12, 20]` from `(matchID, round, move)` (`pvpDamage` in
`internal/server/balance.go`). It is deterministic and server-authoritative,
but it is not Pokemon — no types, no abilities, no status, no items.

Human-vs-human PvP is therefore gated off (`PVP_HUMAN_ENABLED`, default
false; surfaced to clients as `pvp.human_enabled`). Bot practice battles stay
available because a bot's "opponent" is the server itself, so there is
nothing to verify.

Phase D replaces the placeholder with real poke_engine resolution **without
porting the engine to Go**: both clients simulate the same round from the
same inputs and report a hash of the result; the server compares hashes and
only accepts a round both sides agree on. The opponent is the validator.

Flipping `PVP_HUMAN_ENABLED` to true is the last step of this phase, not the
first.

## Why not the obvious alternatives

- **Port poke_engine to Go.** Thousands of lines of move/ability/item
  special-casing, and every upstream engine change becomes a second port.
  The two implementations would drift and the drift would be invisible
  until it decided a match.
- **Trust one client's reported result.** A single reporter can claim any
  outcome. Free win for anyone who edits one JSON file.
- **Run poke_engine server-side in Python.** Possible, but it makes the Go
  service depend on a Python runtime, the addon's bundled engine version,
  and the player's own team data — and it still has to serialize the same
  state this design serializes anyway. Revisit only if peer verification
  proves too lossy in practice.

## The determinism problem (read this first)

Peer verification only works if identical inputs produce identical outputs
on two different machines. Today they do not, for four reasons. Each is a
work item.

### D1. The engine has no seed parameter

`simulate_battle_with_poke_engine`
(`src/Ankimon/poke_engine/ankimon_hooks_to_poke_engine.py`) picks one
outcome out of the weighted set with the **global** `random` module:

```python
weights = [outcome.percentage for outcome in transpose_instructions]
chosen_outcome = random.choices(transpose_instructions, weights=weights, k=1)[0]
```

Two consequences:

1. There is no way to ask for a specific round's roll.
2. Seeding it the obvious way (`random.seed(n)` before the call) reseeds the
   *process* generator, which the wild-battle loop, encounter rolls, IV
   generation and `raid_rewards._pick_moves` all share. A PvP round would
   silently make the next wild encounter deterministic.

**Work item:** add an optional `rng: random.Random | None = None` parameter
threaded to every draw inside the call (`random.choices` here, plus any
`random.*` reachable from `get_all_state_instructions`), defaulting to the
module-global generator so the wild-battle path is byte-for-byte unchanged.
PvP passes `random.Random(seed)`. Do **not** use `random.seed()`.

Auditing "any `random.*` reachable from the engine" is the real cost of this
item, and it must be finished before anything downstream is meaningful.

### D2. The outcome set must have a stable order

`random.choices` indexes into `transpose_instructions`. Same seed + same
weights + *different list order* = different outcome. The order comes out of
poke_engine's own instruction generation. Phase D must either confirm that
order is deterministic across runs and platforms, or sort the outcomes
canonically before drawing (e.g. by the serialized instruction list).

Sorting is the safer choice even if the order looks stable today: it costs
one sort per round and removes a whole class of "works on my machine".

**Measured:** on CPython 3.12 the generation order *is* stable — the same
turn produces the same 6 outcomes in the same order across `PYTHONHASHSEED`
values. Sorting is therefore insurance against a future engine change or a
different interpreter, not a fix for an observed reordering. Done in
`canonical_outcome_key`; the test reverses the engine's output to simulate
the reordering, since nothing reproduces it naturally.

### D3. Engine and data versions must match

Two clients on different addon versions can hold different move data, base
stats or engine logic. That is a legitimate mismatch, not cheating, and must
not be punished as cheating.

Both clients report an `engine_version` string — the addon version plus a
hash of the bundled poke_engine data directory. The server:

- rejects a *match creation* between mismatched versions with a clear
  "update Ankimon to battle this player" message, and
- treats a mid-match version change as a **suspended** match, not a loss.

### D4. Float and dict instability in the hash

The hash must not be taken over a Python `repr` of the state. Serialize
canonically: sorted keys, integers where the engine uses integers, floats
rounded to a fixed decimal count (the engine's damage math is integer at the
end — prefer to hash only post-rounding fields), and no timestamps, no
usernames, no locale-dependent strings.

## Protocol

### Round lifecycle

The existing commit-then-reveal flow is unchanged up to the point where both
moves are in. What changes is what happens next.

```
1. Both players POST /v1/matches/{id}/turns  {move}      (unchanged)
   - server holds moves unrevealed, spends one turn token each
2. Both moves present -> server opens a resolution round:
   - generates seed (crypto random, 64-bit, per round)
   - sets match.resolution = {round, seed, revealed moves, deadline}
   - does NOT compute damage
3. Each client polls state, sees the open resolution, simulates locally:
   simulate_battle_with_poke_engine(..., rng=random.Random(seed))
4. Each client POSTs /v1/matches/{id}/rounds/{round}/result
   {state_hash, hp_after: {a, b}, engine_version, log_digest}
5. Server compares the two submissions:
   - hashes equal        -> apply hp_after, advance round, clear resolution
   - hashes differ       -> replay (see below)
   - one side times out  -> timeout path (see below)
```

The server never computes Pokemon damage. It stores HP, compares hashes, and
enforces caps — the same role it plays for raids.

### New/changed endpoints

- `POST /v1/matches/{id}/rounds/{round}/result` — submit this client's
  simulation result. Idempotent per (match, round, user): a retried submit
  with an identical body is accepted and changes nothing; a *differing*
  body for a round the user already reported is rejected `409` and flags the
  match (a client should never report two different outcomes for one seed).
- `POST /v1/matches` — extended with `engine_version`; mismatch is rejected
  `409` with the update message.
- `GET /v1/state` — the `pvp` block gains, per active match:
  `resolution: {round, seed, moves: {username: move}, deadline, submitted:
  [username], attempt}` when a round is open.

Team state is **not** re-uploaded per round. Both teams are serialized once
at match creation (`POST /v1/matches` gains `team`) and stored on the match;
the round inputs are then exactly: both serialized teams, the accumulated
state, both revealed moves, the seed. Serializing per round would be both
wasteful and an opening for mid-match team edits.

### Mismatch handling

A hash mismatch is not automatically cheating — D1 through D4 are all
honest ways to disagree. So:

1. **Attempt 1 mismatch:** the round is replayed once with a *fresh* seed
   (`attempt: 2`). A transient local divergence usually will not repeat.
2. **Attempt 2 mismatch:** the round is voided and the match moves to
   `suspended`. No winner, no rating change, both players keep their turn
   tokens back for that round. The server records both submissions
   (hashes, hp_after, engine_version) for diagnosis.
3. **Repeated suspensions from the same account** across distinct
   opponents are the actual cheating signal — one player is the common
   factor. That is a review queue, not an automatic ban.

Never resolve a mismatch by picking one side's result. There is no
principled way to choose, and whichever rule is picked becomes the exploit.

### Timeout path

The existing 24–48 h turn timer covers *committing a move*. Phase D adds a
second, much shorter deadline for *reporting a result* (minutes, since the
simulation is instant once the client sees the open resolution — the delay
is only the poll interval).

If one side never reports:

- The round does **not** resolve on the single report. Accepting it would
  hand a free win to anyone who force-quits at the right moment.
- After the deadline the match moves to `stalled`. The reporting player may
  claim the match after a longer grace window (the same forfeit rule Phase 2
  already applies to repeated move timeouts), which ends the match on
  forfeit — not on the unverified round's damage.

This deliberately makes a disconnect cost the *absent* player and gain the
present one nothing but time.

## Anti-cheat boundary

Peer verification catches a lying client only when the other client is
honest. It does **not** cover:

- **Collusion.** Two accounts agreeing to report the same false result. The
  hash matches, the server accepts. Mitigated only at the rating layer
  (rating gain between two accounts that only ever play each other is the
  detectable pattern), not here.
- **Team fabrication.** A client can submit a serialized team it never
  earned. Phase D verifies *resolution*, not *provenance*. If ranked play
  needs provenance, teams have to be server-side, which is a separate and
  much larger change. Ranked ladders should stay off until then; unranked
  and friend matches are the honest scope of this phase.

State both limits in the UI rather than implying PvP is fully verified.

## Client work items

1. ~~Thread `rng` through `simulate_battle_with_poke_engine` (D1)~~ — done.
   The audit that item braced for came back small: the only other randomness
   in `poke_engine/` is `teams/load_team.py`, which the battle path never
   reaches, so the engine core needed no change.
2. ~~Canonical outcome ordering (D2)~~ — done, as insurance; see the
   measurement above.
3. ~~Canonical team/state serializer + `state_hash` (D4)~~ — done. Zero
   side conditions are dropped: the engine's `defaultdict(int)` grows keys
   just by being read, so one client can end a turn holding `'tailwind': 0`
   where the other holds nothing, with no difference in play.
4. ~~`engine_version` computation (D3)~~ — done. Only `moves.json` and
   `pokedex.json` are hashed, and line endings are normalized, so a CRLF
   checkout does not refuse matches against an LF one.
5. ~~Resolution poller in `MultiplayerController`~~ — done. Work is keyed
   by `(match, round, attempt)`, so a replay is new work and a slow poll
   cannot start the same round twice, and the resolver is imported lazily
   because it pulls in the whole move dataset.

   Two things the implementation had to settle that this design did not:

   - **Which side is which.** Both clients put the *challenger* on the
     state's `user` side. A client that put itself there would build the
     mirror image of its opponent's battle, and every honest round would
     disagree. The server tells each client which it is
     (`you_are_challenger`).
   - **Where the between-round state lives.** Originally the client, since
     the server stored only HP and hashes — which meant a lost cache (fresh
     profile, reinstall) cost the match to a suspension through no fault of
     the player's. Now both clients report the end-of-round state and the
     server keeps it **only when the two are identical**, handing it back
     with the next round. That costs no trust: it is exactly as verified as
     the HP beside it, and the server never reads it. Agreeing on a round's
     damage but not on the state it leaves behind is treated as a mismatch -
     the same disagreement, one round early.
6. ~~UI states~~ — done, in `multiplayer/pvp_status.py` (plain functions
   over the state dict, so the wording is testable without Qt) plus
   reviewer toasts, since a player answering cards will not have the
   window open. A suspension always carries its reason and says no one
   won; a stalled battle counts down to its claim and only offers the
   button once the grace window has actually passed.

## Server work items

All six are done (ankimon-multiplayer-api, `feat/pvp-peer-resolution`).

1. ~~`Match.Resolution` and its persistence.~~
2. ~~Seed generation (`crypto/rand`).~~
3. ~~The result endpoint: idempotency, comparison, replay, void, suspend.~~
4. ~~`engine_version` on match creation; the mismatch rejection message.~~
   Also checked when the challenge is *answered*, since the opponent's
   version is not known until then.
5. ~~Result-report deadline sweeping.~~ A stalled match is claimable on
   forfeit only after a 24 h grace window, and only by the player who did
   report.
6. ~~Keep `pvpDamage` for bot matches.~~

One rule the design left implicit and the implementation had to make
explicit: a matching `state_hash` with *differing* `hp_after` is still a
mismatch. Trusting either number would be picking a side.

## Test plan

- Two in-process simulated clients, same engine, honest: rounds resolve,
  HP matches, match completes.
- Same, one client reporting a tampered `hp_after`: attempt 1 mismatch,
  replay, attempt 2 mismatch, match suspended, no winner, tokens refunded.
- One client never reports: match stalls, no damage applied, forfeit path
  after the grace window.
- Mismatched `engine_version`: match creation rejected with the update
  message; a mid-match change suspends rather than losing.
- Seed determinism: the same (teams, state, moves, seed) simulated twice in
  one process, and once after a wild battle has consumed the global RNG,
  produces the same `state_hash`. This is the test that proves D1 landed.
- Wild-battle regression: a fixed-seed wild battle produces identical output
  before and after the `rng` parameter is added.

## What is left

Flipping `PVP_HUMAN_ENABLED`, under the condition below. Every code item on
both sides is done; what is missing is evidence, not code — two real clients
on two real machines agreeing on a round.

`tools/pvp_determinism_probe.py` is how that evidence is collected: both
machines run it and compare one fingerprint. A mismatch is classified rather
than merely reported - an `engine_version` difference is a D3 skew, while the
same version producing a different result is D4 and names the round that
differs.

What has been verified so far, and what it does not cover: the protocol runs
end to end against the real server binary driven by the addon's own resolver
(29 checks - skew refusal, role assignment, both clients agreeing, one report
not resolving, idempotent retry, mismatch to replay to suspension with the
token refunded, and a cache-less client continuing from the server's carried
state). All of it on one machine, which is exactly the D3/D4 blind spot the
probe exists to cover.

## Definition of done

`PVP_HUMAN_ENABLED` defaults to true only when every item above is done and
the seed-determinism test passes across two different machines — not two
runs on one machine, which shares a Python build and a data directory and so
cannot catch D3 or D4.
