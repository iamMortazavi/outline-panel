import time

import outline_panel.core.scheduler as scheduler


class FakeAPI:
    def __init__(self, usage):
        self.usage = usage
        self.limits = {}

    async def get_transfer_metrics(self):
        return self.usage

    async def set_data_limit(self, kid, b):
        self.limits[kid] = b

    async def remove_data_limit(self, kid):
        self.limits[kid] = None


class Reg:
    def __init__(self, api):
        self.api = api

    def get(self, sid):
        return self.api


def _kind(msg):
    for sym, name in (("🔄", "reset"), ("⚠️", "limit"),
                      ("⏳", "expiry"), ("🔴", "disabled")):
        if sym in msg:
            return name
    return "?"


async def test_activation_on_first_use(db):
    await db.add_key("s1", "k1", "A", None, 30)
    reg = Reg(FakeAPI({"k1": 100}))
    await scheduler._check_once(reg, db, None, set())
    k = await db.get_key("s1", "k1")
    assert k["activated_ts"] is not None
    assert k["expiry_ts"] is not None


async def test_monthly_reset_bumps_limit(db):
    now = int(time.time())
    await db.add_key("s1", "k2", "B", 5 * 1024 ** 3, None)
    await db.set_monthly("s1", "k2", 5 * 1024 ** 3, now - 10)
    api = FakeAPI({"k2": 2 * 1024 ** 3})
    await scheduler._check_once(Reg(api), db, None, set())
    k = await db.get_key("s1", "k2")
    assert k["limit_bytes"] == 7 * 1024 ** 3          # used(2) + monthly(5)
    assert k["reset_ts"] > now                        # advanced to next cycle
    assert api.limits["k2"] == 7 * 1024 ** 3          # applied on Outline


async def test_expiry_disables_key(db):
    now = int(time.time())
    await db.add_key("s1", "k5", "E", 1024, None)
    await db.set_expiry("s1", "k5", now - 100)
    api = FakeAPI({})
    msgs = []
    await scheduler._check_once(Reg(api), db, lambda t: msgs.append(t), set())
    k = await db.get_key("s1", "k5")
    assert k["disabled"] == 1
    assert api.limits["k5"] == 0
    assert "disabled" in [_kind(m) for m in msgs]


async def test_limit_and_expiry_notifications_dedupe(db):
    now = int(time.time())
    await db.add_key("s1", "k3", "S", 10 * 1024 ** 3, None)   # 90% used -> warn
    await db.add_key("s1", "k4", "N", None, None)
    await db.set_expiry("s1", "k4", now + 2 * 86400)          # 2 days left -> warn
    api = FakeAPI({"k3": 9 * 1024 ** 3})
    msgs = []
    notified = set()

    async def notifier(t):
        msgs.append(t)

    await scheduler._check_once(Reg(api), db, notifier, notified)
    kinds = sorted(_kind(m) for m in msgs)
    assert "limit" in kinds and "expiry" in kinds

    msgs.clear()
    await scheduler._check_once(Reg(api), db, notifier, notified)
    assert msgs == []   # no duplicate notifications on the second pass
