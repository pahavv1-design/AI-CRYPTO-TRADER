import os
import json
import math
import time
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import aiosqlite

from web3 import AsyncWeb3, AsyncHTTPProvider

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


# ==========================================
# CONFIG
# ==========================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

AI_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

WALLET = os.getenv(
    "WALLET_ADDRESS", ""
).strip()

RPC = os.getenv(
    "BASE_RPC_URL",
    "https://mainnet.base.org"
).strip()

DB_PATH = os.getenv(
    "DB_PATH",
    "trader_v13.db"
)

AI_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free"
).strip()

if not TOKEN or ADMIN_ID <= 0:
    raise RuntimeError(
        "Проверь TELEGRAM_TOKEN и ADMIN_ID"
    )

# Base Mainnet
CHAIN_ID = 8453

# Native USDC on Base
USDC = AsyncWeb3.to_checksum_address(
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
)

# WETH on Base
WETH = AsyncWeb3.to_checksum_address(
    "0x4200000000000000000000000000000000000006"
)

# Экспериментальные параметры
START_BALANCE = 20.0

# Условная комиссия DEX за сторону.
# Не является подтверждённым тарифом Aerodrome.
DEMO_FEE = 0.003

# Дополнительное условное проскальзывание.
DEMO_SLIPPAGE = 0.001

MIN_DEMO_TRADE = 2.0

MAX_DAILY_AI_CALLS = 40

AI_COOLDOWN = 900

# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logging.getLogger("httpx").setLevel(
    logging.WARNING
)

logging.getLogger("httpcore").setLevel(
    logging.WARNING
)

logger = logging.getLogger("BASE_TRADER")


# ==========================================
# APPLICATION
# ==========================================

bot = Bot(token=TOKEN)
dp = Dispatcher()

http = None
web3 = None

trade_lock = asyncio.Lock()

last_ai_error = ""
last_ai_attempt = 0


# ==========================================
# SECURITY
# ==========================================

class AdminMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler,
        event,
        data
    ):

        user = data.get("event_from_user")

        if not user:
            return None

        if user.id != ADMIN_ID:
            return None

        return await handler(event, data)


dp.message.outer_middleware(
    AdminMiddleware()
)

dp.callback_query.outer_middleware(
    AdminMiddleware()
)


# ==========================================
# DATABASE
# ==========================================

DEFAULTS = {
    "cash": "20",
    "initial": "20",
    "quantity": "0",
    "entry_cost": "0",
    "autopilot": "OFF",
    "emergency": "OFF",
    "strategy": "AUTO",
    "interval": "10",
    "daily_limit": "10",
    "max_trade": "50",
    "day": "",
    "day_equity": "20",
    "day_blocked": "",
    "ai_day": "",
    "ai_calls": "0",
    "last_ai": "Нет решений",
    "last_cycle": "Никогда",
    "last_price": "0"
}


async def init_db():

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                side TEXT,
                amount REAL,
                price REAL,
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

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ) as cursor:

            row = await cursor.fetchone()

            return row[0] if row else None


async def put(key, value):

    async with aiosqlite.connect(DB_PATH) as db:

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


async def number(key):

    return float(await get(key))


async def save_trade(
    side,
    amount,
    price,
    pnl
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO trades
            (side,amount,price,pnl,created_at)
            VALUES (?,?,?,?,?)
            """,
            (
                side,
                amount,
                price,
                pnl,
                datetime.now(
                    timezone.utc
                ).isoformat()
            )
        )

        await db.commit()


# ==========================================
# HTTP
# ==========================================

async def request_json(
    url,
    payload=None,
    headers=None
):

    timeout = aiohttp.ClientTimeout(
        total=45
    )

    if payload is None:

        async with http.get(
            url,
            timeout=timeout,
            headers=headers
        ) as response:

            response.raise_for_status()

            return await response.json()

    async with http.post(
        url,
        json=payload,
        headers=headers,
        timeout=timeout
    ) as response:

        response.raise_for_status()

        return await response.json()


# ==========================================
# BASE BLOCKCHAIN
# ==========================================

async def check_base():

    chain = await web3.eth.chain_id

    if chain != CHAIN_ID:

        raise RuntimeError(
            f"Неверная сеть: {chain}"
        )

    return True


async def eth_balance():

    await check_base()

    if not AsyncWeb3.is_address(WALLET):
        raise RuntimeError(
            "Неверный WALLET_ADDRESS"
        )

    address = AsyncWeb3.to_checksum_address(
        WALLET
    )

    wei = await web3.eth.get_balance(
        address
    )

    return float(
        web3.from_wei(wei, "ether")
    )


ERC20_ABI = [
    {
        "constant": True,
        "inputs": [
            {
                "name": "owner",
                "type": "address"
            }
        ],
        "name": "balanceOf",
        "outputs": [
            {
                "name": "",
                "type": "uint256"
            }
        ],
        "type": "function"
    }
]


async def usdc_balance():

    await check_base()

    if not AsyncWeb3.is_address(WALLET):
        raise RuntimeError(
            "Неверный WALLET_ADDRESS"
        )

    contract = web3.eth.contract(
        address=USDC,
        abi=ERC20_ABI
    )

    value = await contract.functions.balanceOf(
        AsyncWeb3.to_checksum_address(WALLET)
    ).call()

    return value / 1_000_000


# ==========================================
# MARKET DATA
# ==========================================

async def get_market():

    # DexScreener предоставляет ориентировочные
    # рыночные данные, а не исполняемую
    # котировку Aerodrome.

    url = (
        "https://api.dexscreener.com/"
        "token-pairs/v1/base/"
        + WETH
    )

    data = await request_json(url)

    if not isinstance(data, list):
        raise RuntimeError(
            "Нет данных DexScreener"
        )

    pairs = []

    for pair in data:

        if str(
            pair.get("chainId", "")
        ).lower() != "base":
            continue

        base = pair.get(
            "baseToken", {}
        ).get("address", "")

        quote = pair.get(
            "quoteToken", {}
        ).get("address", "")

        addresses = {
            base.lower(),
            quote.lower()
        }

        if addresses != {
            WETH.lower(),
            USDC.lower()
        }:
            continue

        # Принимаем только пулы Aerodrome
        dex = str(
            pair.get("dexId", "")
        ).lower()

        if "aerodrome" not in dex:
            continue

        liquidity = float(
            (pair.get("liquidity") or {})
            .get("usd") or 0
        )

        if liquidity < 100000:
            continue

        volume = float(
            (pair.get("volume") or {})
            .get("h24") or 0
        )

        pairs.append(
            (liquidity, volume, pair)
        )

    if not pairs:

        raise RuntimeError(
            "Ликвидный пул WETH/USDC "
            "на Aerodrome не найден"
        )

    pairs.sort(
        key=lambda x: x[0],
        reverse=True
    )

    _, volume, pair = pairs[0]

    # DexScreener priceUsd относится
    # к baseToken. Если baseToken — USDC,
    # берём обратную цену.
    base_address = pair[
        "baseToken"
    ]["address"].lower()

    usd_price = float(
        pair["priceUsd"]
    )

    if base_address == WETH.lower():

        price = usd_price

    else:

        price = 1 / usd_price

    if (
        not math.isfinite(price)
        or price <= 0
    ):
        raise RuntimeError(
            "Некорректная цена"
        )

    changes = pair.get(
        "priceChange"
    ) or {}

    return {
        "symbol": "WETH/USDC",
        "price": price,
        "change_5m": float(
            changes.get("m5") or 0
        ),
        "change_1h": float(
            changes.get("h1") or 0
        ),
        "change_24h": float(
            changes.get("h24") or 0
        ),
        "liquidity": pairs[0][0],
        "volume_24h": volume,
        "dex": pair.get("dexId"),
        "pair_address": pair.get(
            "pairAddress"
        )
    }


# ==========================================
# OPENROUTER
# ==========================================

async def ask_ai(
    system_prompt,
    data
):

    if not AI_KEY:
        raise RuntimeError(
            "Нет OPENROUTER_API_KEY"
        )

    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": json.dumps(
                    data,
                    ensure_ascii=False
                )
            }
        ],
        "temperature": 0.1,
        "max_tokens": 1200
    }

    result = await request_json(
        "https://openrouter.ai/api/v1/"
        "chat/completions",
        payload,
        {
            "Authorization": (
                "Bearer " + AI_KEY
            ),
            "Content-Type": (
                "application/json"
            )
        }
    )

    choices = result.get(
        "choices", []
    )

    if not choices:
        raise RuntimeError(
            "OpenRouter: нет choices"
        )

    content = (
        choices[0]
        .get("message", {})
        .get("content")
    )

    if not isinstance(
        content, str
    ) or not content.strip():

        raise RuntimeError(
            "OpenRouter: пустой ответ"
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
            "Неверный формат ответа ИИ"
        )

    return answer


ANALYST_PROMPT = """
Ты трейдер экспериментального
криптовалютного DEMO-бота.

Торговая пара WETH/USDC.

Используй только полученные данные.
Не придумывай индикаторы или новости.

При отсутствии позиции:
BUY или HOLD.

При открытой позиции:
SELL или HOLD.

Не открывай сделки ради активности.

Ответ строго JSON:

{
 "action": "HOLD",
 "percent": 25,
 "reason": "Краткое объяснение"
}
"""


CRITIC_PROMPT = """
Ты независимый риск-аналитик.

Твоя задача — объективно проверить
предложение трейдера.

Тебе передают:
баланс, комиссию, дневной лимит,
размер позиции и данные рынка.

Не выдумывай отсутствующие данные.

Не отклоняй сделку только потому,
что не знаешь уже переданные лимиты.

Но при существенной неопределённости
или риске отклоняй предложение.

Варианты:
APPROVE — разрешить.
MODIFY — уменьшить размер.
REJECT — отклонить.

Ответ строго JSON:

{
 "decision": "REJECT",
 "percent": 0,
 "reason": "Краткое объяснение"
}

Для MODIFY percent означает
новый процент свободного баланса.

Для SELL изменение percent
не используется.
"""


# ==========================================
# PORTFOLIO
# ==========================================

async def portfolio(price):

    cash = await number("cash")

    qty = await number("quantity")

    sale_value = (
        qty
        * price
        * (1 - DEMO_FEE)
        * (1 - DEMO_SLIPPAGE)
    )

    equity = cash + sale_value

    return {
        "cash": cash,
        "quantity": qty,
        "equity": equity,
        "position": qty > 0
    }


async def check_daily_risk(price):

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    p = await portfolio(price)

    equity = p["equity"]

    last_day = await get("day")

    if last_day != today:

        await put(
            "day",
            today
        )

        await put(
            "day_equity",
            equity
        )

        await put(
            "day_blocked",
            ""
        )

    start = await number(
        "day_equity"
    )

    limit = await number(
        "daily_limit"
    )

    if start <= 0:
        raise RuntimeError(
            "Неверный начальный капитал"
        )

    loss = (
        (start - equity)
        / start
        * 100
    )

    if loss >= limit:

        await put(
            "day_blocked",
            today
        )

    blocked = (
        await get("day_blocked")
        == today
    )

    return {
        "blocked": blocked,
        "loss_percent": loss,
        "limit": limit
    }


# ==========================================
# DEMO EXECUTION
# ==========================================

async def execute_demo(
    action,
    percent,
    price
):

    # Используем trade_lock для исключения
    # одновременного исполнения торговых циклов.

    if await get("autopilot") != "ON":
        return "Автопилот выключен"

    if await get("emergency") == "ON":
        return "Аварийная остановка"

    risk = await check_daily_risk(
        price
    )

    if risk["blocked"]:
        return "Дневной лимит достигнут"

    cash = await number("cash")
    qty = await number("quantity")

    if action == "BUY":

        if qty > 0:
            return "Позиция уже открыта"

        max_percent = await number(
            "max_trade"
        )

        strategy = await get(
            "strategy"
        )

        if strategy == "SAFE":
            max_percent = min(
                max_percent, 25
            )

        if strategy == "AGGRESSIVE":
            max_percent = min(
                max_percent, 75
            )

        percent = min(
            percent,
            max_percent
        )

        amount = (
            cash * percent / 100
        )

        if amount < MIN_DEMO_TRADE:
            return "Маленький размер сделки"

        quantity = (
            amount
            * (1 - DEMO_FEE)
            * (1 - DEMO_SLIPPAGE)
            / price
        )

        if quantity <= 0:
            return "Неверное количество"

        # Дополнительная проверка непосредственно
        # перед изменением портфеля.
        if await get("autopilot") != "ON":
            return "Остановлено"

        if await get("emergency") == "ON":
            return "Заблокировано"

        # Баланс, позиция и журнал
        # сохраняются одной транзакцией.
        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            await db.execute(
                "BEGIN IMMEDIATE"
            )

            await db.execute(
                """
                UPDATE settings SET value=?
                WHERE key='cash'
                """,
                (str(cash - amount),)
            )

            await db.execute(
                """
                UPDATE settings SET value=?
                WHERE key='quantity'
                """,
                (str(quantity),)
            )

            await db.execute(
                """
                UPDATE settings SET value=?
                WHERE key='entry_cost'
                """,
                (str(amount),)
            )

            await db.execute(
                """
                INSERT INTO trades
                (side,amount,price,pnl,created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    "BUY",
                    amount,
                    price,
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
            "🪙 WETH/USDC\n"
            f"💰 Сумма: ${amount:.2f}\n"
            f"📊 Цена: ${price:.2f}\n"
            f"🔢 WETH: {quantity:.8f}"
        )

        return (
            f"BUY ${amount:.2f}"
        )

    if action == "SELL":

        if qty <= 0:
            return "Нет позиции"

        value = (
            qty
            * price
            * (1 - DEMO_FEE)
            * (1 - DEMO_SLIPPAGE)
        )

        cost = await number(
            "entry_cost"
        )

        pnl = value - cost

        if await get("autopilot") != "ON":
            return "Остановлено"

        if await get("emergency") == "ON":
            return "Заблокировано"

        async with aiosqlite.connect(
            DB_PATH
        ) as db:

            await db.execute(
                "BEGIN IMMEDIATE"
            )

            await db.execute(
                """
                UPDATE settings SET value=?
                WHERE key='cash'
                """,
                (str(cash + value),)
            )

            await db.execute(
                """
                UPDATE settings SET value='0'
                WHERE key='quantity'
                """
            )

            await db.execute(
                """
                UPDATE settings SET value='0'
                WHERE key='entry_cost'
                """
            )

            await db.execute(
                """
                INSERT INTO trades
                (side,amount,price,pnl,created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    "SELL",
                    value,
                    price,
                    pnl,
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )
            )

            await db.commit()

        await bot.send_message(
            ADMIN_ID,
            "🔴 ДЕМО-ПРОДАЖА\n\n"
            "🪙 WETH/USDC\n"
            f"💰 Получено: ${value:.2f}\n"
            f"📈 PNL: ${pnl:+.4f}"
        )

        return (
            f"SELL PNL ${pnl:+.4f}"
        )

    return "HOLD"


# ==========================================
# AI BUDGET
# ==========================================

async def ai_budget():

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    if await get("ai_day") != today:

        await put("ai_day", today)
        await put("ai_calls", 0)

    return int(
        await number("ai_calls")
    )


async def counted_ai(prompt, data):

    calls = await ai_budget()

    if calls >= MAX_DAILY_AI_CALLS:

        raise RuntimeError(
            "Дневной лимит запросов ИИ"
        )

    await put(
        "ai_calls",
        calls + 1
    )

    return await ask_ai(
        prompt,
        data
    )


# ==========================================
# TRADING ENGINE
# ==========================================

async def trading_cycle():

    global last_ai_attempt
    global last_ai_error

    if trade_lock.locked():
        return "Анализ уже выполняется"

    async with trade_lock:

        if await get("autopilot") != "ON":
            return "Автопилот выключен"

        if await get("emergency") == "ON":
            return "Аварийная остановка"

        try:

            market = await get_market()

            price = market["price"]

            await put(
                "last_price",
                price
            )

            risk = await check_daily_risk(
                price
            )

            if risk["blocked"]:

                await put(
                    "autopilot",
                    "OFF"
                )

                await bot.send_message(
                    ADMIN_ID,
                    "🚨 Дневной лимит достигнут.\n"
                    "Автопилот остановлен.\n"
                    "Позиция остаётся открытой."
                )

                return "Лимит риска"

            p = await portfolio(price)

            now = time.time()

            if (
                now - last_ai_attempt
                < AI_COOLDOWN
            ):
                return "Ожидание ИИ"

            calls = await ai_budget()

            if (
                calls + 2
                > MAX_DAILY_AI_CALLS
            ):
                return "Лимит бесплатного ИИ"

            last_ai_attempt = now

            strategy = await get(
                "strategy"
            )

            max_trade = await number(
                "max_trade"
            )

            # Экономим бесплатные запросы.
            # Без значимого движения рынка
            # новые покупки не рассматриваем.
            movement = abs(
                market["change_1h"]
            )

            if (
                not p["position"]
                and movement < 0.4
            ):
                return (
                    "HOLD: слабое движение рынка"
                )

            context = {
                "market": market,
                "portfolio": p,
                "strategy": strategy,
                "max_trade_percent": max_trade,
                "daily_loss_limit": (
                    risk["limit"]
                ),
                "daily_loss_percent": (
                    risk["loss_percent"]
                ),
                "fee_percent": (
                    DEMO_FEE * 100
                ),
                "slippage_percent": (
                    DEMO_SLIPPAGE * 100
                ),
                "mode": "DEMO"
            }

            analyst = await counted_ai(
                ANALYST_PROMPT,
                context
            )

            action = str(
                analyst.get(
                    "action",
                    "HOLD"
                )
            ).upper()

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
                        analyst.get(
                            "reason", ""
                        )
                    )[:300]
                )

                await put(
                    "last_ai",
                    text
                )

                return text

            critic = await counted_ai(
                CRITIC_PROMPT,
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

            try:

                percent = float(
                    analyst.get(
                        "percent", 0
                    )
                )

                if not math.isfinite(percent):
                    percent = 0

            except (ValueError, TypeError):

                percent = 0

            if decision == "MODIFY":

                try:

                    new_percent = float(
                        critic.get(
                            "percent", 0
                        )
                    )

                    if (
                        math.isfinite(new_percent)
                        and 0 < new_percent <= percent
                    ):
                        percent = new_percent

                    else:
                        decision = "REJECT"

                except (ValueError, TypeError):

                    decision = "REJECT"

            if decision in (
                "APPROVE",
                "MODIFY"
            ):

                if (
                    action == "BUY"
                    and not p["position"]
                    and percent > 0
                ):

                    result = await execute_demo(
                        "BUY",
                        percent,
                        price
                    )

                elif (
                    action == "SELL"
                    and p["position"]
                ):

                    result = await execute_demo(
                        "SELL",
                        100,
                        price
                    )

            text = (
                "🧠 СОВЕЩАНИЕ ИИ\n\n"
                f"🤖 Аналитик: {action}\n"
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

            last_ai_error = ""

            await bot.send_message(
                ADMIN_ID,
                text[:4000]
            )

            return text

        except Exception as error:

            logger.exception(
                "Trading cycle failed"
            )

            last_ai_error = str(error)[:300]

            # При ошибке ничего не покупаем.
            return (
                "Ошибка анализа: "
                + last_ai_error
            )


# ==========================================
# KEYBOARDS
# ==========================================

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
            ("💰 Демо-баланс", "balance"),
            ("👛 MetaMask", "wallet")
        ],
        [
            ("📊 Статистика", "stats"),
            ("💼 Позиция", "position")
        ],
        [
            ("🧠 ИИ-агенты", "agents"),
            ("📈 Рынок Base", "market")
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
        [
            ("🔙 Главное меню", "menu")
        ]
    ])


# ==========================================
# MENU
# ==========================================

async def main_text():

    cash = await number("cash")

    auto = await get("autopilot")

    emergency = await get("emergency")

    interval = await get("interval")

    strategy = await get("strategy")

    status = (
        "🟢 ВКЛ"
        if auto == "ON"
        else "🔴 ВЫКЛ"
    )

    if emergency == "ON":
        status = "🚨 БЛОКИРОВКА"

    return (
        "🤖 AI CRYPTO TRADER v1.3\n\n"
        "🌐 Сеть: BASE\n"
        "💱 DEX: Aerodrome\n"
        "🪙 Пара: WETH/USDC\n\n"
        f"💰 Свободно: ${cash:.2f}\n"
        f"📊 Стратегия: {strategy}\n"
        f"⚡ Автопилот: {status}\n"
        f"⏰ Интервал: {interval} мин\n\n"
        "🧠 Два ИИ-агента\n"
        "💵 Торговля: DEMO\n"
        "👛 MetaMask: только чтение\n\n"
        "Выбери действие:"
    )


@dp.message(CommandStart())
async def start(message: Message):

    await message.answer(
        await main_text(),
        reply_markup=main_kb()
    )


@dp.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):

    await c.message.edit_text(
        await main_text(),
        reply_markup=main_kb()
    )

    await c.answer()


# ==========================================
# AUTOPILOT
# ==========================================

@dp.callback_query(F.data == "run")
async def run(c: CallbackQuery):

    if await get("emergency") == "ON":

        await c.answer(
            "Сначала сними блокировку",
            show_alert=True
        )

        return

    await put(
        "autopilot",
        "ON"
    )

    await c.message.edit_text(
        "🚀 ДЕМО-АВТОПИЛОТ ВКЛЮЧЁН\n\n"
        "ИИ самостоятельно анализирует рынок.\n"
        "Все сделки виртуальные.",
        reply_markup=back()
    )

    await c.answer()


@dp.callback_query(F.data == "stop")
async def stop(c: CallbackQuery):

    await put(
        "autopilot",
        "OFF"
    )

    await c.message.edit_text(
        "🛑 Автопилот выключен.\n"
        "Новые сделки заблокированы.",
        reply_markup=back()
    )

    await c.answer()


@dp.callback_query(F.data == "emergency")
async def emergency(c: CallbackQuery):

    await put(
        "autopilot",
        "OFF"
    )

    await put(
        "emergency",
        "ON"
    )

    await c.message.edit_text(
        "🚨 АВАРИЙНАЯ ОСТАНОВКА\n\n"
        "Новые сделки запрещены.\n"
        "Открытая демопозиция сохраняется.",
        reply_markup=kb([
            [
                ("🔓 Разблокировать", "unlock")
            ],
            [
                ("🔙 Меню", "menu")
            ]
        ])
    )

    await c.answer()


@dp.callback_query(F.data == "unlock")
async def unlock(c: CallbackQuery):

    await put(
        "emergency",
        "OFF"
    )

    await c.message.edit_text(
        "✅ Блокировка снята.\n"
        "Автопилот остаётся выключенным.",
        reply_markup=back()
    )

    await c.answer()


# ==========================================
# WALLET
# ==========================================

@dp.callback_query(F.data == "wallet")
async def wallet_handler(c: CallbackQuery):

    await c.answer(
        "Проверяю Base..."
    )

    try:

        eth = await eth_balance()
        usdc = await usdc_balance()

        text = (
            "👛 METAMASK — BASE\n\n"
            f"📍 Адрес: {WALLET[:8]}...{WALLET[-6:]}\n\n"
            f"⛽ ETH: {eth:.8f}\n"
            f"💵 USDC: {usdc:.6f}\n\n"
            "✅ Подключение успешно.\n"
            "🔐 Доступ: только чтение.\n"
            "❌ Реальные сделки выключены."
        )

    except Exception as error:

        text = (
            "❌ Ошибка Base:\n"
            + str(error)[:500]
        )

    await c.message.answer(
        text,
        reply_markup=back()
    )


# ==========================================
# MARKET
# ==========================================

@dp.callback_query(F.data == "market")
async def market_handler(c: CallbackQuery):

    await c.answer(
        "Получаю котировки..."
    )

    try:

        m = await get_market()

        text = (
            "📈 AERODROME — BASE\n\n"
            f"🪙 {m['symbol']}\n"
            f"💰 Цена: ${m['price']:.2f}\n"
            f"📊 5 минут: {m['change_5m']:+.2f}%\n"
            f"📊 1 час: {m['change_1h']:+.2f}%\n"
            f"📊 24 часа: {m['change_24h']:+.2f}%\n"
            f"💧 Ликвидность: ${m['liquidity']:,.0f}\n"
            f"📦 Объём 24ч: ${m['volume_24h']:,.0f}\n\n"
            "⚠️ Цена ориентировочная, "
            "не исполняемая котировка."
        )

    except Exception as error:

        text = (
            "❌ Ошибка рынка:\n"
            + str(error)[:500]
        )

    await c.message.answer(
        text,
        reply_markup=back()
    )


# ==========================================
# BALANCE
# ==========================================

@dp.callback_query(F.data == "balance")
async def balance_handler(c: CallbackQuery):

    await c.answer()

    try:

        m = await get_market()

        p = await portfolio(
            m["price"]
        )

        initial = await number(
            "initial"
        )

        pnl = (
            p["equity"] - initial
        )

        text = (
            "💰 ДЕМО-ПОРТФЕЛЬ\n\n"
            f"💵 USDC: ${p['cash']:.4f}\n"
            f"🪙 WETH: {p['quantity']:.8f}\n"
            f"🏦 Капитал: ${p['equity']:.4f}\n"
            f"📈 PNL: ${pnl:+.4f}\n\n"
            "⚠️ Виртуальные средства."
        )

    except Exception as error:

        text = (
            "Ошибка оценки:\n"
            + str(error)[:300]
        )

    await c.message.answer(
        text,
        reply_markup=back()
    )


# ==========================================
# POSITION
# ==========================================

@dp.callback_query(F.data == "position")
async def position_handler(c: CallbackQuery):

    qty = await number(
        "quantity"
    )

    if qty <= 0:

        text = (
            "💼 Открытых позиций нет."
        )

    else:

        text = (
            "💼 ДЕМО-ПОЗИЦИЯ\n\n"
            "🪙 WETH/USDC\n"
            f"🔢 Количество: {qty:.8f}\n"
            f"💰 Вложено: "
            f"${await number('entry_cost'):.4f}"
        )

    await c.message.edit_text(
        text,
        reply_markup=back()
    )

    await c.answer()


# ==========================================
# STATISTICS
# ==========================================

@dp.callback_query(F.data == "stats")
async def stats_handler(c: CallbackQuery):

    async with aiosqlite.connect(
        DB_PATH
    ) as db:

        async with db.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(pnl),0)
            FROM trades
            WHERE side='SELL'
        """) as cursor:

            count, pnl = await cursor.fetchone()

    await c.message.edit_text(
        "📊 СТАТИСТИКА\n\n"
        f"🔄 Закрытых сделок: {count}\n"
        f"💰 PNL: ${pnl:+.4f}\n"
        f"⏰ Последний цикл: "
        f"{await get('last_cycle')}",
        reply_markup=back()
    )

    await c.answer()


# ==========================================
# AGENTS
# ==========================================

@dp.callback_query(F.data == "agents")
async def agents_handler(c: CallbackQuery):

    last = await get(
        "last_ai"
    )

    calls = await ai_budget()

    text = (
        "🧠 ИИ-АГЕНТЫ\n\n"
        f"🤖 Модель: {AI_MODEL}\n"
        f"📊 Запросов сегодня: "
        f"{calls}/{MAX_DAILY_AI_CALLS}\n\n"
        "📜 Последнее решение:\n\n"
        f"{last}"
    )

    if last_ai_error:

        text += (
            "\n\n⚠️ Последняя ошибка:\n"
            + last_ai_error
        )

    await c.message.edit_text(
        text[:4000],
        reply_markup=back()
    )

    await c.answer()


# ==========================================
# ANALYZE
# ==========================================

@dp.callback_query(F.data == "analyze")
async def analyze_handler(c: CallbackQuery):

    await c.answer(
        "Анализирую рынок..."
    )

    result = await trading_cycle()

    await c.message.answer(
        result[:4000],
        reply_markup=back()
    )


# ==========================================
# SETTINGS
# ==========================================

@dp.callback_query(F.data == "settings")
async def settings_handler(c: CallbackQuery):

    await c.message.edit_text(
        "⚙️ НАСТРОЙКИ\n\n"
        f"Стратегия: {await get('strategy')}\n"
        f"Интервал: {await get('interval')} мин\n"
        f"Дневной лимит: {await get('daily_limit')}%\n"
        f"Макс. сделка: {await get('max_trade')}%\n\n"
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
                ("💵 Сделка 25%", "trade_25")
            ],
            [
                ("💵 Сделка 50%", "trade_50")
            ],
            [
                ("💵 Сделка 75%", "trade_75")
            ],
            [
                ("🔙 Меню", "menu")
            ]
        ])
    )

    await c.answer()


@dp.callback_query(
    F.data.startswith("strategy_")
)
async def set_strategy(c: CallbackQuery):

    value = c.data.split("_", 1)[1]

    if value not in (
        "SAFE",
        "AUTO",
        "AGGRESSIVE"
    ):
        await c.answer("Ошибка")
        return

    await put(
        "strategy",
        value
    )

    await c.message.edit_text(
        f"✅ Стратегия: {value}",
        reply_markup=back()
    )

    await c.answer()


@dp.callback_query(
    F.data.startswith("interval_")
)
async def set_interval(c: CallbackQuery):

    value = c.data.split("_", 1)[1]

    if value not in ("5", "10", "15"):
        await c.answer("Ошибка")
        return

    await put(
        "interval",
        value
    )

    await c.message.edit_text(
        f"✅ Интервал: {value} минут",
        reply_markup=back()
    )

    await c.answer()


@dp.callback_query(
    F.data.startswith("loss_")
)
async def set_loss(c: CallbackQuery):

    value = c.data.split("_", 1)[1]

    if value not in ("5", "10", "20"):
        await c.answer("Ошибка")
        return

    await put(
        "daily_limit",
        value
    )

    await c.message.edit_text(
        f"✅ Дневной лимит: {value}%",
        reply_markup=back()
    )

    await c.answer()


@dp.callback_query(
    F.data.startswith("trade_")
)
async def set_trade(c: CallbackQuery):

    value = c.data.split("_", 1)[1]

    if value not in ("25", "50", "75"):
        await c.answer("Ошибка")
        return

    await put(
        "max_trade",
        value
    )

    await c.message.edit_text(
        f"✅ Максимальная сделка: {value}%",
        reply_markup=back()
    )

    await c.answer()


# ==========================================
# BACKGROUND
# ==========================================

async def trading_loop():

    await asyncio.sleep(10)

    while True:

        try:

            if await get("autopilot") == "ON":

                result = await trading_cycle()

                logger.info(
                    "Trading cycle: %s",
                    result[:200]
                )

        except Exception:

            logger.exception(
                "Background trading error"
            )

        # Проверяем интервал каждую минуту,
        # чтобы изменения применялись быстрее.
        interval = int(
            await get("interval")
        )

        for _ in range(interval):

            await asyncio.sleep(60)


# ==========================================
# MAIN
# ==========================================

async def main():

    global http
    global web3

    await init_db()

    # После перезапуска требуется
    # ручное включение автопилота.
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

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info(
        "AI CRYPTO TRADER v1.3 STARTED"
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
