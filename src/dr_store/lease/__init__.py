"""Keyed lease acquire, renew, and terminal publication.

``LeaseAuthority`` acquires and terminalizes leases for exactly-once side
effects. Memory backends accept an injected clock; SQLite and PostgreSQL read
authority time from the database. PostgreSQL opens a fresh raw psycopg
connection per authority transaction and never enlists in caller-owned evidence
transactions — independent commit is the exactly-once mechanism.

``LeaseAuthority.maintain(lease, lease_duration=...)`` returns a
``LeaseMaintenance`` context manager that renews at ``duration / 3`` on a
background thread. Terminalize through ``maintenance.succeed(...)`` /
``maintenance.fail(...)``, which stop the renewer before publishing; a clean
context exit requires prior terminal publication. ``LeaseAuthority.succeed`` /
``fail`` remain the direct path for callers not using maintenance. If terminal
publication fails with a transient authority error, the renewer may restart so
the caller can retry while the context remains open.

A ``LeaseMaintenance`` handle is **single-threaded**: ``__enter__``,
``succeed``, ``fail``, ``check``, and ``__exit__`` must all run on one
thread; only the internal renewal thread runs concurrently. Behavior under
multi-threaded handle use is undefined.
"""

from __future__ import annotations

from dr_store.lease.authority import (
    LeaseAuthority,
    LeaseMaintenance,
)
from dr_store.lease.models import (
    AcquireOutcome,
    AcquireResult,
    Lease,
    LeaseAuthorityError,
    LeaseAuthoritySchemaMismatchError,
    LeaseRequest,
    ReplayPolicy,
    StaleLeaseError,
    Terminal,
    TerminalConflictError,
    TerminalFailure,
    TerminalOutcome,
)

__all__ = [
    "AcquireOutcome",
    "AcquireResult",
    "Lease",
    "LeaseAuthority",
    "LeaseAuthorityError",
    "LeaseAuthoritySchemaMismatchError",
    "LeaseMaintenance",
    "LeaseRequest",
    "ReplayPolicy",
    "StaleLeaseError",
    "Terminal",
    "TerminalConflictError",
    "TerminalFailure",
    "TerminalOutcome",
]
