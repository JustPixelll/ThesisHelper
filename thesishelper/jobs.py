"""Bounded background work so ingestion and long model requests never freeze the UI."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .gateway import Cancelled


class Jobs:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thesishelper")
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.events: dict[str, threading.Event] = {}

    def start(self, kind: str, task: Callable, key: str | None = None) -> str:
        with self.lock:
            for job in self.jobs.values():
                if job["key"] == (key or kind) and job["status"] in {"queued", "running"}:
                    raise ValueError("This task is already running. Wait for it or cancel it first.")
            job_id = uuid.uuid4().hex
            event = threading.Event()
            self.events[job_id] = event
            self.jobs[job_id] = {"id": job_id, "kind": kind, "key": key or kind, "status": "queued",
                                 "progress": {}, "result": None, "error": None, "created_at": time.time()}
            # Only finished jobs are pruned; no active task is silently orphaned.
            finished = [j for j in self.jobs.values() if j["status"] not in {"queued", "running"}]
            for job in sorted(finished, key=lambda j: j["created_at"])[:-80]:
                self.jobs.pop(job["id"], None)
                self.events.pop(job["id"], None)

        def progress(value):
            with self.lock:
                self.jobs[job_id]["progress"] = copy.deepcopy(value)

        def work():
            with self.lock:
                self.jobs[job_id]["status"] = "running"
            try:
                if event.is_set():
                    raise Cancelled("Cancelled.")
                result = task(progress, event)
                with self.lock:
                    self.jobs[job_id].update(status="cancelled" if event.is_set() else "completed", result=result)
            except Cancelled as error:
                with self.lock:
                    self.jobs[job_id].update(status="cancelled", error=str(error))
            except Exception as error:
                with self.lock:
                    self.jobs[job_id].update(status="cancelled" if event.is_set() else "failed", error=str(error)[:1800])

        self.executor.submit(work)
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def active(self) -> list[dict]:
        with self.lock:
            return [copy.deepcopy(job) for job in self.jobs.values() if job["status"] in {"queued", "running"}]

    def cancel(self, job_id: str) -> dict | None:
        with self.lock:
            if job_id not in self.jobs:
                return None
            self.events[job_id].set()
            if self.jobs[job_id]["status"] in {"queued", "running"}:
                self.jobs[job_id]["progress"]["message"] = (
                    "Cancellation requested. An API call already in progress may still finish and be billed.")
            return self.get(job_id)

    def shutdown(self):
        with self.lock:
            for event in self.events.values():
                event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
