"""
Reusable Telegram bot logic (aiogram v3).

`build_dispatcher(db, registry, get_admin_ids, notifier)` returns a configured
Dispatcher with no Bot attached, so the same handlers serve both the standalone
bot (`outline-panel-bot`) and the in-process bot managed from the web panel.

The bot is multi-server: it lists keys across every configured server and, when
more than one exists, asks which server a new key belongs to. Callback data
encodes the server id, e.g. ``key:<sid>:<kid>``.
"""

from __future__ import annotations

import html
import time
from typing import Awaitable, Callable

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..outline_api import OutlineError
from ..utils import fmt_bytes, fmt_expiry, gb_to_bytes


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
    get_admin_ids: Callable[[], "set[int] | Awaitable[set[int]]"],
    notifier=None,
) -> Dispatcher:
    dp = Dispatcher()

    async def admin_ids() -> set[int]:
        res = get_admin_ids()
        if hasattr(res, "__await__"):
            res = await res
        return set(res or ())

    async def is_admin(uid: int) -> bool:
        return uid in await admin_ids()

    async def deny(target: Message | CallbackQuery) -> None:
        text = "⛔️ شما اجازه‌ی استفاده از این بات را ندارید."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)

    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ ساخت یوزر جدید", callback_data="new")],
            [InlineKeyboardButton(text="📋 لیست یوزرها", callback_data="list")],
        ])

    def back_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu")]])

    # ----------------------------------------------------------- commands
    @dp.message(Command("start"))
    async def cmd_start(msg: Message, state: FSMContext) -> None:
        await state.clear()
        if not await is_admin(msg.from_user.id):
            return await deny(msg)
        await msg.answer("🛡 <b>پنل مدیریت Outline</b>\n\nیک گزینه را انتخاب کنید:",
                         reply_markup=main_menu(), parse_mode="HTML")

    @dp.message(Command("id"))
    async def cmd_id(msg: Message) -> None:
        await msg.answer(f"آی‌دی عددی شما: <code>{msg.from_user.id}</code>",
                         parse_mode="HTML")

    @dp.callback_query(F.data == "menu")
    async def cb_menu(cq: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        await cq.message.edit_text("🛡 <b>پنل مدیریت Outline</b>\n\nیک گزینه را انتخاب کنید:",
                                   reply_markup=main_menu(), parse_mode="HTML")
        await cq.answer()

    # -------------------------------------------------------- create user
    @dp.callback_query(F.data == "new")
    async def cb_new(cq: CallbackQuery, state: FSMContext) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        sids = registry.ids()
        if not sids:
            return await cq.answer("هیچ سروری تنظیم نشده است.", show_alert=True)
        if len(sids) == 1:
            await state.update_data(sid=sids[0])
            await state.set_state(NewUser.name)
            await cq.message.edit_text("📝 نام یوزر جدید را وارد کنید:")
        else:
            rows = [[InlineKeyboardButton(
                text=registry.meta(s)["name"], callback_data=f"newsrv:{s}")]
                for s in sids]
            await state.set_state(NewUser.server)
            await cq.message.edit_text("🖥 روی کدام سرور ساخته شود؟",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cq.answer()

    @dp.callback_query(NewUser.server, F.data.startswith("newsrv:"))
    async def cb_new_server(cq: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(sid=cq.data.split(":", 1)[1])
        await state.set_state(NewUser.name)
        await cq.message.edit_text("📝 نام یوزر جدید را وارد کنید:")
        await cq.answer()

    @dp.message(NewUser.name)
    async def step_name(msg: Message, state: FSMContext) -> None:
        await state.update_data(name=msg.text.strip())
        await state.set_state(NewUser.data_limit)
        await msg.answer("💾 سقف حجم را به <b>گیگابایت</b> وارد کنید.\n"
                         "برای حجم نامحدود عدد <code>0</code> را بفرستید.",
                         parse_mode="HTML")

    @dp.message(NewUser.data_limit)
    async def step_limit(msg: Message, state: FSMContext) -> None:
        try:
            gb = float(msg.text.strip().replace(",", "."))
            if gb < 0:
                raise ValueError
        except ValueError:
            return await msg.answer("⚠️ لطفاً یک عدد معتبر وارد کنید (مثلاً 50 یا 0).")
        await state.update_data(limit_gb=gb)
        await state.set_state(NewUser.duration)
        await msg.answer("⏳ مدت اعتبار را به <b>روز</b> وارد کنید.\n"
                         "برای بدون انقضا عدد <code>0</code> را بفرستید.",
                         parse_mode="HTML")

    @dp.message(NewUser.duration)
    async def step_duration(msg: Message, state: FSMContext) -> None:
        try:
            days = int(msg.text.strip())
            if days < 0:
                raise ValueError
        except ValueError:
            return await msg.answer("⚠️ لطفاً یک عدد صحیح وارد کنید (مثلاً 30 یا 0).")
        data = await state.get_data()
        await state.clear()
        sid = data["sid"]
        api = registry.get(sid)
        if api is None:
            return await msg.answer("❌ سرور انتخابی دیگر در دسترس نیست.")
        name, gb = data["name"], data["limit_gb"]
        limit_bytes = gb_to_bytes(gb) if gb > 0 else None
        duration = days if days > 0 else None
        try:
            key = await api.create_key(name=name, limit_bytes=limit_bytes)
        except OutlineError as e:
            return await msg.answer(f"❌ ساخت یوزر ناموفق بود:\n{e}")
        try:
            await db.add_key(sid, key["id"], name, limit_bytes, duration)
        except Exception as e:  # noqa: BLE001 — جلوگیری از کلید یتیم
            try:
                await api.delete_key(key["id"])
            except OutlineError:
                pass
            return await msg.answer(f"❌ ساخت یوزر ناموفق بود:\n{e}")
        exp_txt = f"{duration} روز از اولین اتصال" if duration else "بدون انقضا"
        await msg.answer(
            "✅ <b>یوزر ساخته شد</b>\n\n"
            f"👤 نام: <b>{html.escape(name)}</b>\n"
            f"🆔 شناسه: <code>{key['id']}</code>\n"
            f"💾 سقف حجم: {fmt_bytes(limit_bytes)}\n"
            f"⏳ اعتبار: {exp_txt}\n\n"
            f"🔗 لینک اتصال:\n<code>{html.escape(key['accessUrl'])}</code>",
            parse_mode="HTML", reply_markup=back_menu())

    # --------------------------------------------------------------- list
    @dp.callback_query(F.data == "list")
    async def cb_list(cq: CallbackQuery) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        await cq.answer("در حال دریافت...")
        await render_list(cq.message)

    async def render_list(message: Message) -> None:
        multi = len(registry.ids()) > 1
        lines = ["📋 <b>لیست یوزرها</b>\n"]
        rows: list[list[InlineKeyboardButton]] = []
        total = 0
        for sid in registry.ids():
            api = registry.get(sid)
            sname = registry.meta(sid)["name"]
            try:
                keys = await api.list_keys()
                usage = await api.get_transfer_metrics()
            except OutlineError as e:
                lines.append(f"\n⚠️ <b>{html.escape(sname)}</b>: در دسترس نیست ({html.escape(str(e)[:60])})")
                continue
            local = {k["key_id"]: k for k in await db.keys_for(sid)}
            for k in keys:
                total += 1
                kid = k["id"]
                name = k.get("name") or f"کلید {kid}"
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
                    exp_txt = f"⏳ {dur} روز از اولین اتصال (در انتظار)"
                else:
                    exp_txt = f"⏳ {fmt_expiry(meta.get('expiry_ts'))}"
                status = "🔴 غیرفعال" if meta.get("disabled") else "🟢 فعال"
                tag = f" · {html.escape(sname)}" if multi else ""
                lines.append(
                    f"\n👤 <b>{html.escape(name)}</b> (id: <code>{kid}</code>){tag} {status}\n"
                    f"   📈 مصرف: {fmt_bytes(used)} از {fmt_bytes(limit_b)}\n   {exp_txt}")
                btn = f"⚙️ {name}" + (f" · {sname}" if multi else "")
                rows.append([InlineKeyboardButton(text=btn, callback_data=f"key:{sid}:{kid}")])
        if total == 0 and len(lines) == 1:
            return await message.edit_text("هیچ یوزری وجود ندارد.", reply_markup=back_menu())
        rows.append([InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="menu")])
        await message.edit_text("\n".join(lines), parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # --------------------------------------------------------- key actions
    def key_menu(sid: str, kid: str, disabled: bool) -> InlineKeyboardMarkup:
        toggle = (InlineKeyboardButton(text="✅ فعال‌سازی", callback_data=f"enable:{sid}:{kid}")
                  if disabled else
                  InlineKeyboardButton(text="⛔️ غیرفعال‌سازی", callback_data=f"disable:{sid}:{kid}"))
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 نمایش لینک", callback_data=f"link:{sid}:{kid}")],
            [InlineKeyboardButton(text="✏️ تغییر نام", callback_data=f"rename:{sid}:{kid}"),
             InlineKeyboardButton(text="💾 تغییر سقف", callback_data=f"limit:{sid}:{kid}")],
            [toggle],
            [InlineKeyboardButton(text="➕ تمدید ۳۰ روز", callback_data=f"extend:{sid}:{kid}")],
            [InlineKeyboardButton(text="🗑 حذف یوزر", callback_data=f"del:{sid}:{kid}")],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="list")],
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
        await cq.message.edit_text(f"⚙️ مدیریت یوزر <code>{kid}</code>:",
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
            return await cq.answer(f"خطا: {e}", show_alert=True)
        name = key.get("name") or f"کلید {kid}"
        await cq.message.answer(
            f"🔗 لینک اتصال <b>{html.escape(name)}</b>:\n"
            f"<code>{html.escape(key.get('accessUrl', ''))}</code>", parse_mode="HTML")
        await cq.answer()

    @dp.callback_query(F.data.startswith("rename:"))
    async def cb_rename(cq: CallbackQuery, state: FSMContext) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        sid, kid = parse_cb(cq.data)
        await state.set_state(EditKey.rename)
        await state.update_data(sid=sid, kid=kid)
        await cq.message.edit_text("✏️ نام جدید را وارد کنید:")
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
            return await msg.answer(f"❌ تغییر نام ناموفق بود:\n{e}")
        if not await db.get_key(sid, kid):
            await db.add_key(sid, kid, name, None, None)
        else:
            await db.set_name(sid, kid, name)
        await msg.answer(f"✅ نام به <b>{html.escape(name)}</b> تغییر کرد.",
                         parse_mode="HTML", reply_markup=back_menu())

    @dp.callback_query(F.data.startswith("limit:"))
    async def cb_limit(cq: CallbackQuery, state: FSMContext) -> None:
        if not await is_admin(cq.from_user.id):
            return await deny(cq)
        sid, kid = parse_cb(cq.data)
        await state.set_state(EditKey.limit)
        await state.update_data(sid=sid, kid=kid)
        await cq.message.edit_text("💾 سقف حجم جدید را به <b>گیگابایت</b> وارد کنید "
                                   "(<code>0</code> = نامحدود):", parse_mode="HTML")
        await cq.answer()

    @dp.message(EditKey.limit)
    async def step_set_limit(msg: Message, state: FSMContext) -> None:
        try:
            gb = float(msg.text.strip().replace(",", "."))
            if gb < 0:
                raise ValueError
        except ValueError:
            return await msg.answer("⚠️ لطفاً یک عدد معتبر وارد کنید (مثلاً 50 یا 0).")
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
                return await msg.answer(f"❌ تغییر سقف ناموفق بود:\n{e}")
        await db.set_limit(sid, kid, limit_bytes)
        await msg.answer(f"✅ سقف حجم به {fmt_bytes(limit_bytes)} تغییر کرد.",
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
            return await cq.answer(f"خطا: {e}", show_alert=True)
        await db.set_disabled(sid, kid, True)
        await cq.answer("یوزر غیرفعال شد ⛔️", show_alert=True)
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
            return await cq.answer(f"خطا: {e}", show_alert=True)
        await db.set_disabled(sid, kid, False)
        await cq.answer("یوزر فعال شد ✅", show_alert=True)
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
            return await cq.answer(f"خطا: {e}", show_alert=True)
        await cq.answer("یوزر حذف شد ✅", show_alert=True)
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
                return await cq.answer(f"تمدید شد ولی فعال‌سازی خطا داد: {e}", show_alert=True)
        await cq.answer("۳۰ روز تمدید شد ✅", show_alert=True)
        await render_list(cq.message)

    return dp
