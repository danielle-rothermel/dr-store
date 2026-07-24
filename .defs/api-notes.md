# dr-store API naming notes

Naming problems surfaced while maintaining `.defs/vocab.html`. **Proposals only — do not implement renames from a doc pass.** Hash values, golden files, and fixtures must never change from this work.

## 1. Cross-repo import mismatch: dr-store imports names dr-serialize no longer exports

**RESOLVED** — dr-store's import sites were migrated to the current dr-serialize names (`sha256_json_digest` → `json_hash`, `validate_finite_json` → `validate_strict_json`) as pure renames, no aliases. The `validate_strict_json` rename was confirmed against dr-serialize history to be a name-only change with unchanged acceptance behavior, so no previously-accepted records are now rejected and content hashes are unchanged (content-hash test passes live). The remaining detail below is retained for context.


- **Current names (in dr-store):**
  - `references.py` imports `sha256_json_digest` and `validate_finite_json` from `dr_serialize`.
  - `store.py` imports `validate_finite_json` from `dr_serialize`.
- **Problem:** The serialization layer currently exports `json_hash` (not `sha256_json_digest`) and `validate_strict_json` (not `validate_finite_json`). These imports do not resolve against the present dr-serialize public surface, so dr-store is pinned to a superseded API. `canonical_json` is the only dr-serialize import whose name still matches.
- **Proposed change:** Update dr-store's import sites to the current serialization-layer names — `sha256_json_digest` → `json_hash`, `validate_finite_json` → `validate_strict_json`. Per the breaking-reset rule, migrate directly; do not add aliases or dual-read/fallback import paths.
  - **Trade-off:** `json_hash` and `validate_strict_json` must have equivalent behavior (same canonical-JSON hashing, same strict/finite validation semantics) for the swap to preserve computed content hashes. Verify equivalence before switching; if `validate_strict_json` is stricter than the old `validate_finite_json`, some previously-accepted records could now be rejected. This is a behavior question, not just a rename, so confirm on the dr-serialize side first.
- **Blast radius:** `src/dr_store/references.py` (import block + call in `compute_content_hash`), `src/dr_store/store.py` (import + call sites). Any test that stubs or patches these dr-serialize names. No `__all__` change on the dr-store side. Content-hash values must be identical after the swap — treat any change to a golden hash as a failure, not an update.

## 2. Jargon term `digest` where the shared vocabulary standardizes on `hash`

**PARTIALLY RESOLVED** — the primary fix (adopting `json_hash` via note 1) is done, so the `digest` name is gone from dr-store's imports. The separate prose/messages-only reword of remaining "digest" wording in `references.py` / `errors.py` docstrings is still open and intentionally not bundled into the API migration.


- **Current names/usages:** the imported `sha256_json_digest`, plus `digest` in docstrings and messages across `references.py` (lines ~28, 39, 42, 50, 66) and `errors.py` (line ~26).
- **Problem:** The shared vocabulary uses `hash`, not `digest`. `digest` is exactly the asymmetric-sibling / jargon case the vocab process flags — the dependency name `sha256_json_digest` sits beside dr-store's own correctly-named `content_hash` surface. dr-store's public API already uses `hash` consistently (`content_hash`, `compute_content_hash`, `is_content_hash`, `CONTENT_HASH_LENGTH`), so the divergence is confined to the dr-serialize call name and to dr-store's internal prose.
- **Proposed change:**
  - Primary fix is upstream (adopting `json_hash`, per note 1), which removes the `digest` name from dr-store's imports.
  - Separately, reword dr-store's own docstrings/messages from "digest" to "hash" (e.g. "64-character lowercase hex hash" rather than "digest"). This is a prose/messages-only change with no API surface impact.
  - **Trade-off:** error-message text changes may be asserted verbatim by tests; check message-matching assertions before editing.
- **Blast radius:** `src/dr_store/references.py`, `src/dr_store/errors.py` (docstrings and any user-facing message strings). Tests asserting on message substrings. No exported-name change.

## 3. Parallel status/outcome naming families (no rename proposed)

- **Current names:** caller-facing `PutStatus` / `BindStatus` (values `STORED`/`BOUND`/`IDEMPOTENT`) vs. backend-level `PutOutcome` / `BindOutcome`.
- **Problem:** Two closely-related naming families for related concepts (public non-conflict status vs. internal backend result). A reader could conflate the caller-facing statuses with the backend outcome objects.
- **Proposed change:** **Do not rename.** The distinction is real and the current names carry it (`Status` = caller-facing enum, `Outcome` = backend result object). Clarify in the Exported Names notes column instead — already done in `vocab.html` (Backend row notes that `PutOutcome`/`BindOutcome` are backend-level results distinct from `PutStatus`/`BindStatus`).
- **Blast radius:** none (documentation-only clarification, already present).

## 4. Methods are not exported names (no rename; placement note)

- **Current names:** `ObjectReference.for_record`, `verify_record`, `ObjectStore.resolve` / `put` / `get` / `bind`.
- **Problem:** These are methods, not entries in `__all__`, so they must not appear in the Exported Names names column. `resolve` returns the bound reference or `None` and is the only read-only convenience over the binding table.
- **Proposed change:** none. Keep them in disambiguating note prose only — already the case in `vocab.html`.
- **Blast radius:** none.

## 5. Well-aligned names (recorded, no action)

- `ObjectReference`, `ObjectStore`, `compute_content_hash`, `is_content_hash`, `CONTENT_HASH_LENGTH` map cleanly to the contract terms Object Reference, Object Store, and Content Hash. No repo-side rename proposed.
- Cross-repo contrast terms (Identity Hash and any rollout/result-store concepts) are intentionally not owned by dr-store and correctly get no term row; the Content-Hash-is-not-an-Identity-Hash distinction lives as a guarantee, not a term.
