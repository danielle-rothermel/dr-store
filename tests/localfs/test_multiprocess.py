from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import TYPE_CHECKING

from dr_store.localfs import FileLock

if TYPE_CHECKING:
    from multiprocessing.queues import Queue
    from multiprocessing.synchronize import Event

WATCHDOG_SECONDS = 15


def _exclusive_holder(
    lock_path: str,
    held: Event,
    peer_trying: Event,
    events: Queue[str],
) -> None:
    with FileLock(Path(lock_path)):
        events.put("holder-enter")
        held.set()
        peer_trying.wait()
        events.put("holder-exit")


def _exclusive_waiter(
    lock_path: str,
    held: Event,
    peer_trying: Event,
    events: Queue[str],
) -> None:
    held.wait()
    peer_trying.set()
    with FileLock(Path(lock_path)):
        events.put("waiter-enter")
        events.put("waiter-exit")


def _shared_reader(
    lock_path: str,
    inside: Event,
    peer_inside: Event,
    events: Queue[str],
    identity: str,
) -> None:
    with FileLock(Path(lock_path), shared=True):
        events.put(f"{identity}-inside")
        inside.set()
        peer_inside.wait()
        events.put(f"{identity}-leaving")


def _join(process: multiprocessing.process.BaseProcess) -> None:
    process.join(WATCHDOG_SECONDS)
    assert not process.is_alive()
    assert process.exitcode == 0
    process.close()


def test_exclusive_locks_exclude_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "cross.lock"
    context = multiprocessing.get_context("spawn")
    held = context.Event()
    peer_trying = context.Event()
    events: multiprocessing.Queue[str] = context.Queue()
    holder = context.Process(
        target=_exclusive_holder,
        args=(str(lock_path), held, peer_trying, events),
    )
    waiter = context.Process(
        target=_exclusive_waiter,
        args=(str(lock_path), held, peer_trying, events),
    )
    holder.start()
    waiter.start()
    _join(holder)
    _join(waiter)
    observed = [events.get(timeout=WATCHDOG_SECONDS) for _ in range(4)]
    events.close()
    events.join_thread()
    assert observed == [
        "holder-enter",
        "holder-exit",
        "waiter-enter",
        "waiter-exit",
    ]


def test_shared_locks_overlap_across_processes(tmp_path: Path) -> None:
    lock_path = tmp_path / "shared.lock"
    context = multiprocessing.get_context("spawn")
    first_inside = context.Event()
    second_inside = context.Event()
    events: multiprocessing.Queue[str] = context.Queue()
    first = context.Process(
        target=_shared_reader,
        args=(
            str(lock_path),
            first_inside,
            second_inside,
            events,
            "first",
        ),
    )
    second = context.Process(
        target=_shared_reader,
        args=(
            str(lock_path),
            second_inside,
            first_inside,
            events,
            "second",
        ),
    )
    first.start()
    second.start()
    _join(first)
    _join(second)
    observed = {events.get(timeout=WATCHDOG_SECONDS) for _ in range(4)}
    events.close()
    events.join_thread()
    assert observed == {
        "first-inside",
        "second-inside",
        "first-leaving",
        "second-leaving",
    }
