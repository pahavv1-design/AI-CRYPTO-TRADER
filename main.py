import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import aiohttp
import aiosqlite

from web3 import Web3
from eth_account import Account

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


# ==========================================
# CONFIG
# ==========================================

VERSION = "3.0"

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN", ""
).strip()

ADMIN_ID = int(
    os.getenv("ADMIN_ID", "0")
)

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openrouter/free"
).strip()

ZEROX_API_KEY = os.getenv(
    "ZEROX_API_KEY", ""
).strip()

BASE_RPC_URL = os.getenv(
    "BASE_RPC_URL",
    "https://mainnet.base.org"
).strip()

TRADING_PRIVATE_KEY = os.getenv(
    "TRADING_PRIVATE_KEY", ""
).strip()

WALLET_ADDRESS = os.getenv(
    "WALLET_ADDRESS", ""
).strip()

DB_PATH = os.getenv(
    "DB_PATH",
    "/app/trader_v30.db"
)

CHAIN_ID = 8453

INITIAL_DEMO = Decimal("20")

MIN_TRADE = Decimal("2")

MAX_TRADE = Decimal("5")

DEFAULT_DAILY_LOSS = Decimal("10")

MAX_DRAWDOWN = Decimal("25")

SLIPPAGE_BPS = 100

MAX_GAS_USD = Decimal("0.20")

MAX_AI_CALLS = 100

ZEROX_URL = (
    "https://api.0x.org"
    "/swap/allowance-holder"
)

# 0x AllowanceHolder deployment.
# Never accept an arbitrary spender from an AI response.
ALLOWANCE_HOLDER = Web3.to_checksum_address(
    "0x0000000000001fF3684f28c67538d4D072C22734"
)

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

ASSETS = ["WETH", "cbBTC", "AERO"]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"}
        ],
        "name": "balanceOf",
        "outputs": [
            {"name": "", "type": "uint256"}
        ],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [
            {"name": "", "type": "uint256"}
        ],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [
            {"name": "", "type": "bool"}
        ],
        "type": "function"
    }
]


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)

log = logging.getLogger("trader")


# ==========================================
# GLOBAL OBJECTS
# ==========================================

bot = Bot(token=TELEGRAM_TOKEN)

dp = Dispatcher()

w3 = Web3(
    Web3.HTTPProvider(
        BASE_RPC_URL,
        request_kwargs={"timeout": 25}
    )
)

wallet = None

if TRADING_PRIVATE_KEY:
    wallet = Account.from_key(TRADING_PRIVATE_KEY)

if wallet and WALLET_ADDRESS:
    if wallet.address.lower() != WALLET_ADDRESS.lower():
        raise RuntimeError(
            "WALLET_ADDRESS не совпадает с приватным ключом"
        )

if wallet:
    WALLET_ADDRESS = wallet.address

trade_lock = asyncio.Lock()

http_session = None

last_cycle = 0

last_heartbeat = 0

last_error_notice = 0


# ==========================================
# DATABASE
# ==========================================

async def db_init():

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created INTEGER,
                mode TEXT,
                side TEXT,
                symbol TEXT,
                amount_usdc REAL,
                quantity REAL,
                tx_hash TEXT,
                pnl REAL DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                mode TEXT,
                symbol TEXT,
                quantity REAL,
                cost REAL,
                PRIMARY KEY (mode, symbol)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created INTEGER,
                tx_hash TEXT UNIQUE,
                raw_tx TEXT,
                kind TEXT,
                side TEXT,
                symbol TEXT,
                amount REAL,
                status TEXT
            )
        """)

        await db.commit()

    defaults = {
        "mode": "DEMO",
        "running": "0",
        "interval": "10",
        "daily_loss": "10",
        "demo_cash": "20",
        "demo_equity_start": "20",
        "real_equity_start": "0",
        "real_day_start": "0",
        "real_day_date": "",
        "demo_day_start": "20",
        "demo_day_date": "",
        "emergency": "0",
        "ai_calls": "0",
        "ai_date": ""
    }

    for key, value in defaults.items():

        async with aiosqlite.connect(DB_PATH) as db:

            await db.execute(
                """
                INSERT OR IGNORE
                INTO settings(key,value)
                VALUES(?,?)
                """,
                (key, value)
            )

            await db.commit()


async def get_setting(key, default=""):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ) as cursor:

            row = await cursor.fetchone()

            return row[0] if row else default


async def set_setting(key, value):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO settings(key,value)
            VALUES(?,?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, str(value))
        )

        await db.commit()


async def get_position(mode, symbol):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT quantity,cost
            FROM positions
            WHERE mode=? AND symbol=?
            """,
            (mode, symbol)
        ) as cursor:

            row = await cursor.fetchone()

    if not row:
        return Decimal("0"), Decimal("0")

    return Decimal(str(row[0])), Decimal(str(row[1]))


async def save_position(mode, symbol, quantity, cost):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO positions(mode,symbol,quantity,cost)
            VALUES(?,?,?,?)
            ON CONFLICT(mode,symbol)
            DO UPDATE SET
                quantity=excluded.quantity,
                cost=excluded.cost
            """,
            (
                mode,
                symbol,
                float(quantity),
                float(cost)
            )
        )

        await db.commit()


async def save_trade(
    mode,
    side,
    symbol,
    amount,
    quantity,
    tx_hash="",
    pnl=0
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO trades
            (
                created,mode,side,symbol,
                amount_usdc,quantity,tx_hash,pnl
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                int(time.time()),
                mode,
                side,
                symbol,
                float(amount),
                float(quantity),
                tx_hash,
                float(pnl)
            )
        )

        await db.commit()


# ==========================================
# TELEGRAM UI
# ==========================================

def keyboard():

    buttons = [
        [
            KeyboardButton(text="🚀 Запустить"),
            KeyboardButton(text="🛑 Остановить")
        ],
        [
            KeyboardButton(text="💰 Портфель"),
            KeyboardButton(text="👛 MetaMask")
        ],
        [
            KeyboardButton(text="💼 Позиции"),
            KeyboardButton(text="📊 Статистика")
        ],
        [
            KeyboardButton(text="🧠 ИИ-агенты"),
            KeyboardButton(text="💱 Проверить 0x")
        ],
        [
            KeyboardButton(text="🔍 Анализ сейчас"),
            KeyboardButton(text="⚙️ Настройки")
        ],
        [
            KeyboardButton(
                text="🚨 ЭКСТРЕННАЯ ОСТАНОВКА"
            )
        ]
    ]

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True
    )


def settings_keyboard():

    rows = [
        [
            InlineKeyboardButton(
                text="🟡 DEMO",
                callback_data="mode_demo"
            ),
            InlineKeyboardButton(
                text="🔴 REAL",
                callback_data="mode_real"
            )
        ],
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
                text="Лимит 5%",
                callback_data="loss_5"
            ),
            InlineKeyboardButton(
                text="Лимит 10%",
                callback_data="loss_10"
            )
        ],
        [
            InlineKeyboardButton(
                text="🔓 Снять экстренную блокировку",
                callback_data="unlock"
            )
        ]
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


async def notify(text):

    try:
        await bot.send_message(
            ADMIN_ID,
            text,
            reply_markup=keyboard()
        )

    except Exception as e:
        log.error("Telegram notification failed: %s", e)


async def authorized(message):

    return message.from_user.id == ADMIN_ID


# ==========================================
# WEB3 HELPERS
# ==========================================

def token_contract(symbol):

    address = Web3.to_checksum_address(
        TOKENS[symbol]["address"]
    )

    return w3.eth.contract(
        address=address,
        abi=ERC20_ABI
    )


def to_units(symbol, amount):

    decimals = TOKENS[symbol]["decimals"]

    factor = Decimal(10) ** decimals

    return int(
        (Decimal(str(amount)) * factor)
        .to_integral_value(rounding=ROUND_DOWN)
    )


def from_units(symbol, amount):

    decimals = TOKENS[symbol]["decimals"]

    return Decimal(str(amount)) / (
        Decimal(10) ** decimals
    )


def token_balance(symbol):

    if not WALLET_ADDRESS:
        return Decimal("0")

    contract = token_contract(symbol)

    raw = contract.functions.balanceOf(
        Web3.to_checksum_address(WALLET_ADDRESS)
    ).call()

    return from_units(symbol, raw)


def eth_balance():

    if not WALLET_ADDRESS:
        return Decimal("0")

    raw = w3.eth.get_balance(
        Web3.to_checksum_address(WALLET_ADDRESS)
    )

    return Decimal(str(w3.from_wei(raw, "ether")))


def allowance(symbol):

    contract = token_contract(symbol)

    return contract.functions.allowance(
        wallet.address,
        ALLOWANCE_HOLDER
    ).call()


# ==========================================
# 0X API
# ==========================================

async def zerox_request(
    endpoint,
    sell_symbol,
    buy_symbol,
    sell_amount
):

    if not ZEROX_API_KEY:
        raise RuntimeError("ZEROX_API_KEY отсутствует")

    if not WALLET_ADDRESS:
        raise RuntimeError("WALLET_ADDRESS отсутствует")

    headers = {
        "0x-api-key": ZEROX_API_KEY,
        "0x-version": "v2"
    }

    params = {
        "chainId": CHAIN_ID,
        "sellToken": TOKENS[sell_symbol]["address"],
        "buyToken": TOKENS[buy_symbol]["address"],
        "sellAmount": str(int(sell_amount)),
        "taker": WALLET_ADDRESS,
        "slippageBps": SLIPPAGE_BPS
    }

    timeout = aiohttp.ClientTimeout(total=25)

    async with http_session.get(
        ZEROX_URL + endpoint,
        headers=headers,
        params=params,
        timeout=timeout
    ) as response:

        data = await response.json(
            content_type=None
        )

        if response.status != 200:

            raise RuntimeError(
                f"0x HTTP {response.status}: "
                f"{str(data)[:350]}"
            )

        return data


async def price_quote(
    sell_symbol,
    buy_symbol,
    amount
):

    raw = to_units(sell_symbol, amount)

    data = await zerox_request(
        "/price",
        sell_symbol,
        buy_symbol,
        raw
    )

    if not data.get("liquidityAvailable", True):
        raise RuntimeError("0x: недостаточно ликвидности")

    received = from_units(
        buy_symbol,
        data["buyAmount"]
    )

    return received, data


async def execution_quote(
    sell_symbol,
    buy_symbol,
    amount
):

    raw = to_units(sell_symbol, amount)

    data = await zerox_request(
        "/quote",
        sell_symbol,
        buy_symbol,
        raw
    )

    if not data.get("liquidityAvailable", True):
        raise RuntimeError("Нет ликвидности")

    if not data.get("transaction"):
        raise RuntimeError("0x не вернул транзакцию")

    return data


# ==========================================
# REAL TRANSACTION JOURNAL
# ==========================================

async def pending_count():

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT COUNT(*)
            FROM pending
            WHERE status IN ('SIGNED','SENT')
            """
        ) as cursor:

            row = await cursor.fetchone()

            return row[0]


async def journal_transaction(
    signed,
    kind,
    side,
    symbol,
    amount
):

    raw = signed.raw_transaction.hex()

    if not raw.startswith("0x"):
        raw = "0x" + raw

    tx_hash = signed.hash.hex()

    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            INSERT INTO pending
            (
                created,tx_hash,raw_tx,kind,
                side,symbol,amount,status
            )
            VALUES(?,?,?,?,?,?,?,'SIGNED')
            """,
            (
                int(time.time()),
                tx_hash,
                raw,
                kind,
                side,
                symbol,
                float(amount)
            )
        )

        await db.commit()

    return tx_hash


async def mark_pending(tx_hash, status):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            """
            UPDATE pending
            SET status=?
            WHERE tx_hash=?
            """,
            (status, tx_hash)
        )

        await db.commit()


async def send_journaled(
    transaction,
    kind,
    side,
    symbol,
    amount
):

    if not wallet:
        raise RuntimeError("Приватный ключ не подключён")

    if await pending_count():
        raise RuntimeError(
            "Есть незавершённая транзакция"
        )

    signed = wallet.sign_transaction(
        transaction
    )

    tx_hash = await journal_transaction(
        signed,
        kind,
        side,
        symbol,
        amount
    )

    try:

        w3.eth.send_raw_transaction(
            signed.raw_transaction
        )

        await mark_pending(
            tx_hash,
            "SENT"
        )

    except Exception:

        # Transaction may have reached the network.
        # Do not sign a replacement automatically.
        log.exception(
            "Broadcast uncertain: %s",
            tx_hash
        )

        raise RuntimeError(
            "Статус отправки неизвестен. "
            "Транзакция сохранена для проверки: "
            + tx_hash
        )

    return tx_hash


async def reconcile_pending():

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                tx_hash,raw_tx,kind,
                side,symbol,amount,status
            FROM pending
            WHERE status IN ('SIGNED','SENT')
            ORDER BY id
            """
        ) as cursor:

            rows = await cursor.fetchall()

    for row in rows:

        (
            tx_hash,
            raw_tx,
            kind,
            side,
            symbol,
            amount,
            status
        ) = row

        try:

            receipt = w3.eth.get_transaction_receipt(
                tx_hash
            )

        except Exception:

            # Re-broadcast exactly the same signed
            # transaction, never a newly signed copy.
            if status == "SIGNED":

                try:

                    w3.eth.send_raw_transaction(
                        Web3.to_bytes(hexstr=raw_tx)
                    )

                    await mark_pending(
                        tx_hash,
                        "SENT"
                    )

                except Exception:
                    pass

            continue

        if receipt["status"] != 1:

            await mark_pending(
                tx_hash,
                "FAILED"
            )

            await notify(
                "❌ Транзакция завершилась ошибкой\n"
                + tx_hash
            )

            continue

        # Require confirmations.
        current_block = w3.eth.block_number

        if current_block - receipt["blockNumber"] < 3:
            continue

        await mark_pending(
            tx_hash,
            "CONFIRMED"
        )

        if kind == "SWAP":

            await notify(
                f"✅ REAL {side} {symbol}\n"
                f"Сумма: ${amount:.4f}\n"
                f"TX: https://basescan.org/tx/{tx_hash}\n\n"
                "Баланс обновлён в блокчейне."
            )

        elif kind == "APPROVAL":

            await notify(
                f"✅ Разрешение USDC/{symbol} "
                "подтверждено.\n"
                "Следующий торговый цикл может "
                "выполнить обмен."
            )


# ==========================================
# REAL EXECUTION
# ==========================================

async def real_swap(
    side,
    symbol,
    amount
):

    if not wallet:
        raise RuntimeError(
            "TRADING_PRIVATE_KEY не настроен"
        )

    if await get_setting("mode") != "REAL":
        raise RuntimeError("REAL выключен")

    if await get_setting("running") != "1":
        raise RuntimeError("Автопилот остановлен")

    if await get_setting("emergency") == "1":
        raise RuntimeError("Экстренная блокировка")

    if await pending_count():
        raise RuntimeError(
            "Ожидание предыдущей транзакции"
        )

    if w3.eth.chain_id != CHAIN_ID:
        raise RuntimeError("Неверная сеть RPC")

    if side == "BUY":

        sell_symbol = "USDC"
        buy_symbol = symbol

        available = token_balance("USDC")

        if available < amount:
            raise RuntimeError(
                "Недостаточно USDC"
            )

        sell_amount = amount

    else:

        sell_symbol = symbol
        buy_symbol = "USDC"

        available = token_balance(symbol)

        if available <= 0:
            raise RuntimeError(
                "Нет токенов для продажи"
            )

        sell_amount = min(
            amount,
            available
        )

    quote = await execution_quote(
        sell_symbol,
        buy_symbol,
        sell_amount
    )

    tx_data = quote["transaction"]

    target = Web3.to_checksum_address(
        tx_data["to"]
    )

    if target != ALLOWANCE_HOLDER:
        raise RuntimeError(
            "Неожиданный адрес контракта 0x"
        )

    issues = quote.get("issues") or {}

    allowance_issue = issues.get("allowance")

    if allowance_issue:

        spender = Web3.to_checksum_address(
            allowance_issue["spender"]
        )

        if spender != ALLOWANCE_HOLDER:

            raise RuntimeError(
                "Неожиданный spender"
            )

    raw_amount = to_units(
        sell_symbol,
        sell_amount
    )

    current_allowance = allowance(
        sell_symbol
    )

    if current_allowance < raw_amount:

        contract = token_contract(
            sell_symbol
        )

        # Exact approval rather than unlimited approval.
        approval = contract.functions.approve(
            ALLOWANCE_HOLDER,
            raw_amount
        )

        nonce = w3.eth.get_transaction_count(
            wallet.address,
            "pending"
        )

        gas_price = w3.eth.gas_price

        tx = approval.build_transaction({
            "from": wallet.address,
            "chainId": CHAIN_ID,
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": gas_price
        })

        gas_cost_eth = (
            Decimal(100000)
            * Decimal(gas_price)
            / Decimal(10 ** 18)
        )

        eth_usdc, _ = await price_quote(
            "WETH",
            "USDC",
            Decimal("1")
        )

        if gas_cost_eth * eth_usdc > MAX_GAS_USD:
            raise RuntimeError(
                "Approval: слишком дорогой газ"
            )

        if eth_balance() < gas_cost_eth:
            raise RuntimeError(
                "Недостаточно ETH для газа"
            )

        tx_hash = await send_journaled(
            tx,
            "APPROVAL",
            side,
            sell_symbol,
            sell_amount
        )

        await notify(
            "🔐 Отправлен approval.\n"
            f"TX: {tx_hash}\n"
            "Обмен будет возможен после подтверждения."
        )

        return "APPROVAL"

    # Recheck the actual swap quote.
    quote = await execution_quote(
        sell_symbol,
        buy_symbol,
        sell_amount
    )

    tx_data = quote["transaction"]

    target = Web3.to_checksum_address(
        tx_data["to"]
    )

    if target != ALLOWANCE_HOLDER:
        raise RuntimeError(
            "Адрес исполнения изменился"
        )

    gas_limit = int(
        tx_data["gas"]
    )

    gas_price = int(
        tx_data.get("gasPrice") or w3.eth.gas_price
    )

    gas_cost_eth = (
        Decimal(gas_limit)
        * Decimal(gas_price)
        / Decimal(10 ** 18)
    )

    eth_usdc, _ = await price_quote(
        "WETH",
        "USDC",
        Decimal("1")
    )

    gas_cost_usd = gas_cost_eth * eth_usdc

    if gas_cost_usd > MAX_GAS_USD:
        raise RuntimeError(
            f"Газ слишком дорогой: ${gas_cost_usd:.3f}"
        )

    if eth_balance() < gas_cost_eth:
        raise RuntimeError(
            "Недостаточно ETH для газа"
        )

    nonce = w3.eth.get_transaction_count(
        wallet.address,
        "pending"
    )

    transaction = {
        "chainId": CHAIN_ID,
        "from": wallet.address,
        "to": target,
        "data": tx_data["data"],
        "value": int(tx_data.get("value", "0")),
        "gas": gas_limit,
        "gasPrice": gas_price,
        "nonce": nonce
    }

    tx_hash = await send_journaled(
        transaction,
        "SWAP",
        side,
        symbol,
        sell_amount
    )

    await notify(
        f"📤 REAL {side} {symbol}\n"
        f"Отправлена транзакция.\n"
        f"TX: https://basescan.org/tx/{tx_hash}"
    )

    return tx_hash


# ==========================================
# PORTFOLIO VALUATION
# ==========================================

async def real_equity():

    if not WALLET_ADDRESS:
        return Decimal("0")

    total = token_balance("USDC")

    for symbol in ASSETS:

        balance = token_balance(symbol)

        if balance <= 0:
            continue

        try:

            value, _ = await price_quote(
                symbol,
                "USDC",
                balance
            )

            total += value

        except Exception:

            # Missing valuation must not be treated
            # as a zero-value asset for risk decisions.
            raise RuntimeError(
                f"Не удалось оценить {symbol}"
            )

    return total


async def demo_equity():

    cash = Decimal(
        await get_setting(
            "demo_cash",
            "20"
        )
    )

    total = cash

    for symbol in ASSETS:

        quantity, cost = await get_position(
            "DEMO",
            symbol
        )

        if quantity <= 0:
            continue

        try:

            value, _ = await price_quote(
                symbol,
                "USDC",
                quantity
            )

            total += value

        except Exception:

            total += cost

    return total


async def current_equity(mode):

    if mode == "REAL":
        return await real_equity()

    return await demo_equity()


async def check_risk(mode):

    equity = await current_equity(mode)

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    date_key = (
        "real_day_date"
        if mode == "REAL"
        else "demo_day_date"
    )

    start_key = (
        "real_day_start"
        if mode == "REAL"
        else "demo_day_start"
    )

    initial_key = (
        "real_equity_start"
        if mode == "REAL"
        else "demo_equity_start"
    )

    if await get_setting(date_key) != today:

        await set_setting(
            date_key,
            today
        )

        await set_setting(
            start_key,
            equity
        )

    initial = Decimal(
        await get_setting(initial_key, "0")
    )

    if initial <= 0:

        initial = equity

        await set_setting(
            initial_key,
            initial
        )

    day_start = Decimal(
        await get_setting(start_key, str(equity))
    )

    if day_start <= 0:
        day_start = equity

    day_loss = (
        (day_start - equity)
        / day_start
        * 100
    )

    drawdown = (
        (initial - equity)
        / initial
        * 100
    ) if initial > 0 else Decimal("0")

    daily_limit = Decimal(
        await get_setting(
            "daily_loss",
            "10"
        )
    )

    if day_loss >= daily_limit:

        await set_setting("running", "0")

        await notify(
            "🚨 Достигнут дневной лимит убытка.\n"
            "Автопилот остановлен."
        )

        return False

    if drawdown >= MAX_DRAWDOWN:

        await set_setting("running", "0")

        await notify(
            "🚨 Достигнут лимит общей просадки.\n"
            "Автопилот остановлен."
        )

        return False

    return True


# ==========================================
# OPENROUTER
# ==========================================

async def ask_ai(system_prompt, data):

    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY отсутствует"
        )

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    if await get_setting("ai_date") != today:

        await set_setting("ai_date", today)

        await set_setting("ai_calls", "0")

    calls = int(
        await get_setting("ai_calls", "0")
    )

    if calls >= MAX_AI_CALLS:

        raise RuntimeError(
            "Дневной лимит ИИ исчерпан"
        )

    await set_setting(
        "ai_calls",
        calls + 1
    )

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
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
        "max_tokens": 700,
        "response_format": {
            "type": "json_object"
        }
    }

    headers = {
        "Authorization": (
            "Bearer " + OPENROUTER_API_KEY
        ),
        "Content-Type": "application/json"
    }

    timeout = aiohttp.ClientTimeout(
        total=60
    )

    async with http_session.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=timeout
    ) as response:

        result = await response.json(
            content_type=None
        )

        if response.status != 200:

            raise RuntimeError(
                f"OpenRouter HTTP {response.status}"
            )

    choices = result.get("choices") or []

    if not choices:
        raise RuntimeError(
            "ИИ не вернул choices"
        )

    content = (
        choices[0]
        .get("message", {})
        .get("content")
    )

    if not content:
        raise RuntimeError(
            "ИИ вернул пустой ответ"
        )

    content = content.strip()

    if content.startswith("```"):

        content = content.replace(
            "```json", ""
        ).replace("```", "").strip()

    return json.loads(content)


ANALYST_PROMPT = """
Ты аналитик автономного криптотрейдера.

Доступные действия:
BUY, SELL, HOLD.

Доступные монеты:
WETH, cbBTC, AERO.

Можно повторно покупать уже открытую монету.
Но нельзя превышать доступный баланс.

Размер покупки от 2 до 5 USDC.

Не используй кредитное плечо.

Принимай решения только по предоставленным данным.
Не выдумывай технические индикаторы.

Если данных недостаточно, выбирай HOLD.

Ответ строго JSON:
{
 "action":"BUY",
 "symbol":"WETH",
 "amount":3,
 "reason":"Краткое объяснение"
}
"""


CRITIC_PROMPT = """
Ты независимый риск-менеджер.

Проверь предложение аналитика.

Ты можешь:
APPROVE
MODIFY
REJECT

Не отклоняй сделку только потому,
что позиция этой монеты уже открыта:
дополнительные покупки разрешены.

Не требуй информацию,
которая уже содержится во входных данных.

Не придумывай ликвидность,
комиссии или показатели рынка.

При недостаточных данных
или нарушении риска отклоняй.

Ответ строго JSON:
{
 "decision":"APPROVE",
 "amount":3,
 "reason":"Краткая причина"
}
"""


# ==========================================
# MARKET SCAN
# ==========================================

async def scan_market(mode):

    market = {}

    for symbol in ASSETS:

        try:

            received, quote = await price_quote(
                "USDC",
                symbol,
                Decimal("5")
            )

            if received <= 0:
                continue

            price = (
                Decimal("5")
                / received
            )

            quantity, cost = await get_position(
                "DEMO",
                symbol
            )

            if mode == "REAL":

                quantity = token_balance(
                    symbol
                )

                cost = Decimal("0")

            market[symbol] = {
                "price_usdc": float(price),
                "buy_amount_for_5_usdc": float(
                    received
                ),
                "position_quantity": float(
                    quantity
                ),
                "recorded_cost": float(cost)
            }

        except Exception as e:

            log.warning(
                "Market %s unavailable: %s",
                symbol,
                e
            )

    return market


# ==========================================
# DEMO TRADING
# ==========================================

async def demo_buy(symbol, amount):

    cash = Decimal(
        await get_setting(
            "demo_cash",
            "20"
        )
    )

    if cash < amount:
        return "Недостаточно DEMO USDC"

    quantity, _ = await price_quote(
        "USDC",
        symbol,
        amount
    )

    old_quantity, old_cost = await get_position(
        "DEMO",
        symbol
    )

    await save_position(
        "DEMO",
        symbol,
        old_quantity + quantity,
        old_cost + amount
    )

    await set_setting(
        "demo_cash",
        cash - amount
    )

    await save_trade(
        "DEMO",
        "BUY",
        symbol,
        amount,
        quantity
    )

    return (
        f"🟢 DEMO BUY {symbol}\n"
        f"Потрачено: ${amount:.4f}\n"
        f"Получено: {quantity:.8f}"
    )


async def demo_sell(symbol):

    quantity, cost = await get_position(
        "DEMO",
        symbol
    )

    if quantity <= 0:
        return "Позиция отсутствует"

    received, _ = await price_quote(
        symbol,
        "USDC",
        quantity
    )

    cash = Decimal(
        await get_setting(
            "demo_cash",
            "20"
        )
    )

    pnl = received - cost

    await set_setting(
        "demo_cash",
        cash + received
    )

    await save_position(
        "DEMO",
        symbol,
        0,
        0
    )

    await save_trade(
        "DEMO",
        "SELL",
        symbol,
        received,
        quantity,
        pnl=pnl
    )

    return (
        f"🔴 DEMO SELL {symbol}\n"
        f"Получено: ${received:.4f}\n"
        f"PNL: ${pnl:+.4f}"
    )


# ==========================================
# TRADING CYCLE
# ==========================================

async def trading_cycle(manual=False):

    if trade_lock.locked():
        return

    async with trade_lock:

        mode = await get_setting(
            "mode",
            "DEMO"
        )

        running = await get_setting(
            "running",
            "0"
        )

        emergency = await get_setting(
            "emergency",
            "0"
        )

        if emergency == "1":
            return

        if not manual and running != "1":
            return

        # Manual analysis does not execute trades
        # when autopilot is stopped.

        try:

            await reconcile_pending()

            if await pending_count():

                if manual:
                    await notify(
                        "⏳ Ожидание подтверждения "
                        "предыдущей транзакции."
                    )

                return

            risk_ok = await check_risk(mode)

            if not risk_ok:
                return

            equity = await current_equity(mode)

            market = await scan_market(mode)

            if not market:
                raise RuntimeError(
                    "Нет доступных котировок"
                )

            if mode == "REAL":

                cash = token_balance(
                    "USDC"
                )

            else:

                cash = Decimal(
                    await get_setting(
                        "demo_cash",
                        "20"
                    )
                )

            analyst_data = {
                "mode": mode,
                "equity_usdc": float(equity),
                "free_usdc": float(cash),
                "market": market,
                "trade_min": 2,
                "trade_max": 5,
                "daily_loss_limit_percent":
                    await get_setting("daily_loss"),
                "max_drawdown_percent": 25,
                "leverage": False
            }

            analyst = await ask_ai(
                ANALYST_PROMPT,
                analyst_data
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

            reason = str(
                analyst.get(
                    "reason",
                    ""
                )
            )[:500]

            if symbol not in ASSETS:
                action = "HOLD"

            if action not in (
                "BUY",
                "SELL",
                "HOLD"
            ):
                action = "HOLD"

            if action == "HOLD":

                if manual:

                    await notify(
                        "🧠 Аналитик: HOLD\n"
                        + reason
                    )

                return

            amount = Decimal(
                str(
                    analyst.get(
                        "amount",
                        "2"
                    )
                )
            )

            if not amount.is_finite():
                raise RuntimeError(
                    "ИИ вернул неверную сумму"
                )

            if action == "BUY":

                amount = max(
                    MIN_TRADE,
                    min(
                        MAX_TRADE,
                        amount
                    )
                )

                if cash < amount:

                    if manual:
                        await notify(
                            "Недостаточно USDC "
                            "для покупки."
                        )

                    return

            critic_data = {
                "proposal": analyst,
                "portfolio": analyst_data,
                "additional_buy_allowed": True,
                "max_single_buy_usdc": 5
            }

            critic = await ask_ai(
                CRITIC_PROMPT,
                critic_data
            )

            decision = str(
                critic.get(
                    "decision",
                    "REJECT"
                )
            ).upper()

            critic_reason = str(
                critic.get(
                    "reason",
                    ""
                )
            )[:500]

            if decision not in (
                "APPROVE",
                "MODIFY"
            ):

                if manual:

                    await notify(
                        "🧠 Аналитик: "
                        + action
                        + " "
                        + symbol
                        + "\n🛡 Критик: REJECT\n"
                        + critic_reason
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
                    raise RuntimeError(
                        "Некорректный размер сделки"
                    )

                # Critic may reduce but never enlarge.
                amount = min(
                    amount,
                    critic_amount,
                    MAX_TRADE,
                    cash
                )

                if amount < MIN_TRADE:
                    return

            await notify(
                "🧠 СОВЕЩАНИЕ ИИ\n\n"
                f"🤖 Аналитик: {action} {symbol}\n"
                f"💬 {reason}\n\n"
                f"🛡 Критик: {decision}\n"
                f"💬 {critic_reason}\n\n"
                f"💰 Режим: {mode}"
            )

            # Recheck current settings after AI latency.

            if await get_setting("emergency") == "1":
                return

            if await get_setting("mode") != mode:
                return

            if await get_setting("running") != "1":

                await notify(
                    "ℹ️ Анализ завершён без сделки: "
                    "автопилот выключен."
                )

                return

            if not await check_risk(mode):
                return

            if mode == "DEMO":

                if action == "BUY":

                    result = await demo_buy(
                        symbol,
                        amount
                    )

                else:

                    result = await demo_sell(
                        symbol
                    )

                await notify(result)

            else:

                if action == "BUY":

                    await real_swap(
                        "BUY",
                        symbol,
                        amount
                    )

                else:

                    balance = token_balance(
                        symbol
                    )

                    if balance <= 0:
                        return

                    # Real SELL closes the available
                    # position in this token.
                    await real_swap(
                        "SELL",
                        symbol,
                        balance
                    )

        except Exception as e:

            log.exception(
                "Trading cycle error"
            )

            await notify(
                "⚠️ Ошибка торгового цикла:\n"
                + str(e)[:500]
            )


# ==========================================
# BACKGROUND LOOP
# ==========================================

async def background_loop():

    global last_cycle
    global last_heartbeat
    global last_error_notice

    await asyncio.sleep(10)

    while True:

        try:

            await reconcile_pending()

            now = time.time()

            if now - last_heartbeat > 3600:

                last_heartbeat = now

                log.info(
                    "Heartbeat: trader alive"
                )

            running = await get_setting(
                "running",
                "0"
            )

            interval = int(
                await get_setting(
                    "interval",
                    "10"
                )
            )

            if running == "1":

                if now - last_cycle >= interval * 60:

                    last_cycle = now

                    await trading_cycle()

        except Exception as e:

            log.exception(
                "Background loop error"
            )

            if time.time() - last_error_notice > 1800:

                last_error_notice = time.time()

                await notify(
                    "⚠️ Ошибка фонового процесса:\n"
                    + str(e)[:300]
                )

        await asyncio.sleep(15)


# ==========================================
# COMMANDS
# ==========================================

@dp.message(Command("start"))
@dp.message(Command("menu"))
async def start_handler(message: Message):

    if not await authorized(message):
        return

    mode = await get_setting(
        "mode",
        "DEMO"
    )

    running = await get_setting(
        "running",
        "0"
    )

    await message.answer(
        f"🤖 AI CRYPTO TRADER v{VERSION}\n\n"
        f"💰 Режим: {mode}\n"
        f"⚡ Автопилот: "
        f"{'ВКЛ' if running == '1' else 'ВЫКЛ'}\n"
        "🌐 Base Mainnet\n"
        "💱 0x Aggregator\n"
        "🧠 Два ИИ-агента\n\n"
        "Меню закреплено под строкой ввода.",
        reply_markup=keyboard()
    )


@dp.message(F.text == "🚀 Запустить")
async def start_trading(message: Message):

    if not await authorized(message):
        return

    if await get_setting("emergency") == "1":

        await message.answer(
            "🚨 Действует экстренная блокировка."
        )

        return

    mode = await get_setting("mode")

    if mode == "REAL":

        if not wallet:

            await message.answer(
                "❌ REAL недоступен: "
                "нет TRADING_PRIVATE_KEY."
            )

            return

        if w3.eth.chain_id != CHAIN_ID:

            await message.answer(
                "❌ Неверная сеть RPC."
            )

            return

        if await pending_count():

            await message.answer(
                "⏳ Сначала дождись предыдущей "
                "транзакции."
            )

            return

        try:

            balance = token_balance("USDC")

            gas = eth_balance()

            if gas <= Decimal("0.00001"):

                await message.answer(
                    "❌ На кошельке недостаточно ETH "
                    "для оплаты газа."
                )

                return

            if await get_setting(
                "real_equity_start",
                "0"
            ) == "0":

                equity = await real_equity()

                await set_setting(
                    "real_equity_start",
                    equity
                )

            await message.answer(
                f"🔴 REAL\n"
                f"USDC: {balance:.4f}\n"
                f"ETH: {gas:.8f}\n\n"
                "Автопилот совершает настоящие "
                "транзакции без подтверждения каждой."
            )

        except Exception as e:

            await message.answer(
                "❌ Не удалось проверить кошелёк:\n"
                + str(e)[:300]
            )

            return

    await set_setting(
        "running",
        "1"
    )

    await message.answer(
        f"🚀 Автопилот запущен!\n"
        f"Режим: {mode}\n"
        "Состояние сохраняется после перезапуска.",
        reply_markup=keyboard()
    )


@dp.message(F.text == "🛑 Остановить")
async def stop_trading(message: Message):

    if not await authorized(message):
        return

    await set_setting(
        "running",
        "0"
    )

    await message.answer(
        "🛑 Автопилот остановлен.\n"
        "Новые сделки запрещены.\n"
        "Уже отправленные транзакции "
        "могут завершиться."
    )


@dp.message(F.text == "🚨 ЭКСТРЕННАЯ ОСТАНОВКА")
async def emergency_stop(message: Message):

    if not await authorized(message):
        return

    await set_setting(
        "running",
        "0"
    )

    await set_setting(
        "emergency",
        "1"
    )

    await message.answer(
        "🚨 ЭКСТРЕННАЯ ОСТАНОВКА\n\n"
        "Новые сделки заблокированы.\n"
        "Отправленные транзакции не отменяются.\n"
        "Токены остаются на кошельке."
    )


@dp.message(F.text == "💰 Портфель")
async def portfolio_handler(message: Message):

    if not await authorized(message):
        return

    mode = await get_setting(
        "mode",
        "DEMO"
    )

    try:

        equity = await current_equity(mode)

        if mode == "REAL":

            cash = token_balance("USDC")

        else:

            cash = Decimal(
                await get_setting(
                    "demo_cash",
                    "20"
                )
            )

        await message.answer(
            f"💰 ПОРТФЕЛЬ {mode}\n\n"
            f"Общая стоимость: ${equity:.4f}\n"
            f"Свободно USDC: ${cash:.4f}\n"
            f"В активах: ${equity-cash:.4f}"
        )

    except Exception as e:

        await message.answer(
            "Ошибка оценки портфеля:\n"
            + str(e)[:300]
        )


@dp.message(F.text == "👛 MetaMask")
async def wallet_handler(message: Message):

    if not await authorized(message):
        return

    if not WALLET_ADDRESS:

        await message.answer(
            "Кошелёк не настроен."
        )

        return

    try:

        text = (
            "👛 METAMASK / BASE\n\n"
            f"Адрес:\n{WALLET_ADDRESS}\n\n"
            f"ETH: {eth_balance():.8f}\n"
        )

        for symbol in TOKENS:

            balance = token_balance(symbol)

            text += (
                f"{symbol}: {balance:.8f}\n"
            )

        text += (
            "\n🔐 Подписание: "
            + (
                "ПОДКЛЮЧЕНО"
                if wallet
                else "ТОЛЬКО ЧТЕНИЕ"
            )
        )

        await message.answer(text)

    except Exception as e:

        await message.answer(
            "Ошибка MetaMask:\n"
            + str(e)[:300]
        )


@dp.message(F.text == "💼 Позиции")
async def positions_handler(message: Message):

    if not await authorized(message):
        return

    mode = await get_setting("mode")

    text = f"💼 ПОЗИЦИИ {mode}\n\n"

    found = False

    for symbol in ASSETS:

        if mode == "REAL":

            quantity = token_balance(
                symbol
            )

            cost = Decimal("0")

        else:

            quantity, cost = await get_position(
                "DEMO",
                symbol
            )

        if quantity <= 0:
            continue

        found = True

        text += (
            f"🪙 {symbol}\n"
            f"Количество: {quantity:.8f}\n"
        )

        if mode == "DEMO":

            text += (
                f"Вложено: ${cost:.4f}\n"
            )

        text += "\n"

    if not found:

        text += "Открытых позиций нет."

    await message.answer(text)


@dp.message(F.text == "📊 Статистика")
async def stats_handler(message: Message):

    if not await authorized(message):
        return

    mode = await get_setting("mode")

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(pnl),0)
            FROM trades
            WHERE mode=?
            """,
            (mode,)
        ) as cursor:

            row = await cursor.fetchone()

    running = await get_setting(
        "running",
        "0"
    )

    pending = await pending_count()

    await message.answer(
        "📊 СТАТИСТИКА\n\n"
        f"Режим: {mode}\n"
        f"Записей сделок: {row[0]}\n"
        f"Учтённый DEMO PNL: "
        f"${row[1]:+.4f}\n"
        f"Незавершённых TX: {pending}\n"
        f"Автопилот: "
        f"{'ВКЛ' if running == '1' else 'ВЫКЛ'}\n\n"
        "Для REAL основным источником "
        "стоимости является баланс блокчейна."
    )


@dp.message(F.text == "🧠 ИИ-агенты")
async def ai_handler(message: Message):

    if not await authorized(message):
        return

    calls = await get_setting(
        "ai_calls",
        "0"
    )

    await message.answer(
        "🧠 ДВА ИИ-АГЕНТА\n\n"
        "🤖 Аналитик: BUY / SELL / HOLD\n"
        "🛡 Критик: APPROVE / MODIFY / REJECT\n\n"
        f"Модель: {OPENROUTER_MODEL}\n"
        f"Запросов сегодня: {calls}/{MAX_AI_CALLS}\n\n"
        "При отказе ИИ новые сделки "
        "не выполняются."
    )


@dp.message(F.text == "💱 Проверить 0x")
async def zerox_handler(message: Message):

    if not await authorized(message):
        return

    try:

        quantity, _ = await price_quote(
            "USDC",
            "WETH",
            Decimal("5")
        )

        await message.answer(
            "✅ 0x работает!\n\n"
            f"5 USDC ≈ {quantity:.8f} WETH\n"
            "Сеть: Base Mainnet"
        )

    except Exception as e:

        await message.answer(
            "❌ Ошибка 0x:\n"
            + str(e)[:350]
        )


@dp.message(F.text == "🔍 Анализ сейчас")
async def analysis_handler(message: Message):

    if not await authorized(message):
        return

    await message.answer(
        "🔍 Запускаю совещание ИИ..."
    )

    asyncio.create_task(
        trading_cycle(manual=True)
    )


@dp.message(F.text == "⚙️ Настройки")
async def settings_handler(message: Message):

    if not await authorized(message):
        return

    mode = await get_setting("mode")

    interval = await get_setting("interval")

    daily = await get_setting("daily_loss")

    await message.answer(
        "⚙️ НАСТРОЙКИ\n\n"
        f"Режим: {mode}\n"
        f"Интервал: {interval} минут\n"
        f"Дневной лимит: {daily}%\n"
        "Размер покупки: $2–5\n"
        "Общая просадка: 25%\n"
        "Сеть: Base\n"
        "Агрегатор: 0x",
        reply_markup=settings_keyboard()
    )


# ==========================================
# CALLBACKS
# ==========================================

@dp.callback_query()
async def callbacks(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    data = callback.data

    if data == "mode_demo":

        await set_setting("running", "0")
        await set_setting("mode", "DEMO")

        await callback.message.answer(
            "🟡 Режим DEMO включён.\n"
            "Автопилот остановлен."
        )

    elif data == "mode_real":

        if not wallet:

            await callback.message.answer(
                "❌ Не настроен TRADING_PRIVATE_KEY."
            )

        else:

            await callback.message.answer(
                "⚠️ ВНИМАНИЕ\n\n"
                "REAL использует настоящие USDC.\n"
                "ИИ сможет самостоятельно покупать "
                "и продавать криптовалюту.\n\n"
                "Для подтверждения нажми кнопку.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ ПОДТВЕРЖДАЮ REAL",
                                callback_data="confirm_real"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Отмена",
                                callback_data="cancel_real"
                            )
                        ]
                    ]
                )
            )

    elif data == "confirm_real":

        await set_setting(
            "running",
            "0"
        )

        await set_setting(
            "mode",
            "REAL"
        )

        await callback.message.answer(
            "🔴 REAL выбран.\n\n"
            "Автопилот пока остановлен.\n"
            "Для начала реальной торговли "
            "нажми «🚀 Запустить»."
        )

    elif data == "cancel_real":

        await callback.message.answer(
            "Переключение отменено."
        )

    elif data.startswith("interval_"):

        interval = int(
            data.split("_")[1]
        )

        if interval in (5, 10, 15):

            await set_setting(
                "interval",
                interval
            )

            await callback.message.answer(
                f"⏰ Интервал: {interval} минут."
            )

    elif data.startswith("loss_"):

        loss = int(
            data.split("_")[1]
        )

        if loss in (5, 10):

            await set_setting(
                "daily_loss",
                loss
            )

            await callback.message.answer(
                f"📉 Дневной лимит: {loss}%."
            )

    elif data == "unlock":

        await set_setting(
            "emergency",
            "0"
        )

        await set_setting(
            "running",
            "0"
        )

        await callback.message.answer(
            "🔓 Экстренная блокировка снята.\n"
            "Автопилот остаётся выключенным."
        )

    await callback.answer()


# ==========================================
# STARTUP
# ==========================================

async def main():

    global http_session

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN отсутствует"
        )

    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID отсутствует"
        )

    if not w3.is_connected():
        raise RuntimeError(
            "Base RPC недоступен"
        )

    if w3.eth.chain_id != CHAIN_ID:
        raise RuntimeError(
            "RPC подключён не к Base"
        )

    await db_init()

    http_session = aiohttp.ClientSession()

    try:

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        await bot.set_my_commands([
            {
                "command": "start",
                "description": "Открыть бота"
            },
            {
                "command": "menu",
                "description": "Показать меню"
            }
        ])

        mode = await get_setting("mode")

        running = await get_setting("running")

        await notify(
            f"✅ AI CRYPTO TRADER v{VERSION}\n\n"
            "Бот запущен.\n"
            f"Режим: {mode}\n"
            f"Автопилот: "
            f"{'ВКЛ' if running == '1' else 'ВЫКЛ'}\n\n"
            "Состояние загружено из базы."
        )

        asyncio.create_task(
            background_loop()
        )

        await dp.start_polling(bot)

    finally:

        await http_session.close()

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(main())
