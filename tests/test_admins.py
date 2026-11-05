from server import db, settings
from tests.helpers import ServerTest


class Admins(ServerTest):
    def setUp(self):
        super().setUp()
        settings._cache["admin-ids"] = "1"
        db.conn().execute("INSERT INTO users (uid, name, refresh_token, checked) VALUES ('1', 'Owner', NULL, 0), ('2', 'Bea', NULL, 0), ('3', 'Cal', NULL, 0)")
        self.owner = self.session("1", "Owner", "tok-1")
        self.bea = self.session("2", "Bea", "tok-2")

    def test_an_added_admin_is_one_at_once_and_stops_being_one_once_removed(self):
        self.assertFalse(self.call("GET", "/api/me", cookie=self.bea).json()["admin"])
        listed = self.call("POST", "/api/admins", {"uid": "2"}, self.owner).json()
        self.assertEqual(["Owner", "Bea"], [a["name"] for a in listed["admins"]])
        self.assertEqual(["Cal"], [o["name"] for o in listed["others"]])
        self.assertTrue(self.call("GET", "/api/me", cookie=self.bea).json()["admin"])
        self.assertEqual(200, self.call("POST", "/api/pins", {"parser": "0.5.0"}, self.bea).status)

        self.call("DELETE", "/api/admins/2", cookie=self.owner)
        self.assertFalse(self.call("GET", "/api/me", cookie=self.bea).json()["admin"])
        self.assertEqual(403, self.call("POST", "/api/pins", {"parser": "0.5.0"}, self.bea).status)

    def test_the_settings_admins_cant_be_removed(self):
        self.call("POST", "/api/admins", {"uid": "2"}, self.owner)
        self.assertEqual(409, self.call("DELETE", "/api/admins/1", cookie=self.bea).status)
        self.assertTrue(self.call("GET", "/api/me", cookie=self.owner).json()["admin"])
        self.assertTrue(self.call("GET", "/api/admins", cookie=self.owner).json()["admins"][0]["owner"])

    def test_only_admins_see_or_change_them_and_only_people_who_signed_in_can_be_added(self):
        self.assertEqual(403, self.call("GET", "/api/admins", cookie=self.bea).status)
        self.assertEqual(403, self.call("POST", "/api/admins", {"uid": "2"}, self.bea).status)
        self.assertEqual(403, self.call("DELETE", "/api/admins/3", cookie=self.bea).status)
        self.assertEqual(404, self.call("POST", "/api/admins", {"uid": "99"}, self.owner).status)
