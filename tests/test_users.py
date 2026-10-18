import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.keys import KeyUnusable, SecretBox
from app.store import Store
from app.users import DuplicateUserError, LastAdminError, UserStore


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "d.db"))


@pytest.fixture
def users(store, clock):
    return UserStore(store, SecretBox(b"k" * 32), clock)


def admin(users, email="a@x.io"):
    return users.create("local", email, "Admin", "admin")


def test_create_and_lookup(users):
    u = users.create("local", " Ann@X.io ", "Ann", "viewer", must_change_password=True)
    assert u.email == "Ann@X.io" and u.active and u.must_change_password
    assert users.by_email("local", "ann@x.IO").id == u.id
    assert users.by_email("entra", "ann@x.io") is None
    assert users.get(u.id).recovery_codes == []
    assert users.count() == 1


def test_duplicate_email_case_insensitive(users):
    users.create("local", "a@x.io", "A", "viewer")
    with pytest.raises(DuplicateUserError):
        users.create("local", "A@X.IO", "B", "viewer")
    users.create("entra", "a@x.io", "C", "viewer")  # other kind is fine


def test_list_admins_first_then_name(users):
    users.create("local", "z@x.io", "Zed", "viewer")
    users.create("local", "b@x.io", "Bob", "viewer")
    users.create("local", "y@x.io", "Yan", "admin")
    assert [u.name for u in users.list()] == ["Yan", "Bob", "Zed"]


def test_entra_binding(users):
    u = users.create("entra", "e@x.io", "E", "viewer")
    users.bind_entra(u.id, "t", "o")
    assert users.by_entra("t", "o").id == u.id
    v = users.create("entra", "f@x.io", "F", "viewer")
    with pytest.raises(DuplicateUserError):
        users.bind_entra(v.id, "t", "o")


def test_create_first_admin_once(users):
    assert users.create_first_admin("local", "a@x.io", "A") is not None
    assert users.create_first_admin("local", "b@x.io", "B") is None
    assert users.count() == 1


def test_create_first_admin_threads(users):
    results = []
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        results.append(users.create_first_admin("local", f"{i}@x.io", "A"))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(r is not None for r in results) == 1 and users.count() == 1


def test_create_first_admin_second_connection(tmp_path, clock):
    path = str(tmp_path / "d.db")
    a = UserStore(Store(path), SecretBox(None), clock)
    b = UserStore(Store(path), SecretBox(None), clock)
    assert a.create_first_admin("local", "a@x.io", "A") is not None
    assert b.create_first_admin("local", "b@x.io", "B") is None


def test_last_admin_rules(users):
    a = admin(users)
    with pytest.raises(LastAdminError):
        users.update(a.id, role="viewer")
    with pytest.raises(LastAdminError):
        users.update(a.id, disabled=True)
    with pytest.raises(LastAdminError):
        users.delete(a.id)
    assert users.update(a.id, name="Renamed").name == "Renamed"
    b = admin(users, "b@x.io")
    assert users.update(a.id, role="viewer").role == "viewer"
    with pytest.raises(LastAdminError):
        users.delete(b.id)
    users.update(a.id, role="admin")
    users.delete(b.id)
    assert users.count() == 1


def test_disabled_admin_does_not_count(users):
    a = admin(users)
    b = admin(users, "b@x.io")
    users.update(b.id, disabled=True)
    with pytest.raises(LastAdminError):
        users.update(a.id, role="viewer")
    users.update(b.id, disabled=False)
    users.update(a.id, role="viewer")


def test_delete_removes_sessions_and_invites(users, store, clock):
    admin(users)
    u = users.create("local", "u@x.io", "U", "viewer")
    other = users.create("local", "o@x.io", "O", "viewer")
    store.session_put("s1", {}, clock(), clock(), user_id=u.id)
    store.session_put("s2", {}, clock(), clock(), user_id=other.id)
    token = users.create_invite(u.id)
    users.delete(u.id)
    assert store.session_get("s1") is None and store.session_get("s2") is not None
    assert users.use_invite(token) is None


def test_totp_secret_encrypted_at_rest(users, store):
    u = admin(users)
    users.set_totp(u.id, "JBSWY3DPEHPK3PXP", True)
    raw = store._db.execute("SELECT * FROM users").fetchall()
    assert "JBSW" not in repr(raw)
    u = users.get(u.id)
    assert u.totp_enabled and users.totp_secret(u) == "JBSWY3DPEHPK3PXP"
    users.set_totp(u.id, None, False)
    u = users.get(u.id)
    assert not u.totp_enabled and users.totp_secret(u) is None


def test_totp_with_unusable_key_raises(store, clock):
    us = UserStore(store, SecretBox(None), clock)
    u = us.create("local", "a@x.io", "A", "admin")
    with pytest.raises(KeyUnusable):
        us.set_totp(u.id, "JBSW", True)


def test_totp_last_step(users):
    u = admin(users)
    users.set_totp_last_step(u.id, 123)
    assert users.get(u.id).totp_last_step == 123


def test_recovery_code_used_once(users):
    u = admin(users)
    users.set_recovery_codes(u.id, ["h1", "h2"])
    assert users.use_recovery_code(u.id, "h1") is True
    assert users.use_recovery_code(u.id, "h1") is False
    assert users.get(u.id).recovery_codes == ["h2"]


def test_invite_valid_once(users):
    u = users.create("local", "u@x.io", "U", "viewer")
    token = users.create_invite(u.id)
    assert users.use_invite("nope") is None
    assert users.use_invite(token).id == u.id
    assert users.use_invite(token) is None


def test_invite_expires_after_72h(users, clock):
    u = users.create("local", "u@x.io", "U", "viewer")
    token = users.create_invite(u.id)
    clock.now += timedelta(hours=72, seconds=1)
    assert users.use_invite(token) is None
    token = users.create_invite(u.id)
    clock.now += timedelta(hours=71)
    assert users.use_invite(token).id == u.id


def test_failures_window_and_clear(users, clock):
    users.record_failure("account", "a")
    clock.now += timedelta(minutes=20)
    users.record_failure("account", "a")
    users.record_failure("ip", "a")
    assert users.failures("account", "a", timedelta(minutes=15)) == 1
    assert users.failures("account", "a", timedelta(hours=1)) == 2
    users.purge_failures(timedelta(minutes=10))
    assert users.failures("account", "a", timedelta(days=1)) == 1
    users.clear_failures("account", "a")
    assert users.failures("account", "a", timedelta(days=1)) == 0
    assert users.failures("ip", "a", timedelta(days=1)) == 1


def test_end_sessions_only_that_user(users, store, clock):
    store.session_put("s1", {}, clock(), clock(), user_id=1)
    store.session_put("s2", {}, clock(), clock(), user_id=1)
    store.session_put("s3", {}, clock(), clock(), user_id=2)
    assert users.end_sessions(1) == 2
    assert store.session_get("s3") is not None


def test_touch_sign_in(users, clock):
    u = admin(users)
    users.touch_sign_in(u.id)
    assert users.get(u.id).last_sign_in == clock()


def test_update_rejects_unknown_and_crafted_fields(users):
    u = admin(users)
    before = users.get(u.id)
    for bad in ({"role = 'admin', name": "x"}, {"bogus": 1}, {"totp_secret": "x"},
                {"password_hash": "x"}, {"recovery_codes": []}, {"totp_last_step": 1}):
        with pytest.raises(ValueError):
            users.update(u.id, **bad)
    assert users.get(u.id) == before


def test_only_unique_violations_become_duplicate(users, store):
    u = admin(users)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError) as ei:
        users.update(u.id, name=None)
    assert not isinstance(ei.value, DuplicateUserError)
    with pytest.raises(sqlite3.IntegrityError):
        users.update(u.id, kind="bogus")
    assert users.get(u.id).name == "Admin"


def test_set_password_hash(users):
    u = admin(users)
    users.set_password_hash(u.id, "hh", True)
    u = users.get(u.id)
    assert u.password_hash == "hh" and u.must_change_password


def test_role_validated(users):
    for role in ("pending", "viewer", "operator", "admin"):
        users.create("local", f"{role}@x.io", role, role)
    with pytest.raises(ValueError):
        users.create("local", "n@x.io", "N", "root")
    with pytest.raises(ValueError):
        users.create_first_admin("local", "m@x.io", "M", "root")
    u = users.by_email("local", "admin@x.io")
    with pytest.raises(ValueError):
        users.update(u.id, role="root")
    assert users.get(u.id).role == "admin"


def test_seal_roundtrip(users):
    token = users.seal("SECRET")
    assert "SECRET" not in token and users.unseal(token) == "SECRET"
    assert users.unseal("garbage") is None
