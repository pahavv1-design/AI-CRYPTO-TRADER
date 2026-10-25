import os
import re
import json
import time
import asyncio
import logging

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

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


# =====================================
# CONFIG
# =====================================

VERSION = "4.0"

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN", ""
).strip()

ADMIN_ID = int(
    os.getenv("ADMIN_ID", "0")
)

OXION_API_KEY = os.getenv(
    "OXION_API_KEY", ""
).strip()

OXION_BASE_URL = os.getenv(
    "OXION_BASE_URL",
    "https://oxionai.com/v1"
).rstrip("/")

OXION_ANALYST_MODEL = os.getenv(
    "OXION_ANALYST_MODEL",
    "gpt-5.6-luna"
)

OXION_CRITIC_MODEL = os.getenv(
    "OXION_CRITIC_MODEL",
    "deepseek-v4-pro-0813"
)

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

ZEROX_API_KEY = os.getenv(
    "ZEROX_API_KEY", ""
).strip()

WALLET_ADDRESS = os.getenv(
    "WALLET_ADDRESS", ""
).strip()

BASE_RPC_URL = os.getenv(
    "BASE_RPC_URL",
    "https://mainnet.base.org"
)

DB_PATH = os.getenv(
    "DB_PATH",
    "/app/trader_v40.db"
)

CHAIN_ID = 8453

INITIAL_BALANCE = Decimal("20")

MIN_TRADE = Decimal("2")
MAX_TRADE = Decimal("5")

DAILY_LOSS_LIMIT = Decimal("10")
MAX_DRAWDOWN = Decimal("25")

SIMULATION_COST = Decimal("0.001")

MAX_AI_CALLS = 300

ASSETS = ["WETH", "cbBTC", "AERO"]

TOKENS = {
    "USDC": {
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "decimals": 6
    },
    "WETH": {
        "address": "0x4200000000000000000000000000000000000006",
        "decimals": 18
    },
    "cbBTC": {
        "address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "decimals": 8
    },
    "AERO": {
        "address": "0x940181a94A35A4569E4529A3CDFB74E38FD98631",
        "decimals": 18
    }
}

ERC20_ABI = [{
    "inputs": [
        {"name": "owner", "type": "address"}
    ],
    "name": "balanceOf",
    "outputs": [
        {"name": "", "type": "uint256"}
    ],
    "stateMutability": "view",
    "type": "function"
}]


# =====================================
# LOGGING
# =====================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logging.getLogger("httpx").setLevel(
    logging.WARNING
)

logging.getLogger("httpcore").setLevel(
    logging.WARNING
)

log = logging.getLogger("trader")


# =====================================
# GLOBAL
# =====================================

bot = Bot(token=TELEGRAM_TOKEN)

dp = Dispatcher()

w3 = Web3(
    Web3.HTTPProvider(
        BASE_RPC_URL,
        request_kwargs={"timeout": 20}
    )
)

session = None

trade_lock = asyncio.Lock()

last_cycle = 0
last_ai_error = 0
ai_cooldown_until = 0


# =====================================
# DATABASE
# =====================================

async def init_db():

    os.makedirs(
        os.path.dirname(
            os.path.abspath(DB_PATH)
        ),
        exist_ok=True
    )

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            "PRAGMA journal_mode=WAL"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS positions(
                symbol TEXT PRIMARY KEY,
                quantity TEXT NOT NULL,
                cost TEXT NOT NULL
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
                cost_basis TEXT,
                pnl TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history(
                symbol TEXT,
                created INTEGER,
                price TEXT,
                PRIMARY KEY(symbol,created)
            )
        """)

        await db.commit()

        defaults = {
            "running": "0",
            "interval": "5",
            "cash": "20",
            "initial_equity": "20",
            "peak_equity": "20",
            "day_start": "20",
            "day_date": "",
            "emergency": "0",
            "ai_calls": "0",
            "ai_date": "",
            "daily_limit": "10",
            "risk_locked": "0"
        }

        for key, value in defaults.items():

            await db.execute(
                """
                INSERT OR IGNORE INTO settings
                VALUES(?,?)
                """,
                (key, value)
            )

        await db.commit()


async def get(key, default=""):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ) as cursor:

            row = await cursor.fetchone()

    return row[0] if row else default


async def put(key, value):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO settings VALUES(?,?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, str(value))
        )

        await db.commit()


async def get_position(symbol):

    async with aiosqlite.connect(DB_PATH) as db:

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


# =====================================
# TELEGRAM
# =====================================

def menu():

    rows = [
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
                text="💼 Позиции",
                callback_data="positions"
            ),
            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="stats"
            )
        ],
        [
            InlineKeyboardButton(
                text="🧠 ИИ",
                callback_data="ai"
            ),
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
        inline_keyboard=rows
    )


def settings_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="5 минут",
                    callback_data="interval_5"
                ),
                InlineKeyboardButton(
                    text="10 минут",
                    callback_data="interval_10"
                ),
                InlineKeyboardButton(
                    text="15 минут",
                    callback_data="interval_15"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Дневной лимит 5%",
                    callback_data="loss_5"
                ),
                InlineKeyboardButton(
                    text="Дневной лимит 10%",
                    callback_data="loss_10"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔓 Снять блокировку",
                    callback_data="unlock"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Главное меню",
                    callback_data="menu"
                )
            ]
        ]
    )


async def notify(text):

    try:
        await bot.send_message(
            ADMIN_ID,
            text
        )

    except Exception:
        log.exception(
            "Telegram notification failed"
        )


async def show_menu(message):

    running = await get("running")

    interval = await get("interval")

    await message.answer(
        f"🤖 AI CRYPTO TRADER v{VERSION}\n\n"
        "💰 Режим: DEMO\n"
        f"⚡ Автопилот: "
        f"{'🟢 ВКЛ' if running == '1' else '🔴 ВЫКЛ'}\n"
        f"⏰ Интервал: {interval} минут\n"
        "🧠 Oxion AI Pro\n"
        "🌐 Base / 0x\n\n"
        "Выбери действие:",
        reply_markup=menu()
    )


# =====================================
# WEB3
# =====================================

def token_balance(symbol):

    if not WALLET_ADDRESS:
        return Decimal("0")

    token = TOKENS[symbol]

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(
            token["address"]
        ),
        abi=ERC20_ABI
    )

    raw = contract.functions.balanceOf(
        Web3.to_checksum_address(
            WALLET_ADDRESS
        )
    ).call()

    return Decimal(raw) / Decimal(
        10 ** token["decimals"]
    )


def eth_balance():

    if not WALLET_ADDRESS:
        return Decimal("0")

    raw = w3.eth.get_balance(
        Web3.to_checksum_address(
            WALLET_ADDRESS
        )
    )

    return Decimal(raw) / Decimal(10 ** 18)


# =====================================
# 0X QUOTES
# =====================================

async def quote(sell, buy, amount):

    if not ZEROX_API_KEY:
        raise RuntimeError("ZEROX_API_KEY отсутствует")

    if not WALLET_ADDRESS:
        raise RuntimeError("WALLET_ADDRESS отсутствует")

    sell_decimals = TOKENS[sell]["decimals"]

    buy_decimals = TOKENS[buy]["decimals"]

    raw = int(
        Decimal(str(amount))
        * Decimal(10 ** sell_decimals)
    )

    if raw <= 0:
        raise RuntimeError("Слишком маленькая сумма")

    params = {
        "chainId": CHAIN_ID,
        "sellToken": TOKENS[sell]["address"],
        "buyToken": TOKENS[buy]["address"],
        "sellAmount": str(raw),
        "taker": WALLET_ADDRESS,
        "slippageBps": 50
    }

    headers = {
        "0x-api-key": ZEROX_API_KEY,
        "0x-version": "v2"
    }

    async with session.get(
        "https://api.0x.org/swap/allowance-holder/price",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=30)
    ) as response:

        data = await response.json(
            content_type=None
        )

        if response.status != 200:

            raise RuntimeError(
                f"0x HTTP {response.status}"
            )

    if not data.get("liquidityAvailable", True):
        raise RuntimeError("Недостаточная ликвидность")

    received = Decimal(
        str(data["buyAmount"])
    ) / Decimal(
        10 ** buy_decimals
    )

    if received <= 0:
        raise RuntimeError("Некорректная котировка")

    return received


# =====================================
# AI
# =====================================

def parse_json(content):

    if isinstance(content, list):

        content = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
        )

    if not isinstance(content, str):
        raise ValueError("Пустой ответ")

    content = content.strip()

    if not content:
        raise ValueError("Пустой ответ")

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

    try:
        result = json.loads(content)

    except json.JSONDecodeError:

        start = content.find("{")
        end = content.rfind("}")

        if start < 0 or end < start:
            raise

        result = json.loads(
            content[start:end + 1]
        )

    if not isinstance(result, dict):
        raise ValueError("Неверный JSON")

    return result


async def ai_request(
    base_url,
    api_key,
    model,
    prompt,
    data
):

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

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
                    ensure_ascii=False,
                    default=str
                )
            }
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": False
    }

    async with session.post(
        base_url + "/chat/completions",
        json=payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60)
    ) as response:

        result = await response.json(
            content_type=None
        )

        if response.status != 200:

            raise RuntimeError(
                f"AI HTTP {response.status}"
            )

    choices = result.get("choices") or []

    if not choices:
        raise ValueError("ИИ не вернул choices")

    message = choices[0].get("message") or {}

    return parse_json(
        message.get("content")
    )


async def ask_ai(role, prompt, data):

    global ai_cooldown_until

    if time.time() < ai_cooldown_until:

        raise RuntimeError(
            "ИИ временно недоступен"
        )

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    if await get("ai_date") != today:

        await put("ai_date", today)
        await put("ai_calls", "0")

    model = (
        OXION_ANALYST_MODEL
        if role == "analyst"
        else OXION_CRITIC_MODEL
    )

    providers = []

    if OXION_API_KEY:

        providers.append(
            (
                OXION_BASE_URL,
                OXION_API_KEY,
                model
            )
        )

    if OPENROUTER_API_KEY:

        providers.append(
            (
                "https://openrouter.ai/api/v1",
                OPENROUTER_API_KEY,
                "openrouter/free"
            )
        )

    if not providers:
        raise RuntimeError("Нет ключей ИИ")

    errors = []

    for base_url, api_key, model_id in providers:

        for attempt in range(3):

            calls = int(
                await get("ai_calls", "0")
            )

            if calls >= MAX_AI_CALLS:

                raise RuntimeError(
                    "Лимит запросов ИИ исчерпан"
                )

            await put(
                "ai_calls",
                calls + 1
            )

            try:

                return await ai_request(
                    base_url,
                    api_key,
                    model_id,
                    prompt,
                    data
                )

            except Exception as e:

                errors.append(
                    f"{model_id}: "
                    f"{type(e).__name__}"
                )

                await asyncio.sleep(
                    2 ** attempt
                )

    ai_cooldown_until = time.time() + 900

    raise RuntimeError(
        "ИИ недоступны: "
        + ", ".join(errors[-3:])
    )


ANALYST_PROMPT = """
Ты аналитик автономного криптотрейдера.

Торговые активы:
WETH, cbBTC, AERO.

Действия:
BUY, SELL, HOLD.

Покупки 2-5 USDC.

Повторные покупки разрешены.

Учитывай:
- свободный USDC
- открытые позиции
- среднюю цену покупки
- текущую цену
- изменение цены за наблюдаемый период
- дневные ограничения

Не выдумывай индикаторы.

Если нет торгового преимущества,
выбирай HOLD.

Ответ строго JSON:

{
 "action":"HOLD",
 "symbol":"WETH",
 "amount":2,
 "reason":"Причина"
}
"""


CRITIC_PROMPT = """
Ты независимый риск-менеджер.

Проверь предложение аналитика.

Варианты:
APPROVE, MODIFY, REJECT.

Повторные покупки разрешены.

Покупка от 2 до 5 USDC.

Нельзя превышать свободный баланс.

Нельзя использовать кредитное плечо.

Не требуй данных, которые уже есть.

Не выдумывай информацию.

Ответ строго JSON:

{
 "decision":"REJECT",
 "amount":2,
 "reason":"Причина"
}
"""


# =====================================
# PRICE HISTORY
# =====================================

async def save_price(symbol, price):

    now = int(time.time())

    bucket = now // 300 * 300

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT OR REPLACE INTO price_history
            VALUES(?,?,?)
            """,
            (
                symbol,
                bucket,
                str(price)
            )
        )

        await db.execute(
            """
            DELETE FROM price_history
            WHERE created < ?
            """,
            (now - 86400 * 7,)
        )

        await db.commit()


async def price_change(symbol, current):

    cutoff = int(time.time()) - 3600

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT price
            FROM price_history
            WHERE symbol=? AND created<=?
            ORDER BY created DESC
            LIMIT 1
            """,
            (symbol, cutoff)
        ) as cursor:

            row = await cursor.fetchone()

    if not row:
        return None

    old = Decimal(row[0])

    if old <= 0:
        return None

    return (current - old) / old * 100


# =====================================
# PORTFOLIO / PNL
# =====================================

async def portfolio_value():

    cash = Decimal(
        await get("cash", "20")
    )

    total = cash

    details = []

    for symbol in ASSETS:

        qty, cost = await get_position(symbol)

        if qty <= 0:
            continue

        try:

            value = await quote(
                symbol,
                "USDC",
                qty
            )

        except Exception:

            # Fail closed: never calculate risk from
            # a guessed valuation.
            raise RuntimeError(
                f"Нет оценки позиции {symbol}"
            )

        pnl = value - cost

        details.append({
            "symbol": symbol,
            "quantity": qty,
            "cost": cost,
            "value": value,
            "pnl": pnl
        })

        total += value

    return total, details


async def realized_pnl():

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT pnl
            FROM trades
            WHERE side='SELL'
            """
        ) as cursor:

            rows = await cursor.fetchall()

    return sum(
        (
            Decimal(row[0])
            for row in rows
        ),
        Decimal("0")
    )


async def pnl_report():

    equity, details = await portfolio_value()

    realized = await realized_pnl()

    unrealized = sum(
        (
            item["pnl"]
            for item in details
        ),
        Decimal("0")
    )

    cash = Decimal(
        await get("cash", "20")
    )

    initial = Decimal(
        await get(
            "initial_equity",
            "20"
        )
    )

    total_pnl = equity - initial

    text = (
        "💰 ПОРТФЕЛЬ DEMO\n\n"
        f"💵 Свободно: ${cash:.4f}\n"
        f"💼 Всего: ${equity:.4f}\n\n"
        f"✅ Реализованный PNL: "
        f"${realized:+.4f}\n"
        f"📈 Нереализованный PNL: "
        f"${unrealized:+.4f}\n"
        f"💰 Общий PNL: "
        f"${total_pnl:+.4f}\n\n"
    )

    for item in details:

        text += (
            f"🪙 {item['symbol']}\n"
            f"Количество: "
            f"{item['quantity']:.8f}\n"
            f"Вложено: "
            f"${item['cost']:.4f}\n"
            f"Сейчас: "
            f"${item['value']:.4f}\n"
            f"PNL: "
            f"${item['pnl']:+.4f}\n\n"
        )

    return text


# =====================================
# RISK
# =====================================

async def risk_check():

    if await get("risk_locked") == "1":
        return False

    equity, _ = await portfolio_value()

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    if await get("day_date") != today:

        await put("day_date", today)
        await put("day_start", equity)

    day_start = Decimal(
        await get(
            "day_start",
            str(equity)
        )
    )

    peak = Decimal(
        await get(
            "peak_equity",
            "20"
        )
    )

    if equity > peak:

        peak = equity

        await put(
            "peak_equity",
            peak
        )

    daily_loss = (
        (day_start - equity)
        / day_start * 100
    ) if day_start > 0 else Decimal("0")

    drawdown = (
        (peak - equity)
        / peak * 100
    ) if peak > 0 else Decimal("0")

    daily_limit = Decimal(
        await get(
            "daily_limit",
            "10"
        )
    )

    if (
        daily_loss >= daily_limit
        or drawdown >= MAX_DRAWDOWN
    ):

        await put("running", "0")
        await put("risk_locked", "1")

        await notify(
            "🚨 ЛИМИТ РИСКА\n\n"
            f"Дневной убыток: "
            f"{daily_loss:.2f}%\n"
            f"Просадка от пика: "
            f"{drawdown:.2f}%\n\n"
            "Автопилот заблокирован."
        )

        return False

    return True


# =====================================
# DEMO EXECUTION
# =====================================

async def demo_buy(symbol, amount):

    received = await quote(
        "USDC",
        symbol,
        amount
    )

    fee = amount * SIMULATION_COST

    cash = Decimal(
        await get("cash", "20")
    )

    total_cost = amount + fee

    if cash < total_cost:
        return "Недостаточно USDC"

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            """
            SELECT quantity,cost
            FROM positions
            WHERE symbol=?
            """,
            (symbol,)
        ) as cursor:

            row = await cursor.fetchone()

        old_qty = Decimal(row[0]) if row else Decimal("0")
        old_cost = Decimal(row[1]) if row else Decimal("0")

        async with db.execute(
            """
            SELECT value FROM settings
            WHERE key='cash'
            """
        ) as cursor:

            cash_row = await cursor.fetchone()

        current_cash = Decimal(cash_row[0])

        if current_cash < total_cost:

            await db.rollback()
            return "Недостаточно USDC"

        await db.execute(
            """
            INSERT INTO positions VALUES(?,?,?)
            ON CONFLICT(symbol)
            DO UPDATE SET
                quantity=excluded.quantity,
                cost=excluded.cost
            """,
            (
                symbol,
                str(old_qty + received),
                str(old_cost + total_cost)
            )
        )

        await db.execute(
            """
            UPDATE settings SET value=?
            WHERE key='cash'
            """,
            (str(current_cash - total_cost),)
        )

        await db.execute(
            """
            INSERT INTO trades(
                created,side,symbol,amount,
                quantity,cost_basis,pnl
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                int(time.time()),
                "BUY",
                symbol,
                str(total_cost),
                str(received),
                str(total_cost),
                "0"
            )
        )

        await db.commit()

    return (
        f"🟢 DEMO BUY {symbol}\n"
        f"Потрачено: ${total_cost:.4f}\n"
        f"Получено: {received:.8f}"
    )


async def demo_sell(symbol):

    qty, cost = await get_position(symbol)

    if qty <= 0:
        return "Нет позиции"

    received = await quote(
        symbol,
        "USDC",
        qty
    )

    fee = received * SIMULATION_COST

    net = received - fee

    pnl = net - cost

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            """
            SELECT quantity,cost
            FROM positions WHERE symbol=?
            """,
            (symbol,)
        ) as cursor:

            row = await cursor.fetchone()

        if (
            not row
            or Decimal(row[0]) != qty
            or Decimal(row[1]) != cost
        ):

            await db.rollback()
            return "Позиция изменилась"

        async with db.execute(
            """
            SELECT value FROM settings
            WHERE key='cash'
            """
        ) as cursor:

            cash_row = await cursor.fetchone()

        cash = Decimal(cash_row[0])

        await db.execute(
            """
            UPDATE settings SET value=?
            WHERE key='cash'
            """,
            (str(cash + net),)
        )

        await db.execute(
            """
            UPDATE positions
            SET quantity='0',cost='0'
            WHERE symbol=?
            """,
            (symbol,)
        )

        await db.execute(
            """
            INSERT INTO trades(
                created,side,symbol,amount,
                quantity,cost_basis,pnl
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                int(time.time()),
                "SELL",
                symbol,
                str(net),
                str(qty),
                str(cost),
                str(pnl)
            )
        )

        await db.commit()

    return (
        f"🔴 DEMO SELL {symbol}\n"
        f"Получено: ${net:.4f}\n"
        f"Себестоимость: ${cost:.4f}\n"
        f"✅ PNL: ${pnl:+.4f}"
    )


# =====================================
# MARKET SCAN
# =====================================

async def scan_market():

    result = {}

    for symbol in ASSETS:

        try:

            received = await quote(
                "USDC",
                symbol,
                Decimal("5")
            )

            price = Decimal("5") / received

            change = await price_change(
                symbol,
                price
            )

            await save_price(
                symbol,
                price
            )

            qty, cost = await get_position(symbol)

            average = (
                cost / qty
                if qty > 0
                else Decimal("0")
            )

            result[symbol] = {
                "price": float(price),
                "change_1h_percent": (
                    float(change)
                    if change is not None
                    else None
                ),
                "quantity": float(qty),
                "cost": float(cost),
                "average_price": float(average)
            }

        except Exception as e:

            log.warning(
                "Market %s unavailable: %s",
                symbol,
                type(e).__name__
            )

    return result


# =====================================
# TRADING CYCLE
# =====================================

async def trading_cycle(manual=False):

    global last_ai_error

    if trade_lock.locked():
        return

    async with trade_lock:

        if await get("emergency") == "1":
            return

        if not manual and await get("running") != "1":
            return

        try:

            if not await risk_check():
                return

            market = await scan_market()

            if not market:
                raise RuntimeError(
                    "Котировки недоступны"
                )

            cash = Decimal(
                await get("cash", "20")
            )

            equity, details = await portfolio_value()

            data = {
                "mode": "DEMO",
                "free_usdc": float(cash),
                "equity": float(equity),
                "market": market,
                "trade_min": 2,
                "trade_max": 5,
                "leverage": False
            }

            analyst = await ask_ai(
                "analyst",
                ANALYST_PROMPT,
                data
            )

            action = str(
                analyst.get(
                    "action",
                    "HOLD"
                )
            ).upper()

            symbol = str(
                analyst.get(
                    "symbol",
                    "WETH"
                )
            )

            if symbol not in ASSETS:
                return

            if action not in ("BUY", "SELL"):

                if manual:

                    await notify(
                        "🧠 Аналитик: HOLD\n"
                        + str(
                            analyst.get(
                                "reason",
                                ""
                            )
                        )[:300]
                    )

                return

            amount = Decimal(
                str(
                    analyst.get(
                        "amount",
                        2
                    )
                )
            )

            if not amount.is_finite():
                return

            if action == "BUY":

                amount = min(
                    MAX_TRADE,
                    max(MIN_TRADE, amount)
                )

                if cash < amount * (
                    1 + SIMULATION_COST
                ):
                    return

            if action == "SELL":

                qty, _ = await get_position(symbol)

                if qty <= 0:
                    return

            critic = await ask_ai(
                "critic",
                CRITIC_PROMPT,
                {
                    "proposal": analyst,
                    "portfolio": data
                }
            )

            decision = str(
                critic.get(
                    "decision",
                    "REJECT"
                )
            ).upper()

            if decision not in (
                "APPROVE",
                "MODIFY"
            ):

                if manual:

                    await notify(
                        "🛡 Критик: REJECT\n"
                        + str(
                            critic.get(
                                "reason",
                                ""
                            )
                        )[:300]
                    )

                return

            if action == "BUY":

                critic_amount = Decimal(
                    str(
                        critic.get(
                            "amount",
                            amount
                        )
                    )
                )

                if not critic_amount.is_finite():
                    return

                amount = min(
                    amount,
                    critic_amount
                )

                if amount < MIN_TRADE:
                    return

            if await get("running") != "1":

                if manual:
                    await notify(
                        "ℹ️ Анализ готов. "
                        "Сделка не выполнена: "
                        "автопилот выключен."
                    )

                return

            if await get("emergency") == "1":
                return

            if not await risk_check():
                return

            if action == "BUY":

                result = await demo_buy(
                    symbol,
                    amount
                )

            else:

                result = await demo_sell(
                    symbol
                )

            await notify(
                "🧠 СОВЕЩАНИЕ ИИ\n\n"
                f"🤖 Аналитик: {action} {symbol}\n"
                f"🛡 Критик: {decision}\n\n"
                f"{result}\n\n"
                f"💬 {str(analyst.get('reason',''))[:200]}"
            )

        except Exception as e:

            log.exception(
                "Trading cycle failed"
            )

            if time.time() - last_ai_error > 3600:

                last_ai_error = time.time()

                await notify(
                    "⚠️ Ошибка анализа:\n"
                    + str(e)[:300]
                    + "\n\nПовторные уведомления "
                    "ограничены одним в час."
                )


# =====================================
# BACKGROUND
# =====================================

async def background():

    global last_cycle

    await asyncio.sleep(5)

    while True:

        try:

            running = await get(
                "running",
                "0"
            )

            interval = int(
                await get(
                    "interval",
                    "5"
                )
            )

            now = time.time()

            if running == "1":

                if now - last_cycle >= interval * 60:

                    last_cycle = now

                    await trading_cycle()

        except Exception:

            log.exception(
                "Background error"
            )

        await asyncio.sleep(15)


# =====================================
# COMMANDS
# =====================================

@dp.message(Command("start"))
@dp.message(Command("menu"))
async def start_handler(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        "✅ Компактное меню:",
        reply_markup=ReplyKeyboardRemove()
    )

    await show_menu(message)


# =====================================
# CALLBACKS
# =====================================

@dp.callback_query()
async def callbacks(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer()
        return

    action = callback.data

    await callback.answer()

    if action == "menu":

        await show_menu(
            callback.message
        )

    elif action == "start":

        if await get("emergency") == "1":

            await callback.message.answer(
                "🚨 Экстренная блокировка."
            )

            return

        if await get("risk_locked") == "1":

            await callback.message.answer(
                "🚨 Торговля заблокирована "
                "лимитом риска."
            )

            return

        try:

            if not await risk_check():

                await callback.message.answer(
                    "Лимит риска достигнут."
                )

                return

        except Exception as e:

            await callback.message.answer(
                "Не удалось проверить риск:\n"
                + str(e)[:200]
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
            "🚨 ЭКСТРЕННАЯ ОСТАНОВКА\n"
            "Новые сделки заблокированы."
        )

    elif action == "portfolio":

        try:

            text = await pnl_report()

            await callback.message.answer(text)

        except Exception as e:

            await callback.message.answer(
                "Ошибка портфеля:\n"
                + str(e)[:250]
            )

    elif action == "positions":

        try:

            _, details = await portfolio_value()

            text = "💼 ОТКРЫТЫЕ ПОЗИЦИИ\n\n"

            if not details:
                text += "Позиций нет."

            for item in details:

                text += (
                    f"🪙 {item['symbol']}\n"
                    f"Количество: "
                    f"{item['quantity']:.8f}\n"
                    f"Вложено: "
                    f"${item['cost']:.4f}\n"
                    f"Стоимость: "
                    f"${item['value']:.4f}\n"
                    f"PNL: "
                    f"${item['pnl']:+.4f}\n\n"
                )

            await callback.message.answer(text)

        except Exception as e:

            await callback.message.answer(
                str(e)[:300]
            )

    elif action == "wallet":

        try:

            text = (
                "👛 METAMASK / BASE\n\n"
                f"Адрес:\n{WALLET_ADDRESS}\n\n"
                f"ETH: {eth_balance():.8f}\n"
            )

            for symbol in TOKENS:

                text += (
                    f"{symbol}: "
                    f"{token_balance(symbol):.8f}\n"
                )

            text += (
                "\n🔐 Режим: только чтение.\n"
                "DEMO не тратит реальные USDC."
            )

            await callback.message.answer(text)

        except Exception as e:

            await callback.message.answer(
                str(e)[:300]
            )

    elif action == "stats":

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute(
                """
                SELECT COUNT(*)
                FROM trades
                """
            ) as cursor:

                row = await cursor.fetchone()

        try:

            realized = await realized_pnl()

            equity, details = await portfolio_value()

            unrealized = sum(
                (
                    item["pnl"]
                    for item in details
                ),
                Decimal("0")
            )

            initial = Decimal(
                await get(
                    "initial_equity",
                    "20"
                )
            )

            total = equity - initial

            await callback.message.answer(
                "📊 СТАТИСТИКА DEMO\n\n"
                f"Сделок: {row[0]}\n"
                f"Реализованный PNL: "
                f"${realized:+.4f}\n"
                f"Открытый PNL: "
                f"${unrealized:+.4f}\n"
                f"Общий PNL: "
                f"${total:+.4f}\n"
                f"Стоимость портфеля: "
                f"${equity:.4f}"
            )

        except Exception as e:

            await callback.message.answer(
                str(e)[:300]
            )

    elif action == "ai":

        calls = await get(
            "ai_calls",
            "0"
        )

        await callback.message.answer(
            "🧠 ИИ-АГЕНТЫ\n\n"
            f"🤖 Аналитик:\n"
            f"{OXION_ANALYST_MODEL}\n\n"
            f"🛡 Критик:\n"
            f"{OXION_CRITIC_MODEL}\n\n"
            "Провайдер: Oxion AI\n"
            "Резерв: OpenRouter\n\n"
            f"Запросов сегодня: "
            f"{calls}/{MAX_AI_CALLS}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔌 Проверить Oxion",
                            callback_data="test_ai"
                        )
                    ]
                ]
            )
        )

    elif action == "test_ai":

        await callback.message.answer(
            "🔌 Проверяю обоих агентов..."
        )

        results = []

        for role in ("analyst", "critic"):

            try:

                result = await ask_ai(
                    role,
                    'Верни только JSON '
                    '{"status":"OK"}',
                    {"test": True}
                )

                results.append(
                    f"{role}: "
                    + (
                        "✅ OK"
                        if result.get("status") == "OK"
                        else "⚠️ Неожиданный ответ"
                    )
                )

            except Exception as e:

                results.append(
                    f"{role}: ❌ "
                    + str(e)[:100]
                )

        await callback.message.answer(
            "\n".join(results)
        )

    elif action == "zerox":

        try:

            received = await quote(
                "USDC",
                "WETH",
                Decimal("5")
            )

            await callback.message.answer(
                "✅ 0x работает!\n\n"
                f"5 USDC ≈ {received:.8f} WETH"
            )

        except Exception as e:

            await callback.message.answer(
                "❌ Ошибка 0x:\n"
                + str(e)[:300]
            )

    elif action == "analyze":

        await callback.message.answer(
            "🔍 Запускаю анализ..."
        )

        asyncio.create_task(
            trading_cycle(manual=True)
        )

    elif action == "settings":

        await callback.message.answer(
            "⚙️ НАСТРОЙКИ\n\n"
            "Режим: DEMO\n"
            f"Интервал: "
            f"{await get('interval')} мин\n"
            f"Дневной лимит: "
            f"{await get('daily_limit')}%\n"
            "Максимальная просадка: 25%\n"
            "Размер покупки: $2–5\n"
            "ИИ: Oxion AI Pro",
            reply_markup=settings_menu()
        )

    elif action.startswith("interval_"):

        value = int(
            action.split("_")[1]
        )

        if value in (5, 10, 15):

            await put("interval", value)

            await callback.message.answer(
                f"⏰ Интервал: {value} минут."
            )

    elif action.startswith("loss_"):

        value = int(
            action.split("_")[1]
        )

        if value in (5, 10):

            await put(
                "daily_limit",
                value
            )

            await callback.message.answer(
                f"📉 Дневной лимит: {value}%."
            )

    elif action == "unlock":

        await put("running", "0")
        await put("emergency", "0")

        # Risk locks are not cleared automatically.
        # A breached risk limit must not be bypassed
        # by pressing the emergency unlock button.

        await callback.message.answer(
            "🔓 Экстренная блокировка снята.\n"
            "Автопилот остаётся выключенным.\n"
            "Блокировка лимита риска, если она есть, "
            "сохраняется."
        )


# =====================================
# STARTUP
# =====================================

async def main():

    global session

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN отсутствует"
        )

    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID отсутствует"
        )

    await init_db()

    if not w3.is_connected():

        raise RuntimeError(
            "Base RPC недоступен"
        )

    if w3.eth.chain_id != CHAIN_ID:

        raise RuntimeError(
            "Неверная сеть RPC"
        )

    session = aiohttp.ClientSession()

    try:

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        await bot.set_my_commands([
            BotCommand(
                command="start",
                description="Открыть бота"
            ),
            BotCommand(
                command="menu",
                description="Главное меню"
            )
        ])

        await notify(
            f"✅ AI CRYPTO TRADER v{VERSION}\n\n"
            "🧠 Oxion AI подключён в настройках.\n"
            "💰 Режим: DEMO\n"
            f"⚡ Автопилот: "
            f"{await get('running')}\n\n"
            "Бот запущен."
        )

        asyncio.create_task(
            background()
        )

        await dp.start_polling(bot)

    finally:

        await session.close()

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())
