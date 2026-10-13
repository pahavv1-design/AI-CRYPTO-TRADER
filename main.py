import os
import json
import math
import time
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import aiohttp
import aiosqlite

from web3 import AsyncWeb3, AsyncHTTPProvider

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


# =========================================
# CONFIG
# =========================================

TOKEN = os.getenv(
    "TELEGRAM_TOKEN", ""
).strip()

ADMIN_ID = int(
    os.getenv("ADMIN_ID", "0")
)

OPENROUTER_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

ZEROX_KEY = os.getenv(
    "ZEROX_API_KEY", ""
).strip()

WALLET = os.getenv(
    "WALLET_ADDRESS", ""
).strip()

RPC = os.getenv(
    "BASE_RPC_URL",
    "https://mainnet.base.org"
)

MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free"
)

DB = os.getenv(
    "DB_PATH",
    "trader_v20.db"
)

CHAIN_ID = 8453

START_BALANCE = 20.0

MIN_TRADE = 2.0
MAX_TRADE = 5.0

DAILY_LOSS = 10.0
MAX_DRAWDOWN = 25.0

# Дополнительный резерв на расходы
# виртуального исполнения.
SIMULATION_COST = Decimal("0.001")

MAX_AI_CALLS = 40

ZEROX_API = (
    "https://api.0x.org/"
    "swap/allowance-holder"
)

# =========================================
# TOKENS — BASE
# =========================================

TOKENS = {

    "USDC": {
        "address": (
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        ),
        "decimals": 6
    },

    "WETH": {
        "address": (
            "0x4200000000000000000000000000000000000006"
        ),
        "decimals": 18
    },

    "cbBTC": {
        "address": (
            "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
        ),
        "decimals": 8
    },

    "AERO": {
        "address": (
            "0x940181a94A35A4569E4529A3CDFB74E38FD98631"
        ),
        "decimals": 18
    }

}

TRADE_TOKENS = [
    "WETH",
    "cbBTC",
    "AERO"
]

if not TOKEN or ADMIN_ID <= 0:
    raise RuntimeError(
        "Проверь TELEGRAM_TOKEN и ADMIN_ID"
    )

if not OPENROUTER_KEY:
    raise RuntimeError(
        "OPENROUTER_API_KEY отсутствует"
    )

if not ZEROX_KEY:
    raise RuntimeError(
        "ZEROX_API_KEY отсутствует"
    )

if not AsyncWeb3.is_address(WALLET):
    raise RuntimeError(
        "Неверный WALLET_ADDRESS"
    )


# =========================================
# LOGGING
# =========================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    )
)

logging.getLogger(
    "httpx"
).setLevel(logging.WARNING)

logging.getLogger(
    "httpcore"
).setLevel(logging.WARNING)

logger = logging.getLogger(
    "AI_CRYPTO_TRADER"
)


# =========================================
# BOT
# =========================================

bot = Bot(token=TOKEN)

dp = Dispatcher()

http = None
web3 = None

trade_lock = asyncio.Lock()
db_lock = asyncio.Lock()


# =========================================
# DATABASE
# =========================================

DEFAULTS = {

    "cash": "20",
    "initial": "20",

    "autopilot": "OFF",
    "emergency": "OFF",

    "strategy": "AUTO",
    "interval": "10",

    "daily_limit": "10",
    "max_drawdown": "25",

    "day": "",
    "day_start": "20",
    "day_blocked": "",

    "ai_day": "",
    "ai_calls": "0",

    "last_ai": "Нет решений",
    "last_cycle": "Никогда",

    "status": "Ожидание"

}


async def init_db():

    async with aiosqlite.connect(DB) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
                symbol TEXT,
                side TEXT,
                amount REAL,
                pnl REAL,
                created_at TEXT
            )
        """)

        for key, value in DEFAULTS.items():

            await db.execute(
                """
                INSERT OR IGNORE INTO settings
                VALUES (?,?)
                """,
                (key, value)
            )

        await db.commit()


async def get(key):

    async with aiosqlite.connect(DB) as db:

        async with db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ) as cursor:

            row = await cursor.fetchone()

            return row[0] if row else None


async def put(key, value):

    async with db_lock:

        async with aiosqlite.connect(DB) as db:

            await db.execute(
                """
                INSERT INTO settings(key,value)
                VALUES (?,?)
                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
                """,
                (key, str(value))
            )

            await db.commit()


async def num(key):

    return Decimal(await get(key))


async def positions():

    async with aiosqlite.connect(DB) as db:

        async with db.execute(
            "SELECT symbol,quantity,cost FROM positions"
        ) as cursor:

            rows = await cursor.fetchall()

    return {
        row[0]: {
            "quantity": Decimal(row[1]),
            "cost": Decimal(row[2])
        }
        for row in rows
    }


# =========================================
# HTTP
# =========================================

async def request_json(
    method,
    url,
    params=None,
    payload=None,
    headers=None
):

    timeout = aiohttp.ClientTimeout(
        total=35
    )

    async with http.request(
        method,
        url,
        params=params,
        json=payload,
        headers=headers,
        timeout=timeout
    ) as response:

        text = await response.text()

        if response.status >= 400:

            raise RuntimeError(
                f"HTTP {response.status}: "
                f"{text[:250]}"
            )

        try:
            return json.loads(text)

        except json.JSONDecodeError:

            raise RuntimeError(
                "API вернул некорректный JSON"
            )


# =========================================
# 0X API
# =========================================

def raw_amount(symbol, amount):

    decimals = TOKENS[symbol]["decimals"]

    value = Decimal(str(amount))

    if not value.is_finite() or value <= 0:
        raise ValueError(
            "Некорректная сумма"
        )

    return str(
        int(value * (10 ** decimals))
    )


def human_amount(symbol, amount):

    decimals = TOKENS[symbol]["decimals"]

    return (
        Decimal(str(amount))
        / Decimal(10 ** decimals)
    )


async def quote_price(
    sell_symbol,
    buy_symbol,
    amount
):

    if sell_symbol == buy_symbol:
        raise ValueError(
            "Одинаковые токены"
        )

    if sell_symbol not in TOKENS:
        raise ValueError(
            "Неизвестный токен"
        )

    if buy_symbol not in TOKENS:
        raise ValueError(
            "Неизвестный токен"
        )

    headers = {
        "0x-api-key": ZEROX_KEY,
        "0x-version": "v2"
    }

    params = {
        "chainId": CHAIN_ID,

        "sellToken": (
            TOKENS[sell_symbol]["address"]
        ),

        "buyToken": (
            TOKENS[buy_symbol]["address"]
        ),

        "sellAmount": raw_amount(
            sell_symbol,
            amount
        ),

        "taker": WALLET
    }

    result = await request_json(
        "GET",
        ZEROX_API + "/price",
        params=params,
        headers=headers
    )

    if result.get("liquidityAvailable") is False:

        raise RuntimeError(
            "0x: недостаточно ликвидности"
        )

    buy_amount = result.get(
        "buyAmount"
    )

    if not buy_amount:
        raise RuntimeError(
            "0x не вернул buyAmount"
        )

    received = human_amount(
        buy_symbol,
        buy_amount
    )

    if received <= 0:
        raise RuntimeError(
            "Некорректная котировка"
        )

    return {
        "sell": sell_symbol,
        "buy": buy_symbol,
        "amount": Decimal(str(amount)),
        "received": received,
        "raw": result
    }


# =========================================
# BASE WALLET
# =========================================

ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {
                "name": "owner",
                "type": "address"
            }
        ],
        "outputs": [
            {
                "name": "",
                "type": "uint256"
            }
        ]
    }
]


async def wallet_balance():

    chain = await web3.eth.chain_id

    if chain != CHAIN_ID:

        raise RuntimeError(
            "Неверная сеть"
        )

    address = AsyncWeb3.to_checksum_address(
        WALLET
    )

    wei = await web3.eth.get_balance(
        address
    )

    result = {
        "ETH": Decimal(
            str(web3.from_wei(wei, "ether"))
        )
    }

    for symbol, token in TOKENS.items():

        contract = web3.eth.contract(
            address=AsyncWeb3.to_checksum_address(
                token["address"]
            ),
            abi=ERC20_ABI
        )

        value = await contract.functions.balanceOf(
            address
        ).call()

        result[symbol] = human_amount(
            symbol,
            value
        )

    return result


# =========================================
# PORTFOLIO
# =========================================

async def get_equity():

    cash = await num("cash")

    active = await positions()

    equity = cash

    for symbol, position in active.items():

        quote = await quote_price(
            symbol,
            "USDC",
            position["quantity"]
        )

        equity += (
            quote["received"]
            * (1 - SIMULATION_COST)
        )

    return equity


async def risk_status():

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    equity = await get_equity()

    if await get("day") != today:

        await put("day", today)

        await put(
            "day_start",
            equity
        )

        await put(
            "day_blocked",
            ""
        )

    start = await num("day_start")

    initial = await num("initial")

    daily_limit = await num(
        "daily_limit"
    )

    drawdown_limit = await num(
        "max_drawdown"
    )

    if start <= 0 or initial <= 0:
        raise RuntimeError(
            "Некорректный баланс"
        )

    daily_loss = (
        (start - equity)
        / start
        * 100
    )

    drawdown = (
        (initial - equity)
        / initial
        * 100
    )

    if (
        daily_loss >= daily_limit
        or drawdown >= drawdown_limit
    ):

        await put(
            "day_blocked",
            today
        )

        await put(
            "autopilot",
            "OFF"
        )

    blocked = (
        await get("day_blocked")
        == today
    )

    return {
        "equity": equity,
        "daily_loss": daily_loss,
        "drawdown": drawdown,
        "blocked": blocked
    }


# =========================================
# AI
# =========================================

ANALYST = """
Ты трейдер экспериментального
криптовалютного бота.

Торговля только DEMO.

Используй предоставленные котировки.

Не придумывай технические индикаторы,
новости или дополнительные данные.

Выбирай одно действие:
BUY, SELL или HOLD.

Выбирай только разрешённый символ.

Ответ строго JSON:

{
 "action": "HOLD",
 "symbol": "WETH",
 "amount_usd": 3,
 "reason": "Краткое объяснение"
}

Если данных недостаточно, выбирай HOLD.
"""


CRITIC = """
Ты независимый риск-аналитик.

Проверяй предложение первого агента.

Используй баланс, ограничения
и полученные котировки.

Возможные решения:

APPROVE
MODIFY
REJECT

При MODIFY можно уменьшить сумму.

Не одобряй сделки только ради активности.

Ответ строго JSON:

{
 "decision": "REJECT",
 "amount_usd": 0,
 "reason": "Краткое объяснение"
}

Если данных недостаточно,
выбирай REJECT.
"""


async def ai_budget():

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    if await get("ai_day") != today:

        await put("ai_day", today)

        await put("ai_calls", 0)

    return int(
        await get("ai_calls")
    )


async def ask_ai(prompt, data):

    calls = await ai_budget()

    if calls >= MAX_AI_CALLS:

        raise RuntimeError(
            "Дневной лимит ИИ достигнут"
        )

    await put(
        "ai_calls",
        calls + 1
    )

    payload = {
        "model": MODEL,

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

        "temperature": 0.1,
        "max_tokens": 1000
    }

    result = await request_json(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        payload=payload,
        headers={
            "Authorization": (
                "Bearer " + OPENROUTER_KEY
            )
        }
    )

    choices = result.get(
        "choices", []
    )

    if not choices:
        raise RuntimeError(
            "ИИ не вернул choices"
        )

    content = (
        choices[0]
        .get("message", {})
        .get("content")
    )

    if not isinstance(content, str):
        raise RuntimeError(
            "Пустой ответ ИИ"
        )

    start = content.find("{")
    end = content.rfind("}")

    if start < 0 or end < start:

        raise RuntimeError(
            "ИИ не вернул JSON"
        )

    answer = json.loads(
        content[start:end + 1]
    )

    if not isinstance(answer, dict):

        raise RuntimeError(
            "Неверный ответ ИИ"
        )

    return answer


# =========================================
# MARKET SCANNER
# =========================================

async def scan_market():

    result = {}

    for symbol in TRADE_TOKENS:

        try:

            quote = await quote_price(
                "USDC",
                symbol,
                5
            )

            result[symbol] = {
                "usdc_spent": 5,
                "quantity": str(
                    quote["received"]
                )
            }

        except Exception as error:

            logger.warning(
                "Quote error %s: %s",
                symbol,
                str(error)[:150]
            )

    if not result:

        raise RuntimeError(
            "0x не вернул котировки"
        )

    return result


# =========================================
# DEMO EXECUTION
# =========================================

async def demo_buy(symbol, amount):

    amount = Decimal(str(amount))

    if symbol not in TRADE_TOKENS:
        return "Запрещённый токен"

    if amount < MIN_TRADE:
        return "Сумма меньше $2"

    if amount > MAX_TRADE:
        return "Сумма больше $5"

    if await get("autopilot") != "ON":
        return "Автопилот выключен"

    if await get("emergency") == "ON":
        return "Аварийная блокировка"

    cash = await num("cash")

    if cash < amount:
        return "Недостаточно USDC"

    active = await positions()

    if symbol in active:
        return "Позиция уже открыта"

    risk = await risk_status()

    if risk["blocked"]:
        return "Лимит риска достигнут"

    quote = await quote_price(
        "USDC",
        symbol,
        amount
    )

    quantity = (
        quote["received"]
        * (1 - SIMULATION_COST)
    )

    if await get("autopilot") != "ON":
        return "Автопилот остановлен"

    if await get("emergency") == "ON":
        return "Аварийная блокировка"

    async with db_lock:

        async with aiosqlite.connect(DB) as db:

            await db.execute(
                "BEGIN IMMEDIATE"
            )

            cursor = await db.execute(
                "SELECT value FROM settings WHERE key='cash'"
            )

            row = await cursor.fetchone()

            current_cash = Decimal(row[0])

            cursor = await db.execute(
                "SELECT 1 FROM positions WHERE symbol=?",
                (symbol,)
            )

            existing = await cursor.fetchone()

            if (
                current_cash < amount
                or existing
                or await get("autopilot") != "ON"
                or await get("emergency") == "ON"
            ):

                await db.rollback()

                return "Сделка заблокирована"

            await db.execute(
                """
                UPDATE settings
                SET value=?
                WHERE key='cash'
                """,
                (str(current_cash - amount),)
            )

            await db.execute(
                """
                INSERT INTO positions
                VALUES (?,?,?)
                """,
                (
                    symbol,
                    str(quantity),
                    str(amount)
                )
            )

            await db.execute(
                """
                INSERT INTO trades
                (symbol,side,amount,pnl,created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    symbol,
                    "BUY",
                    float(amount),
                    0,
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )
            )

            await db.commit()

    await bot.send_message(
        ADMIN_ID,
        "🟢 ДЕМО-ПОКУПКА\n\n"
        f"🪙 {symbol}\n"
        f"💰 Потрачено: ${amount}\n"
        f"🔢 Получено: {quantity:.8f}\n"
        "💱 Котировка: 0x"
    )

    return f"BUY {symbol} ${amount}"


async def demo_sell(symbol):

    if symbol not in TRADE_TOKENS:
        return "Запрещённый токен"

    if await get("autopilot") != "ON":
        return "Автопилот выключен"

    if await get("emergency") == "ON":
        return "Аварийная блокировка"

    active = await positions()

    if symbol not in active:
        return "Позиция отсутствует"

    position = active[symbol]

    quote = await quote_price(
        symbol,
        "USDC",
        position["quantity"]
    )

    received = (
        quote["received"]
        * (1 - SIMULATION_COST)
    )

    pnl = (
        received - position["cost"]
    )

    if await get("autopilot") != "ON":
        return "Автопилот остановлен"

    if await get("emergency") == "ON":
        return "Аварийная блокировка"

    async with db_lock:

        async with aiosqlite.connect(DB) as db:

            await db.execute(
                "BEGIN IMMEDIATE"
            )

            cursor = await db.execute(
                "SELECT quantity,cost FROM positions WHERE symbol=?",
                (symbol,)
            )

            row = await cursor.fetchone()

            if (
                not row
                or Decimal(row[0]) != position["quantity"]
                or await get("autopilot") != "ON"
                or await get("emergency") == "ON"
            ):

                await db.rollback()

                return "Сделка заблокирована"

            cursor = await db.execute(
                "SELECT value FROM settings WHERE key='cash'"
            )

            cash = Decimal(
                (await cursor.fetchone())[0]
            )

            await db.execute(
                """
                UPDATE settings
                SET value=?
                WHERE key='cash'
                """,
                (str(cash + received),)
            )

            await db.execute(
                "DELETE FROM positions WHERE symbol=?",
                (symbol,)
            )

            await db.execute(
                """
                INSERT INTO trades
                (symbol,side,amount,pnl,created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    symbol,
                    "SELL",
                    float(received),
                    float(pnl),
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )
            )

            await db.commit()

    await bot.send_message(
        ADMIN_ID,
        "🔴 ДЕМО-ПРОДАЖА\n\n"
        f"🪙 {symbol}\n"
        f"💰 Получено: ${received:.4f}\n"
        f"📈 PNL: ${pnl:+.4f}"
    )

    return f"SELL {symbol} PNL ${pnl:+.4f}"


# =========================================
# TRADING ENGINE
# =========================================

async def trading_cycle():

    if trade_lock.locked():
        return "Анализ уже выполняется"

    async with trade_lock:

        if await get("autopilot") != "ON":
            return "Автопилот выключен"

        if await get("emergency") == "ON":
            return "Аварийная остановка"

        try:

            risk = await risk_status()

            if risk["blocked"]:

                await put(
                    "autopilot",
                    "OFF"
                )

                await bot.send_message(
                    ADMIN_ID,
                    "🚨 Лимит риска достигнут.\n"
                    "Автопилот остановлен."
                )

                return "Риск-лимит"

            calls = await ai_budget()

            if calls + 2 > MAX_AI_CALLS:

                return "Лимит бесплатного ИИ"

            market = await scan_market()

            active = await positions()

            cash = await num("cash")

            context = {
                "market": market,

                "positions": {
                    symbol: {
                        "quantity": str(p["quantity"]),
                        "cost": str(p["cost"])
                    }
                    for symbol, p in active.items()
                },

                "cash": str(cash),

                "risk": {
                    "daily_loss_percent": str(
                        risk["daily_loss"]
                    ),

                    "drawdown_percent": str(
                        risk["drawdown"]
                    ),

                    "daily_limit": str(
                        await get("daily_limit")
                    ),

                    "max_drawdown": str(
                        await get("max_drawdown")
                    )
                },

                "min_trade": MIN_TRADE,
                "max_trade": MAX_TRADE,

                "strategy": await get(
                    "strategy"
                )
            }

            analyst = await ask_ai(
                ANALYST,
                context
            )

            action = str(
                analyst.get("action", "HOLD")
            ).upper()

            symbol = str(
                analyst.get("symbol", "")
            )

            if action not in (
                "BUY",
                "SELL",
                "HOLD"
            ):

                action = "HOLD"

            if action == "HOLD":

                text = (
                    "🤖 Аналитик: HOLD\n"
                    + str(
                        analyst.get("reason", "")
                    )[:300]
                )

                await put("last_ai", text)

                return text

            critic = await ask_ai(
                CRITIC,
                {
                    **context,
                    "proposal": analyst
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
                "MODIFY",
                "REJECT"
            ):

                decision = "REJECT"

            result = "HOLD"

            if decision in (
                "APPROVE",
                "MODIFY"
            ):

                try:

                    amount = Decimal(
                        str(
                            analyst.get(
                                "amount_usd", 0
                            )
                        )
                    )

                    if decision == "MODIFY":

                        modified = Decimal(
                            str(
                                critic.get(
                                    "amount_usd", 0
                                )
                            )
                        )

                        if (
                            modified <= 0
                            or modified > amount
                        ):
                            raise ValueError(
                                "Неверное изменение суммы"
                            )

                        amount = modified

                    if not amount.is_finite():
                        raise ValueError(
                            "Некорректная сумма"
                        )

                    if (
                        action == "BUY"
                        and symbol in TRADE_TOKENS
                    ):

                        result = await demo_buy(
                            symbol,
                            amount
                        )

                    elif (
                        action == "SELL"
                        and symbol in active
                    ):

                        result = await demo_sell(
                            symbol
                        )

                except (
                    ValueError,
                    InvalidOperation
                ):

                    result = (
                        "Некорректная сумма ИИ"
                    )

            text = (
                "🧠 СОВЕЩАНИЕ ИИ\n\n"

                f"🤖 Аналитик: {action}\n"
                f"🪙 Монета: {symbol}\n"

                f"💬 {str(analyst.get('reason', ''))[:300]}\n\n"

                f"🛡️ Критик: {decision}\n"

                f"💬 {str(critic.get('reason', ''))[:300]}\n\n"

                f"⚖️ Итог: {result}"
            )

            await put(
                "last_ai",
                text
            )

            await put(
                "last_cycle",
                datetime.now(
                    timezone.utc
                ).strftime(
                    "%d.%m %H:%M UTC"
                )
            )

            await bot.send_message(
                ADMIN_ID,
                text[:4000]
            )

            return text

        except Exception as error:

            logger.exception(
                "Trading error"
            )

            return (
                "Ошибка анализа: "
                + str(error)[:400]
            )


# =========================================
# KEYBOARDS
# =========================================

def kb(rows):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=data
                )
                for text, data in row
            ]
            for row in rows
        ]
    )


def main_kb():

    return kb([
        [
            ("🚀 Запустить", "run"),
            ("🛑 Остановить", "stop")
        ],
        [
            ("💰 Портфель", "portfolio"),
            ("👛 MetaMask", "wallet")
        ],
        [
            ("💼 Позиции", "positions"),
            ("📊 Статистика", "stats")
        ],
        [
            ("🧠 ИИ-агенты", "agents"),
            ("💱 Проверить 0x", "test_0x")
        ],
        [
            ("🔍 Анализ сейчас", "analyze")
        ],
        [
            ("⚙️ Настройки", "settings")
        ],
        [
            ("🚨 ЭКСТРЕННАЯ ОСТАНОВКА", "emergency")
        ]
    ])


def back():

    return kb([
        [("🔙 Главное меню", "menu")]
    ])


# =========================================
# TELEGRAM
# =========================================

async def menu_text():

    cash = await num("cash")

    auto = await get("autopilot")

    emergency = await get("emergency")

    status = (
        "🟢 ВКЛ"
        if auto == "ON"
        else "🔴 ВЫКЛ"
    )

    if emergency == "ON":
        status = "🚨 БЛОКИРОВКА"

    return (
        "🤖 AI CRYPTO TRADER v2.0\n\n"

        "🌐 Сеть: Base\n"
        "💱 Агрегатор: 0x\n"

        "🪙 WETH / cbBTC / AERO\n\n"

        f"💰 Свободно: ${cash:.2f}\n"
        f"⚡ Автопилот: {status}\n"

        f"⏰ Интервал: "
        f"{await get('interval')} мин\n"

        "💵 Сделка: $2–5\n"

        f"📉 Дневной лимит: "
        f"{await get('daily_limit')}%\n\n"

        "🧠 Два ИИ-агента\n"

        "💰 Режим: DEMO\n"
        "🔐 MetaMask: только чтение"
    )


@dp.message(CommandStart())
async def start(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        await menu_text(),
        reply_markup=main_kb()
    )


@dp.callback_query()
async def callbacks(c: CallbackQuery):

    if c.from_user.id != ADMIN_ID:

        await c.answer(
            "Нет доступа",
            show_alert=True
        )

        return

    action = c.data

    await c.answer()

    try:

        if action == "menu":

            await c.message.edit_text(
                await menu_text(),
                reply_markup=main_kb()
            )

        elif action == "run":

            if await get("emergency") == "ON":

                text = (
                    "🚨 Сначала сними блокировку"
                )

            else:

                await put(
                    "autopilot",
                    "ON"
                )

                text = (
                    "🚀 ДЕМО-АВТОПИЛОТ ВКЛЮЧЁН\n\n"
                    "ИИ анализирует рынок.\n"
                    "Реальные деньги не используются."
                )

            await c.message.answer(
                text,
                reply_markup=back()
            )

        elif action == "stop":

            await put(
                "autopilot",
                "OFF"
            )

            await c.message.answer(
                "🛑 Автопилот выключен.",
                reply_markup=back()
            )

        elif action == "emergency":

            await put(
                "autopilot",
                "OFF"
            )

            await put(
                "emergency",
                "ON"
            )

            await c.message.answer(
                "🚨 АВАРИЙНАЯ БЛОКИРОВКА\n\n"
                "Новые сделки запрещены.",
                reply_markup=kb([
                    [
                        ("🔓 Разблокировать", "unlock")
                    ],
                    [
                        ("🔙 Меню", "menu")
                    ]
                ])
            )

        elif action == "unlock":

            await put(
                "emergency",
                "OFF"
            )

            await c.message.answer(
                "✅ Блокировка снята.\n"
                "Автопилот выключен.",
                reply_markup=back()
            )

        elif action == "wallet":

            balances = await wallet_balance()

            text = (
                "👛 METAMASK — BASE\n\n"
                f"📍 {WALLET[:8]}...{WALLET[-6:]}\n\n"
            )

            for symbol, amount in balances.items():

                text += (
                    f"{symbol}: {amount:.8f}\n"
                )

            text += (
                "\n🔐 Доступ только на чтение."
            )

            await c.message.answer(
                text,
                reply_markup=back()
            )

        elif action == "test_0x":

            quote = await quote_price(
                "USDC",
                "WETH",
                5
            )

            await c.message.answer(
                "✅ 0x API РАБОТАЕТ\n\n"
                "💰 Продать: 5 USDC\n"
                f"🪙 Получить: "
                f"{quote['received']:.8f} WETH\n\n"
                "⚠️ Это ориентировочная котировка, "
                "не гарантия исполнения.",
                reply_markup=back()
            )

        elif action == "portfolio":

            cash = await num("cash")

            equity = await get_equity()

            initial = await num("initial")

            await c.message.answer(
                "💰 ДЕМО-ПОРТФЕЛЬ\n\n"
                f"💵 Свободно: ${cash:.4f}\n"
                f"🏦 Капитал: ${equity:.4f}\n"
                f"📈 PNL: ${equity-initial:+.4f}\n\n"
                "⚠️ Виртуальные средства.",
                reply_markup=back()
            )

        elif action == "positions":

            active = await positions()

            if not active:

                text = (
                    "💼 Открытых позиций нет."
                )

            else:

                text = "💼 ДЕМО-ПОЗИЦИИ\n\n"

                for symbol, p in active.items():

                    text += (
                        f"🪙 {symbol}\n"
                        f"Количество: "
                        f"{p['quantity']:.8f}\n"
                        f"Вложено: "
                        f"${p['cost']:.2f}\n\n"
                    )

            await c.message.answer(
                text,
                reply_markup=back()
            )

        elif action == "stats":

            async with aiosqlite.connect(DB) as db:

                async with db.execute("""
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(pnl),0)
                    FROM trades
                    WHERE side='SELL'
                """) as cursor:

                    count, pnl = await cursor.fetchone()

            await c.message.answer(
                "📊 СТАТИСТИКА\n\n"
                f"Закрытых сделок: {count}\n"
                f"PNL: ${pnl:+.4f}\n"
                f"Последний анализ: "
                f"{await get('last_cycle')}",
                reply_markup=back()
            )

        elif action == "agents":

            calls = await ai_budget()

            await c.message.answer(
                "🧠 ИИ-АГЕНТЫ\n\n"
                f"Модель: {MODEL}\n"
                f"Запросов: {calls}/{MAX_AI_CALLS}\n\n"
                f"{await get('last_ai')}",
                reply_markup=back()
            )

        elif action == "analyze":

            result = await trading_cycle()

            await c.message.answer(
                result[:4000],
                reply_markup=back()
            )

        elif action == "settings":

            await c.message.answer(
                "⚙️ НАСТРОЙКИ\n\n"
                f"Стратегия: {await get('strategy')}\n"
                f"Интервал: {await get('interval')} мин\n"
                f"Дневной лимит: "
                f"{await get('daily_limit')}%\n\n"
                "Выбери параметр:",
                reply_markup=kb([
                    [
                        ("🟢 SAFE", "strategy_SAFE")
                    ],
                    [
                        ("🤖 AUTO", "strategy_AUTO")
                    ],
                    [
                        ("🔴 AGGRESSIVE", "strategy_AGGRESSIVE")
                    ],
                    [
                        ("⏰ 5 минут", "interval_5")
                    ],
                    [
                        ("⏰ 10 минут", "interval_10")
                    ],
                    [
                        ("⏰ 15 минут", "interval_15")
                    ],
                    [
                        ("📉 Лимит 5%", "loss_5")
                    ],
                    [
                        ("📉 Лимит 10%", "loss_10")
                    ],
                    [
                        ("📉 Лимит 20%", "loss_20")
                    ],
                    [
                        ("🔙 Меню", "menu")
                    ]
                ])
            )

        elif action.startswith("strategy_"):

            value = action.split("_", 1)[1]

            if value in (
                "SAFE",
                "AUTO",
                "AGGRESSIVE"
            ):

                await put(
                    "strategy",
                    value
                )

                await c.message.answer(
                    f"✅ Стратегия: {value}",
                    reply_markup=back()
                )

        elif action.startswith("interval_"):

            value = action.split("_", 1)[1]

            if value in (
                "5",
                "10",
                "15"
            ):

                await put(
                    "interval",
                    value
                )

                await c.message.answer(
                    f"✅ Интервал: {value} минут",
                    reply_markup=back()
                )

        elif action.startswith("loss_"):

            value = action.split("_", 1)[1]

            if value in (
                "5",
                "10",
                "20"
            ):

                await put(
                    "daily_limit",
                    value
                )

                await c.message.answer(
                    f"✅ Дневной лимит: {value}%",
                    reply_markup=back()
                )

    except Exception as error:

        logger.exception(
            "Telegram handler error"
        )

        await c.message.answer(
            "❌ Ошибка:\n"
            + str(error)[:400],
            reply_markup=back()
        )


# =========================================
# BACKGROUND LOOP
# =========================================

async def trading_loop():

    await asyncio.sleep(10)

    while True:

        try:

            if await get("autopilot") == "ON":

                result = await trading_cycle()

                logger.info(
                    "Trading cycle: %s",
                    result[:150]
                )

        except Exception:

            logger.exception(
                "Background error"
            )

        interval = int(
            await get("interval")
        )

        await asyncio.sleep(
            interval * 60
        )


# =========================================
# START
# =========================================

async def main():

    global http
    global web3

    await init_db()

    # После перезапуска автопилот
    # необходимо включить вручную.
    await put(
        "autopilot",
        "OFF"
    )

    http = aiohttp.ClientSession()

    web3 = AsyncWeb3(
        AsyncHTTPProvider(
            RPC,
            request_kwargs={
                "timeout": 20
            }
        )
    )

    chain = await web3.eth.chain_id

    if chain != CHAIN_ID:

        raise RuntimeError(
            "Подключена не Base Mainnet"
        )

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info(
        "AI CRYPTO TRADER v2.0 STARTED"
    )

    task = asyncio.create_task(
        trading_loop()
    )

    try:

        await dp.start_polling(bot)

    finally:

        task.cancel()

        try:

            await task

        except asyncio.CancelledError:
            pass

        await http.close()

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())
