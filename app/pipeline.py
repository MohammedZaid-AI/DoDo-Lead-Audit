"""Glue: listen > classify (M4). Detect, audit and score are added in M5."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlmodel import select

from app import db
from app.listen import RawSignal
from app.listen.classify import classify, worth_classifying
from app.listen.github import fetch_github, repo_homepage
from app.listen.hn import fetch_hn
from app.llm import DailyLimitError

SOURCES: dict[str, Callable[..., list[RawSignal]]] = {"hn": fetch_hn, "github": fetch_github}


@dataclass
class ListenStats:
    fetched: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    new: int = 0
    classified: int = 0
    pain: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: str = ""


def to_raw(sig: db.Signal) -> RawSignal:
    return RawSignal(sig.source, sig.url, sig.author, sig.title or "", sig.body or "", sig.created_at, sig.context or {})


def listen(days: int, sources: list[str], do_classify: bool = True, log=print) -> ListenStats:
    """Fetch from each source, store new signals, then classify pending ones."""
    stats = ListenStats()
    with db.session() as s:
        for name in sources:
            log(f"fetching {name} (last {days} days)...")
            try:
                raws = SOURCES[name](days=days)
            except Exception as e:  # noqa: BLE001 - one source failing should not stop the other
                stats.errors[name] = f"{type(e).__name__}: {e}"[:200]
                continue
            stats.fetched[name] = len(raws)
            stats.new += db.store_signals(s, raws)
    if do_classify:
        classify_pending(stats, log=log)
    return stats


def classify_pending(stats: ListenStats | None = None, limit: int | None = None, log=print) -> ListenStats:
    """Classify pending and previously failed signals; fill product_url from the repo homepage."""
    stats = stats or ListenStats()
    with db.session() as s:
        pending = s.exec(select(db.Signal).where(db.Signal.status.in_(["pending", "failed"]))).all()
        rows = []
        for sig in pending:
            if worth_classifying(to_raw(sig)):
                rows.append(sig)
            else:
                sig.status, sig.is_payment_pain = "skipped", False
                sig.classification = {"one_line_reason": "Pre-filter: no payment provider plus pain or decision terms."}
                s.add(sig)
                stats.skipped += 1
        s.commit()
        log(f"  {len(rows)} posts to classify, {stats.skipped} skipped by the keyword pre-filter")
        for i, sig in enumerate(rows[:limit] if limit else rows, 1):
            try:
                result = classify(to_raw(sig))
            except DailyLimitError as e:
                stats.stopped = str(e)
                log(f"  stopped: {e}. Run 'python cli.py classify' later to continue ({len(rows) - i + 1} left).")
                break
            if result is None:
                sig.status = "failed"
                stats.failed += 1
            else:
                if result.is_payment_pain and not result.product_url and sig.source == "github":
                    result.product_url = repo_homepage(sig.context.get("repo", ""))
                sig.classification = result.model_dump()
                sig.is_payment_pain = result.is_payment_pain
                sig.pain_type, sig.urgency, sig.product_url = result.pain_type, result.urgency, result.product_url
                sig.status = "classified"
                stats.classified += 1
                stats.pain += int(result.is_payment_pain)
            s.add(sig)
            s.commit()
            if i % 5 == 0:
                log(f"  classified {i}/{len(rows)}")
    return stats


def mark_pain_for_recheck(all_classified: bool = False) -> int:
    """Send posts back to the classifier after a prompt change: pain only, or every classified post."""
    with db.session() as s:
        q = select(db.Signal).where(db.Signal.status == "classified")
        if not all_classified:
            q = q.where(db.Signal.is_payment_pain == True)  # noqa: E712
        rows = s.exec(q).all()
        for r in rows:
            r.status = "pending"
            s.add(r)
        s.commit()
        return len(rows)
