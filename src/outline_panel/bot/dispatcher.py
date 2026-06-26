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
from ..core.utils import fmt_bytes, fmt_expiry, gb_to_bytes


class NewUser(StatesGroup):
    server = State()
    name = State()
    data_limit = State()
    duration = State()


class EditKey(StatesGroup):
    rename = State()
    limit = State()


def build_dispatcher(
    db,
    registry,
    get_admin_ids: Callable[[], set[int] | Awaitable[set[int]]],
    notifier=None,
    get_webapp_url: Callable[[], str | None | Awaitable[str | None]] | None = None,
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

    async def is_admin(uid: int) -> bool:
        return uid in await admin_ids()

    async def deny(target: Message | CallbackQuery) -> None:
        text = "⛔️ You are not authorized to use this bot."
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
        if not await is_admin(msg.from_user.id):
            return await deny(msg)
        await msg.answer("🛡 <b>Outline Panel</b>\n\nChoose an option:",
                         reply_markup=main_menu(await webapp_url()), parse_mode="HTML")

    @dp.message(Command("id"))
    async def cmd_id(msg: Message) -> None:
        await msg.answer(f"Your numeric ID: <code>{msg.from_user.id}</code>",
                         parse_mode="HTML")

    @dp.callback_query(F.data == "menu")
    async def cb_menu(cq: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        await cq.message.edit_text("🛡 <b>Outline Panel</b>\n\nChoose an option:",
                                   reply_markup=main_menu(await webapp_url()), parse_mode="HTML")
        await cq.answer()

    # -------------------------------------------------------- create user
    @dp.callback_query(F.data == "new")
    async def cb_new(cq: CallbackQuery, state: FSMContext) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        sids = registry.ids()
        if not sids:
            return await cq.answer("No server is configured.", show_alert=True)
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
        await state.update_data(sid=cq.data.split(":", 1)[1])
        await state.set_state(NewUser.name)
        await cq.message.edit_text("📝 Enter the new user's name:")
        await cq.answer()

    @dp.message(NewUser.name)
    async def step_name(msg: Message, state: FSMContext) -> None:
        await state.update_data(name=msg.text.strip())
        await state.set_state(NewUser.data_limit)
        await msg.answer("💾 Enter the data limit in <b>GB</b>.\n"
                         "Send <code>0</code> for unlimited.",
                         parse_mode="HTML")

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
        data = await state.get_data()
        await state.clear()
        sid = data["sid"]
        api = registry.get(sid)
        if api is None:
            return await msg.answer("❌ The selected server is no longer available.")
        name, gb = data["name"], data["limit_gb"]
        limit_bytes = gb_to_bytes(gb) if gb > 0 else None
        duration = days if days > 0 else None
        try:
            key = await api.create_key(name=name, limit_bytes=limit_bytes)
        except OutlineError as e:
            return await msg.answer(f"❌ Could not create the user:\n{e}")
        try:
            await db.add_key(sid, key["id"], name, limit_bytes, duration)
        except Exception as e:  # noqa: BLE001 — avoid an orphan key on the server
            try:
                await api.delete_key(key["id"])
            except OutlineError:
                pass
            return await msg.answer(f"❌ Could not create the user:\n{e}")
        exp_txt = f"{duration} days from first connection" if duration else "No expiry"
        await msg.answer(
            "✅ <b>User created</b>\n\n"
            f"👤 Name: <b>{html.escape(name)}</b>\n"
            f"🆔 ID: <code>{key['id']}</code>\n"
            f"💾 Data limit: {fmt_bytes(limit_bytes)}\n"
            f"⏳ Validity: {exp_txt}\n\n"
            f"🔗 Connection link:\n<code>{html.escape(key['accessUrl'])}</code>",
            parse_mode="HTML", reply_markup=back_menu())

    # --------------------------------------------------------------- list
    @dp.callback_query(F.data == "list")
    async def cb_list(cq: CallbackQuery) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        await cq.answer("Loading…")
        await render_list(cq.message)

    async def render_list(message: Message) -> None:
        multi = len(registry.ids()) > 1
        lines = ["📋 <b>Users</b>\n"]
        rows: list[list[InlineKeyboardButton]] = []
        total = 0
        for sid in registry.ids():
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
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        sid, kid = parse_cb(cq.data)
        meta = await db.get_key(sid, kid)
        disabled = bool(meta and meta.get("disabled"))
        await cq.message.edit_text(f"⚙️ Manage user <code>{kid}</code>:",
                                   parse_mode="HTML", reply_markup=key_menu(sid, kid, disabled))
        await cq.answer()

    @dp.callback_query(F.data.startswith("link:"))
    async def cb_link(cq: CallbackQuery) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
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
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
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
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
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
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
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
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
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
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        sid, kid = parse_cb(cq.data)
        api = registry.get(sid)
        try:
            await api.delete_key(kid)
            await db.delete_key(sid, kid)
        except OutlineError as e:
            return await cq.answer(f"Error: {e}", show_alert=True)
        await cq.answer("User deleted ✅", show_alert=True)
        await render_list(cq.message)

    @dp.callback_query(F.data.startswith("extend:"))
    async def cb_extend(cq: CallbackQuery) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
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
        await render_list(cq.message)

    return dp
