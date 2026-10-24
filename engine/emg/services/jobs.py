"""Postgres-backed job queue. Workers claim jobs with FOR UPDATE SKIP LOCKED,
so several workers can run safely; failed jobs retry with backoff."""
from __future__ import annotations

import os
import socket
import time
import traceback

from ..db.conn import J, connect

HANDLERS: dict = {}
MAX_ATTEMPTS = 3


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(c, kind: str, payload: dict, actor: str) -> dict:
    return c.execute("insert into jobs(kind,payload,created_by) values (%s,%s,%s) returning *", (kind, J(payload), actor)).fetchone()


def get(c, job_id) -> dict | None:
    return c.execute("select * from jobs where id=%s", (job_id,)).fetchone()


def claim(c, worker: str) -> dict | None:
    return c.execute(
        "update jobs set status='running', attempts=attempts+1, locked_by=%s, locked_at=now(), updated_at=now()"
        " where id = (select id from jobs where status='queued' and run_after <= now() order by id for update skip locked limit 1) returning *",
        (worker,)).fetchone()


def run_one(worker: str | None = None) -> bool:
    worker = worker or f"{socket.gethostname()}:{os.getpid()}"
    with connect() as c:
        job = claim(c, worker)
    if not job:
        return False
    try:
        with connect() as c:
            result = HANDLERS[job["kind"]](c, job["payload"], job["created_by"] or "system")
            c.execute("update jobs set status='done', result=%s, updated_at=now() where id=%s", (J(result), job["id"]))
    except Exception as e:  # recorded on the job; the transaction above rolled back
        with connect() as c:
            again = job["attempts"] < MAX_ATTEMPTS and not isinstance(e, (ValueError, LookupError))
            c.execute("update jobs set status=%s, error=%s, run_after=now() + (%s || ' seconds')::interval, updated_at=now() where id=%s",
                      ("queued" if again else "failed", f"{e}\n{traceback.format_exc()[-2000:]}", 30 * job["attempts"], job["id"]))
    return True


def work_forever(poll: float = 2.0) -> None:  # pragma: no cover
    from . import job_handlers  # noqa: F401  (registers handlers)

    last_tick = 0.0
    while True:
        if time.time() - last_tick > 60:  # schedules (leaderboards, connectors) are checked once a minute
            last_tick = time.time()
            with connect() as c:
                if not c.execute("select 1 from jobs where kind='scheduler.tick' and status in ('queued','running')").fetchone():
                    enqueue(c, "scheduler.tick", {}, "scheduler")
        if not run_one():
            time.sleep(poll)


if __name__ == "__main__":  # pragma: no cover
    # Run via the package module so handlers register on the same HANDLERS dict
    # (under `python -m`, this file is `__main__`, a separate module object).
    from emg.services.jobs import work_forever as _work

    _work()
