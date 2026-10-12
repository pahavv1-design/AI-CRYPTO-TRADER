import asyncio
import logging
import os
from datetime import datetime, timezone

import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


# =====================================
# CONFIG
# =====================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "trader.db")

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не установлен")

if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID не установлен")


logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()


# =====================================
# DATABASE
# =====================================

DEFAULT_SETTINGS = {
    "balance": "100.0",
    "initial_balance": "100.0",
    "strategy": "AUTO",
    "daily_loss_limit": "10",
    "max_trade_percent": "50",
    "autopilot": "OFF",
    "emergency_stop": "OFF",
}


async def init_db():

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                side TEXT,
                amount REAL,
                price REAL,
                pnl REAL,
                created_at TEXT
            )
        """)

        for key, value in DEFAULT_SETTINGS.items():

            await db.execute(
                """
                INSERT OR IGNORE INTO settings
                (key, value)
                VALUES (?, ?)
                """,
                (key, value)
            )

        await db.commit()


async def get_setting(key):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ) as cursor:

            row = await cursor.fetchone()

            return row[0] if row else None


async def set_setting(key, value):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (key, str(value))
        )

        await db.commit()


# =====================================
# SECURITY
# =====================================

def is_admin(user_id):

    return user_id == ADMIN_ID


@dp.message.outer_middleware()
async def message_security(handler, event, data):

    if not event.from_user:
        return

    if not is_admin(event.from_user.id):
        return

    return await handler(event, data)


@dp.callback_query.outer_middleware()
async def callback_security(handler, event, data):

    if not event.from_user:
        return

    if not is_admin(event.from_user.id):
        await event.answer(
            "Доступ запрещён",
            show_alert=True
        )
        return

    return await handler(event, data)


# =====================================
# STATES
# =====================================

class SettingsStates(StatesGroup):

    waiting_loss_limit = State()

    waiting_trade_percent = State()

    waiting_demo_balance = State()


# =====================================
# KEYBOARDS
# =====================================

def main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Запустить",
                    callback_data="start_auto"
                ),
                InlineKeyboardButton(
                    text="🛑 Остановить",
                    callback_data="stop_auto"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Баланс",
                    callback_data="balance"
                ),
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="stats"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧠 ИИ-агенты",
                    callback_data="agents"
                ),
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    callback_data="settings"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚨 ЭКСТРЕННАЯ ОСТАНОВКА",
                    callback_data="emergency"
                )
            ]
        ]
    )


def settings_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Стратегия",
                    callback_data="strategy"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📉 Дневной лимит убытка",
                    callback_data="change_loss"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💵 Максимальный размер сделки",
                    callback_data="change_trade"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Изменить демобаланс",
                    callback_data="change_balance"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Главное меню",
                    callback_data="menu"
                )
            ]
        ]
    )


def strategy_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Осторожная",
                    callback_data="strategy_SAFE"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔴 Агрессивная",
                    callback_data="strategy_AGGRESSIVE"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Полный автопилот",
                    callback_data="strategy_AUTO"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Назад",
                    callback_data="settings"
                )
            ]
        ]
    )


def back_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Главное меню",
                    callback_data="menu"
                )
            ]
        ]
    )


# =====================================
# MAIN MENU
# =====================================

async def build_main_text():

    balance = float(
        await get_setting("balance")
    )

    strategy = await get_setting("strategy")

    loss = await get_setting("daily_loss_limit")

    autopilot = await get_setting("autopilot")

    emergency = await get_setting("emergency_stop")

    status = "🟢 ВКЛЮЧЁН" if autopilot == "ON" else "🔴 ВЫКЛЮЧЕН"

    if emergency == "ON":
        status = "🚨 АВАРИЙНАЯ ОСТАНОВКА"

    return (
        "🤖 AI CRYPTO TRADER v1.0\n\n"
        f"💰 Демо-баланс: ${balance:.2f}\n"
        f"📊 Стратегия: {strategy}\n"
        f"📉 Дневной лимит: {loss}%\n"
        f"⚡ Автопилот: {status}\n\n"
        "🧠 ИИ-агенты: ещё не подключены\n"
        "🏦 Биржа: ещё не подключена\n"
        "💵 Торговля: DEMO\n\n"
        "⚙️ Выбери действие:"
    )


@dp.message(CommandStart())
async def start_command(message: Message):

    text = await build_main_text()

    await message.answer(
        text,
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery):

    text = await build_main_text()

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard()
    )

    await callback.answer()


# =====================================
# AUTOPILOT
# =====================================

@dp.callback_query(F.data == "start_auto")
async def start_autopilot(callback: CallbackQuery):

    emergency = await get_setting("emergency_stop")

    if emergency == "ON":

        await callback.answer(
            "Сначала необходимо снять аварийную блокировку.",
            show_alert=True
        )

        return

    await set_setting("autopilot", "ON")

    await callback.message.edit_text(
        "🚀 АВТОПИЛОТ ВКЛЮЧЁН\n\n"
        "⚠️ Торговый движок пока не подключён.\n"
        "Реальные и виртуальные сделки не выполняются.",
        reply_markup=back_keyboard()
    )

    await callback.answer()


@dp.callback_query(F.data == "stop_auto")
async def stop_autopilot(callback: CallbackQuery):

    await set_setting("autopilot", "OFF")

    await callback.message.edit_text(
        "🛑 Автопилот остановлен.\n\n"
        "Новые сделки запрещены.",
        reply_markup=back_keyboard()
    )

    await callback.answer()


# =====================================
# EMERGENCY STOP
# =====================================

@dp.callback_query(F.data == "emergency")
async def emergency_stop(callback: CallbackQuery):

    await set_setting("autopilot", "OFF")

    await set_setting("emergency_stop", "ON")

    await callback.message.edit_text(
        "🚨 АВАРИЙНАЯ БЛОКИРОВКА\n\n"
        "Автопилот выключен.\n"
        "Повторный запуск заблокирован.\n\n"
        "⚠️ В этой версии нет открытых ордеров.\n"
        "После подключения биржи потребуется "
        "отдельная логика отмены ордеров.\n\n"
        "Для снятия блокировки открой настройки.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔓 Снять блокировку",
                        callback_data="unlock"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Главное меню",
                        callback_data="menu"
                    )
                ]
            ]
        )
    )

    await callback.answer()


@dp.callback_query(F.data == "unlock")
async def unlock(callback: CallbackQuery):

    await set_setting("emergency_stop", "OFF")

    await callback.message.edit_text(
        "🔓 Аварийная блокировка снята.\n\n"
        "Автопилот остаётся выключенным.",
        reply_markup=back_keyboard()
    )

    await callback.answer()


# =====================================
# BALANCE
# =====================================

@dp.callback_query(F.data == "balance")
async def balance_callback(callback: CallbackQuery):

    balance = float(
        await get_setting("balance")
    )

    initial = float(
        await get_setting("initial_balance")
    )

    pnl = balance - initial

    pnl_percent = (
        pnl / initial * 100
        if initial > 0
        else 0
    )

    await callback.message.edit_text(
        "💰 ДЕМО-ПОРТФЕЛЬ\n\n"
        f"💵 Баланс: ${balance:.2f}\n"
        f"🏦 Начальный баланс: ${initial:.2f}\n"
        f"📈 PNL: ${pnl:+.2f}\n"
        f"📊 Доходность: {pnl_percent:+.2f}%\n\n"
        "⚠️ Реальные средства не используются.",
        reply_markup=back_keyboard()
    )

    await callback.answer()


# =====================================
# STATISTICS
# =====================================

@dp.callback_query(F.data == "stats")
async def stats_callback(callback: CallbackQuery):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades"
        ) as cursor:

            row = await cursor.fetchone()

            total_trades = row[0]
            total_pnl = row[1]

    await callback.message.edit_text(
        "📊 СТАТИСТИКА\n\n"
        f"🔄 Всего сделок: {total_trades}\n"
        f"💰 Реализованный PNL: ${total_pnl:+.2f}\n\n"
        "🧠 Торговый движок пока не подключён.\n"
        "Статистика начнёт заполняться после "
        "реализации демоторговли.",
        reply_markup=back_keyboard()
    )

    await callback.answer()


# =====================================
# AI AGENTS
# =====================================

@dp.callback_query(F.data == "agents")
async def agents_callback(callback: CallbackQuery):

    await callback.message.edit_text(
        "🧠 ИИ-АГЕНТЫ\n\n"
        "🤖 Агент №1 — Аналитик\n"
        "Статус: ⏳ Не подключён\n\n"
        "🛡️ Агент №2 — Критик\n"
        "Статус: ⏳ Не подключён\n\n"
        "🔌 Планируемое подключение: OpenRouter\n\n"
        "На следующем этапе добавим "
        "анализ криптовалют с помощью "
        "бесплатных моделей.",
        reply_markup=back_keyboard()
    )

    await callback.answer()


# =====================================
# SETTINGS
# =====================================

@dp.callback_query(F.data == "settings")
async def settings_callback(callback: CallbackQuery):

    strategy = await get_setting("strategy")

    loss = await get_setting("daily_loss_limit")

    trade = await get_setting("max_trade_percent")

    await callback.message.edit_text(
        "⚙️ НАСТРОЙКИ\n\n"
        f"🤖 Стратегия: {strategy}\n"
        f"📉 Дневной лимит: {loss}%\n"
        f"💵 Максимальная сделка: {trade}% баланса\n\n"
        "Выбери параметр:",
        reply_markup=settings_keyboard()
    )

    await callback.answer()


# =====================================
# STRATEGIES
# =====================================

@dp.callback_query(F.data == "strategy")
async def strategy_callback(callback: CallbackQuery):

    await callback.message.edit_text(
        "🤖 ВЫБОР СТРАТЕГИИ\n\n"
        "🟢 SAFE — осторожная\n\n"
        "🔴 AGGRESSIVE — агрессивная\n\n"
        "🤖 AUTO — полный автопилот\n\n"
        "⚠️ Пока это сохранённые настройки. "
        "Реализуем их торговую логику позже.",
        reply_markup=strategy_keyboard()
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("strategy_"))
async def select_strategy(callback: CallbackQuery):

    strategy = callback.data.replace(
        "strategy_", ""
    )

    allowed = {
        "SAFE",
        "AGGRESSIVE",
        "AUTO"
    }

    if strategy not in allowed:
        await callback.answer("Неверная стратегия")
        return

    await set_setting("strategy", strategy)

    await callback.message.edit_text(
        f"✅ Стратегия изменена: {strategy}",
        reply_markup=back_keyboard()
    )

    await callback.answer()


# =====================================
# CHANGE DAILY LOSS
# =====================================

@dp.callback_query(F.data == "change_loss")
async def change_loss(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        SettingsStates.waiting_loss_limit
    )

    await callback.message.answer(
        "📉 Введи максимальный дневной убыток "
        "в процентах.\n\n"
        "Например: 10\n\n"
        "Допустимый диапазон: от 1 до 30%."
    )

    await callback.answer()


@dp.message(SettingsStates.waiting_loss_limit)
async def save_loss(
    message: Message,
    state: FSMContext
):

    try:

        value = float(
            message.text.replace(",", ".")
        )

        if not 1 <= value <= 30:
            raise ValueError

    except (ValueError, AttributeError):

        await message.answer(
            "❌ Введи число от 1 до 30."
        )

        return

    await set_setting(
        "daily_loss_limit",
        value
    )

    await state.clear()

    await message.answer(
        f"✅ Дневной лимит установлен: {value}%",
        reply_markup=back_keyboard()
    )


# =====================================
# CHANGE TRADE SIZE
# =====================================

@dp.callback_query(F.data == "change_trade")
async def change_trade(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        SettingsStates.waiting_trade_percent
    )

    await callback.message.answer(
        "💵 Введи максимальный размер "
        "одной сделки в процентах баланса.\n\n"
        "Например: 50\n\n"
        "Допустимый диапазон: от 1 до 100%."
    )

    await callback.answer()


@dp.message(SettingsStates.waiting_trade_percent)
async def save_trade(
    message: Message,
    state: FSMContext
):

    try:

        value = float(
            message.text.replace(",", ".")
        )

        if not 1 <= value <= 100:
            raise ValueError

    except (ValueError, AttributeError):

        await message.answer(
            "❌ Введи число от 1 до 100."
        )

        return

    await set_setting(
        "max_trade_percent",
        value
    )

    await state.clear()

    await message.answer(
        f"✅ Максимальная сделка: {value}% баланса",
        reply_markup=back_keyboard()
    )


# =====================================
# CHANGE DEMO BALANCE
# =====================================

@dp.callback_query(F.data == "change_balance")
async def change_balance(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        SettingsStates.waiting_demo_balance
    )

    await callback.message.answer(
        "💰 Введи новый виртуальный баланс.\n\n"
        "Например: 100\n\n"
        "Допустимый диапазон: от $10 до $100000.\n\n"
        "⚠️ Это сброс стартовой суммы, "
        "а не пополнение реального счёта."
    )

    await callback.answer()


@dp.message(SettingsStates.waiting_demo_balance)
async def save_balance(
    message: Message,
    state: FSMContext
):

    try:

        value = float(
            message.text.replace(",", ".")
        )

        if not 10 <= value <= 100000:
            raise ValueError

    except (ValueError, AttributeError):

        await message.answer(
            "❌ Введи сумму от 10 до 100000."
        )

        return

    await set_setting(
        "balance",
        value
    )

    await set_setting(
        "initial_balance",
        value
    )

    await state.clear()

    await message.answer(
        f"✅ Демо-баланс установлен: ${value:.2f}\n\n"
        "⚠️ История предыдущих сделок "
        "при этом не удаляется.",
        reply_markup=back_keyboard()
    )


# =====================================
# START APPLICATION
# =====================================

async def main():

    await init_db()

    logging.info("AI CRYPTO TRADER STARTED")

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(bot)


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logging.info("Bot stopped")
