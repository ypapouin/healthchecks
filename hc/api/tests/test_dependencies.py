from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from datetime import timedelta as td
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import time_machine
from django.core.exceptions import ValidationError
from django.utils.timezone import now

from hc.api.dependencies import DependencyGraph, set_dependencies
from hc.api.management.commands.sendalerts import Command
from hc.api.models import Check, Flip, Ping
from hc.test import BaseTestCase


class InlineExecutor:
    def submit(self, fn: Callable[..., str | None], *args: Any) -> Future[str | None]:
        result: Future[str | None] = Future()
        result.set_result(fn(*args))
        return result


@time_machine.travel("2026-10-05 12:00:00Z", tick=False)
class DependenciesTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = Check.objects.create(
            project=self.project, name="Host", timeout=td(hours=1)
        )
        self.child = Check.objects.create(
            project=self.project,
            name="Job",
            parent=self.parent,
            timeout=td(minutes=1),
            grace=td(minutes=2),
        )
        self.command = Command(stdout=Mock())
        self.command.executor.shutdown()
        self.command.executor = Mock(wraps=InlineExecutor())
        self.notify = self.enterContext(
            patch("hc.api.management.commands.sendalerts.notify", return_value=None)
        )

    def ping(
        self, check: Check, action: str = "success", rid: UUID | None = None
    ) -> None:
        check.ping("127.0.0.1", "http", "GET", "", b"", action, rid)
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
        self.child.refresh_from_db()
        return self.child.flip_set.get(new_status="down")

    def test_timeout_keeps_real_downtime_and_waits_without_spinning(self) -> None:
        flip = self.timeout()
        self.assertEqual(self.child.status, "down")
        self.assertEqual(flip.notification_state, "waiting")
        self.assertIsNone(flip.processed)
        assert self.child.last_ping is not None
        self.assertEqual(flip.grace_start, self.child.last_ping + self.child.timeout)
        self.assertEqual(flip.incident_grace, self.child.grace)
        self.assertFalse(self.command.process_one_flip())
        self.assertTrue(any(d.duration > td() for d in self.child.downtimes(1, "UTC")))
        self.assertFalse(DependencyGraph(self.project.id).permits_reminder(self.child))

    def test_success_before_original_grace_is_insufficient(self) -> None:
        self.ping(self.parent)
        flip = self.timeout()
        self.assertEqual(flip.notification_state, "waiting")
        self.assertIn(
            "before",
            DependencyGraph(self.project.id).describe(self.child)["blockers"][0][
                "reason"
            ],
        )

    def test_eligible_parent_allows_normal_timeout(self) -> None:
        self.ping(self.child)
        with time_machine.travel(now() + td(minutes=2), tick=False):
            self.ping(self.parent)
        with time_machine.travel(now() + td(minutes=4), tick=False):
            self.command.handle_going_down()
            self.drain()
        self.assertEqual(
            self.child.flip_set.get(new_status="down").notification_state, "claimed"
        )

    def test_recovery_grace_survives_restart_and_success_does_not_extend_it(
        self,
    ) -> None:
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        flip.refresh_from_db()
        deadline = now() + self.child.grace
        self.assertEqual(flip.resume_after, deadline)
        with time_machine.travel(now() + td(seconds=50), tick=False):
            self.ping(self.parent)
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, deadline)
        with time_machine.travel(deadline, tick=False):
            restarted = Command(stdout=Mock())
            restarted.executor.shutdown()
            restarted.executor = Mock(wraps=InlineExecutor())
            restarted.process_one_flip()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "claimed")
        self.assertEqual(self.child.flip_set.filter(new_status="down").count(), 1)

    def test_relapse_and_recovery_between_polls_restarts_grace(self) -> None:
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        with time_machine.travel(now() + td(seconds=60), tick=False):
            self.ping(self.parent, "fail")
            self.ping(self.parent)
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, now() + self.child.grace)

    def test_late_then_success_between_polls_restarts_grace(self) -> None:
        self.parent.timeout = td(seconds=30)
        self.parent.save()
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        with time_machine.travel(now() + td(seconds=31), tick=False):
            self.ping(self.parent)
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, now() + self.child.grace)

    def test_relapse_interrupts_recovery_grace(self) -> None:
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        self.ping(self.parent, "fail")
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "waiting")
        self.assertIsNone(flip.resume_after)

    def test_start_timeout_and_restart_between_polls_restarts_grace(self) -> None:
        self.parent.grace = td(seconds=30)
        self.parent.save()
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        self.ping(self.parent, "start")
        with time_machine.travel(now() + td(seconds=31), tick=False):
            self.ping(self.parent, "start")
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, now() + self.child.grace)

    def test_child_success_cancels_timeout_and_recovery(self) -> None:
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        self.ping(self.child)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "cancelled")
        recovery = self.child.flip_set.filter(old_status="down", new_status="up").get()
        self.assertEqual(recovery.notification_state, "cancelled")
        self.assertEqual(recovery.select_channels(), [])

    def test_claimed_incident_has_notifiable_recovery(self) -> None:
        self.child.parent = None
        self.child.save()
        self.timeout()
        self.ping(self.child)
        recovery = self.child.flip_set.get(old_status="down", new_status="up")
        self.assertEqual(recovery.notification_state, "ready")
        self.assertIsNone(recovery.processed)

    def test_explicit_failure_releases_same_incident_with_separate_reason(self) -> None:
        flip = self.timeout()
        self.ping(self.child, "fail")
        self.ping(self.child, "fail")
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.reason, "timeout")
        self.assertEqual(flip.notification_reason, "fail")
        self.assertEqual(flip.reason_long(), "received a failure signal")
        self.assertEqual(flip.notification_state, "claimed")
        self.assertEqual(self.child.flip_set.filter(new_status="down").count(), 1)

    def test_explicit_failure_during_recovery_grace(self) -> None:
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        self.ping(self.child, "fail")
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "claimed")

    def test_explicit_failure_bypasses_parents(self) -> None:
        self.ping(self.child, "fail")
        self.drain()
        flip = self.child.flip_set.get(new_status="down")
        self.assertEqual(flip.reason, "fail")
        self.assertEqual(flip.notification_state, "claimed")
        self.assertIsNone(self.child.last_success)
        self.assertEqual(
            DependencyGraph(self.project.id).describe(self.child)["blockers"], []
        )

    def test_all_ancestors_must_qualify(self) -> None:
        root = Check.objects.create(project=self.project, name="Root")
        set_dependencies(self.parent, parent=str(root.code))
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "waiting")
        info = DependencyGraph(self.project.id).describe(self.child)
        self.assertEqual([b["name"] for b in info["blockers"]], ["Root"])

    def test_parent_late_down_and_new_block(self) -> None:
        flip = self.timeout()
        for status in ("new", "down", "up"):
            with self.subTest(status=status):
                self.parent.status = status
                self.parent.last_ping = now() - self.parent.timeout - td(seconds=1)
                self.parent.last_success = now()
                self.parent.save()
                graph = DependencyGraph(self.project.id)
                self.assertTrue(graph.blockers(self.child, flip.grace_start))

    def test_paused_parent_does_not_require_a_success(self) -> None:
        self.parent.status = "paused"
        self.parent.save()
        flip = self.timeout()
        self.assertIsNone(self.parent.last_success)
        self.assertEqual(flip.notification_state, "claimed")
        graph = DependencyGraph(self.project.id)
        self.assertEqual(graph.describe(self.child)["blockers"], [])
        self.assertTrue(graph.permits_reminder(self.child))

    def test_paused_parent_with_old_success_does_not_block(self) -> None:
        self.ping(self.parent)
        self.parent.status = "paused"
        self.parent.save()
        self.assertEqual(self.timeout().notification_state, "claimed")

    def test_paused_parents_are_transparent_to_grandparent(self) -> None:
        root = Check.objects.create(project=self.project, name="Root")
        middle = Check.objects.create(
            project=self.project, parent=root, status="paused"
        )
        self.parent.parent = middle
        self.parent.status = "paused"
        self.parent.save()
        self.ping(root)
        flip = self.timeout()
        self.assertEqual(flip.notification_state, "waiting")
        info = DependencyGraph(self.project.id).describe(self.child)
        self.assertEqual([b["name"] for b in info["blockers"]], ["Root"])
        self.assertIn("before", info["blockers"][0]["reason"])
        self.assertEqual(len(info["ancestors"]), 3)
        self.ping(root)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "resuming")
        self.ping(root, "fail")
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "waiting")

    def test_chain_of_only_paused_ancestors_allows_timeout(self) -> None:
        root = Check.objects.create(project=self.project, status="paused")
        self.parent.parent = root
        self.parent.status = "paused"
        self.parent.save()
        self.assertEqual(self.timeout().notification_state, "claimed")

    def test_pausing_parent_releases_waiting_incident_with_grace(self) -> None:
        flip = self.timeout()
        self.client.force_login(self.alice)
        response = self.client.post(f"/checks/{self.parent.code}/pause/")
        self.assertEqual(response.status_code, 302)
        flip.refresh_from_db()
        assert flip.next_evaluation is not None
        self.assertLessEqual(flip.next_evaluation, now())
        self.drain()
        flip.refresh_from_db()
        deadline = now() + self.child.grace
        self.assertEqual(flip.resume_after, deadline)
        with time_machine.travel(deadline, tick=False):
            self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "claimed")

    def test_pausing_parent_does_not_extend_recovery_grace(self) -> None:
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        flip.refresh_from_db()
        deadline = flip.resume_after
        with time_machine.travel(now() + td(seconds=30), tick=False):
            self.client.post(
                f"/api/v3/checks/{self.parent.code}/pause", HTTP_X_API_KEY="X" * 32
            )
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.notification_state, "resuming")
            self.assertEqual(flip.resume_after, deadline)
        assert deadline is not None
        with time_machine.travel(deadline, tick=False):
            self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "claimed")

    def test_resuming_paused_parent_reinstates_its_requirements(self) -> None:
        flip = self.timeout()
        self.parent.status = "paused"
        self.parent.save()
        self.drain()
        self.client.force_login(self.alice)
        self.client.post(f"/checks/{self.parent.code}/resume/")
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "waiting")
        self.assertIsNone(flip.resume_after)
        with time_machine.travel(now() + td(seconds=30), tick=False):
            self.ping(self.parent)
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, now() + self.child.grace)

    def test_paused_parent_does_not_hide_grandparent_from_reminders(self) -> None:
        root = Check.objects.create(project=self.project, name="Root")
        self.parent.parent = root
        self.parent.status = "paused"
        self.parent.save()
        self.ping(self.child, "fail")
        self.drain()
        self.assertFalse(DependencyGraph(self.project.id).permits_reminder(self.child))
        self.ping(root)
        self.assertTrue(DependencyGraph(self.project.id).permits_reminder(self.child))

    def test_api_and_refresh_skip_paused_parent_but_keep_grandparent(self) -> None:
        root = Check.objects.create(project=self.project, name="Root")
        self.parent.parent = root
        self.parent.status = "paused"
        self.parent.save()
        self.timeout()
        response = self.client.get(
            f"/api/v3/checks/{self.child.code}", HTTP_X_API_KEY="X" * 32
        )
        dependency = response.json()["dependency"]
        self.assertEqual(dependency["parent"]["status"], "paused")
        self.assertEqual([b["name"] for b in dependency["blockers"]], ["Root"])
        self.client.force_login(self.alice)
        response = self.client.get(f"/checks/{self.child.code}/status/")
        self.assertEqual(response.json()["dependency"], dependency)

    def test_pausing_child_cancels_pending(self) -> None:
        flip = self.timeout()
        self.client.force_login(self.alice)
        self.client.post(f"/checks/{self.child.code}/pause/")
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "cancelled")

    def test_ignored_success_does_not_update_last_success(self) -> None:
        self.parent.status = "paused"
        self.parent.manual_resume = True
        self.parent.save()
        self.ping(self.parent)
        self.assertIsNone(self.parent.last_success)
        self.assertIsNone(self.parent.up_since)

    def test_detachment_releases_with_a_new_grace(self) -> None:
        flip = self.timeout()
        set_dependencies(self.child, parent=None)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.notification_state, "resuming")
        self.assertEqual(flip.resume_after, now() + self.child.grace)

    def test_bulk_children_validation_is_atomic(self) -> None:
        other = Check.objects.create(project=self.project)
        with self.assertRaises(ValidationError):
            set_dependencies(
                self.child, children=[str(other.code), str(self.parent.code)]
            )
        other.refresh_from_db()
        self.assertIsNone(other.parent_id)
        foreign = Check.objects.create(project=self.bobs_project)
        with self.assertRaises(ValidationError):
            set_dependencies(self.child, parent=str(foreign.code))
        with self.assertRaises(ValidationError):
            set_dependencies(self.child, parent=str(self.child.code))

    def test_deletion_and_transfer_detach_only_direct_children(self) -> None:
        leaf = Check.objects.create(project=self.project, parent=self.child)
        self.parent.rename_and_delete()
        self.child.refresh_from_db()
        leaf.refresh_from_db()
        self.assertIsNone(self.child.parent_id)
        self.assertEqual(leaf.parent_id, self.child.id)
        self.child.project = self.bobs_project
        self.child.save(update_fields=("project",))
        leaf.refresh_from_db()
        self.assertIsNone(leaf.parent_id)
        self.assertEqual(leaf.project_id, self.project.id)

    def test_deletion_does_not_follow_a_concurrent_transfer(self) -> None:
        stale = Check.objects.get(pk=self.child.pk)
        self.child.project = self.bobs_project
        self.child.save(update_fields=("project",))
        stale.rename_and_delete()
        self.assertTrue(Check.objects.filter(pk=self.child.pk).exists())

    def test_clear_history_resets_success_and_incident(self) -> None:
        self.timeout()
        self.client.force_login(self.alice)
        self.client.post(f"/checks/{self.child.code}/clear_events/")
        self.child.refresh_from_db()
        self.assertIsNone(self.child.last_success)
        self.assertIsNone(self.child.up_since)
        self.assertFalse(self.child.flip_set.exists())

    def test_schedule_and_start_preserve_original_grace_start(self) -> None:
        for kind, schedule in (
            ("simple", ""),
            ("cron", "*/5 * * * *"),
            ("oncalendar", "*-*-* *:0/5:00"),
        ):
            with self.subTest(kind=kind):
                check = Check.objects.create(
                    project=self.project,
                    parent=self.parent,
                    kind=kind,
                    schedule=schedule,
                    grace=td(minutes=1),
                )
                self.ping(check)
                start = check.get_grace_start()
                assert start is not None
                with time_machine.travel(
                    start + check.grace + td(seconds=1), tick=False
                ):
                    self.command.handle_going_down()
                self.assertEqual(
                    check.flip_set.get(new_status="down").grace_start, start
                )
        started = Check.objects.create(
            project=self.project, parent=self.parent, grace=td(minutes=1)
        )
        self.ping(started, "start")
        with time_machine.travel(now() + td(minutes=2), tick=False):
            self.command.handle_going_down()
        self.assertEqual(
            started.flip_set.get(new_status="down").grace_start, started.last_start
        )

    def test_v3_api_parent_validation_and_readonly_masking(self) -> None:
        url = f"/api/v3/checks/{self.child.code}"
        response = self.client.post(
            url,
            {"parent": None},
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["parent"])
        response = self.client.post(
            url,
            {"parent": str(self.parent.code)},
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(response.json()["parent"], str(self.parent.code))
        response = self.client.post(
            url,
            {"name": "Changed"},
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(response.json()["parent"], str(self.parent.code))
        response = self.client.post(
            url,
            {"parent": str(self.child.code)},
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(response.status_code, 400)
        doc = self.child.to_dict(readonly=True)
        self.assertEqual(doc["parent"], self.parent.unique_key)
        self.assertNotIn(str(self.parent.code), str(doc))
        self.assertNotIn("parent", self.child.to_dict(v=2))
        self.assertNotIn("dependency", self.child.to_dict(v=1))

    def test_refresh_includes_dependencies_without_health_change(self) -> None:
        self.timeout()
        self.client.force_login(self.alice)
        url = f"/checks/{self.child.code}/status/"
        first = self.client.get(url).json()
        self.ping(self.parent)
        self.drain()
        second = self.client.get(
            url, {"u": first["updated"], "d": first["dependency_updated"]}
        ).json()
        self.assertEqual(first["status"], second["status"])
        self.assertNotEqual(first["dependency_updated"], second["dependency_updated"])
        self.assertEqual(second["dependency"]["state"], "resuming")
        self.assertIn("events", second)

    def test_hierarchy_changes_between_polls_restart_only_affected_grace(self) -> None:
        flip = self.timeout()
        self.ping(self.parent)
        self.drain()
        flip.refresh_from_db()
        original_deadline = flip.resume_after
        with time_machine.travel(now() + td(seconds=30), tick=False):
            unrelated = Check.objects.create(project=self.project)
            set_dependencies(unrelated, parent=str(self.parent.code))
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, original_deadline)
            set_dependencies(self.child, parent=None)
            set_dependencies(self.child, parent=str(self.parent.code))
            self.drain()
            flip.refresh_from_db()
            self.assertEqual(flip.resume_after, now() + self.child.grace)

    def test_incident_grace_is_not_changed_by_schedule_edits(self) -> None:
        flip = self.timeout()
        self.child.grace = td(hours=1)
        self.child.save()
        self.ping(self.parent)
        self.drain()
        flip.refresh_from_db()
        self.assertEqual(flip.resume_after, now() + td(minutes=2))

    def test_pending_incident_survives_routine_pruning(self) -> None:
        flip = self.timeout()
        flip.created = now() - td(days=100)
        flip.save()
        self.child.n_pings = 101
        self.child.save()
        Ping.objects.create(owner=self.child, n=101, created=now())
        self.child.prune()
        self.assertTrue(Flip.objects.filter(pk=flip.pk).exists())

    def test_copy_keeps_parent_without_incident_or_children(self) -> None:
        self.timeout()
        Check.objects.create(project=self.project, parent=self.child)
        self.client.force_login(self.alice)
        response = self.client.post(f"/checks/{self.child.code}/copy/")
        self.assertEqual(response.status_code, 302)
        copied = Check.objects.get(name="Job (copy)")
        self.assertEqual(copied.parent_id, self.parent.id)
        self.assertFalse(copied.children.exists())
        self.assertFalse(copied.flip_set.exists())
        self.assertIsNone(copied.last_success)

    def test_api_creation_and_validation_are_atomic(self) -> None:
        response = self.client.post(
            "/api/v3/checks/",
            {"name": "Created", "parent": str(self.parent.code)},
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["parent"], str(self.parent.code))
        foreign = Check.objects.create(project=self.bobs_project)
        response = self.client.post(
            f"/api/v3/checks/{self.child.code}",
            {"parent": str(foreign.code), "name": "Must not be saved"},
            content_type="application/json",
            HTTP_X_API_KEY="X" * 32,
        )
        self.assertEqual(response.status_code, 400)
        self.child.refresh_from_db()
        self.assertEqual(self.child.name, "Job")

    def test_readonly_api_masks_all_ancestor_ids(self) -> None:
        self.timeout()
        self.project.api_key_readonly = "R" * 32
        self.project.save()
        response = self.client.get("/api/v3/checks/", HTTP_X_API_KEY="R" * 32)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, str(self.parent.code))
        self.assertNotContains(response, str(self.child.code))
        self.assertContains(response, self.parent.unique_key)

    def test_dependencies_require_write_access_and_post(self) -> None:
        from hc.accounts.models import Member

        self.bobs_membership.role = Member.Role.READONLY
        self.bobs_membership.save()
        self.client.force_login(self.bob)
        url = f"/checks/{self.child.code}/dependencies/"
        self.assertEqual(self.client.post(url, {"parent": ""}).status_code, 403)
        self.client.force_login(self.charlie)
        self.assertEqual(self.client.post(url, {"parent": ""}).status_code, 404)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, {"parent": ""}).status_code, 302)

    def test_children_endpoint_replaces_selection_and_preserves_descendants(
        self,
    ) -> None:
        other = Check.objects.create(project=self.project)
        leaf = Check.objects.create(project=self.project, parent=self.child)
        self.client.force_login(self.alice)
        response = self.client.post(
            f"/checks/{self.parent.code}/dependencies/",
            {"operation": "children", "children": [str(other.code)]},
        )
        self.assertEqual(response.status_code, 302)
        self.child.refresh_from_db()
        other.refresh_from_db()
        leaf.refresh_from_db()
        self.assertIsNone(self.child.parent_id)
        self.assertEqual(other.parent_id, self.parent.id)
        self.assertEqual(leaf.parent_id, self.child.id)

    def test_nag_is_silent_during_pending_timeout(self) -> None:
        self.timeout()
        self.profile.nag_period = td(hours=1)
        self.profile.save()
        self.assertFalse(self.profile.send_report(nag=True))

    def test_dst_grace_start_is_captured_in_utc(self) -> None:
        with time_machine.travel("2026-10-25 00:55:00Z", tick=False):
            self.child.kind = "cron"
            self.child.schedule = "15 * * * *"
            self.child.tz = "Europe/Paris"
            self.child.save()
            self.ping(self.child)
            start = self.child.get_grace_start()
            assert start is not None
        with time_machine.travel(start + self.child.grace + td(seconds=1), tick=False):
            self.command.handle_going_down()
        self.assertEqual(self.child.flip_set.get(new_status="down").grace_start, start)
