import asyncio
import aiohttp
import aiosqlite
import json
import logging
import math
import os

from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


# ==========================================
# CONFIG
# ==========================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

OPENROUTER_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

DB_PATH = os.getenv("DB_PATH", "trader.db")

INTERVAL = 900

START_BALANCE = 20.0

# Приблизительная комиссия одной стороны
FEE = 0.001

# Условная половина спреда
HALF_SPREAD = 0.0005

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "AVAXUSDT",
    "TRXUSDT"
]

MEXC_URL = "https://api.mexc.com"

OPENROUTER_URL = (
    "https://openrouter.ai/api/v1"
)

# Можно указать конкретную бесплатную модель
# в переменных окружения Bothost
MODEL = os.getenv(
    "OPENROUTER_MODEL", ""
).strip()

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN отсутствует")

if ADMIN_ID <= 0:
    raise RuntimeError("Неверный ADMIN_ID")


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("AI_TRADER")


# ==========================================
# BOT
# ==========================================

bot = Bot(token=TOKEN)
dp = Dispatcher()

session = None

trading_lock = asyncio.Lock()

selected_model = None


# ==========================================
# DATABASE
# ==========================================

DEFAULTS = {
    "balance": "20",
    "initial_balance": "20",
    "strategy": "AUTO",
    "daily_loss_limit": "10",
    "max_trade_percent": "50",
    "autopilot": "OFF",
    "emergency_stop": "OFF",
    "last_day": "",
    "day_start_equity": "20",
    "risk_blocked_day": "",
    "position_symbol": "",
    "position_qty": "0",
    "position_entry": "0",
    "position_cost": "0",
    "last_cycle": "Никогда"
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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                analyst TEXT,
                critic TEXT,
                result TEXT,
                created_at TEXT
            )
        """)

        for key, value in DEFAULTS.items():

            await db.execute(
                """
                INSERT OR IGNORE INTO settings
                VALUES (?, ?)
                """,
                (key, value)
            )

        await db.commit()


async def get_setting(key):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ) as cursor:

            row = await cursor.fetchone()

            return row[0] if row else None


async def set_setting(key, value):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO settings VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, str(value))
        )

        await db.commit()


async def get_float(key):

    return float(await get_setting(key))


async def save_trade(
    symbol, side, amount, price, pnl
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO trades
            (symbol,side,amount,price,pnl,created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (
                symbol,
                side,
                amount,
                price,
                pnl,
                datetime.now(timezone.utc).isoformat()
            )
        )

        await db.commit()


async def save_decision(
    symbol, analyst, critic, result
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO decisions
            (symbol,analyst,critic,result,created_at)
            VALUES (?,?,?,?,?)
            """,
            (
                symbol,
                analyst,
                critic,
                result,
                datetime.now(timezone.utc).isoformat()
            )
        )

        await db.commit()


# ==========================================
# SECURITY
# ==========================================

class AdminMiddleware(BaseMiddleware):

    async def __call__(
        self, handler, event, data
    ):

        user = data.get("event_from_user")

        if not user:
            return None

        if user.id != ADMIN_ID:
            return None

        return await handler(event, data)


dp.message.outer_middleware(AdminMiddleware())
dp.callback_query.outer_middleware(AdminMiddleware())


# ==========================================
# STATES
# ==========================================

class SettingsStates(StatesGroup):

    loss = State()
    trade = State()
    balance = State()


# ==========================================
# KEYBOARDS
# ==========================================

def keyboard(rows):

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

    return keyboard([
        [
            ("🚀 Запустить", "start"),
            ("🛑 Остановить", "stop")
        ],
        [
            ("💰 Баланс", "balance"),
            ("💼 Позиция", "position")
        ],
        [
            ("📊 Статистика", "stats"),
            ("🧠 Решения ИИ", "decisions")
        ],
        [
            ("🪙 Монеты", "coins"),
            ("⚙️ Настройки", "settings")
        ],
        [
            ("🔍 Анализ сейчас", "analyze")
        ],
        [
            ("🚨 ЭКСТРЕННАЯ ОСТАНОВКА", "emergency")
        ]
    ])


def back_kb():

    return keyboard([
        [("🔙 Главное меню", "menu")]
    ])


def settings_kb():

    return keyboard([
        [("🤖 Стратегия", "strategy")],
        [("📉 Дневной убыток", "loss")],
        [("💵 Размер сделки", "trade_size")],
        [("💰 Сброс демобаланса", "reset_balance")],
        [("🔙 Назад", "menu")]
    ])


# ==========================================
# HTTP
# ==========================================

async def http_json(url, params=None, headers=None, body=None):

    timeout = aiohttp.ClientTimeout(total=25)

    if body is None:

        async with session.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout
        ) as response:

            response.raise_for_status()

            return await response.json()

    async with session.post(
        url,
        json=body,
        headers=headers,
        timeout=timeout
    ) as response:

        response.raise_for_status()

        return await response.json()


# ==========================================
# MEXC
# ==========================================

async def get_candles(symbol):

    data = await http_json(
        MEXC_URL + "/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": "15m",
            "limit": 102
        }
    )

    if not isinstance(data, list) or len(data) < 55:
        raise RuntimeError("Недостаточно свечей")

    # Последняя свеча может быть незакрытой
    return data[:-1]


async def get_price(symbol):

    data = await http_json(
        MEXC_URL + "/api/v3/ticker/bookTicker",
        params={"symbol": symbol}
    )

    bid = float(data["bidPrice"])
    ask = float(data["askPrice"])

    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError("Некорректные котировки")

    return bid, ask


# ==========================================
# INDICATORS
# ==========================================

def ema(values, period):

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:

        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def rsi(values, period=14):

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [
        max(x, 0) for x in changes
    ]

    losses = [
        max(-x, 0) for x in changes
    ]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50

    rs = avg_gain / avg_loss

    return 100 - 100 / (1 + rs)


async def analyze_symbol(symbol):

    candles = await get_candles(symbol)

    closes = [
        float(c[4])
        for c in candles
    ]

    volumes = [
        float(c[5])
        for c in candles
    ]

    price = closes[-1]

    indicator_rsi = rsi(closes)

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    change = (
        (price / closes[-5] - 1) * 100
    )

    avg_volume = sum(volumes[-21:-1]) / 20

    volume_ratio = (
        volumes[-1] / avg_volume
        if avg_volume > 0
        else 0
    )

    score = 0

    if 30 < indicator_rsi < 55:
        score += 2

    if ema20 > ema50:
        score += 2

    if price > ema20:
        score += 1

    if volume_ratio > 1.2:
        score += 1

    return {
        "symbol": symbol,
        "price": round(price, 8),
        "rsi": round(indicator_rsi, 2),
        "ema20": round(ema20, 8),
        "ema50": round(ema50, 8),
        "change_percent": round(change, 2),
        "volume_ratio": round(volume_ratio, 2),
        "score": score
    }


async def scan_market():

    tasks = [
        analyze_symbol(symbol)
        for symbol in SYMBOLS
    ]

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True
    )

    valid = []

    for symbol, result in zip(SYMBOLS, results):

        if isinstance(result, Exception):

            logger.warning(
                "MEXC %s: %s",
                symbol,
                result
            )

            continue

        valid.append(result)

    valid.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return valid


# ==========================================
# OPENROUTER
# ==========================================

async def find_free_model():

    global selected_model

    if MODEL:
        return MODEL

    if selected_model:
        return selected_model

    models = await http_json(
        OPENROUTER_URL + "/models"
    )

    free = []

    for item in models.get("data", []):

        pricing = item.get("pricing", {})

        try:
            prompt_cost = float(
                pricing.get("prompt", "1")
            )

            completion_cost = float(
                pricing.get("completion", "1")
            )

        except (ValueError, TypeError):
            continue

        if prompt_cost != 0 or completion_cost != 0:
            continue

        context = item.get("context_length") or 0

        if context < 4096:
            continue

        free.append(item["id"])

    # Предпочитаем модели с явным суффиксом free
    free.sort(
        key=lambda x: (
            not x.endswith(":free"),
            x
        )
    )

    if not free:
        raise RuntimeError(
            "Бесплатные модели OpenRouter не найдены"
        )

    selected_model = free[0]

    logger.info(
        "OpenRouter model: %s",
        selected_model
    )

    return selected_model


async def ask_ai(system_prompt, user_data):

    if not OPENROUTER_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY отсутствует"
        )

    model = await find_free_model()

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": json.dumps(
                    user_data,
                    ensure_ascii=False
                )
            }
        ],
        "temperature": 0.2,
        "max_tokens": 400
    }

    result = await http_json(
        OPENROUTER_URL + "/chat/completions",
        headers=headers,
        body=payload
    )

    content = result["choices"][0]["message"]["content"]

    if not isinstance(content, str):
        raise RuntimeError("ИИ вернул пустой ответ")

    # Некоторые модели добавляют Markdown
    content = content.strip()

    if "```" in content:
        content = content.replace("```json", "")
        content = content.replace("```", "")

    start = content.find("{")
    end = content.rfind("}")

    if start == -1 or end == -1:
        raise RuntimeError("ИИ не вернул JSON")

    return json.loads(content[start:end + 1])


# ==========================================
# AI AGENTS
# ==========================================

ANALYST_PROMPT = """
Ты аналитик экспериментального криптотрейдера.

Работаешь только в деморежиме.

Тебе передают технические индикаторы,
текущую стратегию и информацию о позиции.

Не выдумывай цены, новости или индикаторы.

Твоя задача — предложить действие.

Если позиции нет:
BUY или HOLD.

Если позиция открыта:
SELL или HOLD.

Если предлагаешь BUY, выбирай только
символ из переданного списка.

Верни строго JSON:

{
 "action": "BUY",
 "symbol": "BTCUSDT",
 "percent": 30,
 "reason": "Краткая причина"
}

percent — процент свободного баланса,
от 1 до 100.

При недостаточных данных выбирай HOLD.
"""


CRITIC_PROMPT = """
Ты независимый риск-аналитик
экспериментального криптотрейдера.

Проверь предложение первого агента.

Не соглашайся автоматически.

Учитывай комиссии, тренд,
волатильность и ограничения риска.

Не выдумывай данные.

Верни строго JSON:

{
 "approved": false,
 "reason": "Краткое объяснение"
}

Для HOLD возвращай approved=false.

Если данных недостаточно,
отклоняй сделку.
"""


async def ai_decision(market, position, strategy):

    payload = {
        "market": market,
        "position": position,
        "strategy": strategy,
        "fee_percent": FEE * 100
    }

    analyst = await ask_ai(
        ANALYST_PROMPT,
        payload
    )

    action = str(
        analyst.get("action", "HOLD")
    ).upper()

    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"

    analyst["action"] = action

    critic = await ask_ai(
        CRITIC_PROMPT,
        {
            "market": market,
            "position": position,
            "proposal": analyst,
            "strategy": strategy
        }
    )

    approved = critic.get("approved") is True

    return analyst, critic, approved


# ==========================================
# PORTFOLIO
# ==========================================

async def get_position():

    symbol = await get_setting("position_symbol")

    if not symbol:
        return None

    return {
        "symbol": symbol,
        "qty": await get_float("position_qty"),
        "entry": await get_float("position_entry"),
        "cost": await get_float("position_cost")
    }


async def get_equity():

    balance = await get_float("balance")

    position = await get_position()

    if not position:
        return balance

    try:

        bid, ask = await get_price(
            position["symbol"]
        )

        liquidation_value = (
            position["qty"]
            * bid
            * (1 - FEE)
        )

        return balance + liquidation_value

    except Exception:

        # При отсутствии котировок не принимаем
        # новых решений о риске
        raise RuntimeError(
            "Невозможно оценить открытую позицию"
        )


async def update_daily_risk():

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    last_day = await get_setting("last_day")

    equity = await get_equity()

    if last_day != today:

        await set_setting("last_day", today)

        await set_setting(
            "day_start_equity",
            equity
        )

        await set_setting(
            "risk_blocked_day",
            ""
        )

    start = await get_float(
        "day_start_equity"
    )

    limit = await get_float(
        "daily_loss_limit"
    )

    if start <= 0:
        raise RuntimeError("Некорректный капитал")

    loss_percent = (
        (start - equity) / start * 100
    )

    blocked = (
        await get_setting("risk_blocked_day")
        == today
    )

    if loss_percent >= limit:

        await set_setting(
            "risk_blocked_day",
            today
        )

        blocked = True

    return {
        "equity": equity,
        "daily_pnl_percent": -loss_percent,
        "blocked": blocked,
        "limit": limit
    }


# ==========================================
# DEMO TRADING
# ==========================================

async def demo_buy(symbol, percent):

    if await get_setting("autopilot") != "ON":
        return "Автопилот выключен"

    if await get_setting("emergency_stop") == "ON":
        return "Аварийная блокировка"

    if await get_position():
        return "Позиция уже открыта"

    risk = await update_daily_risk()

    if risk["blocked"]:
        return "Дневной лимит убытка достигнут"

    balance = await get_float("balance")

    maximum = await get_float(
        "max_trade_percent"
    )

    strategy = await get_setting("strategy")

    if strategy == "SAFE":
        maximum = min(maximum, 25)

    elif strategy == "AGGRESSIVE":
        maximum = min(maximum, 75)

    else:
        maximum = min(maximum, 100)

    percent = max(
        0,
        min(float(percent), maximum)
    )

    amount = balance * percent / 100

    # Минимальная сумма виртуального ордера.
    # Это НЕ подтверждённый лимит MEXC.
    if amount < 5:
        return "Сумма покупки меньше $5"

    bid, ask = await get_price(symbol)

    # Используем реальную котировку ask
    qty = (amount * (1 - FEE)) / ask

    if not math.isfinite(qty) or qty <= 0:
        return "Некорректный объём"

    await set_setting(
        "balance",
        balance - amount
    )

    await set_setting(
        "position_symbol",
        symbol
    )

    await set_setting(
        "position_qty",
        qty
    )

    await set_setting(
        "position_entry",
        ask
    )

    await set_setting(
        "position_cost",
        amount
    )

    await save_trade(
        symbol,
        "BUY",
        amount,
        ask,
        0
    )

    await bot.send_message(
        ADMIN_ID,
        "🟢 ДЕМО-ПОКУПКА\n\n"
        f"🪙 {symbol}\n"
        f"💰 Потрачено: ${amount:.2f}\n"
        f"📊 Цена: ${ask:.8f}\n"
        f"🔢 Количество: {qty:.8f}\n"
        f"💸 Комиссия учтена: {FEE*100:.2f}%"
    )

    return f"BUY {symbol} ${amount:.2f}"


async def demo_sell():

    position = await get_position()

    if not position:
        return "Нет открытой позиции"

    symbol = position["symbol"]

    bid, ask = await get_price(symbol)

    value = (
        position["qty"]
        * bid
        * (1 - FEE)
    )

    pnl = value - position["cost"]

    balance = await get_float("balance")

    await set_setting(
        "balance",
        balance + value
    )

    await set_setting(
        "position_symbol",
        ""
    )

    await set_setting(
        "position_qty",
        0
    )

    await set_setting(
        "position_entry",
        0
    )

    await set_setting(
        "position_cost",
        0
    )

    await save_trade(
        symbol,
        "SELL",
        value,
        bid,
        pnl
    )

    await bot.send_message(
        ADMIN_ID,
        "🔴 ДЕМО-ПРОДАЖА\n\n"
        f"🪙 {symbol}\n"
        f"💰 Получено: ${value:.2f}\n"
        f"📊 Цена: ${bid:.8f}\n"
        f"📈 Результат: ${pnl:+.4f}"
    )

    return f"SELL {symbol} PNL ${pnl:+.4f}"


# ==========================================
# TRADING ENGINE
# ==========================================

async def trading_cycle(force=False):

    if trading_lock.locked():
        return "Анализ уже выполняется"

    async with trading_lock:

        if await get_setting("emergency_stop") == "ON":
            return "Аварийная блокировка"

        if await get_setting("autopilot") != "ON":
            return "Автопилот выключен"

        if not OPENROUTER_KEY:
            return "OpenRouter API-ключ отсутствует"

        try:

            market = await scan_market()

            if not market:
                return "MEXC не вернула рыночные данные"

            position = await get_position()

            strategy = await get_setting(
                "strategy"
            )

            risk = await update_daily_risk()

            if risk["blocked"]:

                await set_setting(
                    "autopilot",
                    "OFF"
                )

                await bot.send_message(
                    ADMIN_ID,
                    "🚨 Достигнут дневной лимит убытка.\n"
                    "Автопилот остановлен.\n"
                    "Открытая позиция не закрывается "
                    "автоматически."
                )

                return "Лимит убытка"

            # Если позиция открыта, передаём
            # агентам данные именно по ней.
            if position:

                current = next(
                    (
                        x for x in market
                        if x["symbol"] == position["symbol"]
                    ),
                    None
                )

                if not current:
                    return "Нет данных открытой позиции"

                selected = [current]

            else:

                selected = market[:3]

            analyst, critic, approved = (
                await ai_decision(
                    selected,
                    position,
                    strategy
                )
            )

            action = analyst["action"]

            symbol = str(
                analyst.get("symbol", "")
            ).upper()

            reason = str(
                analyst.get("reason", "")
            )[:500]

            critic_reason = str(
                critic.get("reason", "")
            )[:500]

            result = "HOLD"

            if approved:

                if action == "BUY" and not position:

                    allowed_symbols = {
                        x["symbol"]
                        for x in selected
                    }

                    if symbol in allowed_symbols:

                        try:
                            percent = float(
                                analyst.get("percent", 0)
                            )

                            if math.isfinite(percent):

                                result = await demo_buy(
                                    symbol,
                                    percent
                                )

                        except (ValueError, TypeError):
                            result = "Неверный размер сделки"

                elif action == "SELL" and position:

                    result = await demo_sell()

            await save_decision(
                symbol or (
                    position["symbol"]
                    if position else "MARKET"
                ),
                json.dumps(
                    analyst,
                    ensure_ascii=False
                ),
                json.dumps(
                    critic,
                    ensure_ascii=False
                ),
                result
            )

            now = datetime.now(
                timezone.utc
            ).strftime("%d.%m %H:%M UTC")

            await set_setting(
                "last_cycle",
                now
            )

            text = (
                "🧠 СОВЕЩАНИЕ ИИ\n\n"
                f"🤖 Аналитик: {action}\n"
                f"🪙 Монета: {symbol or '—'}\n"
                f"💬 {reason}\n\n"
                f"🛡️ Критик: "
                f"{'✅ Одобрено' if approved else '❌ Отклонено'}\n"
                f"💬 {critic_reason}\n\n"
                f"⚖️ Итог: {result}"
            )

            logger.info(
                "Trading result: %s",
                result
            )

            # HOLD тоже сохраняется,
            # но не засоряем Telegram
            # уведомлениями каждые 15 минут.
            if action != "HOLD":
                await bot.send_message(
                    ADMIN_ID,
                    text[:4000]
                )

            return text

        except Exception as error:

            logger.exception(
                "Trading cycle failed"
            )

            return (
                "Ошибка анализа: "
                + str(error)[:300]
            )


async def trading_loop():

    await asyncio.sleep(15)

    while True:

        try:

            if await get_setting("autopilot") == "ON":

                result = await trading_cycle()

                logger.info(
                    "Cycle: %s",
                    result[:200]
                )

        except Exception:

            logger.exception("Trading loop error")

        await asyncio.sleep(INTERVAL)


# ==========================================
# MAIN MENU
# ==========================================

async def main_text():

    balance = await get_float("balance")

    strategy = await get_setting("strategy")

    autopilot = await get_setting("autopilot")

    emergency = await get_setting("emergency_stop")

    limit = await get_setting("daily_loss_limit")

    status = (
        "🟢 ВКЛ"
        if autopilot == "ON"
        else "🔴 ВЫКЛ"
    )

    if emergency == "ON":
        status = "🚨 ЗАБЛОКИРОВАН"

    return (
        "🤖 AI CRYPTO TRADER v1.1\n\n"
        f"💰 Свободный баланс: ${balance:.2f}\n"
        f"📊 Стратегия: {strategy}\n"
        f"📉 Дневной лимит: {limit}%\n"
        f"⚡ Автопилот: {status}\n\n"
        "🧠 Агенты: OpenRouter\n"
        "🏦 Рынок: MEXC\n"
        "💵 Режим: DEMO\n"
        "⏰ Анализ: каждые 15 минут\n\n"
        "⚙️ Выбери действие:"
    )


@dp.message(CommandStart())
async def start_command(message: Message):

    await message.answer(
        await main_text(),
        reply_markup=main_kb()
    )


@dp.message(Command("id"))
async def id_command(message: Message):

    await message.answer(
        f"Твой Telegram ID: {message.from_user.id}"
    )


@dp.message(Command("cancel"))
async def cancel_command(
    message: Message,
    state: FSMContext
):

    await state.clear()

    await message.answer(
        "Ввод отменён.",
        reply_markup=back_kb()
    )


@dp.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery):

    await callback.message.edit_text(
        await main_text(),
        reply_markup=main_kb()
    )

    await callback.answer()


# ==========================================
# AUTOPILOT
# ==========================================

@dp.callback_query(F.data == "start")
async def start_auto(callback: CallbackQuery):

    if await get_setting("emergency_stop") == "ON":

        await callback.answer(
            "Сначала сними аварийную блокировку",
            show_alert=True
        )

        return

    if not OPENROUTER_KEY:

        await callback.answer(
            "Нет OpenRouter API-ключа",
            show_alert=True
        )

        return

    await set_setting("autopilot", "ON")

    await callback.message.edit_text(
        "🚀 АВТОПИЛОТ ЗАПУЩЕН\n\n"
        "🤖 Два ИИ-агента активированы.\n"
        "📊 Интервал: 15 минут.\n"
        "💵 Режим: DEMO.\n\n"
        "Первый анализ начнётся "
        "в ближайшем торговом цикле.",
        reply_markup=back_kb()
    )

    await callback.answer()


@dp.callback_query(F.data == "stop")
async def stop_auto(callback: CallbackQuery):

    await set_setting("autopilot", "OFF")

    await callback.message.edit_text(
        "🛑 Автопилот выключен.\n\n"
        "Новые торговые циклы остановлены.\n"
        "Открытая позиция сохраняется.\n\n"
        "⚠️ Уже выполняющийся анализ "
        "может завершиться.",
        reply_markup=back_kb()
    )

    await callback.answer()


# ==========================================
# EMERGENCY
# ==========================================

@dp.callback_query(F.data == "emergency")
async def emergency(callback: CallbackQuery):

    await set_setting("autopilot", "OFF")

    await set_setting("emergency_stop", "ON")

    await callback.message.edit_text(
        "🚨 АВАРИЙНАЯ ОСТАНОВКА\n\n"
        "Новые торговые циклы заблокированы.\n"
        "Открытая позиция сохраняется.\n\n"
        "⚠️ В этой версии блокировка "
        "не ликвидирует позицию.",
        reply_markup=keyboard([
            [("🔓 Снять блокировку", "unlock")],
            [("🔙 Главное меню", "menu")]
        ])
    )

    await callback.answer()


@dp.callback_query(F.data == "unlock")
async def unlock(callback: CallbackQuery):

    await set_setting("emergency_stop", "OFF")

    await callback.message.edit_text(
        "🔓 Блокировка снята.\n"
        "Автопилот остаётся выключенным.",
        reply_markup=back_kb()
    )

    await callback.answer()


# ==========================================
# BALANCE
# ==========================================

@dp.callback_query(F.data == "balance")
async def balance_handler(callback: CallbackQuery):

    try:

        balance = await get_float("balance")

        equity = await get_equity()

        initial = await get_float(
            "initial_balance"
        )

        pnl = equity - initial

        await callback.message.edit_text(
            "💰 ДЕМО-ПОРТФЕЛЬ\n\n"
            f"💵 Свободно: ${balance:.4f}\n"
            f"🏦 Общий капитал: ${equity:.4f}\n"
            f"📈 PNL: ${pnl:+.4f}\n"
            f"📊 Доходность: "
            f"{pnl/initial*100:+.2f}%\n\n"
            "⚠️ Виртуальные деньги.",
            reply_markup=back_kb()
        )

    except Exception as error:

        await callback.message.answer(
            f"Ошибка оценки портфеля: {error}"
        )

    await callback.answer()


# ==========================================
# POSITION
# ==========================================

@dp.callback_query(F.data == "position")
async def position_handler(callback: CallbackQuery):

    position = await get_position()

    if not position:

        text = "💼 Открытых позиций нет."

    else:

        try:

            bid, ask = await get_price(
                position["symbol"]
            )

            value = (
                position["qty"]
                * bid
                * (1 - FEE)
            )

            pnl = value - position["cost"]

            text = (
                "💼 ОТКРЫТАЯ ПОЗИЦИЯ\n\n"
                f"🪙 {position['symbol']}\n"
                f"📊 Вход: ${position['entry']:.8f}\n"
                f"💰 Вложено: ${position['cost']:.4f}\n"
                f"💵 Текущая стоимость: ${value:.4f}\n"
                f"📈 PNL: ${pnl:+.4f}"
            )

        except Exception as error:

            text = (
                "Не удалось получить цену: "
                + str(error)
            )

    await callback.message.edit_text(
        text,
        reply_markup=back_kb()
    )

    await callback.answer()


# ==========================================
# STATS
# ==========================================

@dp.callback_query(F.data == "stats")
async def stats_handler(callback: CallbackQuery):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT
                COUNT(*),
                COALESCE(SUM(pnl),0)
            FROM trades
            WHERE side='SELL'
        """) as cursor:

            count, pnl = await cursor.fetchone()

    last = await get_setting("last_cycle")

    await callback.message.edit_text(
        "📊 СТАТИСТИКА\n\n"
        f"🔄 Закрытых сделок: {count}\n"
        f"💰 Реализованный PNL: ${pnl:+.4f}\n"
        f"⏰ Последний анализ: {last}",
        reply_markup=back_kb()
    )

    await callback.answer()


# ==========================================
# AI DECISIONS
# ==========================================

@dp.callback_query(F.data == "decisions")
async def decisions_handler(callback: CallbackQuery):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT symbol,analyst,critic,result
            FROM decisions
            ORDER BY id DESC
            LIMIT 1
        """) as cursor:

            row = await cursor.fetchone()

    if not row:

        text = "🧠 Решений ИИ пока нет."

    else:

        symbol, analyst_raw, critic_raw, result = row

        analyst = json.loads(analyst_raw)
        critic = json.loads(critic_raw)

        text = (
            "🧠 ПОСЛЕДНЕЕ РЕШЕНИЕ\n\n"
            f"🪙 {symbol}\n\n"
            "🤖 АНАЛИТИК\n"
            f"Действие: {analyst.get('action')}\n"
            f"Причина: {analyst.get('reason')}\n\n"
            "🛡️ КРИТИК\n"
            f"Одобрено: {critic.get('approved')}\n"
            f"Причина: {critic.get('reason')}\n\n"
            f"⚖️ Итог: {result}"
        )

    await callback.message.edit_text(
        text[:4000],
        reply_markup=back_kb()
    )

    await callback.answer()


# ==========================================
# COINS
# ==========================================

@dp.callback_query(F.data == "coins")
async def coins_handler(callback: CallbackQuery):

    text = (
        "🪙 АНАЛИЗИРУЕМЫЕ МОНЕТЫ\n\n"
        + "\n".join(SYMBOLS)
        + "\n\n📊 Источник: MEXC"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_kb()
    )

    await callback.answer()


# ==========================================
# MANUAL ANALYSIS
# ==========================================

@dp.callback_query(F.data == "analyze")
async def analyze_handler(callback: CallbackQuery):

    await callback.answer(
        "Запускаю анализ..."
    )

    result = await trading_cycle(force=True)

    await callback.message.answer(
        result[:4000],
        reply_markup=back_kb()
    )


# ==========================================
# SETTINGS
# ==========================================

@dp.callback_query(F.data == "settings")
async def settings_handler(callback: CallbackQuery):

    strategy = await get_setting("strategy")

    loss = await get_setting("daily_loss_limit")

    trade = await get_setting(
        "max_trade_percent"
    )

    await callback.message.edit_text(
        "⚙️ НАСТРОЙКИ\n\n"
        f"🤖 Стратегия: {strategy}\n"
        f"📉 Дневной лимит: {loss}%\n"
        f"💵 Размер сделки: {trade}%\n\n"
        "Выбери параметр:",
        reply_markup=settings_kb()
    )

    await callback.answer()


# ==========================================
# STRATEGIES
# ==========================================

@dp.callback_query(F.data == "strategy")
async def strategy_handler(callback: CallbackQuery):

    await callback.message.edit_text(
        "🤖 ВЫБОР СТРАТЕГИИ",
        reply_markup=keyboard([
            [("🟢 Осторожная", "s_SAFE")],
            [("🔴 Агрессивная", "s_AGGRESSIVE")],
            [("🤖 Автопилот", "s_AUTO")],
            [("🔙 Назад", "settings")]
        ])
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("s_"))
async def strategy_select(callback: CallbackQuery):

    strategy = callback.data[2:]

    if strategy not in (
        "SAFE",
        "AGGRESSIVE",
        "AUTO"
    ):
        await callback.answer("Ошибка")
        return

    await set_setting("strategy", strategy)

    await callback.message.edit_text(
        f"✅ Стратегия: {strategy}",
        reply_markup=back_kb()
    )

    await callback.answer()


# ==========================================
# LOSS LIMIT
# ==========================================

@dp.callback_query(F.data == "loss")
async def loss_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        SettingsStates.loss
    )

    await callback.message.answer(
        "📉 Введи дневной лимит убытка.\n"
        "Допустимо: 1–30%.\n\n"
        "Отмена: /cancel"
    )

    await callback.answer()


@dp.message(SettingsStates.loss)
async def save_loss(
    message: Message,
    state: FSMContext
):

    try:

        value = float(
            message.text.replace(",", ".")
        )

        if not math.isfinite(value):
            raise ValueError

        if not 1 <= value <= 30:
            raise ValueError

    except (ValueError, AttributeError):

        await message.answer(
            "Введи число от 1 до 30."
        )

        return

    await set_setting(
        "daily_loss_limit",
        value
    )

    await state.clear()

    await message.answer(
        f"✅ Лимит: {value}%",
        reply_markup=back_kb()
    )


# ==========================================
# TRADE SIZE
# ==========================================

@dp.callback_query(F.data == "trade_size")
async def trade_size_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        SettingsStates.trade
    )

    await callback.message.answer(
        "💵 Введи максимальный размер сделки.\n"
        "От 1 до 100% баланса.\n\n"
        "Отмена: /cancel"
    )

    await callback.answer()


@dp.message(SettingsStates.trade)
async def save_trade_size(
    message: Message,
    state: FSMContext
):

    try:

        value = float(
            message.text.replace(",", ".")
        )

        if not math.isfinite(value):
            raise ValueError

        if not 1 <= value <= 100:
            raise ValueError

    except (ValueError, AttributeError):

        await message.answer(
            "Введи число от 1 до 100."
        )

        return

    await set_setting(
        "max_trade_percent",
        value
    )

    await state.clear()

    await message.answer(
        f"✅ Размер сделки: {value}%",
        reply_markup=back_kb()
    )


# ==========================================
# RESET DEMO BALANCE
# ==========================================

@dp.callback_query(F.data == "reset_balance")
async def reset_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    if await get_setting("autopilot") == "ON":

        await callback.answer(
            "Сначала останови автопилот",
            show_alert=True
        )

        return

    await state.set_state(
        SettingsStates.balance
    )

    await callback.message.answer(
        "💰 Введи новый демобаланс.\n"
        "От $10 до $100000.\n\n"
        "⚠️ Открытая позиция будет удалена.\n"
        "История предыдущих сделок сохранится.\n\n"
        "Отмена: /cancel"
    )

    await callback.answer()


@dp.message(SettingsStates.balance)
async def save_demo_balance(
    message: Message,
    state: FSMContext
):

    if await get_setting("autopilot") == "ON":

        await state.clear()

        await message.answer(
            "Сначала останови автопилот."
        )

        return

    try:

        value = float(
            message.text.replace(",", ".")
        )

        if not math.isfinite(value):
            raise ValueError

        if not 10 <= value <= 100000:
            raise ValueError

    except (ValueError, AttributeError):

        await message.answer(
            "Введи сумму от 10 до 100000."
        )

        return

    async with trading_lock:

        await set_setting("balance", value)

        await set_setting(
            "initial_balance",
            value
        )

        await set_setting(
            "position_symbol",
            ""
        )

        await set_setting("position_qty", 0)

        await set_setting("position_entry", 0)

        await set_setting("position_cost", 0)

        await set_setting("last_day", "")

        await set_setting(
            "risk_blocked_day",
            ""
        )

    await state.clear()

    await message.answer(
        f"✅ Демо-баланс: ${value:.2f}",
        reply_markup=back_kb()
    )


# ==========================================
# FALLBACK
# ==========================================

@dp.message()
async def unknown_message(message: Message):

    await message.answer(
        "Используй /start"
    )


# ==========================================
# START APPLICATION
# ==========================================

async def main():

    global session

    await init_db()

    # Безопасный старт после обновления:
    # автопилот нужно включить вручную.
    await set_setting("autopilot", "OFF")

    session = aiohttp.ClientSession()

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info(
        "AI CRYPTO TRADER v1.1 STARTED"
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

        await session.close()
        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())
