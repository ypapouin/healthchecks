from __future__ import annotations

from datetime import timedelta as td
from unittest.mock import Mock, patch

import time_machine
from django.utils.timezone import now

from hc.api.dependencies import DependencyGraph, set_sharing
from hc.api.management.commands.sendalerts import Command
from hc.api.models import Check, Flip
from hc.api.tests.test_dependencies import InlineExecutor
from hc.test import BaseTestCase


@time_machine.travel("2026-10-06 12:00:00Z", tick=False)
class SharedDependencyAlertsTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Check.objects.create(
            project=self.project, shared=True, timeout=td(hours=1)
        )
        self.middle = Check.objects.create(
            project=self.bobs_project, parent=self.root, shared=True, status="paused"
        )
        self.child = Check.objects.create(
            project=self.charlies_project,
            parent=self.middle,
            timeout=td(minutes=1),
            grace=td(minutes=2),
        )
        self.command = Command(stdout=Mock())
        self.command.executor.shutdown()
        self.command.executor = Mock(wraps=InlineExecutor())
        self.notify = self.enterContext(
            patch("hc.api.management.commands.sendalerts.notify", return_value=None)
        )

    def ping(self, check: Check, action: str = "success") -> None:
        check.ping("127.0.0.1", "http", "GET", "", b"", action, None)
        check.refresh_from_db()

    def drain(self) -> None:
        for _ in range(30):
            if not self.command.process_one_flip():
                return
        self.fail("Notification queue did not become idle")

    def timeout(self) -> Flip:
        self.ping(self.child)
        self.enterContext(time_machine.travel(now() + td(minutes=4), tick=False))
        self.command.handle_going_down()
        self.drain()
        return self.child.flip_set.get(new_status="down")

    def test_paused_external_parent_traverses_to_grandparent_and_releases_after_grace(
        self,
    ) -> None:
        flip = self.timeout()
        self.assertEqual(flip.notification_state, "waiting")
        self.child.refresh_from_db()
        graph = DependencyGraph(self.child.project_id)
        self.assertEqual(
            [b["check"].pk for b in graph.blockers(self.child, flip.grace_start)],
            [self.root.pk],
        )
        self.assertFalse(graph.permits_reminder(self.child))
        self.ping(self.root)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "resuming")
        deadline = flip.resume_after
        with time_machine.travel(now() + td(seconds=30), tick=False):
            self.ping(self.root)
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, deadline)
        assert deadline is not None
        with time_machine.travel(deadline, tick=False):
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.notification_state, "claimed")
            self.assertTrue(
                DependencyGraph(self.child.project_id).permits_reminder(self.child)
            )

    def test_failure_and_recovery_between_polls_restart_external_grace(self) -> None:
        flip = self.timeout()
        self.ping(self.root)
        self.drain()
        flip.refresh_from_db()
        first_deadline = flip.resume_after
        with time_machine.travel(now() + td(seconds=30), tick=False):
            self.ping(self.root, "fail")
            self.ping(self.root)
            self.drain()
            flip.refresh_from_db()
            self.assertIsNotNone(first_deadline)
            self.assertEqual(flip.resume_after, now() + self.child.grace)
            self.assertNotEqual(flip.resume_after, first_deadline)

    def test_revocation_starts_recovery_grace_for_external_descendants(self) -> None:
        flip = self.timeout()
        set_sharing(self.root, False, self.alice)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "resuming")
        self.assertEqual(flip.resume_after, now() + self.child.grace)
        self.assertIsNone(flip.processed)

    def test_explicit_failure_bypasses_external_blockers(self) -> None:
        flip = self.timeout()
        self.ping(self.child, "fail")
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "claimed")
        self.assertIsNotNone(flip.processed)

    def test_external_success_reschedules_descendant_project_reminders(self) -> None:
        self.timeout()
        with patch(
            "hc.accounts.models.Project.update_next_nag_dates", autospec=True
        ) as update:
            self.ping(self.root)
        self.assertIn(
            self.child.project_id, {call.args[0].pk for call in update.call_args_list}
        )

    def test_pausing_external_ancestor_resumes_suspended_reminders(self) -> None:
        self.timeout()
        self.ping(self.child, "fail")
        self.drain()
        self.charlies_profile.nag_period = td(hours=1)
        self.charlies_profile.save()
        self.assertFalse(self.charlies_profile.send_report(nag=True))
        self.assertIsNone(self.charlies_profile.next_nag_date)
        with self.captureOnCommitCallbacks(execute=True):
            self.root.status = "paused"
            self.root.save(update_fields=("status",))
        self.charlies_profile.refresh_from_db()
        self.assertEqual(self.charlies_profile.next_nag_date, now() + td(hours=1))
        self.assertTrue(
            DependencyGraph(self.child.project_id).permits_reminder(self.child)
        )

    def test_fresh_success_from_already_up_parent_resumes_reminders(self) -> None:
        self.ping(self.root)
        self.timeout()
        self.ping(self.child, "fail")
        self.drain()
        self.charlies_profile.nag_period = td(hours=1)
        self.charlies_profile.save()
        self.assertFalse(self.charlies_profile.send_report(nag=True))
        self.assertEqual(self.root.get_status(), "up")
        self.ping(self.root)
        self.charlies_profile.refresh_from_db()
        self.assertEqual(self.charlies_profile.next_nag_date, now() + td(hours=1))
        self.assertTrue(
            DependencyGraph(self.child.project_id).permits_reminder(self.child)
        )

    def test_revocation_does_not_replay_claimed_notification(self) -> None:
        flip = self.timeout()
        self.ping(self.child, "fail")
        self.drain()
        flip.refresh_from_db()
        processed = flip.processed
        count = self.notify.call_count
        set_sharing(self.root, False, self.alice)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "claimed")
        self.assertEqual(flip.processed, processed)
        self.assertIsNone(flip.next_evaluation)
        self.assertEqual(self.notify.call_count, count)
