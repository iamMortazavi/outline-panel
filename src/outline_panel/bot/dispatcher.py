"""
Telegram bot handlers and dispatcher factory (aiogram v3).

`build_dispatcher(db, registry, get_admin_ids, notifier, get_webapp_url)` returns
a configured Dispatcher with no Bot attached, so the same handlers serve both the
standalone bot (`outline-panel-bot`) and the in-process bot managed from the panel.

The bot is multi-server: it lists keys across every configured server and, when
more than one exists, asks which server a new key belongs to. Callback data
encodes the server id, e.g. ``key:<sid>:<kid>``.
"""

from __future__ import annotations

import html
import time
from collections.abc import Awaitable, Callable

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from ..core.outline_api import OutlineError
from ..core.rights import can_see, has_cap, on_credit, owns, price_for
from ..core.utils import fmt_bytes, fmt_expiry, gb_to_bytes


class NewUser(StatesGroup):
    server = State()
    name = State()
    package = State()      # credit admins buy instead of naming a size
    data_limit = State()
    duration = State()


class EditKey(StatesGroup):
    rename = State()
    limit = State()


class _Uid:
    """admin_of() takes an aiogram event; is_admin() takes a bare id."""

    def __init__(self, uid: int):
        self.from_user = type("U", (), {"id": uid})()


def build_dispatcher(
    db,
    registry,
    get_admin_ids: Callable[[], set[int] | Awaitable[set[int]]],
    notifier=None,
    get_webapp_url: Callable[[], str | None | Awaitable[str | None]] | None = None,
    resolve_admin=None,
    create_key=None,
) -> Dispatcher:
    dp = Dispatcher()

    async def admin_ids() -> set[int]:
        res = get_admin_ids()
        if hasattr(res, "__await__"):
            res = await res
        return set(res or ())

    async def webapp_url() -> str | None:
        """Full Mini App URL (`<base>/tma`); only HTTPS opens as a Web App."""
        if get_webapp_url is None:
            return None
        res = get_webapp_url()
        if hasattr(res, "__await__"):
            res = await res
        if not res or not res.startswith("https://"):
            return None
        return f"{res.rstrip('/')}/tma"

    async def admin_of(target) -> dict | None:
        """The panel admin behind a Telegram user, with their caps and scope.

        The bot used to answer a flat yes/no from a list of ids, so every bot
        admin could do everything to every server. Now it carries the same
        identity the dashboard does.
        """
        uid = target.from_user.id
        if resolve_admin is not None:
            return await resolve_admin(uid)
        return {"is_owner": 1} if uid in await admin_ids() else None  # tests

    async def is_admin(uid: int) -> bool:
        return await admin_of(_Uid(uid)) is not None

    async def gate(target, cap: str | None = None) -> dict | None:
        """Resolve + authorise in one line, or answer and return None."""
        admin = await admin_of(target)
        if admin is None:
            await deny(target)
            return None
        if cap and not has_cap(admin, cap):
            await deny(target, "⛔️ You do not have permission for this.")
            return None
        return admin

    async def deny(target: Message | CallbackQuery, text: str | None = None) -> None:
        text = text or "⛔️ You are not authorized to use this bot."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)

    def main_menu(wa_url: str | None = None) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(text="➕ New user", callback_data="new")],
            [InlineKeyboardButton(text="📋 Users", callback_data="list")],
        ]
        if wa_url:
            rows.insert(0, [InlineKeyboardButton(
                text="🚀 Open Web App", web_app=WebAppInfo(url=wa_url))])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def back_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Main menu", callback_data="menu")]])

    # ----------------------------------------------------------- commands
    @dp.message(Command("start"))
    async def cmd_start(msg: Message, state: FSMContext) -> None:
        await state.clear()
        if not await gate(msg):
            return
        await msg.answer("🛡 <b>Outline Panel</b>\n\nChoose an option:",
                         reply_markup=main_menu(await webapp_url()), parse_mode="HTML")

    @dp.message(Command("id"))
    async def cmd_id(msg: Message) -> None:
        await msg.answer(f"Your numeric ID: <code>{msg.from_user.id}</code>",
                         parse_mode="HTML")

    @dp.callback_query(F.data == "menu")
    async def cb_menu(cq: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await gate(cq):
            return
        await cq.message.edit_text("🛡 <b>Outline Panel</b>\n\nChoose an option:",
                                   reply_markup=main_menu(await webapp_url()), parse_mode="HTML")
        await cq.answer()

    # -------------------------------------------------------- create user
    @dp.callback_query(F.data == "new")
    async def cb_new(cq: CallbackQuery, state: FSMContext) -> None:
        admin = await gate(cq, "keys.create")
        if not admin:
            return
        sids = [x for x in registry.ids() if can_see(admin, x)]
        if not sids:
            return await cq.answer("No server is available to you.", show_alert=True)
        # remember who is buying: the FSM outlives this callback
        await state.update_data(aid=admin["id"], credit=on_credit(admin))
        if len(sids) == 1:
            await state.update_data(sid=sids[0])
            await state.set_state(NewUser.name)
            await cq.message.edit_text("📝 Enter the new user's name:")
        else:
            rows = [[InlineKeyboardButton(
                text=registry.meta(s)["name"], callback_data=f"newsrv:{s}")]
                for s in sids]
            await state.set_state(NewUser.server)
            await cq.message.edit_text("🖥 Which server should it be created on?",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cq.answer()

    @dp.callback_query(NewUser.server, F.data.startswith("newsrv:"))
    async def cb_new_server(cq: CallbackQuery, state: FSMContext) -> None:
        admin = await gate(cq, "keys.create")
        if not admin:
            return
        sid = cq.data.split(":", 1)[1]
        if not can_see(admin, sid):   # callback data comes from the client
            return await cq.answer("Unknown server.", show_alert=True)
        await state.update_data(sid=sid)
        await state.set_state(NewUser.name)
        await cq.message.edit_text("📝 Enter the new user's name:")
        await cq.answer()

    @dp.message(NewUser.name)
    async def step_name(msg: Message, state: FSMContext) -> None:
        await state.update_data(name=msg.text.strip())
        data = await state.get_data()
        if not data.get("credit"):
            await state.set_state(NewUser.data_limit)
            return await msg.answer("💾 Enter the data limit in <b>GB</b>.\n"
                                    "Send <code>0</code> for unlimited.",
                                    parse_mode="HTML")
        # On credit: only the price list. A free-form size here would let a
        # reseller mint whatever they liked and never be charged for it.
        admin = await admin_of(msg)
        pkgs = await db.all_packages()
        if not pkgs:
            await state.clear()
            return await msg.answer("❌ No packages are available yet. "
                                    "Ask the owner to add one.")
        rows = []
        for pk in pkgs:
            price = price_for(pk, admin)
            size = "Unlimited" if pk["gb"] is None else f"{pk['gb']:g} GB"
            term = f"{pk['days']}d" if pk["days"] else "no expiry"
            afford = int(admin.get("credit") or 0) >= price
            rows.append([InlineKeyboardButton(
                text=f"{'' if afford else '🔒 '}{pk['name']} · {size} · {term} · {price:,}",
                callback_data=f"newpkg:{pk['id']}")])
        await state.set_state(NewUser.package)
        await msg.answer(
            f"📦 Pick a package.\nYour credit: <b>{int(admin.get('credit') or 0):,}</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def _finish_create(target: Message, state: FSMContext, admin: dict,
                             **fields) -> None:
        """Create through the panel's own path, so the bot charges credit and
        records ownership exactly like the dashboard."""
        data = await state.get_data()
        await state.clear()
        sid = data["sid"]
        if create_key is None:      # only in tests without the injection
            return await target.answer("❌ Key creation is unavailable.")
        try:
            key = await create_key(admin, sid, name=data["name"], **fields)
        except Exception as e:  # noqa: BLE001 — HTTPException carries the reason
            reason = getattr(e, "detail", None) or str(e)
            return await target.answer(f"❌ Could not create the user:\n{reason}")
        dur = key.get("durationDays")
        exp_txt = f"{dur} days from first connection" if dur else "No expiry"
        await target.answer(
            "✅ <b>User created</b>\n\n"
            f"👤 Name: <b>{html.escape(data['name'])}</b>\n"
            f"🆔 ID: <code>{key['id']}</code>\n"
            f"💾 Data limit: {fmt_bytes(key.get('limit'))}\n"
            f"⏳ Validity: {exp_txt}\n\n"
            f"🔗 Connection link:\n<code>{html.escape(key['accessUrl'])}</code>",
            parse_mode="HTML", reply_markup=back_menu())

    @dp.callback_query(NewUser.package, F.data.startswith("newpkg:"))
    async def cb_new_package(cq: CallbackQuery, state: FSMContext) -> None:
        admin = await gate(cq, "keys.create")
        if not admin:
            return
        await cq.answer()
        await _finish_create(cq.message, state, admin,
                             package_id=int(cq.data.split(":", 1)[1]))

    @dp.message(NewUser.data_limit)
    async def step_limit(msg: Message, state: FSMContext) -> None:
        try:
            gb = float(msg.text.strip().replace(",", "."))
            if gb < 0:
                raise ValueError
        except ValueError:
            return await msg.answer("⚠️ Please enter a valid number (e.g. 50 or 0).")
        await state.update_data(limit_gb=gb)
        await state.set_state(NewUser.duration)
        await msg.answer("⏳ Enter the validity period in <b>days</b>.\n"
                         "Send <code>0</code> for no expiry.",
                         parse_mode="HTML")

    @dp.message(NewUser.duration)
    async def step_duration(msg: Message, state: FSMContext) -> None:
        try:
            days = int(msg.text.strip())
            if days < 0:
                raise ValueError
        except ValueError:
            return await msg.answer("⚠️ Please enter a whole number (e.g. 30 or 0).")
        admin = await admin_of(msg)
        if admin is None:
            await state.clear()
            return await deny(msg)
        data = await state.get_data()
        await _finish_create(msg, state, admin,
                             limit_gb=data.get("limit_gb", 0), days=days)

    # --------------------------------------------------------------- list
    @dp.callback_query(F.data == "list")
    async def cb_list(cq: CallbackQuery) -> None:
        admin = await gate(cq, "keys.view")
        if not admin:
            return
        await cq.answer("Loading…")
        await render_list(cq.message, admin)

    async def render_list(message: Message, admin: dict) -> None:
        sids = [s for s in registry.ids() if can_see(admin, s)]
        multi = len(sids) > 1
        lines = ["📋 <b>Users</b>\n"]
        rows: list[list[InlineKeyboardButton]] = []
        total = 0
        for sid in sids:
            api = registry.get(sid)
            sname = registry.meta(sid)["name"]
            try:
                keys = await api.list_keys()
                usage = await api.get_transfer_metrics()
            except OutlineError as e:
                lines.append(f"\n⚠️ <b>{html.escape(sname)}</b>: unavailable ({html.escape(str(e)[:60])})")
                continue
            local = {k["key_id"]: k for k in await db.keys_for(sid)}
            for k in keys:
                # a sub-admin's list is their own customers only, same as the panel
                if not owns(admin, local.get(k["id"])):
                    continue
                total += 1
                kid = k["id"]
                name = k.get("name") or f"Key {kid}"
                used = int(usage.get(str(kid), 0))
                meta = local.get(kid, {})
                if meta.get("disabled"):
                    limit_b = meta.get("limit_bytes")
                else:
                    limit_b = k.get("dataLimit", {}).get("bytes")
                    if limit_b is None:
                        limit_b = meta.get("limit_bytes")
                dur, activated = meta.get("duration_days"), meta.get("activated_ts")
                if dur and not activated:
                    exp_txt = f"⏳ {dur} days from first connection (pending)"
                else:
                    exp_txt = f"⏳ {fmt_expiry(meta.get('expiry_ts'))}"
                status = "🔴 Disabled" if meta.get("disabled") else "🟢 Active"
                tag = f" · {html.escape(sname)}" if multi else ""
                lines.append(
                    f"\n👤 <b>{html.escape(name)}</b> (id: <code>{kid}</code>){tag} {status}\n"
                    f"   📈 Usage: {fmt_bytes(used)} of {fmt_bytes(limit_b)}\n   {exp_txt}")
                btn = f"⚙️ {name}" + (f" · {sname}" if multi else "")
                rows.append([InlineKeyboardButton(text=btn, callback_data=f"key:{sid}:{kid}")])
        if total == 0 and len(lines) == 1:
            return await message.edit_text("No users yet.", reply_markup=back_menu())
        rows.append([InlineKeyboardButton(text="🏠 Main menu", callback_data="menu")])
        await message.edit_text("\n".join(lines), parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # --------------------------------------------------------- key actions
    def key_menu(sid: str, kid: str, disabled: bool) -> InlineKeyboardMarkup:
        toggle = (InlineKeyboardButton(text="✅ Enable", callback_data=f"enable:{sid}:{kid}")
                  if disabled else
                  InlineKeyboardButton(text="⛔️ Disable", callback_data=f"disable:{sid}:{kid}"))
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Show link", callback_data=f"link:{sid}:{kid}")],
            [InlineKeyboardButton(text="✏️ Rename", callback_data=f"rename:{sid}:{kid}"),
             InlineKeyboardButton(text="💾 Data limit", callback_data=f"limit:{sid}:{kid}")],
            [toggle],
            [InlineKeyboardButton(text="➕ Extend 30 days", callback_data=f"extend:{sid}:{kid}")],
            [InlineKeyboardButton(text="🗑 Delete user", callback_data=f"del:{sid}:{kid}")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="list")],
        ])

    def parse_cb(data: str) -> tuple[str, str]:
        _, sid, kid = data.split(":", 2)
        return sid, kid

    @dp.callback_query(F.data.startswith("key:"))
    async def cb_key(cq: CallbackQuery) -> None:
        admin = await gate(cq, "keys.view")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        meta = await db.get_key(sid, kid)
        disabled = bool(meta and meta.get("disabled"))
        await cq.message.edit_text(f"⚙️ Manage user <code>{kid}</code>:",
                                   parse_mode="HTML", reply_markup=key_menu(sid, kid, disabled))
        await cq.answer()

    @dp.callback_query(F.data.startswith("link:"))
    async def cb_link(cq: CallbackQuery) -> None:
        admin = await gate(cq, "keys.view")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        api = registry.get(sid)
        try:
            key = await api.get_key(kid)
        except OutlineError as e:
            return await cq.answer(f"Error: {e}", show_alert=True)
        name = key.get("name") or f"Key {kid}"
        await cq.message.answer(
            f"🔗 Connection link for <b>{html.escape(name)}</b>:\n"
            f"<code>{html.escape(key.get('accessUrl', ''))}</code>", parse_mode="HTML")
        await cq.answer()

    @dp.callback_query(F.data.startswith("rename:"))
    async def cb_rename(cq: CallbackQuery, state: FSMContext) -> None:
        admin = await gate(cq, "keys.edit")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        await state.set_state(EditKey.rename)
        await state.update_data(sid=sid, kid=kid)
        await cq.message.edit_text("✏️ Enter the new name:")
        await cq.answer()

    @dp.message(EditKey.rename)
    async def step_rename(msg: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        sid, kid, name = data["sid"], data["kid"], msg.text.strip()
        api = registry.get(sid)
        try:
            await api.rename_key(kid, name)
        except OutlineError as e:
            return await msg.answer(f"❌ Rename failed:\n{e}")
        if not await db.get_key(sid, kid):
            await db.add_key(sid, kid, name, None, None)
        else:
            await db.set_name(sid, kid, name)
        await msg.answer(f"✅ Renamed to <b>{html.escape(name)}</b>.",
                         parse_mode="HTML", reply_markup=back_menu())

    @dp.callback_query(F.data.startswith("limit:"))
    async def cb_limit(cq: CallbackQuery, state: FSMContext) -> None:
        admin = await gate(cq, "keys.edit")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        await state.set_state(EditKey.limit)
        await state.update_data(sid=sid, kid=kid)
        await cq.message.edit_text("💾 Enter the new data limit in <b>GB</b> "
                                   "(<code>0</code> = unlimited):", parse_mode="HTML")
        await cq.answer()

    @dp.message(EditKey.limit)
    async def step_set_limit(msg: Message, state: FSMContext) -> None:
        try:
            gb = float(msg.text.strip().replace(",", "."))
            if gb < 0:
                raise ValueError
        except ValueError:
            return await msg.answer("⚠️ Please enter a valid number (e.g. 50 or 0).")
        data = await state.get_data()
        await state.clear()
        sid, kid = data["sid"], data["kid"]
        api = registry.get(sid)
        limit_bytes = gb_to_bytes(gb) if gb > 0 else None
        meta = await db.get_key(sid, kid)
        if not meta:
            await db.add_key(sid, kid, "", None, None)
            meta = await db.get_key(sid, kid)
        if not meta.get("disabled"):
            try:
                if limit_bytes is not None:
                    await api.set_data_limit(kid, limit_bytes)
                else:
                    await api.remove_data_limit(kid)
            except OutlineError as e:
                return await msg.answer(f"❌ Could not change the data limit:\n{e}")
        await db.set_limit(sid, kid, limit_bytes)
        await msg.answer(f"✅ Data limit changed to {fmt_bytes(limit_bytes)}.",
                         reply_markup=back_menu())

    @dp.callback_query(F.data.startswith("disable:"))
    async def cb_disable(cq: CallbackQuery) -> None:
        admin = await gate(cq, "keys.edit")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        api = registry.get(sid)
        if not await db.get_key(sid, kid):
            await db.add_key(sid, kid, "", None, None)
        try:
            await api.set_data_limit(kid, 0)
        except OutlineError as e:
            return await cq.answer(f"Error: {e}", show_alert=True)
        await db.set_disabled(sid, kid, True)
        await cq.answer("User disabled ⛔️", show_alert=True)
        await cq.message.edit_reply_markup(reply_markup=key_menu(sid, kid, True))

    @dp.callback_query(F.data.startswith("enable:"))
    async def cb_enable(cq: CallbackQuery) -> None:
        admin = await gate(cq, "keys.edit")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        api = registry.get(sid)
        meta = await db.get_key(sid, kid) or {}
        try:
            if meta.get("limit_bytes") is not None:
                await api.set_data_limit(kid, int(meta["limit_bytes"]))
            else:
                await api.remove_data_limit(kid)
        except OutlineError as e:
            return await cq.answer(f"Error: {e}", show_alert=True)
        await db.set_disabled(sid, kid, False)
        await cq.answer("User enabled ✅", show_alert=True)
        await cq.message.edit_reply_markup(reply_markup=key_menu(sid, kid, False))

    @dp.callback_query(F.data.startswith("del:"))
    async def cb_del(cq: CallbackQuery) -> None:
        admin = await gate(cq, "keys.delete")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        api = registry.get(sid)
        try:
            await api.delete_key(kid)
            await db.delete_key(sid, kid)
        except OutlineError as e:
            return await cq.answer(f"Error: {e}", show_alert=True)
        await cq.answer("User deleted ✅", show_alert=True)
        await render_list(cq.message, admin)

    @dp.callback_query(F.data.startswith("extend:"))
    async def cb_extend(cq: CallbackQuery) -> None:
        admin = await gate(cq, "keys.edit")
        if not admin:
            return
        sid, kid = parse_cb(cq.data)
        api = registry.get(sid)
        meta = await db.get_key(sid, kid)
        if not meta:
            await db.add_key(sid, kid, "", None, None)
            meta = await db.get_key(sid, kid)
        if meta.get("duration_days") is not None and meta.get("activated_ts") is None:
            await db.set_duration(sid, kid, int(meta["duration_days"]) + 30)
        else:
            base = max(meta.get("expiry_ts") or 0, int(time.time()))
            await db.set_expiry(sid, kid, base + 30 * 86400)
        if meta.get("disabled"):
            try:
                if meta.get("limit_bytes") is not None:
                    await api.set_data_limit(kid, int(meta["limit_bytes"]))
                else:
                    await api.remove_data_limit(kid)
                await db.set_disabled(sid, kid, False)
            except OutlineError as e:
                return await cq.answer(f"Extended, but enabling failed: {e}", show_alert=True)
        await cq.answer("Extended by 30 days ✅", show_alert=True)
        await render_list(cq.message, admin)

    return dp
