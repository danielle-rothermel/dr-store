# dr-store

Generic append-only content-addressed object store for the dr-* stack.

`dr-store` owns three things and nothing else:

1. **Immutable put** — an absent typed `(schema, content_hash)` key
   atomically accepts a verified complete canonical record value; replay of
   the same canonical value is idempotent success; different content at the
   same key is a typed conflict and never overwrites the stored value.
2. **Verified get** — every read recomputes and verifies the Content Hash
   and schema declared by the `ObjectReference`; missing, schema-mismatched,
   or corrupted content fails with a typed error.
3. **Atomic key-to-reference binding** — one generic compare-and-set that
   binds an opaque caller-owned key to an `ObjectReference`: an unbound key
   binds; the same reference replays idempotently; a different reference
   conflicts and never overwrites the winner. No overwrite path is exposed.

The Content Hash is the full 64-character lowercase SHA-256 digest of the
complete canonical persisted record, canonicalized through
[`dr-serialize`](https://github.com/danielle-rothermel/dr-serialize)'s
canonical JSON. `dr-store` does not invent a second canonicalization
dialect, and a Content Hash is not an Identity Hash.

## Ecosystem

`dr-store` depends only on `dr-serialize` for canonical JSON and strict
finite-JSON validation. It carries no Whetstone, Rollout, workflow, retry,
or campaign vocabulary; the public contract is domain-neutral.

## Backends

- **in-memory** (`MemoryBackend`) — for tests and single-process use.
- **sqlite** (`SqliteBackend`) — durable and safe under concurrent
  cross-process use via serialized transactions.

Both satisfy the same backend-neutral contract, exercised by a shared
concurrency test proving parallel binds of one unbound key produce exactly
one winner.
