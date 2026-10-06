from __future__ import annotations

from datetime import timedelta as td
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.timezone import now

from hc.accounts.models import Member, Project
from hc.api.dependencies import (
    DependencyGraph,
    connected_projects,
    delete_projects,
    delete_users,
    deleting_checks,
    locked_projects,
    set_dependencies,
    set_sharing,
)
from hc.api.models import Check, Flip
from hc.test import BaseTestCase


class SharedDependenciesTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = Check.objects.create(
            project=self.project, name="Shared host", shared=True
        )
        self.child = Check.objects.create(
            project=self.bobs_project, name="External child"
        )
        self.ref = f"shared:{self.parent.dependency_id}"
        self.bobs_project.api_key = "B" * 32
        self.bobs_project.api_key_readonly = "R" * 32
        self.bobs_project.save()
        self.url = f"/checks/{self.parent.code}/dependencies/"

    def attach(self) -> None:
        set_dependencies(self.child, parent=self.ref)

    def pending(self, check: Check, state: str = "waiting") -> Flip:
        check.status = "down"
        check.save()
        return Flip.objects.create(
            owner=check,
            created=now(),
            old_status="up",
            new_status="down",
            reason="timeout",
            grace_start=now() - td(minutes=5),
            incident_grace=td(minutes=1),
            notification_state=state,
            resume_after=now() + td(minutes=1) if state == "resuming" else None,
        )

    def test_defaults_and_independent_ids(self) -> None:
        self.assertFalse(self.child.shared)
        self.assertNotEqual(self.parent.dependency_id, self.parent.code)
        self.assertNotEqual(self.parent.dependency_id, self.child.dependency_id)

    def test_shared_parent_can_be_selected_without_membership(self) -> None:
        self.bobs_membership.delete()
        self.client.force_login(self.bob)
        response = self.client.post(
            f"/checks/{self.child.code}/dependencies/", {"parent": self.ref}
        )
        self.assertEqual(response.status_code, 302)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.parent.id)
        self.assertEqual(
            self.client.get(self.parent.details_url(False)).status_code, 404
        )

    def test_unshared_parent_and_external_ping_uuid_are_rejected(self) -> None:
        for value in (str(self.parent.code), "shared:invalid"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                set_dependencies(self.child, parent=value)
        self.parent.shared = False
        self.parent.save(update_fields=("shared",))
        with self.assertRaises(ValidationError):
            self.attach()
        # Owning both projects does not bypass the opt-in.
        self.bobs_project.owner = self.alice
        self.bobs_project.save()
        with self.assertRaises(ValidationError):
            self.attach()

    def test_sharing_permissions(self) -> None:
        self.client.force_login(self.bob)
        self.assertEqual(
            self.client.post(self.url, {"operation": "sharing"}).status_code, 403
        )
        self.bobs_membership.role = Member.Role.READONLY
        self.bobs_membership.save()
        self.assertEqual(
            self.client.post(self.url, {"operation": "sharing"}).status_code, 403
        )
        response = self.client.get(self.parent.details_url(False))
        self.assertFalse(response.context["can_share"])
        self.client.force_login(self.charlie)
        self.assertEqual(
            self.client.post(self.url, {"operation": "sharing"}).status_code, 404
        )
        for role in ("owner", "manager", "superuser"):
            with self.subTest(role=role):
                if role == "owner":
                    self.client.force_login(self.alice)
                elif role == "manager":
                    self.bobs_membership.role = Member.Role.MANAGER
                    self.bobs_membership.save()
                    self.client.force_login(self.bob)
                else:
                    self.charlie.is_superuser = True
                    self.charlie.save()
                    self.client.force_login(self.charlie)
                self.assertEqual(
                    self.client.post(self.url, {"operation": "sharing"}).status_code,
                    302,
                )
                self.parent.refresh_from_db()
                self.assertFalse(self.parent.shared)
                self.assertEqual(
                    self.client.post(
                        self.url, {"operation": "sharing", "shared": "on"}
                    ).status_code,
                    302,
                )

    def test_local_children_editor_preserves_foreign_children(self) -> None:
        self.attach()
        local = Check.objects.create(project=self.project, parent=self.parent)
        set_dependencies(self.parent, children=[])
        local.refresh_from_db()
        self.child.refresh_from_db()
        self.assertIsNone(local.parent_id)
        self.assertEqual(self.child.parent_id, self.parent.id)
        with self.assertRaises(ValidationError):
            set_dependencies(
                self.parent, children=[str(local.code), str(self.child.code)]
            )
        local.refresh_from_db()
        self.assertIsNone(local.parent_id)

    def test_external_children_have_links_only_with_access(self) -> None:
        self.attach()
        self.client.force_login(self.alice)
        response = self.client.get(self.parent.details_url(False))
        child = response.context["children"][0]
        self.assertEqual(child["name"], self.child.name)
        self.assertEqual(child["project"], "Unnamed project")
        self.assertIsNone(child["url"])
        self.assertNotContains(response, str(self.child.code))
        Member.objects.create(
            project=self.bobs_project, user=self.alice, role=Member.Role.READONLY
        )
        response = self.client.get(self.parent.details_url(False))
        self.assertEqual(
            response.context["children"][0]["url"],
            reverse("hc-uncloak", args=[self.child.unique_key]),
        )
        # Even with access, the child is not an editable option.
        self.assertNotContains(response, f'value="{self.child.code}"')

    def test_unnamed_child_project_has_safe_label_in_html_and_updates(self) -> None:
        self.attach()
        self.client.force_login(self.alice)
        response = self.client.get(self.parent.details_url(False))
        self.assertContains(response, "Unnamed project — External child")
        self.assertNotContains(response, self.bob.email)
        response = self.client.get(f"/checks/{self.parent.code}/status/")
        self.assertEqual(response.json()["children"][0]["project"], "Unnamed project")
        self.assertNotContains(response, self.bob.email)

    def test_unnamed_parent_project_has_safe_label_in_ui_and_api(self) -> None:
        self.project.name = ""
        self.project.save(update_fields=("name",))
        self.attach()
        self.bobs_membership.delete()
        self.client.force_login(self.bob)
        response = self.client.get(self.child.details_url(False))
        label = "Unnamed project — Shared host"
        self.assertContains(response, label)
        self.assertNotContains(response, self.alice.email)
        option = next(
            c for c in response.context["parent_options"] if c["value"] == self.ref
        )
        self.assertEqual(option["label"], label)
        for path, headers in (
            (f"/checks/{self.child.code}/status/", {}),
            (f"/api/v3/checks/{self.child.code}", {"X-Api-Key": "B" * 32}),
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=headers)
                parent = response.json()["dependency"]["parent"]
                self.assertEqual(parent["project"], "Unnamed project")
                self.assertEqual(parent["label"], label)
                self.assertNotContains(response, self.alice.email)
        response = self.client.get("/api/v3/shared-checks/", HTTP_X_API_KEY="B" * 32)
        self.assertEqual(response.json()["checks"][0]["project"], "Unnamed project")
        self.assertNotContains(response, self.alice.email)

    def test_superuser_can_follow_external_child_link(self) -> None:
        self.attach()
        self.charlie.is_superuser = True
        self.charlie.save()
        self.client.force_login(self.charlie)
        response = self.client.get(self.parent.details_url(False))
        url = response.context["children"][0]["url"]
        self.assertRedirects(self.client.get(url), self.child.details_url(False))

    def test_revocation_detaches_only_external_children_and_restarts_grace(
        self,
    ) -> None:
        self.attach()
        local = Check.objects.create(project=self.project, parent=self.parent)
        self.child.shared = True
        self.child.save(update_fields=("shared",))
        leaf = Check.objects.create(project=self.charlies_project, parent=self.child)
        child_flip = self.pending(self.child, "resuming")
        leaf_flip = self.pending(leaf, "resuming")
        set_sharing(self.parent, False, self.alice)
        self.child.refresh_from_db()
        local.refresh_from_db()
        leaf.refresh_from_db()
        self.assertIsNone(self.child.parent_id)
        self.assertEqual(local.parent_id, self.parent.id)
        self.assertEqual(leaf.parent_id, self.child.id)
        for flip in (child_flip, leaf_flip):
            flip.refresh_from_db()
            self.assertEqual(flip.notification_state, "waiting")
            self.assertIsNone(flip.resume_after)

    def test_cross_project_cycles_are_atomic(self) -> None:
        self.attach()
        self.child.shared = True
        self.child.save(update_fields=("shared",))
        third = Check.objects.create(
            project=self.charlies_project, shared=True, parent=self.child
        )
        with self.assertRaises(ValidationError):
            set_dependencies(self.parent, parent=f"shared:{third.dependency_id}")
        self.parent.refresh_from_db()
        self.assertIsNone(self.parent.parent_id)

    def test_private_ancestors_are_evaluated_but_anonymous_in_html_and_json(
        self,
    ) -> None:
        root = Check.objects.create(project=self.project, name="SECRET-ANCESTOR")
        self.parent.parent = root
        self.parent.status = "paused"
        self.parent.save()
        self.attach()
        self.pending(self.child)
        self.bobs_membership.delete()
        self.client.force_login(self.bob)
        for path in (
            self.child.details_url(False),
            f"/checks/{self.child.code}/status/",
            f"/projects/{self.bobs_project.code}/checks/",
            f"/projects/{self.bobs_project.code}/checks/status/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                for secret in (
                    root.name,
                    str(root.code),
                    str(root.dependency_id),
                    str(self.parent.code),
                ):
                    self.assertNotContains(response, secret)
                self.assertContains(response, "Private dependency")
        graph = DependencyGraph(self.child.project_id)
        self.assertEqual(graph.blockers(self.child, now())[0]["check"].pk, root.pk)
        doc = graph.describe(self.child)
        self.assertEqual(len(doc["blockers"]), 1)
        self.assertIsNone(doc["blockers"][0]["id"])
        self.assertIsNone(doc["blockers"][0]["last_success"])
        self.assertFalse(graph.permits_reminder(self.child))

    def test_members_can_see_private_ancestors_without_leaking_external_ping_ids(
        self,
    ) -> None:
        root = Check.objects.create(project=self.project, name="Private root")
        self.parent.parent = root
        self.parent.save()
        self.attach()
        self.client.force_login(self.bob)
        url = f"/checks/{self.child.code}/status/"
        response = self.client.get(url)
        self.assertContains(response, root.name)
        self.assertNotContains(response, str(root.code))
        self.assertIsNotNone(response.json()["dependency"]["ancestors"][0]["url"])
        self.bobs_membership.delete()
        response = self.client.get(url)
        self.assertNotContains(response, root.name)
        self.assertIsNone(response.json()["dependency"]["ancestors"][0]["url"])

    def test_api_shared_parent_and_readonly_references(self) -> None:
        url = f"/api/v3/checks/{self.child.code}"
        response = self.client.post(
            url,
            {"parent": self.ref},
            content_type="application/json",
            HTTP_X_API_KEY="B" * 32,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["parent"], self.ref)
        for key in ("B" * 32, "R" * 32):
            response = self.client.get(url, HTTP_X_API_KEY=key)
            self.assertEqual(response.json()["parent"], self.ref)
            self.assertNotContains(response, str(self.parent.code))
            self.assertIsNone(response.json()["dependency"]["parent"]["url"])
        self.assertEqual(
            self.client.get(
                f"/api/v3/checks/{self.parent.code}", HTTP_X_API_KEY="B" * 32
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f"/ping/{self.parent.dependency_id}").status_code, 404
        )

    def test_api_cannot_change_sharing(self) -> None:
        for path in (f"/api/v3/checks/{self.child.code}", "/api/v3/checks/"):
            for field in ("shared", "dependency_id"):
                with self.subTest(path=path, field=field):
                    response = self.client.post(
                        path,
                        {field: True, "name": "Not applied"},
                        content_type="application/json",
                        HTTP_X_API_KEY="B" * 32,
                    )
                    self.assertEqual(response.status_code, 400)
        self.child.refresh_from_db()
        self.assertEqual(self.child.name, "External child")
        self.assertFalse(self.child.shared)

    def test_catalog_authentication_safe_fields_search_and_pagination(self) -> None:
        path = "/api/v3/shared-checks/"
        self.assertEqual(self.client.get(path).status_code, 401)
        response = self.client.get(path, HTTP_X_API_KEY="R" * 32)
        item = response.json()["checks"][0]
        self.assertEqual(set(item), {"id", "name", "project", "status", "last_success"})
        self.assertEqual(item["id"], self.ref)
        self.assertNotContains(response, str(self.parent.code))
        self.assertNotContains(response, self.child.name)
        self.assertEqual(
            self.client.get(path, {"q": "missing"}, HTTP_X_API_KEY="R" * 32).json()[
                "checks"
            ],
            [],
        )
        for page in ("bad", "0", "-1", "999"):
            self.assertEqual(
                self.client.get(
                    path, {"page": page}, HTTP_X_API_KEY="R" * 32
                ).status_code,
                400,
            )
        Check.objects.bulk_create(
            [
                Check(project=self.project, name=f"Shared {i}", shared=True)
                for i in range(51)
            ]
        )
        response = self.client.get(path, HTTP_X_API_KEY="R" * 32)
        self.assertEqual(len(response.json()["checks"]), 50)
        self.assertEqual(response.json()["next_page"], 2)
        second = self.client.get(path, {"page": 2}, HTTP_X_API_KEY="R" * 32).json()
        self.assertEqual(len(second["checks"]), 2)
        self.assertIsNone(second["next_page"])

    def test_creating_check_with_shared_parent(self) -> None:
        response = self.client.post(
            "/api/v3/checks/",
            {"name": "API child", "parent": self.ref},
            content_type="application/json",
            HTTP_X_API_KEY="B" * 32,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["parent"], self.ref)
        self.client.force_login(self.bob)
        response = self.client.post(
            f"/projects/{self.bobs_project.code}/checks/add/",
            {
                "name": "Web child",
                "parent": self.ref,
                "kind": "simple",
                "tz": "UTC",
                "timeout": 60,
                "grace": 60,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Check.objects.get(name="Web child").parent_id, self.parent.id)

    def test_copy_is_unshared_and_transfer_revokes_sharing(self) -> None:
        self.attach()
        self.child.shared = True
        self.child.save(update_fields=("shared",))
        self.client.force_login(self.bob)
        self.assertEqual(
            self.client.post(f"/checks/{self.child.code}/copy/").status_code, 302
        )
        copy = Check.objects.get(name="External child (copy)")
        self.assertFalse(copy.shared)
        self.assertNotEqual(copy.dependency_id, self.child.dependency_id)
        self.assertEqual(copy.parent_id, self.parent.id)
        self.parent.project = self.charlies_project
        self.parent.save(update_fields=("project",))
        self.assertFalse(self.parent.shared)
        for check in (self.child, copy):
            check.refresh_from_db()
            self.assertIsNone(check.parent_id)

    def test_check_project_and_account_deletion_wake_external_children(self) -> None:
        for action in ("check", "project", "account"):
            with self.subTest(action=action):
                project = Project.objects.create(
                    owner=self.alice, badge_key=f"delete-{action}"
                )
                parent = Check.objects.create(project=project, shared=True)
                set_dependencies(self.child, parent=f"shared:{parent.dependency_id}")
                flip = self.pending(self.child, "resuming")
                if action == "check":
                    parent.rename_and_delete()
                elif action == "project":
                    delete_projects(Project.objects.filter(pk=project.pk))
                else:
                    delete_users(type(self.alice).objects.filter(pk=self.alice.pk))
                self.child.refresh_from_db()
                flip.refresh_from_db()
                self.assertIsNone(self.child.parent_id)
                self.assertEqual(flip.notification_state, "waiting")

    def test_bulk_deletion_retries_if_selected_check_was_transferred(self) -> None:
        first = True

        def discover(ids: set[int]) -> set[int]:
            nonlocal first
            component = connected_projects(ids)
            if first:
                first = False
                self.child.project = self.charlies_project
                self.child.save(update_fields=("project",))
            return component

        checks = Check.objects.filter(pk=self.child.pk)
        with patch("hc.api.dependencies.connected_projects", side_effect=discover):
            with deleting_checks(checks):
                # Nested locking refuses any project not in the outer scope.
                with locked_projects(self.charlies_project.pk):
                    checks.delete()
        self.assertFalse(checks.exists())
