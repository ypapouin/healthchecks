from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta as td
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.dependencies import (
    DependencyGraph,
    connected_projects,
    locked_projects,
    set_dependencies,
    set_sharing,
)
from hc.api.management.commands.sendalerts import Command
from hc.api.models import Check, Flip
from hc.api.tests.test_dependencies import InlineExecutor


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class DependencyConcurrencyTestCase(TransactionTestCase):
    def setUp(self) -> None:
        user = User.objects.create(username="dependency-concurrency")
        self.project = Project.objects.create(owner=user)
        self.parent = Check.objects.create(project=self.project)
        self.child = Check.objects.create(project=self.project, parent=self.parent)

    def parallel(self, *actions: Callable[[], None]) -> None:
        barrier = Barrier(len(actions))

        def run(action: Callable[[], None]) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                action()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            futures = [pool.submit(run, action) for action in actions]
            for future in futures:
                future.result(timeout=20)

    def process(self) -> None:
        command = Command(stdout=Mock())
        command.executor.shutdown()
        command.executor = Mock(wraps=InlineExecutor())
        command.process_one_flip()

    def ping_child(self) -> None:
        self.child.ping("127.0.0.1", "http", "GET", "", b"", "success", None)

    def pending(self) -> Flip:
        self.child.status = "down"
        self.child.save()
        return Flip.objects.create(
            owner=self.child,
            created=now() - td(hours=1),
            old_status="up",
            new_status="down",
            reason="timeout",
            grace_start=now() - td(hours=2),
            incident_grace=td(minutes=1),
        )

    def test_concurrent_hierarchy_edits_cannot_create_cycle(self) -> None:
        set_dependencies(self.child, parent=None)
        errors = []

        def attach(child: Check, parent: Check) -> None:
            try:
                set_dependencies(child, parent=str(parent.code))
            except ValidationError:
                errors.append(True)

        self.parallel(
            lambda: attach(self.child, self.parent),
            lambda: attach(self.parent, self.child),
        )
        self.assertEqual(len(errors), 1)

    def test_two_workers_claim_only_once(self) -> None:
        flip = self.pending()
        flip.notification_reason = "fail"
        flip.save()
        with patch(
            "hc.api.management.commands.sendalerts.notify", return_value=None
        ) as notify:
            self.parallel(self.process, self.process)
        self.assertEqual(notify.call_count, 1)
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "claimed")

    def test_child_ping_and_claim_have_consistent_recovery_policy(self) -> None:
        set_dependencies(self.child, parent=None)
        flip = self.pending()
        with patch("hc.api.management.commands.sendalerts.notify", return_value=None):
            self.parallel(self.process, self.ping_child)
        flip.refresh_from_db()
        recovery = self.child.flip_set.get(new_status="up")
        if flip.notification_state == "claimed":
            self.assertNotEqual(recovery.notification_state, "cancelled")
        else:
            self.assertEqual(flip.notification_state, "cancelled")
            self.assertEqual(recovery.notification_state, "cancelled")

    def test_parent_success_and_worker_leave_pending_incident(self) -> None:
        flip = self.pending()

        def success() -> None:
            self.parent.ping("127.0.0.1", "http", "GET", "", b"", "success", None)

        with patch("hc.api.management.commands.sendalerts.notify", return_value=None):
            # Record the original blocked state first.
            self.process()
            self.parallel(success, self.process)
            self.process()
        flip.refresh_from_db()
        self.assertIn(flip.notification_state, ("waiting", "resuming"))
        self.assertIsNone(flip.processed)

    def test_two_workers_record_only_one_timeout(self) -> None:
        self.child.status = "up"
        self.child.last_ping = now() - td(days=2)
        self.child.alert_after = self.child.going_down_after()
        self.child.save()

        def timeout() -> None:
            command = Command()
            try:
                command.handle_going_down()
            finally:
                command.executor.shutdown()

        self.parallel(timeout, timeout)
        self.assertEqual(self.child.flip_set.filter(new_status="down").count(), 1)

    def test_parent_pause_and_worker_keep_grandparent_blocking(self) -> None:
        root = Check.objects.create(project=self.project, status="down")
        self.parent.parent = root
        self.parent.save()
        flip = self.pending()

        def pause() -> None:
            self.parent.status = "paused"
            self.parent.save(update_fields=("status",))

        with patch(
            "hc.api.management.commands.sendalerts.notify", return_value=None
        ) as notify:
            self.parallel(pause, self.process)
            self.process()
        notify.assert_not_called()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "waiting")
        blockers = DependencyGraph(self.project.id).blockers(
            self.child, flip.grace_start
        )
        self.assertEqual([b["check"].pk for b in blockers], [root.pk])


class SharedDependencyConcurrencyTestCase(DependencyConcurrencyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent.shared = True
        self.parent.save(update_fields=("shared",))
        target = Project.objects.create(
            owner=self.project.owner, badge_key="cross-project"
        )
        self.child.project = target
        self.child.shared = True
        self.child.save(update_fields=("project",))
        self.child.shared = True
        self.child.save(update_fields=("shared",))
        set_dependencies(self.child, parent=f"shared:{self.parent.dependency_id}")

    def test_concurrent_hierarchy_edits_cannot_create_cycle(self) -> None:
        set_dependencies(self.child, parent=None)
        errors = []

        def attach(child: Check, parent: Check) -> None:
            try:
                set_dependencies(child, parent=f"shared:{parent.dependency_id}")
            except ValidationError:
                errors.append(True)

        self.parallel(
            lambda: attach(self.child, self.parent),
            lambda: attach(self.parent, self.child),
        )
        self.assertEqual(len(errors), 1)

    def test_revocation_racing_with_attachment_cannot_leave_external_link(self) -> None:
        set_dependencies(self.child, parent=None)

        def attach() -> None:
            try:
                set_dependencies(
                    self.child, parent=f"shared:{self.parent.dependency_id}"
                )
            except ValidationError:
                pass

        self.parallel(
            attach, lambda: set_sharing(self.parent, False, self.project.owner)
        )
        self.parent.refresh_from_db()
        self.child.refresh_from_db()
        self.assertFalse(self.parent.shared)
        self.assertIsNone(self.child.parent_id)

    def test_parent_failure_and_claim_are_serialized(self) -> None:
        self.parent.ping("127.0.0.1", "http", "GET", "", b"", "success", None)
        # Remove the initial Up flip to focus on the child's incident.
        self.parent.flip_set.all().delete()
        flip = self.pending()

        def fail() -> None:
            self.parent.ping("127.0.0.1", "http", "GET", "", b"", "fail", None)

        with patch("hc.api.management.commands.sendalerts.notify", return_value=None):
            self.parallel(self.process, fail)
        flip.refresh_from_db()
        parent_failure = self.parent.flip_set.get(new_status="down")
        if flip.processed:
            self.assertLessEqual(flip.processed, parent_failure.created)
        else:
            self.assertEqual(flip.notification_state, "waiting")

    def test_revocation_and_worker_preserve_recovery_grace(self) -> None:
        flip = self.pending()
        flip.notification_state = "waiting"
        flip.save()
        with patch(
            "hc.api.management.commands.sendalerts.notify", return_value=None
        ) as notify:
            self.parallel(
                self.process,
                lambda: set_sharing(self.parent, False, self.project.owner),
            )
            self.process()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "resuming")
        self.assertIsNone(flip.processed)
        notify.assert_not_called()

    def test_lock_acquisition_retries_when_another_project_joins(self) -> None:
        third = Project.objects.create(owner=self.project.owner, badge_key="third")
        leaf = Check.objects.create(project=third)
        discovered = Event()
        attached = Event()

        def attach() -> None:
            close_old_connections()
            try:
                self.assertTrue(discovered.wait(timeout=10))
                set_dependencies(leaf, parent=f"shared:{self.child.dependency_id}")
            finally:
                attached.set()
                connections.close_all()

        first = True

        def discover(ids: set[int]) -> set[int]:
            nonlocal first
            component = connected_projects(ids)
            if first:
                first = False
                discovered.set()
                self.assertTrue(attached.wait(timeout=10))
            return component

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(attach)
            with patch("hc.api.dependencies.connected_projects", side_effect=discover):
                with locked_projects(self.child.project_id) as held:
                    self.assertIn(third.pk, held)
            future.result(timeout=10)
