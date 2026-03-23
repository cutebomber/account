"""
aiogram handlers — all bot commands and callback buttons.
"""

import logging
from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,

)

from config import OWNER_ID
from session_manager import SessionManager

logger = logging.getLogger(__name__)
router = Router()


# ── FSM states ────────────────────────────────────────────────────────────────

class LoginFlow(StatesGroup):
    waiting_phone   = State()
    waiting_otp     = State()
    waiting_2fa     = State()


class SendFlow(StatesGroup):
    waiting_phone     = State()
    waiting_recipient = State()
    waiting_text      = State()


# ── Guard: only owner can use the bot ────────────────────────────────────────

def _is_owner(message: Message) -> bool:
    return message.from_user.id == OWNER_ID


def _owner_only(handler):
    async def wrapper(message: Message, *args, **kwargs):
        if not _is_owner(message):
            await message.answer("⛔ Unauthorised.")
            return
        return await handler(message, *args, **kwargs)
    return wrapper


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, session_manager: SessionManager):
    if not _is_owner(message):
        await message.answer("⛔ Unauthorised.")
        return

    accounts = session_manager.accounts
    count = len(accounts)
    connected = sum(1 for a in accounts if a.get("connected"))

    buttons = [
        [InlineKeyboardButton(text="➕ Add account", callback_data="add_account")],
        [InlineKeyboardButton(text="📋 List accounts", callback_data="list_accounts")],
        [InlineKeyboardButton(text="✉️ Send message", callback_data="send_message")],
    ]

    await message.answer(
        f"👋 *Telegram Session Backup Bot*\n\n"
        f"📱 Saved accounts: *{count}* ({connected} connected)\n\n"
        f"Choose an action:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )


# ── /addaccount ───────────────────────────────────────────────────────────────

@router.message(Command("addaccount"))
async def cmd_add_account(message: Message, state: FSMContext):
    if not _is_owner(message): return
    await state.set_state(LoginFlow.waiting_phone)
    await message.answer(
        "📱 Enter the phone number to add (with country code):\n"
        "Example: `+919876543210`",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "add_account")
async def cb_add_account(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(LoginFlow.waiting_phone)
    await call.message.answer(
        "📱 Enter the phone number to add (with country code):\n"
        "Example: `+919876543210`",
        parse_mode="Markdown",
    )


@router.message(LoginFlow.waiting_phone)
async def handle_phone_input(message: Message, state: FSMContext, session_manager: SessionManager):
    phone = message.text.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await message.answer("❌ Invalid format. Include country code e.g. `+919876543210`", parse_mode="Markdown")
        return

    await state.update_data(phone=phone)
    try:
        msg = await session_manager.begin_login(phone)
        await state.set_state(LoginFlow.waiting_otp)
        await message.answer(msg, parse_mode="Markdown")
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Error: {e}")


@router.message(LoginFlow.waiting_otp)
async def handle_otp_input(message: Message, state: FSMContext, session_manager: SessionManager):
    code = message.text.strip().replace(" ", "")
    data = await state.get_data()
    phone = data["phone"]

    try:
        result = await session_manager.complete_login(phone, code)
        await state.clear()
        await message.answer(result, parse_mode="Markdown")
    except ValueError as ve:
        if str(ve) == "2FA_REQUIRED":
            await state.set_state(LoginFlow.waiting_2fa)
            await message.answer("🔒 This account has 2FA enabled. Enter your password:")
        else:
            await state.clear()
            await message.answer(f"❌ {ve}")
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Login failed: {e}")


@router.message(LoginFlow.waiting_2fa)
async def handle_2fa_input(message: Message, state: FSMContext, session_manager: SessionManager):
    password = message.text.strip()
    data = await state.get_data()
    phone = data["phone"]

    # Delete the password message immediately for security
    try:
        await message.delete()
    except Exception:
        pass

    try:
        result = await session_manager.complete_2fa(phone, password)
        await state.clear()
        await message.answer(result, parse_mode="Markdown")
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ 2FA failed: {e}")


# ── /accounts ─────────────────────────────────────────────────────────────────

@router.message(Command("accounts"))
async def cmd_accounts(message: Message, session_manager: SessionManager):
    if not _is_owner(message): return
    await _show_accounts(message, session_manager)


@router.callback_query(F.data == "list_accounts")
async def cb_list_accounts(call: CallbackQuery, session_manager: SessionManager):
    await call.answer()
    await _show_accounts(call.message, session_manager)


async def _show_accounts(message: Message, session_manager: SessionManager):
    accounts = session_manager.accounts
    if not accounts:
        await message.answer("📭 No accounts saved yet. Use /addaccount to add one.")
        return

    buttons = []
    lines = ["📱 *Saved accounts:*\n"]
    for i, acc in enumerate(accounts, 1):
        status = "🟢" if acc.get("connected") else "🔴"
        name = acc.get("name", "Unknown")
        phone = acc["phone"]
        uname = f"@{acc['username']}" if acc.get("username") else ""
        lines.append(f"{i}. {status} *{name}* {uname}\n   `{phone}`")
        buttons.append([
            InlineKeyboardButton(text=f"🗑 Remove {phone}", callback_data=f"remove__{phone}"),
        ])

    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="back_home")])
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("remove__"))
async def cb_remove_account(call: CallbackQuery, session_manager: SessionManager):
    phone = call.data.split("__", 1)[1]
    await call.answer("Removing…")
    await session_manager.disconnect_account(phone)
    await call.message.edit_text(f"🗑 Account `{phone}` removed.", parse_mode="Markdown")


# ── /send ─────────────────────────────────────────────────────────────────────

@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext, session_manager: SessionManager):
    if not _is_owner(message): return
    await _start_send_flow(message, state, session_manager)


@router.callback_query(F.data == "send_message")
async def cb_send_message(call: CallbackQuery, state: FSMContext, session_manager: SessionManager):
    await call.answer()
    await _start_send_flow(call.message, state, session_manager)


async def _start_send_flow(message: Message, state: FSMContext, session_manager: SessionManager):
    accounts = [a for a in session_manager.accounts if a.get("connected")]
    if not accounts:
        await message.answer("❌ No connected accounts. Add one with /addaccount first.")
        return

    buttons = [
        [InlineKeyboardButton(
            text=f"{acc['name']} ({acc['phone']})",
            callback_data=f"pick_sender__{acc['phone']}",
        )]
        for acc in accounts
    ]
    await message.answer(
        "📤 Choose the account to *send from*:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )
    await state.set_state(SendFlow.waiting_phone)


@router.callback_query(F.data.startswith("pick_sender__"), SendFlow.waiting_phone)
async def cb_pick_sender(call: CallbackQuery, state: FSMContext):
    phone = call.data.split("__", 1)[1]
    await state.update_data(phone=phone)
    await state.set_state(SendFlow.waiting_recipient)
    await call.answer()
    await call.message.answer(
        f"✅ Sending from `{phone}`\n\n"
        "👤 Enter the recipient (username like `@someone`, phone, or Telegram user ID):",
        parse_mode="Markdown",
    )


@router.message(SendFlow.waiting_recipient)
async def handle_recipient(message: Message, state: FSMContext):
    await state.update_data(recipient=message.text.strip())
    await state.set_state(SendFlow.waiting_text)
    await message.answer("✏️ Now type the message text to send:")


@router.message(SendFlow.waiting_text)
async def handle_message_text(message: Message, state: FSMContext, session_manager: SessionManager):
    data = await state.get_data()
    phone = data["phone"]
    recipient = data["recipient"]
    text = message.text

    try:
        await session_manager.send_message(phone, recipient, text)
        await state.clear()
        await message.answer(f"✅ Message sent from `{phone}` to `{recipient}`!", parse_mode="Markdown")
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Failed to send: {e}")


# ── Back home ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "back_home")
async def cb_back_home(call: CallbackQuery, state: FSMContext, session_manager: SessionManager):
    await call.answer()
    await state.clear()
    # Re-use start logic
    accounts = session_manager.accounts
    count = len(accounts)
    connected = sum(1 for a in accounts if a.get("connected"))
    buttons = [
        [InlineKeyboardButton(text="➕ Add account", callback_data="add_account")],
        [InlineKeyboardButton(text="📋 List accounts", callback_data="list_accounts")],
        [InlineKeyboardButton(text="✉️ Send message", callback_data="send_message")],
    ]
    await call.message.edit_text(
        f"👋 *Telegram Session Backup Bot*\n\n"
        f"📱 Saved accounts: *{count}* ({connected} connected)\n\n"
        f"Choose an action:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown",
    )
