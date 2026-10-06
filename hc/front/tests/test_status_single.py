from __future__ import annotations

from datetime import timedelta as td

from django.utils.timezone import now

from hc.api.models import Channel, Check, Notification, Ping
from hc.test import BaseTestCase


class StatusSingleTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.check = Check(project=self.project, name="Alice Was Here")
        self.check.save()

        self.url = f"/checks/{self.check.code}/status/"

    def test_it_works(self) -> None:
        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        doc = r.json()

        self.assertEqual(doc["status"], "new")
        self.assertIn("never received a ping", doc["status_text"])
        self.assertIn("not received any pings yet", doc["events"])

    def test_status_text_shows_elapsed_run_time(self) -> None:
        Ping.objects.create(owner=self.check, n=1, kind="start")
        self.check.status = "new"
        self.check.n_pings = 1
        self.check.last_start = now() - td(minutes=1)
        self.check.save()

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        doc = r.json()

        self.assertEqual(doc["status"], "new")
        self.assertIn("This check is ready for pings.", doc["status_text"])
        self.assertIn("Currently running, started 1 min", doc["status_text"])

    def test_it_returns_403_for_anon_requests(self) -> None:
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 403)

    def test_it_returns_events(self) -> None:
        p = Ping.objects.create(owner=self.check, ua="test-user-agent", n=1)
        self.check.status = "up"
        self.check.last_ping = p.created
        self.check.save()

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        doc = r.json()

        self.assertEqual(doc["status"], "up")
        self.assertEqual(doc["updated"], str(p.created.timestamp()))
        self.assertIn("test-user-agent", doc["events"])

    def test_it_omits_events(self) -> None:
        p = Ping.objects.create(owner=self.check, ua="test-user-agent", n=1)
        self.check.status = "up"
        self.check.last_ping = p.created
        self.check.save()

        timestamp = str(p.created.timestamp())
        url = self.url + f"?u={timestamp}"

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(url)
        doc = r.json()

        self.assertNotIn("events", doc)

    def test_last_notification_updates_without_a_health_change(self) -> None:
        self.check.status = "down"
        self.check.save()
        channel = Channel.objects.create(project=self.project, kind="email")
        Notification.objects.create(
            owner=self.check,
            channel=channel,
            check_status="down",
            created=now() - td(hours=1),
        )
        self.check.create_flip("down", mark_as_processed=True)
        self.client.force_login(self.alice)
        first = self.client.get(self.url).json()
        # A previous incident's notification must not stand in for a new claim.
        self.assertIsNone(first["last_notification"])

        for _ in range(2):
            notification = Notification.objects.create(
                owner=self.check, channel=channel, check_status="down"
            )
        # Neither a recovery nor another check's alert is this incident's alert.
        Notification.objects.create(
            owner=self.check, channel=channel, check_status="up"
        )
        other = Check.objects.create(project=self.project)
        Notification.objects.create(owner=other, channel=channel, check_status="down")

        second = self.client.get(
            self.url, {"u": first["updated"], "d": first["dependency_updated"]}
        ).json()
        self.assertEqual(second["status"], first["status"])
        self.assertNotIn("events", second)
        self.assertEqual(second["last_notification"], notification.created.isoformat())

    def test_last_notification_is_only_available_for_claimed_down_incidents(self) -> None:
        self.check.status = "down"
        self.check.save()
        self.check.create_flip("down", mark_as_processed=True)
        channel = Channel.objects.create(project=self.project, kind="email")
        Notification.objects.create(
            owner=self.check, channel=channel, check_status="down"
        )
        self.client.force_login(self.alice)
        for state in ("waiting", "resuming", "cancelled"):
            with self.subTest(state=state):
                self.check.flip_set.update(notification_state=state)
                self.assertIsNone(self.client.get(self.url).json()["last_notification"])

        self.check.flip_set.update(notification_state="claimed")
        self.check.status = "up"
        self.check.last_ping = now()
        self.check.save()
        self.assertIsNone(self.client.get(self.url).json()["last_notification"])

    def test_it_allows_cross_team_access(self) -> None:
        self.client.login(username="bob@example.org", password="password")
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)

    def test_it_handles_manual_resume(self) -> None:
        self.check.status = "paused"
        self.check.manual_resume = True
        self.check.save()

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        doc = r.json()

        self.assertEqual(doc["status"], "paused")
        self.assertIn("will ignore pings until resumed", doc["status_text"])
        self.assertIn("resume-btn", doc["status_text"])

    def test_resume_requires_rw_access(self) -> None:
        self.bobs_membership.role = "r"
        self.bobs_membership.save()

        self.check.status = "paused"
        self.check.manual_resume = True
        self.check.save()

        self.client.login(username="bob@example.org", password="password")
        r = self.client.get(self.url)
        doc = r.json()

        self.assertEqual(doc["status"], "paused")
        self.assertIn("will ignore pings until resumed", doc["status_text"])
        self.assertNotIn("resume-btn", doc["status_text"])

    def test_it_shows_ignored_nonzero_exitstatus(self) -> None:
        p = Ping(owner=self.check)
        p.n = 1
        p.kind = "ign"
        p.exitstatus = 123
        p.save()

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        doc = r.json()

        self.assertIn("Ignored", doc["events"])

    def test_it_handles_log_event(self) -> None:
        p = Ping.objects.create(owner=self.check, kind="log", n=1)
        self.check.status = "up"
        self.check.last_ping = p.created
        self.check.save()

        self.client.login(username="alice@example.org", password="password")
        r = self.client.get(self.url)
        doc = r.json()

        self.assertEqual(doc["status"], "up")
        self.assertEqual(doc["updated"], str(p.created.timestamp()))
        self.assertIn("label-log", doc["events"])
