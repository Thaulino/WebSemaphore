"""Smoke tests: run with .venv/bin/python -m unittest test_app -v"""

import os
import tempfile
import unittest

os.environ["SEMAPHORE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import app as app_module  # noqa: E402  (import after SEMAPHORE_DB is set)
import auth  # noqa: E402
import db  # noqa: E402


class SemaphoreTest(unittest.TestCase):
    def setUp(self):
        for path in (db.DB_PATH, db.DB_PATH + "-wal", db.DB_PATH + "-shm"):
            if os.path.exists(path):
                os.remove(path)
        db.init()
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def csrf(self, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            return session.get("csrf", "")

    def post(self, url, client=None, **data):
        client = client or self.client
        data["csrf"] = self.csrf(client)
        return client.post(url, data=data, follow_redirects=True)

    def sign_up(self, client, username, password="hunter2hunter2"):
        client.get("/register")
        return self.post(
            "/register",
            client=client,
            username=username,
            password=password,
            confirm=password,
        )

    def latest_claim_id(self):
        conn = db.connect()
        try:
            return conn.execute("SELECT MAX(id) AS id FROM claims").fetchone()["id"]
        finally:
            conn.close()

    def user_id(self, username):
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def test_login_is_required(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_register_login_logout(self):
        self.sign_up(self.client, "alice")
        self.assertEqual(self.client.get("/").status_code, 200)

        self.post("/logout")
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 302)

        self.client.get("/login")
        self.post("/login", username="alice", password="wrong-password")
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 302)

        self.client.get("/login")
        self.post("/login", username="alice", password="hunter2hunter2")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_post_without_csrf_is_rejected(self):
        self.sign_up(self.client, "alice")
        response = self.client.post("/logout", data={"csrf": "nope"})
        self.assertEqual(response.status_code, 400)

    def create_resource(self, client, name="lab-pi-03", capacity=1, labels="hardware rack-b"):
        self.post(
            "/resources/new",
            client=client,
            name=name,
            description="Pi with the CAN adapter",
            capacity=capacity,
            labels=labels,
        )
        conn = db.connect()
        try:
            return conn.execute(
                "SELECT id FROM resources WHERE name = ?", (name,)
            ).fetchone()["id"]
        finally:
            conn.close()

    def test_create_resource_with_description_and_labels(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)

        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'id="r{resource_id}"', page)
        self.assertIn("lab-pi-03", page)
        self.assertIn("Pi with the CAN adapter", page)
        self.assertIn("rack-b", page)

        listing = self.client.get("/?label=rack-b").get_data(as_text=True)
        self.assertIn("lab-pi-03", listing)
        self.assertNotIn("lab-pi-03", self.client.get("/?label=nope").get_data(as_text=True))

    def test_duplicate_name_is_refused(self):
        self.sign_up(self.client, "alice")
        self.create_resource(self.client)
        response = self.post(
            "/resources/new", name="lab-pi-03", description="", capacity=1, labels=""
        )
        self.assertIn("already exists", response.get_data(as_text=True))

    def test_semaphore_blocks_when_full(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        resource_id = self.create_resource(alice, capacity=1)

        self.post(f"/resources/{resource_id}/acquire", client=alice, note="flashing")
        blocked = self.post(f"/resources/{resource_id}/acquire", client=bob, note="tests")
        self.assertIn("fully occupied", blocked.get_data(as_text=True))

        conn = db.connect()
        claim_id = conn.execute(
            "SELECT id FROM claims WHERE released_at IS NULL"
        ).fetchone()["id"]
        conn.close()

        # Bob may not release someone else's slot on a resource he does not own.
        denied = self.post(f"/claims/{claim_id}/release", client=bob)
        self.assertIn("Only the holder", denied.get_data(as_text=True))

        self.post(f"/claims/{claim_id}/release", client=alice)
        taken = self.post(f"/resources/{resource_id}/acquire", client=bob, note="tests")
        self.assertIn("Resource locked", taken.get_data(as_text=True))

    def test_capacity_allows_several_holders(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        resource_id = self.create_resource(alice, capacity=2)

        self.post(f"/resources/{resource_id}/acquire", client=alice)
        response = self.post(f"/resources/{resource_id}/acquire", client=bob)
        self.assertIn("Resource locked", response.get_data(as_text=True))
        self.assertIn("Locked 2/2", response.get_data(as_text=True))

    def test_only_owner_can_edit_or_delete(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        resource_id = self.create_resource(alice)

        self.assertEqual(bob.get(f"/resources/{resource_id}/edit").status_code, 403)
        with bob.session_transaction() as session:
            token = session["csrf"]
        response = bob.post(
            f"/resources/{resource_id}/delete", data={"csrf": token}
        )
        self.assertEqual(response.status_code, 403)

        edited = self.post(
            f"/resources/{resource_id}/edit",
            client=alice,
            name="lab-pi-03",
            description="Now in rack C",
            capacity=3,
            labels="hardware rack-c",
        )
        self.assertIn("Now in rack C", edited.get_data(as_text=True))
        self.assertIn("Free 3/3", edited.get_data(as_text=True))

    def test_capacity_cannot_drop_below_slots_in_use(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client, capacity=2)
        self.post(f"/resources/{resource_id}/acquire")
        self.post(f"/resources/{resource_id}/acquire")
        response = self.post(
            f"/resources/{resource_id}/edit",
            name="lab-pi-03",
            description="",
            capacity=1,
            labels="",
        )
        self.assertIn("cannot drop below", response.get_data(as_text=True))

    # --- everything on the index page ----------------------------------

    def test_there_is_no_separate_detail_page(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        self.assertEqual(self.client.get(f"/resources/{resource_id}").status_code, 404)

    def test_index_shows_holder_and_comment(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        resource_id = self.create_resource(alice)
        self.post(
            f"/resources/{resource_id}/acquire", client=alice, note="flashing firmware"
        )

        page = bob.get("/").get_data(as_text=True)
        self.assertIn("alice", page)
        self.assertIn("flashing firmware", page)

    def test_index_lock_form_has_a_comment_field(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        page = self.client.get("/").get_data(as_text=True)
        form = page.split(f"/resources/{resource_id}/acquire")[1].split("</form>")[0]
        self.assertIn('name="note"', form)
        self.assertIn(">Lock</button>", form)

    def test_comment_entered_on_index_is_stored_and_shown(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        page = self.post(
            f"/resources/{resource_id}/acquire", note="debugging the CAN bus"
        ).get_data(as_text=True)
        self.assertIn("debugging the CAN bus", page)

    def test_history_is_collapsed_by_default(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        self.post(f"/resources/{resource_id}/acquire", note="first run")
        claim_id = self.latest_claim_id()
        self.post(f"/claims/{claim_id}/release")

        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("<details", page)
        self.assertIn("Recent history", page)
        self.assertNotIn("<details open", page)
        self.assertIn("first run", page)

    def test_add_button_sits_by_the_search_box_not_in_the_header(self):
        self.sign_up(self.client, "alice")
        page = self.client.get("/").get_data(as_text=True)

        header = page.split("<header")[1].split("</header>")[0]
        self.assertNotIn("/resources/new", header)

        head = page.split('class="card-head search-only"')[1].split("</div>")[0]
        self.assertIn('class="search"', head)
        self.assertIn('href="/resources/new"', head)
        self.assertIn("Add resource", head)
        self.assertEqual(200, self.client.get("/resources/new").status_code)

    def test_list_has_column_headers(self):
        self.sign_up(self.client, "alice")
        self.create_resource(self.client)
        head = self.client.get("/").get_data(as_text=True)
        head = head.split('class="res-head"')[1].split("</div>")[0]
        for column in ("Name", "Description", "Labels", "State", "Lock"):
            self.assertIn(f"<span>{column}</span>", head)

    def test_collapsed_row_shows_only_the_summary_fields(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        page = self.client.get("/").get_data(as_text=True)
        summary = page.split('class="res-row"')[1].split("</summary>")[0]

        # Visible while collapsed: name, description, labels, state, lock form.
        self.assertIn("lab-pi-03", summary)
        self.assertIn("Pi with the CAN adapter", summary)
        self.assertIn("rack-b", summary)
        self.assertIn("Free 1/1", summary)
        self.assertIn('name="note"', summary)
        self.assertIn(">Lock</button>", summary)

        # Hidden until expanded: owner, edit/delete, history.
        self.assertNotIn("owner alice", summary)
        self.assertNotIn(">Edit</a>", summary)
        self.assertNotIn("Recent history", summary)

    def test_rows_are_collapsed_by_default(self):
        self.sign_up(self.client, "alice")
        self.create_resource(self.client)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("<details>", page)
        self.assertNotIn("<details open", page)

    def test_edit_and_delete_sit_together(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        page = self.client.get("/").get_data(as_text=True)
        actions = page.split('class="res-actions"')[1].split("</div>")[0]
        self.assertIn(f"/resources/{resource_id}/edit", actions)
        self.assertIn(f"/resources/{resource_id}/delete", actions)

    def test_state_badge_keeps_free_and_busy_styling(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client, capacity=2)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="badge free"', page)

        self.post(f"/resources/{resource_id}/acquire")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Free 1/2", page)

        self.post(f"/resources/{resource_id}/acquire")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="badge busy"', page)
        self.assertIn("Locked 2/2", page)

    def test_your_resources_section_is_named_correctly(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        self.post(f"/resources/{resource_id}/acquire")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Your resources", page)
        self.assertNotIn("Your slots", page)

    def test_index_heading_says_resources(self):
        self.sign_up(self.client, "alice")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="brand" href="/">Resources</a>', page)

    # --- admin ---------------------------------------------------------

    def test_first_user_is_admin_and_second_is_not(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        self.assertEqual(alice.get("/admin/users").status_code, 200)
        self.assertEqual(bob.get("/admin/users").status_code, 403)

    def test_admin_changes_a_password(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        bob_id = self.user_id("bob")

        self.post(
            f"/admin/users/{bob_id}/password",
            client=alice,
            password="brandnewpass",
            confirm="brandnewpass",
        )
        fresh = app_module.app.test_client()
        fresh.get("/login")
        self.post("/login", client=fresh, username="bob", password="brandnewpass")
        self.assertEqual(fresh.get("/").status_code, 200)

    def test_admin_password_change_requires_matching_confirmation(self):
        alice = app_module.app.test_client()
        bob = app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        response = self.post(
            f"/admin/users/{self.user_id('bob')}/password",
            client=alice,
            password="brandnewpass",
            confirm="something-else",
        )
        self.assertIn("do not match", response.get_data(as_text=True))

    def test_admin_deletes_user_and_inherits_resources(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        resource_id = self.create_resource(bob, name="bobs-box")
        bob_id = self.user_id("bob")

        self.post(f"/admin/users/{bob_id}/delete", client=alice)
        self.assertIsNone(self.user_id("bob"))

        detail = alice.get("/").get_data(as_text=True)
        self.assertIn("owner alice", detail)

    def test_admin_cannot_delete_self_or_last_admin(self):
        alice = app_module.app.test_client()
        self.sign_up(alice, "alice")
        response = self.post(
            f"/admin/users/{self.user_id('alice')}/delete", client=alice
        )
        self.assertIn("cannot delete your own account", response.get_data(as_text=True))

        response = self.post(
            f"/admin/users/{self.user_id('alice')}/admin", client=alice, grant="0"
        )
        self.assertIn("at least one admin", response.get_data(as_text=True))

    def test_non_admin_cannot_reach_admin_actions(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        response = self.post(
            f"/admin/users/{self.user_id('alice')}/password",
            client=bob,
            password="takeover123",
            confirm="takeover123",
        )
        self.assertEqual(response.status_code, 403)

    # --- password reveal ----------------------------------------------

    def test_login_password_field_is_revealable(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn("data-reveal", page)
        self.assertIn("app.js", page)

    # --- locking from the index ---------------------------------------

    def test_lock_from_index_returns_to_index(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f"/resources/{resource_id}/acquire", page)

        response = self.client.post(
            f"/resources/{resource_id}/acquire",
            data={"csrf": self.csrf(), "back": "/?label=rack-b"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/?label=rack-b"))

    def test_back_parameter_cannot_redirect_off_site(self):
        self.sign_up(self.client, "alice")
        resource_id = self.create_resource(self.client)
        response = self.client.post(
            f"/resources/{resource_id}/acquire",
            data={"csrf": self.csrf(), "back": "//evil.example.com/"},
            follow_redirects=False,
        )
        self.assertEqual(f"/#r{resource_id}", response.headers["Location"])

    # --- REST API ------------------------------------------------------

    def api_token(self, client):
        """The plaintext token is shown exactly once, on the page after creation."""
        page = self.post("/settings/tokens", client=client, name="test").get_data(
            as_text=True
        )
        return page.split('id="new-token">')[1].split("</pre>")[0].strip()

    def test_api_requires_a_valid_token(self):
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer sem_dead_beef"}
        )
        self.assertEqual(response.status_code, 401)

    def test_api_ignores_session_cookies(self):
        """A logged-in browser session must not be enough to drive the API."""
        self.sign_up(self.client, "alice")
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_api_lock_explicit_and_by_label(self):
        self.sign_up(self.client, "alice")
        self.create_resource(self.client, name="lab-pi-03", labels="rack-b hardware")
        self.create_resource(self.client, name="lab-pi-04", labels="rack-b hardware")
        token = self.api_token(self.client)
        headers = {"Authorization": f"Bearer {token}"}

        me = self.client.get("/api/me", headers=headers)
        self.assertEqual(me.get_json()["username"], "alice")

        listed = self.client.get("/api/v1/resources?label=rack-b", headers=headers)
        self.assertEqual(len(listed.get_json()["resources"]), 2)

        explicit = self.client.post(
            "/api/v1/locks", json={"resource": "lab-pi-03", "note": "ci"}, headers=headers
        )
        self.assertEqual(explicit.status_code, 201)
        self.assertEqual(explicit.get_json()["resource"], "lab-pi-03")

        # Both are capacity 1, so by-label must skip the one just taken.
        by_label = self.client.post(
            "/api/v1/locks", json={"label": "rack-b"}, headers=headers
        )
        self.assertEqual(by_label.status_code, 201)
        self.assertEqual(by_label.get_json()["resource"], "lab-pi-04")

        # Nothing free now.
        full = self.client.post("/api/v1/locks", json={"label": "rack-b"}, headers=headers)
        self.assertEqual(full.status_code, 409)
        self.assertIn("occupied", full.get_json()["error"])

        locks = self.client.get("/api/v1/locks", headers=headers).get_json()["locks"]
        self.assertEqual(len(locks), 2)

        released = self.client.delete(
            f"/api/v1/locks/{locks[0]['lock_id']}", headers=headers
        )
        self.assertEqual(released.status_code, 200)
        retaken = self.client.post(
            "/api/v1/locks", json={"label": "rack-b"}, headers=headers
        )
        self.assertEqual(retaken.status_code, 201)

    def test_api_rejects_ambiguous_or_unknown_targets(self):
        self.sign_up(self.client, "alice")
        self.create_resource(self.client)
        headers = {"Authorization": f"Bearer {self.api_token(self.client)}"}

        both = self.client.post(
            "/api/v1/locks", json={"resource": "x", "label": "y"}, headers=headers
        )
        self.assertEqual(both.status_code, 400)

        neither = self.client.post("/api/v1/locks", json={}, headers=headers)
        self.assertEqual(neither.status_code, 400)

        unknown = self.client.post(
            "/api/v1/locks", json={"resource": "nope"}, headers=headers
        )
        self.assertEqual(unknown.status_code, 404)

        no_label = self.client.post(
            "/api/v1/locks", json={"label": "nope"}, headers=headers
        )
        self.assertEqual(no_label.status_code, 404)

    def test_revoked_token_stops_working(self):
        self.sign_up(self.client, "alice")
        token = self.api_token(self.client)
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/me", headers=headers).status_code, 200)

        page = self.client.get("/settings").get_data(as_text=True)
        token_id = page.split("/settings/tokens/")[1].split("/revoke")[0]
        self.post(f"/settings/tokens/{token_id}/revoke")
        self.assertEqual(self.client.get("/api/me", headers=headers).status_code, 401)

    def test_password_change_revokes_tokens(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        headers = {"Authorization": f"Bearer {self.api_token(bob)}"}
        self.assertEqual(alice.get("/api/me", headers=headers).status_code, 200)

        self.post(
            f"/admin/users/{self.user_id('bob')}/password",
            client=alice,
            password="brandnewpass",
            confirm="brandnewpass",
        )
        self.assertEqual(alice.get("/api/me", headers=headers).status_code, 401)

    def test_tokens_containing_underscores_resolve(self):
        """The random secret is urlsafe base64, so it often contains '_'."""
        self.sign_up(self.client, "alice")
        conn = db.connect()
        try:
            user_id = self.user_id("alice")
            saw_underscore = False
            for _ in range(25):
                _, token = auth.create_token(conn, user_id, "loop")
                saw_underscore |= "_" in token.split("_", 2)[2]
                self.assertIsNotNone(
                    auth.user_for_token(conn, token), f"failed to resolve {token}"
                )
            self.assertTrue(saw_underscore, "test never exercised the '_' case")
        finally:
            conn.close()

    def test_api_docs_render_without_external_assets(self):
        """The docs must work on a machine with no internet access."""
        self.sign_up(self.client, "alice")
        page = self.client.get("/api/docs").get_data(as_text=True)
        self.assertNotIn("http://unpkg", page)
        self.assertNotIn("https://", page.split("</head>")[-1])

        spec = self.client.get("/api/openapi.json").get_json()
        for path, operations in spec["paths"].items():
            self.assertIn(path, page)
            for method in operations:
                self.assertIn(f">{method.upper()}</span>", page)

    def test_openapi_document_is_served(self):
        spec = self.client.get("/api/openapi.json").get_json()
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/api/v1/locks", spec["paths"])
        self.assertIn("bearerAuth", spec["components"]["securitySchemes"])
        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                self.assertIn("responses", operation, f"{method} {path}")

    def test_bad_label_is_rejected(self):
        self.sign_up(self.client, "alice")
        response = self.post(
            "/resources/new",
            name="box",
            description="",
            capacity=1,
            labels="good bad!label",
        )
        self.assertIn("Invalid label", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
