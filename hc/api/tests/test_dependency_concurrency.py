from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta as td
from threading import Barrier
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils.timezone import now

from hc.accounts.models import Project
from hc.api.dependencies import DependencyGraph, set_dependencies
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
