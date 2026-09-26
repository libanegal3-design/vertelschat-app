"""Durable database-backed job queue.

- enqueue() participates in the caller's transaction (a job exists only if the business change commits);
- dedupe keys make every side effect idempotent (webhook retries, double clicks, worker restarts);
- claiming uses a conditional UPDATE so several workers can run safely (works on SQLite and PostgreSQL);
- failures retry with capped exponential backoff and end in status 'dead' with an on_dead hook,
  which marks the domain object and notifies a human. Nothing is silently dropped."""
from __future__ import annotations

import logging
import random
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import SessionLocal, utcnow
from .models import Job

log = logging.getLogger("vertelschat.jobs")


class RetryLater(Exception):
    def __init__(self, message: str = "", delay: float | None = None) -> None:
        super().__init__(message)
        self.delay = delay


class PermanentFailure(Exception):
    pass


@dataclass
class _Handler:
    fn: Callable
    max_attempts: int
    on_dead: Callable | None


HANDLERS: dict[str, _Handler] = {}


def job(kind: str, max_attempts: int = 8, on_dead: Callable | None = None):
    def deco(fn):
        HANDLERS[kind] = _Handler(fn, max_attempts, on_dead)
        return fn
    return deco


def enqueue(session: Session, kind: str, payload: dict | None = None, *, dedupe_key: str | None = None,
            delay: float = 0, run_after: datetime | None = None, max_attempts: int | None = None) -> Job:
    if dedupe_key:
        existing = session.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is not None:
            return existing
    handler = HANDLERS.get(kind)
    j = Job(kind=kind, payload=payload or {}, dedupe_key=dedupe_key,
            run_after=run_after or (utcnow() + timedelta(seconds=delay)),
            max_attempts=max_attempts or (handler.max_attempts if handler else 8))
    try:
        with session.begin_nested():
            session.add(j)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is None:
            raise
        return existing
    return j


def backoff_seconds(attempt: int) -> float:
    base = min(6 * 3600, 20 * (2 ** max(0, attempt - 1)))
    return base * random.uniform(0.85, 1.15)


def _claim(session: Session, worker_id: str) -> Job | None:
    now = utcnow()
    ids = session.scalars(select(Job.id).where(Job.status == "queued", Job.run_after <= now)
                          .order_by(Job.run_after, Job.id).limit(10)).all()
    for jid in ids:
        res = session.execute(update(Job).where(Job.id == jid, Job.status == "queued")
                              .values(status="running", locked_at=now, locked_by=worker_id,
                                      attempts=Job.attempts + 1))
        session.commit()
        if res.rowcount == 1:
            return session.get(Job, jid)
    return None


def _finish(job_id: int, **values) -> None:
    with SessionLocal() as s:
        s.execute(update(Job).where(Job.id == job_id).values(**values))
        s.commit()


def run_one(worker_id: str = "inline") -> bool:
    with SessionLocal() as s:
        j = _claim(s, worker_id)
        if j is None:
            return False
        job_id, kind, payload, attempts, max_attempts = j.id, j.kind, dict(j.payload or {}), j.attempts, j.max_attempts
    handler = HANDLERS.get(kind)
    if handler is None:
        _finish(job_id, status="dead", last_error=f"no handler for {kind}", finished_at=utcnow())
        return True
    session = SessionLocal()
    try:
        handler.fn(session, payload)
        session.commit()
        _finish(job_id, status="done", finished_at=utcnow(), locked_at=None)
    except RetryLater as exc:
        session.rollback()
        if attempts >= max_attempts:
            _dead(job_id, kind, payload, f"retries exhausted: {exc}")
        else:
            delay = exc.delay if exc.delay is not None else backoff_seconds(attempts)
            _finish(job_id, status="queued", locked_at=None, last_error=str(exc)[:2000],
                    run_after=utcnow() + timedelta(seconds=delay))
    except PermanentFailure as exc:
        session.rollback()
        _dead(job_id, kind, payload, f"permanent: {exc}")
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never swallowed
        session.rollback()
        err = f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc(limit=6)}"
        log.warning("job %s (%s) failed attempt %s: %s", job_id, kind, attempts, exc)
        if attempts >= max_attempts:
            _dead(job_id, kind, payload, err)
        else:
            _finish(job_id, status="queued", locked_at=None, last_error=err[:4000],
                    run_after=utcnow() + timedelta(seconds=backoff_seconds(attempts)))
    finally:
        session.close()
    return True


def _dead(job_id: int, kind: str, payload: dict, error: str) -> None:
    _finish(job_id, status="dead", last_error=error[:4000], finished_at=utcnow(), locked_at=None)
    log.error("job %s (%s) is dead: %s", job_id, kind, error[:300])
    handler = HANDLERS.get(kind)
    if handler and handler.on_dead:
        s = SessionLocal()
        try:
            handler.on_dead(s, payload, error)
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
            log.exception("on_dead hook for %s failed", kind)
        finally:
            s.close()


def drain(max_jobs: int = 500, advance_time: bool = False) -> int:
    """Run queued jobs until idle. advance_time=True also runs delayed jobs (tests, dev 'process now')."""
    done = 0
    while done < max_jobs:
        if advance_time:
            with SessionLocal() as s:
                s.execute(update(Job).where(Job.status == "queued", Job.run_after > utcnow())
                          .values(run_after=utcnow()))
                s.commit()
        if not run_one():
            break
        done += 1
    return done


def recover_stale(session: Session, older_than: timedelta = timedelta(minutes=15)) -> int:
    res = session.execute(update(Job).where(Job.status == "running", Job.locked_at < utcnow() - older_than)
                          .values(status="queued", locked_at=None, run_after=utcnow()))
    return res.rowcount or 0


def retry_dead(session: Session, job_id: int) -> None:
    session.execute(update(Job).where(Job.id == job_id, Job.status == "dead")
                    .values(status="queued", attempts=0, run_after=utcnow(), last_error=""))
