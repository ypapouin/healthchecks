from __future__ import annotations

import logging
import signal
import time
from argparse import ArgumentParser
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta as td
from threading import BoundedSemaphore
from types import FrameType
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection
from django.utils.timezone import now

from hc.api.dependencies import (
    DependencyGraph,
    evaluate,
    locked_check,
    update_reminders,
)
from hc.api.models import Check, Flip
from hc.lib.statsd import statsd

logger = logging.getLogger("hc")


def notify(flip: Flip) -> str | None:
    # This is run via ThreadPoolExecutor. The thread may already have an open
    # db connection. If notify has not run recently then the db connection may have
    # timed out. We call close_old_connections() to make sure we have a working db
    # connection. The if condition makes sure this does not run during tests.
    if not connection.in_atomic_block:
        close_old_connections()

    # Set or clear dates for followup nags
    check = flip.owner
    update_reminders({check.pk})
    channels = flip.select_channels()
    if not channels:
        return None

    send_start = now()
    logs = [f"{check.code} goes {flip.new_status}"]
    for ch in channels:
        notify_start = time.time()
        error = ch.notify(flip)
        secs = time.time() - notify_start
        code8 = str(ch.code)[:8]
        if error:
            logs.append(f"  {code8} ({ch.kind}) Error in {secs:.1f}s: {error}")
            statsd.incr(f"hc.notifications.{ch.kind}.fail")
        else:
            logs.append(f"  {code8} ({ch.kind}) OK in {secs:.1f}s")
            statsd.incr(f"hc.notifications.{ch.kind}.success")

    statsd.timing("hc.sendalerts.dwellTime", send_start - flip.created)
    statsd.timing("hc.sendalerts.sendTime", now() - send_start)
    return "\n".join(logs)


class Command(BaseCommand):
    help = "Sends UP/DOWN email alerts"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.seats = BoundedSemaphore(10)
        self.shutdown = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--num-workers",
            type=int,
            default=1,
            help="The number of concurrent worker processes to use",
        )

        parser.add_argument(
            "--pool",
            action="store_true",
            help="Use DB connection pool (PostgreSQL-only)",
        )

    def on_notify_done(self, future: Future[str | None]) -> None:
        self.seats.release()

        try:
            if logs := future.result():
                self.stdout.write(logs)
        except Exception as exc:
            logger.error("Exception in notify", exc_info=exc)
            raise

    def process_one_flip(self) -> bool:
        """Find unprocessed flip, send notifications.

        Return True if the main loop should continue right away.

        Return False if the main loop should  wait a bit before continuing.
        (because either all workers are currently busy or there are currently no
        unprocessed flips in the database).

        """

        if not self.seats.acquire(timeout=1):
            return False  # Workers busy, main thread should wait a bit

        flip = (
            Flip.objects.filter(processed=None, next_evaluation__lte=now())
            .order_by("next_evaluation", "id")
            .first()
        )
        if flip is None:
            self.seats.release()
            return False

        try:
            with locked_check(flip.owner) as check:
                flip = Flip.objects.get(pk=flip.pk)
                if (
                    flip.processed is not None
                    or flip.next_evaluation is None
                    or flip.next_evaluation > now()
                ):
                    self.seats.release()
                    return True
                flip.owner = check
                if not evaluate(flip, DependencyGraph(check.project_id)):
                    self.seats.release()
                    return True
                # Claim under the same lock used by pings and hierarchy edits.
                # Recovery after this point remains notifiable. Network I/O is
                # outside this transaction, retaining existing at-most-once delivery.
                flip.processed = now()
                flip.notification_state = "claimed"
                flip.next_evaluation = None
                flip.save(
                    update_fields=("processed", "notification_state", "next_evaluation")
                )
        except (Check.DoesNotExist, Flip.DoesNotExist):
            self.seats.release()
            return True
        except Exception:
            self.seats.release()
            raise

        statsd.incr("hc.sendalerts.processFlip")
        f = self.executor.submit(notify, flip)
        f.add_done_callback(self.on_notify_done)
        return True

    def handle_going_down(self) -> bool:
        """Process a single check going down.

        1. Find a check with alert_after in the past, and status other than "down".
        2. Calculate its current status.
        3. If calculation throws an exception, push alert_after forward and re-raise.
        4. If the current status is not "down", update alert_after and return.
        5. Update the check's status in the database to "down".
        6. If exactly 1 row gets updated, create a Flip object.

        """

        q = Check.objects.filter(alert_after__lt=now()).exclude(status="down")
        # Sort by alert_after, to avoid unnecessary sorting by id:
        check = q.order_by("alert_after").first()
        if check is None:
            return False

        with locked_check(check) as check:
            if (
                check.status == "down"
                or check.alert_after is None
                or check.alert_after >= now()
            ):
                return True
            old_status = check.status
            try:
                status = check.get_status()
            except Exception:
                logger.exception("Cannot calculate status for %s", check.code)
                Check.objects.filter(pk=check.pk).update(
                    alert_after=now() + td(hours=1)
                )
                return True
            if status != "down":
                Check.objects.filter(pk=check.pk).update(
                    alert_after=check.going_down_after()
                )
                return True

            grace_start = check.get_grace_start()
            flip_time = check.going_down_after()
            assert flip_time is not None
            Check.objects.filter(pk=check.pk).update(
                alert_after=None, status="down", up_since=None
            )
            check.status = "down"
            flip = Flip.objects.create(
                owner=check,
                created=flip_time,
                old_status=old_status,
                new_status="down",
                reason="timeout",
                grace_start=grace_start,
                incident_grace=check.grace,
            )
            evaluate(flip, DependencyGraph(check.project_id))

        return True

    def on_signal(self, signum: int, frame: FrameType | None) -> None:
        desc = signal.strsignal(signum)
        self.stdout.write(f"{desc}, finishing...\n")
        self.shutdown = True

    def handle(self, num_workers: int, pool: bool, **options: Any) -> str:
        db = settings.DATABASES["default"]
        if "OPTIONS" in db and "application_name" in db["OPTIONS"]:
            db["OPTIONS"]["application_name"] = "sendalerts"

        if pool:
            self.stdout.write(
                "WARNING: The --pool argument is not supported any more "
                "and will be ignored.\n"
            )

        self.seats = BoundedSemaphore(num_workers)
        self.executor = ThreadPoolExecutor(max_workers=num_workers)

        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)

        self.stdout.write("sendalerts is now running\n")
        while not self.shutdown:
            # Create flips for any checks going down
            for _ in range(100):
                if self.shutdown or not self.handle_going_down():
                    break

            # Submit unprocessed flips to the self.executor
            for _ in range(100):
                if self.shutdown or not self.process_one_flip():
                    break

            # Either all workers are busy or there are no unprocessed flips.
            # Wait a bit:
            if not self.shutdown:
                time.sleep(2)

        self.executor.shutdown(wait=True)
        return "Done."
