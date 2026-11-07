"""Long imports run in the background so a browser doesn't sit waiting. Holds progress, not logic."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from ..domain.errors import UserError, message_for_people
from ..services.views import ImportReport

log = logging.getLogger("uppyt.jobs")


@dataclass
class ImportJob:
    id: str
    profile: str
    done: int = 0
    total: int = 0
    finished: bool = False
    report: ImportReport | None = None
    problem: str | None = None


class ImportJobs:
    def __init__(self) -> None:
        self._jobs: dict[str, ImportJob] = {}

    def start(self, profile: str, work: Callable[[Callable[[int, int], None]], ImportReport]) -> ImportJob:
        job = ImportJob(uuid4().hex, profile)
        self._jobs[job.id] = job

        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        def run() -> None:
            try:
                job.report = work(progress)
            except UserError as error:
                job.problem = message_for_people(error)
            except Exception:
                log.exception("Import failed")
                job.problem = "The import stopped unexpectedly. Anything already added has been kept."
            finally:
                job.finished = True

        threading.Thread(target=run, name=f"uppyt-import-{job.id[:8]}", daemon=True).start()
        return job

    def get(self, job_id: str) -> ImportJob | None:
        return self._jobs.get(job_id)
