"""One process-local FIFO admission queue, shared by every production feature.

Durable job IDs reserve their turn when enqueued, rather than when a worker
eventually reaches a GPU call. Existing manifests remain the recovery source.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Any, ParamSpec, TypeVar
from weakref import WeakKeyDictionary

P = ParamSpec("P")
T = TypeVar("T")


def recovery_order(created_at: str | None, identifier: str) -> tuple[float, str]:
    """Old manifests without a usable timestamp recover first, deterministically."""
    try:
        instant = datetime.fromisoformat(created_at or "")
        return instant.replace(tzinfo=instant.tzinfo or timezone.utc).timestamp(), identifier
    except (ValueError, TypeError, OverflowError):
        return 0, identifier


@dataclass(eq=False)
class Ticket:
    kind: str
    identifier: str
    order: tuple[float, str]
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    released: bool = False
    running: bool = False


current: ContextVar[Ticket | None] = ContextVar("production_job", default=None)
_schedulers: WeakKeyDictionary = WeakKeyDictionary()


class Scheduler:
    def __init__(self) -> None:
        self.waiting: deque[Ticket] = deque()

    def reserve(self, kind: str, identifier: str, created_at: str | None = None) -> Ticket:
        ticket = Ticket(kind, identifier, recovery_order(created_at, kind + ":" + identifier))
        self.waiting.append(ticket)
        if len(self.waiting) == 1:
            ticket.ready.set()
        return ticket

    def release(self, ticket: Ticket) -> None:
        if ticket.released:
            return
        ticket.released = True
        ticket.running = False
        self.waiting.remove(ticket)
        ticket.ready.set()
        if self.waiting:
            self.waiting[0].ready.set()

    def sort_recovery(self) -> None:
        """Called during startup, before workers can enter any reserved turn."""
        if any(ticket.running for ticket in self.waiting):
            raise RuntimeError("Cannot reorder production work after dispatch")
        self.waiting = deque(sorted(self.waiting, key=lambda ticket: ticket.order))
        for ticket in self.waiting:
            ticket.ready.clear()
        if self.waiting:
            self.waiting[0].ready.set()

    @asynccontextmanager
    async def turn(self, ticket: Ticket) -> AsyncIterator[bool]:
        context = None
        try:
            await ticket.ready.wait()
            if ticket.released:
                yield False
                return
            ticket.running = True
            context = current.set(ticket)
            yield True
        finally:
            if context is not None:
                current.reset(context)
            self.release(ticket)

    def snapshot(self) -> dict[str, Any]:
        active = None
        waiting = []
        for ticket in self.waiting:
            item = {"kind": ticket.kind, "id": ticket.identifier}
            if ticket.running:
                active = item
            else:
                waiting.append({**item, "position": len(waiting) + 1})
        return {"concurrency": 1, "active": active, "waiting": waiting}


def scheduler() -> Scheduler:
    loop = asyncio.get_running_loop()
    if loop not in _schedulers:
        _schedulers[loop] = Scheduler()
    return _schedulers[loop]


@asynccontextmanager
async def turn(kind: str, identifier: str = "request") -> AsyncIterator[None]:
    """Nested model operations keep their parent job's turn, avoiding deadlocks."""
    parent = current.get()
    if parent is not None and parent.running and not parent.released:
        yield
        return
    shared = scheduler()
    ticket = shared.reserve(kind, identifier)
    async with shared.turn(ticket):
        yield


def job(kind: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            async with turn(kind):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


class JobQueue(asyncio.Queue[str]):
    """Keep per-feature consumers while reserving turns in the shared queue."""
    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.shared = scheduler()
        self.tickets: dict[str, Ticket] = {}

    def put_nowait(self, identifier: str) -> None:
        self.enqueue(identifier)

    def enqueue(self, identifier: str, created_at: str | None = None) -> None:
        if identifier in self.tickets:
            return
        self.tickets[identifier] = self.shared.reserve(self.kind, identifier, created_at)
        super().put_nowait(identifier)

    async def run(self, identifier: str, function: Callable[[str], Awaitable[Any]]) -> None:
        ticket = self.tickets[identifier]
        try:
            async with self.shared.turn(ticket) as admitted:
                if admitted:
                    await function(identifier)
        finally:
            self.tickets.pop(identifier, None)

    def discard(self, identifier: str) -> None:
        ticket = self.tickets.get(identifier)
        if ticket is not None:
            self.shared.release(ticket)

    def close(self) -> None:
        for ticket in self.tickets.values():
            self.shared.release(ticket)
        self.tickets.clear()


async def run(queue: asyncio.Queue, identifier: str, function: Callable[[str], Awaitable[Any]]) -> None:
    if isinstance(queue, JobQueue):
        await queue.run(identifier, function)
    else:
        # Supports isolated consumers supplied by integrations and unit tests.
        async with turn("production", identifier):
            await function(identifier)


def discard(queue: asyncio.Queue | None, identifier: str) -> None:
    if isinstance(queue, JobQueue):
        queue.discard(identifier)


def close(queue: asyncio.Queue | None) -> None:
    if isinstance(queue, JobQueue):
        queue.close()
