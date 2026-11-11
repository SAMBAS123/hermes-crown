# Design decisions

1. **A single local authority before distributed consensus.** Three cooperating
   Linux processes share a SQLite file. BEGIN IMMEDIATE serializes acquisition
   and publishing. There is no polling-based “check then send” gap.
2. **Lease plus fencing.** A unique process-incarnation ID and increasing epoch
   identify the lease. Every write to the outlet validates both plus expiry.
   A process can continue to believe it is leader after suspension; the outlet
   still refuses it. Safety does not depend on that process noticing takeover.
3. **The outlet is in the transaction.** We do not grant permission, release the
   transaction and send to Telegram. Such a design could send after takeover or
   duplicate a message after a crash. An external outlet will need its own
   idempotency/fencing contract and crash recovery before it can be promised.
4. **Explicit enrollment.** /crown here starts a participant, /crown leave stops
   it. Enrolled runtimes heartbeat while alive. Attention prediction, focus
   scoring, priority elections and user-device presence are outside v0.1.
5. **Typed durable facts.** Questions have stable IDs; human answers reference
   exact questions; source instance/task/conversation are retained. Context
   injection reads this record but cannot invent or overwrite human answers.
6. **No summaries in the safety path.** Text formatting and LLM reasoning do not
   decide who may publish. The deterministic demo has zero inference calls.
7. **No automatic re-asking.** An unanswered published question remains pending
   through takeover. A new speaker does not republish it under another ID.
8. **Failure visibility.** Context recall may fail quietly without blocking a
   normal Hermes conversation; Crown publication never fails open. Worker
   errors are inspectable via /crown status. Native Hermes output is outside the
   Crown guarantee and must be disclosed as such in the demo.

## Remaining integration risks

- Actual Hermes lifecycle, profile scoping, command dispatch and model behavior
  still need testing in a full installed runtime with configured models.
- Several profiles can share one gateway process. Killing that process removes
  all its participants; real failover needs another independent runtime alive.
- A normal final response from an unenrolled/native Hermes session is not
  captured by this plugin. The first demo must use the Crown outlet explicitly.
- A process with direct write access to the database can bypass the plugin.
  All participants are within one user's trusted local boundary.
- SQLite polling and full record exports are suitable for the small proof, not
  yet a claim of long-running fleet scale. No notification batching is included.
