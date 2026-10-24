import os
import json
import time
import asyncio
import logging
import re

from decimal import Decimal
from datetime import datetime, timezone

import aiohttp
import aiosqlite

from web3 import Web3

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    ReplyKeyboardRemove
)

VERSION = "3.3 SAFE"

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

OPENROUTER_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

ZEROX_KEY = os.getenv(
    "ZEROX_API_KEY", ""
).strip()

WALLET_ADDRESS = os.getenv(
    "WALLET_ADDRESS", ""
).strip()

RPC = os.getenv(
    "BASE_RPC_URL",
    "https://mainnet.base.org"
)

DB = os.getenv(
    "DB_PATH",
    "/app/trader_v33.db"
)

CHAIN_ID = 8453

TOKENS = {
    "USDC": (
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        6
    ),
    "WETH": (
        "0x4200000000000000000000000000000000000006",
        18
    ),
    "cbBTC": (
        "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        8
    ),
    "AERO": (
        "0x940181a94A35A4569E4529A3CDFB74E38FD98631",
        18
    )
}

ASSETS = ["WETH", "cbBTC", "AERO"]

ABI = [{
    "inputs": [{"name": "owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
}]

logging.basicConfig(level=logging.INFO)

logging.getLogger("httpx").setLevel(logging.WARNING)

bot = Bot(token=TOKEN)
dp = Dispatcher()

w3 = Web3(Web3.HTTPProvider(RPC))

session = None

cycle_lock = asyncio.Lock()


async def init_db():

    os.makedirs(
        os.path.dirname(os.path.abspath(DB)),
        exist_ok=True
    )

    async with aiosqlite.connect(DB) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created INTEGER,
                side TEXT,
                symbol TEXT,
                amount TEXT,
                quantity TEXT,
                pnl TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS positions(
                symbol TEXT PRIMARY KEY,
                quantity TEXT,
                cost TEXT
            )
        """)

        await db.commit()

    defaults = {
        "running": "0",
        "mode": "DEMO",
        "interval": "5",
        "demo_cash": "20",
        "emergency": "0",
        "daily_limit": "10",
        "ai_calls": "0",
        "ai_day": ""
    }

    async with aiosqlite.connect(DB) as db:

        for key, value in defaults.items():

            await db.execute(
                """
                INSERT OR IGNORE INTO settings
                VALUES(?,?)
                """,
                (key, value)
            )

        # Prevent an old REAL setting from
        # automatically enabling live transactions.
        await db.execute(
            """
            UPDATE settings SET value='DEMO'
            WHERE key='mode'
            """
        )

        await db.commit()


async def get(key, default=""):

    async with aiosqlite.connect(DB) as db:

        async with db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ) as cursor:

            row = await cursor.fetchone()

    return row[0] if row else default


async def put(key, value):

    async with aiosqlite.connect(DB) as db:

        await db.execute(
            """
            INSERT INTO settings VALUES(?,?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, str(value))
        )

        await db.commit()


def main_menu():

    buttons = [
        [
            InlineKeyboardButton(
                text="🚀 Старт",
                callback_data="start"
            ),
            InlineKeyboardButton(
                text="🛑 Стоп",
                callback_data="stop"
            )
        ],
        [
            InlineKeyboardButton(
                text="💰 Портфель",
                callback_data="portfolio"
            ),
            InlineKeyboardButton(
                text="👛 MetaMask",
                callback_data="wallet"
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="stats"
            ),
            InlineKeyboardButton(
                text="🧠 ИИ",
                callback_data="ai"
            )
        ],
        [
            InlineKeyboardButton(
                text="💱 Проверить 0x",
                callback_data="zerox"
            )
        ],
        [
            InlineKeyboardButton(
                text="🔍 Анализ сейчас",
                callback_data="analyze"
            )
        ],
        [
            InlineKeyboardButton(
                text="⚙️ Настройки",
                callback_data="settings"
            )
        ],
        [
            InlineKeyboardButton(
                text="🚨 ЭКСТРЕННАЯ ОСТАНОВКА",
                callback_data="emergency"
            )
        ]
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


def settings_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="5 мин",
                    callback_data="interval_5"
                ),
                InlineKeyboardButton(
                    text="10 мин",
                    callback_data="interval_10"
                ),
                InlineKeyboardButton(
                    text="15 мин",
                    callback_data="interval_15"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Меню",
                    callback_data="menu"
                )
            ]
        ]
    )


async def notify(text):

    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception:
        logging.exception("Telegram notification")


def token_balance(symbol):

    if not WALLET_ADDRESS:
        return Decimal("0")

    address, decimals = TOKENS[symbol]

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=ABI
    )

    raw = contract.functions.balanceOf(
        Web3.to_checksum_address(WALLET_ADDRESS)
    ).call()

    return Decimal(raw) / Decimal(10 ** decimals)


def eth_balance():

    if not WALLET_ADDRESS:
        return Decimal("0")

    raw = w3.eth.get_balance(
        Web3.to_checksum_address(WALLET_ADDRESS)
    )

    return Decimal(raw) / Decimal(10 ** 18)


async def quote(sell, buy, amount):

    if not ZEROX_KEY:
        raise RuntimeError("Нет ZEROX_API_KEY")

    if not WALLET_ADDRESS:
        raise RuntimeError("Нет WALLET_ADDRESS")

    sell_address, sell_decimals = TOKENS[sell]
    buy_address, buy_decimals = TOKENS[buy]

    raw = int(
        Decimal(str(amount))
        * Decimal(10 ** sell_decimals)
    )

    params = {
        "chainId": CHAIN_ID,
        "sellToken": sell_address,
        "buyToken": buy_address,
        "sellAmount": str(raw),
        "taker": WALLET_ADDRESS
    }

    headers = {
        "0x-api-key": ZEROX_KEY,
        "0x-version": "v2"
    }

    async with session.get(
        "https://api.0x.org/swap/allowance-holder/price",
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=25)
    ) as response:

        data = await response.json(
            content_type=None
        )

        if response.status != 200:
            raise RuntimeError(
                f"0x HTTP {response.status}"
            )

        if not data.get("liquidityAvailable", True):
            raise RuntimeError("Нет ликвидности")

        received = (
            Decimal(str(data["buyAmount"]))
            / Decimal(10 ** buy_decimals)
        )

        return received


async def free_models():

    models = ["openrouter/free"]

    try:

        async with session.get(
            "https://openrouter.ai/api/v1/models",
            timeout=aiohttp.ClientTimeout(total=15)
        ) as response:

            data = await response.json()

        for model in data.get("data", []):

            model_id = model.get("id", "")

            if model_id.endswith(":free"):
                models.append(model_id)

        return models[:12]

    except Exception:
        return models


async def ask_ai(prompt, data):

    if not OPENROUTER_KEY:
        raise RuntimeError("Нет OPENROUTER_API_KEY")

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    if await get("ai_day") != today:

        await put("ai_day", today)
        await put("ai_calls", "0")

    models = await free_models()

    last_error = "Нет ответа"

    for attempt in range(5):

        calls = int(await get("ai_calls", "0"))

        if calls >= 100:
            raise RuntimeError(
                "Дневной лимит ИИ исчерпан"
            )

        await put("ai_calls", calls + 1)

        model = models[attempt % len(models)]

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": prompt
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        data,
                        ensure_ascii=False
                    )
                }
            ],
            "temperature": 0.2,
            "max_tokens": 1500
        }

        headers = {
            "Authorization": (
                "Bearer " + OPENROUTER_KEY
            ),
            "Content-Type": "application/json"
        }

        try:

            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:

                result = await response.json(
                    content_type=None
                )

                if response.status != 200:

                    last_error = (
                        f"HTTP {response.status}"
                    )

                    await asyncio.sleep(2)
                    continue

            choices = result.get("choices") or []

            if not choices:
                last_error = "Нет choices"
                continue

            content = (
                choices[0]
                .get("message", {})
                .get("content")
            )

            if not content:
                last_error = "Пустой ответ"
                continue

            content = content.strip()

            if content.startswith("```"):

                content = re.sub(
                    r"^```(?:json)?\s*",
                    "",
                    content
                )

                content = re.sub(
                    r"\s*```$",
                    "",
                    content
                )

            result_json = json.loads(content)

            if isinstance(result_json, dict):
                return result_json

            last_error = "Некорректный JSON"

        except Exception as e:

            last_error = type(e).__name__

        await asyncio.sleep(2)

    raise RuntimeError(
        "Бесплатные ИИ недоступны: "
        + last_error
    )


ANALYST_PROMPT = """
Ты аналитик криптовалют.

Выбирай BUY, SELL или HOLD.

Монеты:
WETH, cbBTC, AERO.

Покупки 2-5 USDC.

Повторные покупки разрешены.

Не выдумывай исторические данные.

Ответ JSON:
{
 "action":"HOLD",
 "symbol":"WETH",
 "amount":2,
 "reason":"Причина"
}
"""


CRITIC_PROMPT = """
Ты независимый риск-менеджер.

Ответ APPROVE, MODIFY или REJECT.

Учитывай баланс и открытые позиции.

Нельзя использовать кредитное плечо.

Ответ JSON:
{
 "decision":"REJECT",
 "amount":2,
 "reason":"Причина"
}
"""


async def demo_position(symbol):

    async with aiosqlite.connect(DB) as db:

        async with db.execute(
            """
            SELECT quantity,cost
            FROM positions
            WHERE symbol=?
            """,
            (symbol,)
        ) as cursor:

            row = await cursor.fetchone()

    if not row:
        return Decimal("0"), Decimal("0")

    return Decimal(row[0]), Decimal(row[1])


async def save_position(symbol, qty, cost):

    async with aiosqlite.connect(DB) as db:

        await db.execute(
            """
            INSERT INTO positions VALUES(?,?,?)
            ON CONFLICT(symbol)
            DO UPDATE SET
            quantity=excluded.quantity,
            cost=excluded.cost
            """,
            (symbol, str(qty), str(cost))
        )

        await db.commit()


async def demo_trade(action, symbol, amount):

    cash = Decimal(
        await get("demo_cash", "20")
    )

    qty, cost = await demo_position(symbol)

    if action == "BUY":

        if cash < amount:
            return "Недостаточно USDC"

        received = await quote(
            "USDC", symbol, amount
        )

        await save_position(
            symbol,
            qty + received,
            cost + amount
        )

        await put(
            "demo_cash",
            cash - amount
        )

        pnl = Decimal("0")
        trade_qty = received

        text = (
            f"🟢 DEMO BUY {symbol}\n"
            f"Потрачено: ${amount:.4f}\n"
            f"Получено: {received:.8f}"
        )

    else:

        if qty <= 0:
            return "Нет позиции"

        received = await quote(
            symbol, "USDC", qty
        )

        pnl = received - cost

        await save_position(
            symbol,
            0,
            0
        )

        await put(
            "demo_cash",
            cash + received
        )

        trade_qty = qty
        amount = received

        text = (
            f"🔴 DEMO SELL {symbol}\n"
            f"Получено: ${received:.4f}\n"
            f"PNL: ${pnl:+.4f}"
        )

    async with aiosqlite.connect(DB) as db:

        await db.execute(
            """
            INSERT INTO trades(
                created,side,symbol,
                amount,quantity,pnl
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                int(time.time()),
                action,
                symbol,
                str(amount),
                str(trade_qty),
                str(pnl)
            )
        )

        await db.commit()

    return text


async def trading_cycle(manual=False):

    if cycle_lock.locked():
        return

    async with cycle_lock:

        if await get("emergency") == "1":
            return

        if not manual and await get("running") != "1":
            return

        try:

            cash = Decimal(
                await get("demo_cash", "20")
            )

            market = {}

            for symbol in ASSETS:

                try:

                    received = await quote(
                        "USDC",
                        symbol,
                        Decimal("5")
                    )

                    qty, cost = await demo_position(
                        symbol
                    )

                    market[symbol] = {
                        "price": float(
                            Decimal("5") / received
                        ),
                        "position": float(qty),
                        "cost": float(cost)
                    }

                except Exception:
                    continue

            if not market:
                raise RuntimeError(
                    "Нет котировок"
                )

            data = {
                "mode": "DEMO",
                "free_usdc": float(cash),
                "market": market,
                "trade_min": 2,
                "trade_max": 5
            }

            analyst = await ask_ai(
                ANALYST_PROMPT,
                data
            )

            action = str(
                analyst.get("action", "HOLD")
            ).upper()

            symbol = str(
                analyst.get("symbol", "WETH")
            )

            if symbol not in ASSETS:
                return

            if action not in ("BUY", "SELL"):

                if manual:
                    await notify(
                        "🧠 HOLD\n"
                        + str(
                            analyst.get("reason", "")
                        )[:300]
                    )

                return

            amount = Decimal(
                str(analyst.get("amount", 2))
            )

            if not amount.is_finite():
                return

            amount = min(
                Decimal("5"),
                max(Decimal("2"), amount)
            )

            critic = await ask_ai(
                CRITIC_PROMPT,
                {
                    "proposal": analyst,
                    "portfolio": data
                }
            )

            decision = str(
                critic.get("decision", "REJECT")
            ).upper()

            if decision not in (
                "APPROVE",
                "MODIFY"
            ):
                return

            if action == "BUY":

                amount = min(amount, cash)

                if amount < 2:
                    return

            if await get("running") != "1":
                return

            if await get("emergency") == "1":
                return

            result = await demo_trade(
                action,
                symbol,
                amount
            )

            await notify(
                "🧠 СОВЕЩАНИЕ ИИ\n"
                f"Аналитик: {action} {symbol}\n"
                f"Критик: {decision}\n\n"
                + result
            )

        except Exception as e:

            logging.exception("Trading cycle")

            await notify(
                "⚠️ Ошибка:\n"
                + str(e)[:300]
            )


async def background():

    last_cycle = 0

    while True:

        try:

            interval = int(
                await get("interval", "5")
            )

            if await get("running") == "1":

                if (
                    time.time() - last_cycle
                    >= interval * 60
                ):

                    last_cycle = time.time()

                    await trading_cycle()

        except Exception:
            logging.exception("Background")

        await asyncio.sleep(15)


@dp.message(Command("start"))
@dp.message(Command("menu"))
async def start(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        "Компактное меню:",
        reply_markup=ReplyKeyboardRemove()
    )

    await message.answer(
        f"🤖 AI CRYPTO TRADER {VERSION}\n"
        "Режим: DEMO\n"
        f"Автопилот: {await get('running')}",
        reply_markup=main_menu()
    )


@dp.callback_query()
async def callbacks(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    action = callback.data

    await callback.answer()

    if action == "menu":

        await callback.message.answer(
            "Главное меню:",
            reply_markup=main_menu()
        )

    elif action == "start":

        if await get("emergency") == "1":

            await callback.message.answer(
                "Экстренная блокировка активна."
            )

            return

        await put("running", "1")

        await callback.message.answer(
            "🚀 DEMO-автопилот запущен."
        )

    elif action == "stop":

        await put("running", "0")

        await callback.message.answer(
            "🛑 Автопилот остановлен."
        )

    elif action == "emergency":

        await put("running", "0")
        await put("emergency", "1")

        await callback.message.answer(
            "🚨 Экстренная остановка."
        )

    elif action == "wallet":

        try:

            text = (
                "👛 BASE\n\n"
                f"{WALLET_ADDRESS}\n\n"
                f"ETH: {eth_balance():.8f}\n"
            )

            for symbol in TOKENS:

                text += (
                    f"{symbol}: "
                    f"{token_balance(symbol):.8f}\n"
                )

            await callback.message.answer(text)

        except Exception as e:

            await callback.message.answer(
                str(e)[:300]
            )

    elif action == "portfolio":

        cash = Decimal(
            await get("demo_cash", "20")
        )

        await callback.message.answer(
            "💰 DEMO\n"
            f"Свободно: ${cash:.4f}"
        )

    elif action == "stats":

        async with aiosqlite.connect(DB) as db:

            async with db.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(CAST(pnl AS REAL)),0)
                FROM trades
                """
            ) as cursor:

                row = await cursor.fetchone()

        await callback.message.answer(
            "📊 DEMO\n"
            f"Сделок: {row[0]}\n"
            f"Реализованный PNL: ${row[1]:+.4f}"
        )

    elif action == "ai":

        await callback.message.answer(
            "🧠 Два бесплатных ИИ\n"
            "Аналитик + критик\n"
            "Повторные попытки при ошибке.\n"
            f"Запросов: {await get('ai_calls')}"
        )

    elif action == "zerox":

        try:

            received = await quote(
                "USDC",
                "WETH",
                Decimal("5")
            )

            await callback.message.answer(
                "✅ 0x работает\n"
                f"5 USDC ≈ {received:.8f} WETH"
            )

        except Exception as e:

            await callback.message.answer(
                str(e)[:300]
            )

    elif action == "analyze":

        await callback.message.answer(
            "Запускаю анализ..."
        )

        asyncio.create_task(
            trading_cycle(manual=True)
        )

    elif action == "settings":

        await callback.message.answer(
            "⚙️ DEMO SAFE\n"
            f"Интервал: {await get('interval')} мин\n"
            "Реальная торговля заблокирована "
            "до проверки исполнения.",
            reply_markup=settings_menu()
        )

    elif action.startswith("interval_"):

        value = int(action.split("_")[1])

        if value in (5, 10, 15):

            await put("interval", value)

            await callback.message.answer(
                f"Интервал: {value} минут."
            )


async def main():

    global session

    await init_db()

    if not w3.is_connected():
        raise RuntimeError("Base RPC недоступен")

    if w3.eth.chain_id != CHAIN_ID:
        raise RuntimeError("Неверная сеть")

    session = aiohttp.ClientSession()

    try:

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        await bot.set_my_commands([
            BotCommand(
                command="start",
                description="Открыть меню"
            ),
            BotCommand(
                command="menu",
                description="Показать меню"
            )
        ])

        await notify(
            f"✅ AI CRYPTO TRADER {VERSION}\n"
            "Реальная торговля заблокирована.\n"
            "DEMO доступен."
        )

        asyncio.create_task(background())

        await dp.start_polling(bot)

    finally:

        await session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
