"""Dependency validation and notification policy shared by all entry points.

Writers lock the project before checks or flips. Network I/O must happen after
releasing these locks. A project lock also serializes edits to the whole graph.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F, OuterRef, Subquery
from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.models import Check, Flip, isostring
from hc.lib.statsd import statsd

PENDING = ("ready", "waiting", "resuming")
RECHECK = timedelta(seconds=10)


def lock_projects(*ids: int) -> None:
    # SQLite has no row locks. Acquire its write lock before reading the graph.
    q = Project.objects.filter(pk__in=ids).order_by("pk")
    if connection.vendor == "sqlite":
        q.update(name=F("name"))
    else:
        list(q.select_for_update())


@contextmanager
def locked_check(check: Check) -> Iterator[Check]:
    # A transfer may commit between finding the project and acquiring its lock.
    # Retry outside the transaction, always preserving project -> check order.
    while True:
        project_id = Check.objects.values_list("project_id", flat=True).get(pk=check.pk)
        with transaction.atomic():
            lock_projects(project_id)
            current = Check.objects.get(pk=check.pk)
            if current.project_id != project_id:
                continue
            yield current
            return


def validate_graph(checks: dict[int, Check], parents: dict[int, int | None]) -> None:
    done: set[int] = set()
    for child in parents:
        path: set[int] = set()
        node: int | None = child
        while node is not None and node not in done:
            if node not in checks:
                raise ValidationError(
                    {"parent": "Parent must belong to the same project."}
                )
            if node in path:
                raise ValidationError(
                    {"parent": "A check cannot depend on itself or its descendants."}
                )
            path.add(node)
            node = parents[node]
        done.update(path)


def wake_pending(project_id: int, changed: set[int] | None = None) -> None:
    pending = Flip.objects.filter(
        owner__project_id=project_id, processed=None, notification_state__in=PENDING
    )
    pending.update(next_evaluation=now())
    if changed:
        # Also invalidate a recovery grace when a path changed twice between
        # worker polls. Unrelated branches keep their existing deadlines.
        children: dict[int, list[int]] = {}
        for pk, parent_id in Check.objects.filter(project_id=project_id).values_list(
            "id", "parent_id"
        ):
            if parent_id is not None:
                children.setdefault(parent_id, []).append(pk)
        affected = set(changed)
        todo = list(changed)
        while todo:
            for child in children.get(todo.pop(), []):
                if child not in affected:
                    affected.add(child)
                    todo.append(child)
        pending.filter(owner_id__in=affected, notification_state="resuming").update(
            notification_state="waiting", resume_after=None, resume_signature={}
        )


def set_dependencies(
    check: Check, *, parent: str | None = None, children: list[str] | None = None
) -> None:
    with locked_check(check) as current:
        if current.project_id != check.project_id:
            raise ValidationError("Check was transferred; reload and try again.")
        graph = DependencyGraph(current.project_id)
        by_code = {str(c.code): c.id for c in graph.checks.values()}
        parents = {c.id: c.parent_id for c in graph.checks.values()}
        if children is None:
            if parent is not None and parent not in by_code:
                raise ValidationError(
                    {"parent": "Parent must belong to the same project."}
                )
            parents[current.id] = by_code[parent] if parent else None
        else:
            if any(code not in by_code for code in children):
                raise ValidationError(
                    {"children": "Children must belong to the same project."}
                )
            selected = {by_code[code] for code in children}
            for c in graph.checks.values():
                if c.id in selected:
                    parents[c.id] = current.id
                elif c.parent_id == current.id:
                    parents[c.id] = None
        validate_graph(graph.checks, parents)
        changed = set()
        for pk, parent_id in parents.items():
            if graph.checks[pk].parent_id != parent_id:
                changed.add(pk)
                Check.objects.filter(pk=pk).update(parent_id=parent_id)
        wake_pending(current.project_id, changed)
        check.parent_id = parents[current.id]


class DependencyGraph:
    def __init__(self, project_id: int, checks: list[Check] | None = None):
        self.checks = {
            c.id: c
            for c in (
                checks
                if checks is not None
                else Check.objects.filter(project_id=project_id)
            )
        }
        latest = Flip.objects.filter(
            owner_id=OuterRef("pk"), new_status="down"
        ).order_by("-created", "-id")
        ids = (
            Check.objects.filter(pk__in=self.checks)
            .annotate(incident_id=Subquery(latest.values("id")[:1]))
            .values("incident_id")
        )
        self.incidents = {f.owner_id: f for f in Flip.objects.filter(pk__in=ids)}

    def ancestors(self, check: Check) -> list[Check]:
        result = []
        seen = {check.id}
        parent_id = check.parent_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = self.checks.get(parent_id)
            if parent is None:
                break
            result.append(parent)
            parent_id = parent.parent_id
        return result

    def blockers(
        self, check: Check, grace_start: datetime | None
    ) -> list[dict[str, Any]]:
        result = []
        for ancestor in self.ancestors(check):
            # A paused check is transparent: require neither its health nor a
            # recent success, but still examine every ancestor above it.
            if ancestor.status == "paused":
                continue
            status = ancestor.get_status()
            reasons = []
            if status != "up":
                reasons.append(
                    "Parent is " + ("Late" if status == "grace" else status.title())
                )
            if ancestor.last_success is None:
                reasons.append("No accepted success signal")
            elif grace_start and ancestor.last_success < grace_start:
                reasons.append("Last success is before this incident's grace period")
            if reasons:
                result.append({"check": ancestor, "reason": "; ".join(reasons)})
        return result

    def signature(self, check: Check) -> dict[str, str | None]:
        return {
            str(c.id): c.up_since.isoformat() if c.up_since else None
            for c in self.ancestors(check)
            if c.status != "paused"
        }

    def describe(self, check: Check, *, readonly: bool = False) -> dict[str, Any]:
        incident = self.incidents.get(check.id) if check.status == "down" else None
        start = incident.grace_start if incident else check.get_grace_start()

        def ref(c: Check) -> dict[str, Any]:
            return {
                "id": c.unique_key if readonly else str(c.code),
                "name": c.name or "unnamed",
                "status": c.get_status(),
                "last_success": isostring(c.last_success),
            }

        blockers = [
            {**ref(b["check"]), "reason": b["reason"]}
            for b in self.blockers(check, start)
        ]
        parent = self.checks.get(check.parent_id) if check.parent_id else None
        state = incident.notification_state if incident else "none"
        if incident and "fail" in (incident.reason, incident.notification_reason):
            blockers = []
        deadline = incident.resume_after if incident and state == "resuming" else None
        return {
            "parent": ref(parent) if parent else None,
            "ancestors": [ref(c) for c in reversed(self.ancestors(check))],
            "state": state,
            "blockers": blockers,
            "notification_after": isostring(deadline),
            "pending": state in ("waiting", "resuming"),
        }

    def permits_reminder(self, check: Check) -> bool:
        incident = self.incidents.get(check.id)
        if incident and (
            incident.notification_state in ("waiting", "resuming", "cancelled")
            or (incident.reason == "timeout" and incident.processed is None)
        ):
            return False
        start = incident.grace_start if incident else check.get_grace_start()
        return not self.blockers(check, start)


def cancel_pending(check: Check) -> bool:
    q = check.flip_set.filter(
        new_status="down",
        reason="timeout",
        processed=None,
        notification_reason="",
        notification_state__in=PENDING,
    )
    count = q.update(
        notification_state="cancelled",
        processed=now(),
        next_evaluation=None,
        resume_after=None,
    )
    if count:
        statsd.incr("hc.dependencies.cancelled", count)
    return bool(count)


def evaluate(flip: Flip, graph: DependencyGraph) -> bool:
    """Under the project lock, decide whether a queued notification can be claimed."""
    if (
        flip.new_status != "down"
        or flip.reason != "timeout"
        or flip.notification_reason == "fail"
    ):
        return True
    check = flip.owner
    if check.status != "down":
        cancel_pending(check)
        return False
    stamp = now()
    blockers = graph.blockers(check, flip.grace_start)
    signature = graph.signature(check)
    paused_ids = {str(c.id) for c in graph.ancestors(check) if c.status == "paused"}
    # Pausing an eligible ancestor removes a requirement. It must not extend an
    # already running grace. Active ancestors and actual path changes still count.
    previous_signature = {
        pk: since for pk, since in flip.resume_signature.items() if pk not in paused_ids
    }
    previous = flip.notification_state
    if blockers:
        flip.notification_state = "waiting"
        flip.resume_after = None
        flip.resume_signature = {}
        if previous == "ready":
            statsd.incr("hc.dependencies.deferred")
    elif previous == "ready":
        return True
    elif previous == "waiting" or signature != previous_signature:
        flip.notification_state = "resuming"
        flip.resume_after = stamp + (flip.incident_grace or timedelta())
        flip.resume_signature = signature
    elif flip.resume_after is not None and stamp >= flip.resume_after:
        statsd.incr("hc.dependencies.released")
        return True
    flip.next_evaluation = (
        min(stamp + RECHECK, flip.resume_after)
        if flip.resume_after
        else stamp + RECHECK
    )
    flip.save(
        update_fields=(
            "notification_state",
            "resume_after",
            "resume_signature",
            "next_evaluation",
        )
    )
    return False
