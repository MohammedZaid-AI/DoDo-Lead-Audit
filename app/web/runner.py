"""Runs the pipeline in a background thread for the dashboard's Run button. One run at a time."""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from app import pipeline

MAX_LOG = 400


class Runner:
    def __init__(self, target: Callable | None = None):
        self._target = target or pipeline.run
        self._lock = threading.Lock()
        self.state = "idle"  # idle | running | done | failed
        self.params: dict = {}
        self.started: str | None = None
        self.finished: str | None = None
        self.summary = ""
        self.log: deque[str] = deque(maxlen=MAX_LOG)

    def start(self, days: int, top: int, fetch: bool, opener: bool, yc: int = 0) -> bool:
        """Start a run. False if one is already running."""
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.summary, self.finished = "running", "", None
            self.params = {"days": days, "top": top, "fetch": fetch, "opener": opener, "yc": yc}
            self.started = _now()
            self.log.clear()
        threading.Thread(target=self._work, args=(days, top, fetch, opener, yc), daemon=True).start()
        return True

    def _work(self, days: int, top: int, fetch: bool, opener: bool, yc: int) -> None:
        try:
            st, leads = self._target(days, top, fetch, None, opener, log=self._say, yc=yc)
            self.summary = (f"{len(leads)} leads audited from {st.candidates} pain posts and {st.sites} sites. "
                            f"Openers ok: {st.openers}.")
            self.state = "done"
        except Exception as e:  # shown in the dashboard, the server keeps running
            self._say(f"ERROR: {type(e).__name__}: {e}")
            self.summary, self.state = f"Run failed: {e}", "failed"
        finally:
            self.finished = _now()

    def _say(self, *parts) -> None:
        self.log.append(" ".join(str(p) for p in parts))

    def status(self) -> dict:
        return {"state": self.state, "params": self.params, "started": self.started, "finished": self.finished,
                "summary": self.summary, "log": list(self.log)[-60:]}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
