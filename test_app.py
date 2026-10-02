"""Smoke tests: run with .venv/bin/python -m unittest test_app -v"""

import os
import tempfile
import unittest

os.environ["SEMAPHORE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import app as app_module  # noqa: E402  (import after SEMAPHORE_DB is set)
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

        page = self.client.get(f"/resources/{resource_id}").get_data(as_text=True)
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
        self.assertIn("Slot is yours", taken.get_data(as_text=True))

    def test_capacity_allows_several_holders(self):
        alice, bob = app_module.app.test_client(), app_module.app.test_client()
        self.sign_up(alice, "alice")
        self.sign_up(bob, "bob")
        resource_id = self.create_resource(alice, capacity=2)

        self.post(f"/resources/{resource_id}/acquire", client=alice)
        response = self.post(f"/resources/{resource_id}/acquire", client=bob)
        self.assertIn("Slot is yours", response.get_data(as_text=True))
        self.assertIn("0 / 2 free", response.get_data(as_text=True))

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
        self.assertIn("3 free", edited.get_data(as_text=True))

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
