from outline_panel.core import security
from outline_panel.core.settings import OWNER_USERNAME, SettingsStore


async def test_bootstrap_gives_an_existing_password_an_identity(db, monkeypatch):
    """Upgrading an installed panel: the password already in `settings` must
    keep working, now reachable as a username."""
    monkeypatch.setattr("outline_panel.core.config.ADMIN_PASSWORD", "")
    h, s = security.hash_password("hunter2")
    await db.set_setting("admin_password_hash", h)
    await db.set_setting("admin_password_salt", s)

    st = SettingsStore(db)
    await st.bootstrap()
    owner = await db.get_owner()
    assert owner["username"] == OWNER_USERNAME and owner["is_owner"] == 1
    assert await st.verify_login(OWNER_USERNAME, "hunter2") is not None
    assert await st.verify_login(OWNER_USERNAME, "wrong") is None


async def test_bootstrap_is_idempotent(db, monkeypatch):
    monkeypatch.setattr("outline_panel.core.config.ADMIN_PASSWORD", "pw")
    st = SettingsStore(db)
    await st.bootstrap()
    await st.bootstrap()
    assert len(await db.all_admins()) == 1


async def test_sub_admin_login_uses_its_own_hash(db, monkeypatch):
    monkeypatch.setattr("outline_panel.core.config.ADMIN_PASSWORD", "pw")
    st = SettingsStore(db)
    await st.bootstrap()
    h, s = security.hash_password("sara-pw")
    await db.add_admin("sara", h, s, caps="keys.view", servers="s1")

    assert await st.verify_login("sara", "sara-pw") is not None
    assert await st.verify_login("sara", "pw") is None        # not the owner's
    assert await st.verify_login("admin", "sara-pw") is None


async def test_disabled_admin_cannot_log_in(db, monkeypatch):
    monkeypatch.setattr("outline_panel.core.config.ADMIN_PASSWORD", "pw")
    st = SettingsStore(db)
    await st.bootstrap()
    h, s = security.hash_password("sara-pw")
    aid = await db.add_admin("sara", h, s)
    assert await st.verify_login("sara", "sara-pw") is not None
    await db.update_admin(aid, disabled=1)
    assert await st.verify_login("sara", "sara-pw") is None
