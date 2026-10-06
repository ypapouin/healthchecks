"""Dependency validation and notification policy shared by all entry points.

Writers lock connected projects before checks or flips. Network I/O must happen
after releasing these locks. Presentation never grants access to another project.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.urls import reverse
from django.utils.timezone import now

from hc.accounts.models import Member, Project
from hc.api.models import Check, Flip, isostring
from hc.lib.statsd import statsd

PENDING = ("ready", "waiting", "resuming")
RECHECK = timedelta(seconds=10)
_held_projects: ContextVar[set[int] | None] = ContextVar(
    "dependency_locks", default=None
)


def connected_projects(ids: set[int]) -> set[int]:
    result, frontier = set(ids), set(ids)
    while frontier:
        edges = Check.objects.filter(
            Q(project_id__in=frontier) | Q(parent__project_id__in=frontier),
            parent__isnull=False,
        ).exclude(project_id=F("parent__project_id"))
        found = {
            pk
            for edge in edges.values_list("project_id", "parent__project_id")
            for pk in edge
        }
        frontier = found - result
        result.update(frontier)
    return result


def lock_projects(*ids: int) -> None:
    # SQLite has no row locks. Acquire its write lock before reading the graph.
    q = Project.objects.filter(pk__in=ids).order_by("pk")
    if connection.vendor == "sqlite":
        q.update(name=F("name"))
    else:
        list(q.select_for_update())


class _GraphExpanded(Exception):
    pass


@contextmanager
def locked_projects(*ids: int) -> Iterator[set[int]]:
    """Lock an entire project component, retrying before any mutation on growth.

    Nested callers may only reuse the outer locks. They must never acquire an
    additional component out of order: proposed parents/destinations are seeds
    of the outermost operation, not discovered after it has started writing.
    """
    seeds = set(ids)
    held = _held_projects.get()
    if held is not None:
        if not seeds <= held:
            raise ValidationError("Dependencies changed; reload and try again.")
        yield held
        return
    while True:
        component = connected_projects(seeds)
        try:
            with transaction.atomic():
                lock_projects(*component)
                if not connected_projects(seeds) <= component:
                    raise _GraphExpanded
                token = _held_projects.set(component)
                try:
                    yield component
                finally:
                    _held_projects.reset(token)
            return
        except _GraphExpanded:
            # Roll back the whole lock acquisition, including its savepoint.
            continue


@contextmanager
def locked_check(check: Check, *extra_projects: int) -> Iterator[Check]:
    # A transfer may commit between finding the project and acquiring its lock.
    # Retry outside the transaction, always preserving project -> check order.
    while True:
        project_id = Check.objects.values_list("project_id", flat=True).get(pk=check.pk)
        with locked_projects(project_id, *extra_projects):
            current = Check.objects.get(pk=check.pk)
            if current.project_id != project_id:
                continue
            yield current
            return


def resolve_parent(project_id: int, value: str | None) -> Check | None:
    if not value:
        return None
    try:
        if value.startswith("shared:"):
            parent = Check.objects.filter(dependency_id=value[7:], shared=True).first()
        else:
            parent = Check.objects.filter(project_id=project_id, code=value).first()
    except (ValidationError, ValueError):
        parent = None
    if parent is None:
        raise ValidationError({"parent": "Parent is unavailable or is not shared."})
    held = _held_projects.get()
    if held is not None and parent.project_id not in held:
        raise ValidationError("Parent changed; reload and try again.")
    return parent


def parent_project(project_id: int, value: str | None) -> int:
    parent = resolve_parent(project_id, value)
    return parent.project_id if parent else project_id


def load_ancestors(checks: dict[int, Check]) -> None:
    while (
        missing := {c.parent_id for c in checks.values() if c.parent_id} - checks.keys()
    ):
        parents = list(Check.objects.filter(pk__in=missing).select_related("project"))
        if not parents:
            break
        checks.update({c.id: c for c in parents})


def validate_graph(checks: dict[int, Check], parents: dict[int, int | None]) -> None:
    held = _held_projects.get()
    if held is not None and not {c.project_id for c in checks.values()} <= held:
        raise ValidationError("Dependencies changed; reload and try again.")
    for pk, parent_id in parents.items():
        if parent_id is not None:
            parent = checks.get(parent_id)
            if parent is None or (
                parent.project_id != checks[pk].project_id and not parent.shared
            ):
                raise ValidationError(
                    {"parent": "Parent is unavailable or is not shared."}
                )
    done: set[int] = set()
    for child in parents:
        path: set[int] = set()
        node: int | None = child
        while node is not None and node not in done:
            if node not in checks:
                raise ValidationError(
                    {"parent": "Parent is unavailable or is not shared."}
                )
            if node in path:
                raise ValidationError(
                    {"parent": "A check cannot depend on itself or its descendants."}
                )
            path.add(node)
            node = parents[node]
        done.update(path)


def descendant_ids(roots: set[int]) -> set[int]:
    result, frontier = set(roots), set(roots)
    while frontier:
        found = set(
            Check.objects.filter(parent_id__in=frontier).values_list("id", flat=True)
        )
        frontier = found - result
        result.update(frontier)
    return result


def update_reminders(roots: set[int]) -> None:
    projects = Project.objects.filter(check__pk__in=descendant_ids(roots)).distinct()
    for project in projects:
        project.update_next_nag_dates()


def wake_pending(project_id: int, changed: set[int] | None = None) -> None:
    roots = (
        changed
        if changed is not None
        else set(
            Check.objects.filter(project_id=project_id).values_list("id", flat=True)
        )
    )
    affected = descendant_ids(roots)
    pending = Flip.objects.filter(
        owner_id__in=affected, processed=None, notification_state__in=PENDING
    )
    pending.update(next_evaluation=now())
    if changed:
        # Also invalidate a recovery grace when a path changed twice between
        # worker polls. Unrelated branches keep their existing deadlines.
        pending.filter(owner_id__in=affected, notification_state="resuming").update(
            notification_state="waiting", resume_after=None, resume_signature={}
        )
        transaction.on_commit(lambda: update_reminders(affected))


def set_dependencies(
    check: Check, *, parent: str | None = None, children: list[str] | None = None
) -> None:
    extra = (
        parent_project(check.project_id, parent)
        if children is None
        else check.project_id
    )
    with locked_check(check, extra) as current:
        if current.project_id != check.project_id:
            raise ValidationError("Check was transferred; reload and try again.")
        graph = DependencyGraph(current.project_id)
        by_code = {
            str(c.code): c.id
            for c in graph.checks.values()
            if c.project_id == current.project_id
        }
        parents = {c.id: c.parent_id for c in graph.checks.values()}
        if children is None:
            candidate = resolve_parent(current.project_id, parent)
            if candidate:
                graph.checks[candidate.id] = candidate
                load_ancestors(graph.checks)
                parents.update({c.id: c.parent_id for c in graph.checks.values()})
            parents[current.id] = candidate.id if candidate else None
        else:
            if any(code not in by_code for code in children):
                raise ValidationError(
                    {"children": "Children must belong to the same project."}
                )
            selected = {by_code[code] for code in children}
            for c in graph.checks.values():
                if c.project_id != current.project_id:
                    continue
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


def can_share(user: User | AnonymousUser, project: Project) -> bool:
    if not isinstance(user, User):
        return False
    return (
        user.is_superuser
        or project.owner_id == user.id
        or Member.objects.filter(
            user=user, project=project, role=Member.Role.MANAGER
        ).exists()
    )


def visible_projects(user: User) -> set[int]:
    q = Project.objects.all()
    if not user.is_superuser:
        q = q.filter(Q(owner=user) | Q(member__user=user))
    return set(q.values_list("id", flat=True))


def project_label(project: Project) -> str:
    # Unlike Project.__str__, dependency metadata must not expose the owner's email.
    return project.name or "Unnamed project"


def parent_options(
    project_id: int, check_id: int | None = None
) -> list[dict[str, Any]]:
    checks = (
        Check.objects.filter(Q(project_id=project_id) | Q(shared=True))
        .exclude(pk=check_id or 0)
        .select_related("project")
        .order_by("project__name", "name", "id")
    )
    return [
        {
            "pk": c.pk,
            "value": str(c.code)
            if c.project_id == project_id
            else f"shared:{c.dependency_id}",
            "label": c.name or "unnamed"
            if c.project_id == project_id
            else f"{project_label(c.project)} — {c.name or 'unnamed'}",
            "external": c.project_id != project_id,
        }
        for c in checks
    ]


def set_sharing(check: Check, shared: bool, user: User) -> None:
    from django.core.exceptions import PermissionDenied

    with locked_check(check) as current:
        if current.project_id != check.project_id or not can_share(
            user, current.project
        ):
            raise PermissionDenied
        current.shared = shared
        current.save(update_fields=("shared",))


@contextmanager
def deleting_checks(checks: QuerySet[Check], *project_ids: int) -> Iterator[None]:
    while True:
        ids = set(checks.values_list("project_id", flat=True)) | set(project_ids)
        with locked_projects(*ids) as held:
            # A bulk-selected check may have moved before acquiring the locks.
            if not set(checks.values_list("project_id", flat=True)) <= held:
                continue
            detached = set(
                Check.objects.filter(parent__in=checks)
                .exclude(pk__in=checks)
                .values_list("id", flat=True)
            )
            yield
            wake_pending(0, detached)
            return


def delete_projects(projects: QuerySet[Project]) -> tuple[int, dict[str, int]]:
    ids = list(projects.values_list("id", flat=True))
    with deleting_checks(Check.objects.filter(project_id__in=ids), *ids):
        return projects.delete()


def delete_users(users: QuerySet[User]) -> tuple[int, dict[str, int]]:
    ids = list(Project.objects.filter(owner__in=users).values_list("id", flat=True))
    with deleting_checks(Check.objects.filter(project_id__in=ids), *ids):
        return users.delete()


class DependencyGraph:
    def __init__(self, project_id: int, checks: list[Check] | None = None):
        self.checks = {
            c.id: c
            for c in (
                checks
                if checks is not None
                else Check.objects.filter(project_id=project_id).select_related(
                    "project"
                )
            )
        }
        load_ancestors(self.checks)
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

    def describe(
        self,
        check: Check,
        *,
        readonly: bool = False,
        accessible: set[int] | None = None,
    ) -> dict[str, Any]:
        incident = self.incidents.get(check.id) if check.status == "down" else None
        start = incident.grace_start if incident else check.get_grace_start()
        accessible = accessible if accessible is not None else {check.project_id}

        def ref(c: Check) -> dict[str, Any]:
            external = c.project_id != check.project_id
            can_view = c.project_id in accessible
            if not can_view and not c.shared:
                return {
                    "id": None,
                    "name": "Private dependency",
                    "label": "Private dependency",
                    "project": None,
                    "status": None,
                    "last_success": None,
                    "hidden": True,
                    "external": True,
                    "url": None,
                }
            name = c.name or "unnamed"
            return {
                "id": f"shared:{c.dependency_id}"
                if external
                else c.unique_key
                if readonly
                else str(c.code),
                "name": name,
                "label": f"{project_label(c.project)} — {name}" if external else name,
                "project": project_label(c.project),
                "status": c.get_status(),
                "last_success": isostring(c.last_success),
                "hidden": False,
                "external": external,
                "url": reverse("hc-uncloak", args=[c.unique_key]) if can_view else None,
            }

        blockers = []
        private_blocker = False
        for b in self.blockers(check, start):
            item = ref(b["check"])
            if item["hidden"]:
                if private_blocker:
                    continue
                private_blocker = True
                item["reason"] = "A private dependency is not ready"
            else:
                item["reason"] = b["reason"]
            blockers.append(item)
        ancestors: list[dict[str, Any]] = []
        for c in reversed(self.ancestors(check)):
            item = ref(c)
            if item["hidden"] and ancestors and ancestors[-1]["hidden"]:
                continue
            ancestors.append(item)
        parent = self.checks.get(check.parent_id) if check.parent_id else None
        state = incident.notification_state if incident else "none"
        if incident and "fail" in (incident.reason, incident.notification_reason):
            blockers = []
        deadline = incident.resume_after if incident and state == "resuming" else None
        return {
            "parent": ref(parent) if parent else None,
            "ancestors": ancestors,
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
