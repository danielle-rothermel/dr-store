# dr-store

[![CI](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml/badge.svg)](https://github.com/danielle-rothermel/dr-store/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dr-store.svg)](https://pypi.org/project/dr-store/)

| [Repo Definitions](https://danielle-rothermel.github.io/dr-store/) | [dr-serialize v0.1.1](https://github.com/danielle-rothermel/dr-serialize) |
| --- | --- |

**dr-store provides domain-neutral storage primitives for immutable records and durable document artifacts.**
It is organized into these functional areas:

- **Object references and content hashing** identify complete records by their
  declared schemas and the SHA-256 digests of their canonical JSON
  representations.
- **Object storage** provides immutable puts, verified reads, and atomic
  caller-owned key bindings with idempotent replay and typed conflicts.
- **Storage backends** supply interchangeable atomic persistence operations,
  with in-memory and SQLite implementations included.
- **Document directories** manage atomically published canonical-JSON manifests
  alongside streamed binary sidecars with bounded retention and read-back
  verification.
- **Typed failures** distinguish invalid references, missing or corrupted
  content, conflicting writes, publication failures, and sidecar verification
  failures.
