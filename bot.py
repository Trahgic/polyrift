import os
import json
import requests
import asyncio
import io
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, BotCommand, BufferedInputFile
)
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from eth_account import Account
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from supabase import create_client
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from web3 import Web3
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timezone, timedelta
import uuid

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
FERNET_KEY = os.getenv("MASTER_FERNET_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ALCHEMY_RPC = os.getenv("ALCHEMY_RPC")
RELAY_PRIVATE_KEY = os.getenv("RELAY_PRIVATE_KEY")
DOME_API_KEY = os.getenv("DOME_API_KEY")
ONEINCH_API_KEY = os.getenv("ONEINCH_API_KEY", "")
FEE_PRIVATE_KEY = os.getenv("FEE_PRIVATE_KEY")

FEE_PERCENT = 0.01
FEE_WALLET = Web3.to_checksum_address(os.getenv('FEE_WALLET'))

RELAY_THRESHOLD_POL = 0.05
RELAY_AMOUNT_POL    = 0.15
RELAY_COOLDOWN_HRS  = 24

fernet = Fernet(FERNET_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Dome API client ──────────────────────────────────────────
try:
    from dome_api_sdk import DomeClient
    dome = DomeClient({"api_key": DOME_API_KEY}) if DOME_API_KEY else None
except ImportError:
    dome = None
    print("[dome] dome-api-sdk not installed, Dome features disabled")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler()

token_store = {}
position_store = {}
last_prices = {}
rate_limits = {}  # user_id -> last action timestamp
referral_cache = {}  # user_id -> referred_by (None or int)

def is_rate_limited(user_id, cooldown_seconds=2):
    """Block users from spamming actions faster than cooldown_seconds."""
    now = datetime.now(timezone.utc).timestamp()
    # Cap dict at 10000 entries to prevent memory leak
    if len(rate_limits) > 10000:
        oldest = sorted(rate_limits.items(), key=lambda x: x[1])[:5000]
        for k, _ in oldest:
            del rate_limits[k]
    last = rate_limits.get(user_id, 0)
    if now - last < cooldown_seconds:
        return True
    rate_limits[user_id] = now
    return False

def rate_guard(cooldown_seconds=2):
    """Decorator to rate limit any callback handler."""
    def decorator(func):
        async def wrapper(callback: CallbackQuery, *args, **kwargs):
            if is_rate_limited(callback.from_user.id, cooldown_seconds):
                await safe_answer(callback, "⏳ Slow down!", show_alert=False)
                return
            return await func(callback, *args, **kwargs)
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator

USDC_E = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
CTF = Web3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
EXCHANGE = Web3.to_checksum_address('0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E')
SPENDERS = [
    '0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E',
    '0xC5d563A36AE78145C45a50134d48A1215220f80a',
    '0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296'
]
APPROVE_ABI = [
    {'inputs':[{'name':'spender','type':'address'},{'name':'amount','type':'uint256'}],'name':'approve','outputs':[{'name':'','type':'bool'}],'type':'function'},
    {'inputs':[{'name':'owner','type':'address'},{'name':'spender','type':'address'}],'name':'allowance','outputs':[{'name':'','type':'uint256'}],'type':'function'}
]
CTF_ABI = [
    {'inputs':[{'name':'operator','type':'address'},{'name':'approved','type':'bool'}],'name':'setApprovalForAll','outputs':[],'type':'function'},
    {'inputs':[{'name':'account','type':'address'},{'name':'operator','type':'address'}],'name':'isApprovedForAll','outputs':[{'name':'','type':'bool'}],'type':'function'}
]
ERC20_ABI = [
    {'inputs':[{'name':'spender','type':'address'},{'name':'amount','type':'uint256'}],'name':'approve','outputs':[{'name':'','type':'bool'}],'type':'function'},
    {'inputs':[{'name':'owner','type':'address'},{'name':'spender','type':'address'}],'name':'allowance','outputs':[{'name':'','type':'uint256'}],'type':'function'},
    {'inputs':[{'name':'account','type':'address'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'type':'function'},
    {'inputs':[{'name':'to','type':'address'},{'name':'amount','type':'uint256'}],'name':'transfer','outputs':[{'name':'','type':'bool'}],'type':'function'}
]

class TradeStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_confirm = State()
    waiting_for_sell_amount = State()
    waiting_for_limit_sell_price = State()
    waiting_for_limit_sell_amount = State()
    waiting_for_search = State()
    waiting_for_copy_wallet = State()
    waiting_for_copy_percent = State()
    waiting_for_copy_max = State()
    waiting_for_copy_fixed = State()
    waiting_for_copy_min_win_rate = State()
    waiting_for_copy_budget = State()
    waiting_for_alert_wallet = State()
    waiting_for_copy_min_size = State()
    waiting_for_analytics_wallet = State()
    waiting_for_copy_max_odds = State()
    waiting_for_autosell_price = State()
    waiting_for_display_name = State()
    waiting_for_withdraw_address = State()
    waiting_for_withdraw_amount = State()
    waiting_for_withdraw_confirm = State()
    waiting_for_limit_price = State()
    waiting_for_limit_amount = State()

# ─── UI helpers ──────────────────────────────────────────────

def sentiment_bar(yes_price_float):
    filled = round(yes_price_float * 10)
    filled = max(0, min(10, filled))
    return "🟩" * filled + "⬜" * (10 - filled)

def format_market_card(m, tokens):
    question = m.get("question", "Unknown")
    yes_price_f = 0.5
    no_price_f = 0.5
    yes_str = "N/A"
    no_str = "N/A"
    for t in tokens:
        try:
            p = float(t.get('price', 0))
            if t.get("outcome") == "Yes":
                yes_price_f = p
                yes_str = f"{round(p * 100)}¢"
            if t.get("outcome") == "No":
                no_price_f = p
                no_str = f"{round(p * 100)}¢"
        except: pass
    if yes_str == "N/A":
        try:
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str): prices = json.loads(prices)
            yes_price_f = float(prices[0])
            no_price_f = float(prices[1])
            yes_str = f"{round(yes_price_f * 100)}¢"
            no_str = f"{round(no_price_f * 100)}¢"
        except: pass
    bar = sentiment_bar(yes_price_f)
    volume = m.get("volume24hr", 0)
    vol_str = f"${float(volume):,.0f}" if volume else "N/A"
    end_date = m.get("endDate", "")
    end_str = ""
    if end_date:
        try:
            end = datetime.strptime(end_date[:10], "%Y-%m-%d")
            days_left = (end - datetime.now()).days
            if days_left <= 1:
                end_str = "\n⚠️ _Expires tomorrow!_"
            elif days_left <= 3:
                end_str = f"\n⚠️ _Expires in {days_left} days_"
            elif days_left <= 7:
                end_str = f"\n⏳ _Expires in {days_left} days_"
        except: pass
    return (
        f"*{question}*\n\n"
        f"{bar}\n"
        f"🟢 Yes: *{yes_str}*  🔴 No: *{no_str}*\n"
        f"📊 24h Vol: {vol_str}{end_str}"
    ), yes_price_f, no_price_f

def dynamic_amount_buttons(bal, outcome, token_key):
    ten = round(bal * 0.10, 2)
    twenty = round(bal * 0.25, 2)
    fifty = round(bal * 0.50, 2)
    row = []
    for pct, amt in [("10%", ten), ("25%", twenty), ("50%", fifty)]:
        if amt >= 1:
            row.append(InlineKeyboardButton(
                text=f"{pct} (${amt:.2f})",
                callback_data=f"amt:{amt}:{outcome}:{token_key}"
            ))
    buttons = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def moonpay_url(wallet_address):
    return (
        f"https://buy.moonpay.com/"
        f"?currencyCode=usdc_polygon"
        f"&walletAddress={wallet_address}"
    )

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 Markets", callback_data="menu:markets"),
            InlineKeyboardButton(text="🤖 Copy Trade", callback_data="menu:copy"),
        ],
        [
            InlineKeyboardButton(text="💼 Portfolio", callback_data="menu:portfolio"),
            InlineKeyboardButton(text="💰 Wallet", callback_data="menu:balance"),
        ],
        [
            InlineKeyboardButton(text="🛩 Smart Pilot", callback_data="smart_pilot:menu"),
            InlineKeyboardButton(text="📋 Limit Orders", callback_data="menu:limit_orders"),
        ],
        [
            InlineKeyboardButton(text="📣 Referrals", callback_data="menu:referral"),
            InlineKeyboardButton(text="🏆 Leaderboard", callback_data="menu:leaderboard"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings"),
            InlineKeyboardButton(text="❓ Help", callback_data="menu:help"),
        ],
        [
            InlineKeyboardButton(text="🔄 Refresh", callback_data="menu:main"),
        ],
    ])

def back_to_copy():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Back to Copy Trade", callback_data="menu:copy")]
    ])

def back_to_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
    ])

async def safe_answer(callback, text="", show_alert=False):
    """Safely answer a callback without crashing if it's expired."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass

# ─── Web3 helpers ────────────────────────────────────────────

# ─── Web3 singleton ──────────────────────────────────────────
_w3 = None

def get_w3():
    global _w3
    if _w3 is None or not _w3.is_connected():
        _w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC))
        try:
            from web3.middleware import ExtraDataToPOAMiddleware
            _w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except ImportError:
            try:
                from web3.middleware import geth_poa_middleware
                _w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except ImportError:
                pass
    return _w3

def get_base_fee(w3):
    """Return a safe fixed gas price for Polygon — avoids POA block decoding errors."""
    return 150_000_000_000  # 150 gwei

def build_tx(w3, account_address, contract_fn, nonce, gas=100000):
    """Build a raw tx dict without calling build_transaction (avoids POA block fetch)."""
    return {
        'from': account_address,
        'to': contract_fn.address,
        'nonce': nonce,
        'gas': gas,
        'gasPrice': 150_000_000_000,
        'data': contract_fn._encode_transaction_data(),
        'chainId': 137,
    }

def get_pol_balance(wallet_address):
    try:
        w3 = get_w3()
        return w3.eth.get_balance(Web3.to_checksum_address(wallet_address)) / 1e18
    except:
        return 0

def relay_gas(to_address):
    if not RELAY_PRIVATE_KEY:
        return None
    try:
        w3 = get_w3()
        relay_account = w3.eth.account.from_key(RELAY_PRIVATE_KEY)
        amount_wei = w3.to_wei(RELAY_AMOUNT_POL, 'ether')
        nonce = w3.eth.get_transaction_count(relay_account.address)
        base_fee = get_base_fee(w3)
        tx = {
            'to': Web3.to_checksum_address(to_address),
            'value': amount_wei,
            'gas': 21000,
            'gasPrice': 150_000_000_000,  # 150 gwei
            'nonce': nonce,
            'chainId': 137
        }
        signed = w3.eth.account.sign_transaction(tx, RELAY_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()
    except Exception as e:
        print(f"Gas relay error: {e}")
        return None

def needs_gas_relay(user):
    wallet = user.get("wallet_address")
    if not wallet:
        return False
    pol_bal = get_pol_balance(wallet)
    if pol_bal >= RELAY_THRESHOLD_POL:
        return False
    last_relay = user.get("last_relay_at")
    if last_relay:
        try:
            last_dt = datetime.fromisoformat(last_relay.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last_dt < timedelta(hours=RELAY_COOLDOWN_HRS):
                return False
        except:
            pass
    return True

async def maybe_relay_gas(user):
    if not needs_gas_relay(user):
        return
    tx_hash = relay_gas(user["wallet_address"])
    if tx_hash:
        supabase.table("users").update({
            "last_relay_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", user["id"]).execute()
        try:
            await bot.send_message(
                user["id"],
                f"⛽ *Gas Top-Up Sent!*\n\n"
                f"We noticed your wallet was low on POL for gas fees.\n"
                f"Sent *{RELAY_AMOUNT_POL} POL* to your wallet — you're all set!\n\n"
                f"🔗 [View on PolygonScan](https://polygonscan.com/tx/{tx_hash})",
                parse_mode="Markdown"
            )
        except:
            pass

def collect_fee(private_key, amount_usdc, user_id=None):
    try:
        fee = round(amount_usdc * FEE_PERCENT, 6)
        fee_raw = int(fee * 1_000_000)
        print(f"[fee] collecting ${fee:.4f} ({fee_raw} raw) from trade ${amount_usdc:.2f}")
        if fee_raw <= 0:
            print(f"[fee] skipped — fee_raw={fee_raw}")
            return 0
        w3 = get_w3()
        account = w3.eth.account.from_key(private_key)
        contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        tx = {
            'from': account.address,
            'to': USDC_E,
            'nonce': nonce,
            'gas': 100000,
            'maxFeePerGas': 150_000_000_000,
            'maxPriorityFeePerGas': 30_000_000_000,
            'data': contract.encode_abi('transfer', [FEE_WALLET, fee_raw]),
            'chainId': 137,
        }
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"[fee] tx sent: {tx_hash.hex()}")
        # Credit referral commission (20% of fee) to referrer
        if user_id:
            try:
                # Use cache to avoid DB read on every trade
                if user_id not in referral_cache:
                    user_row = supabase.table("users").select("referred_by").eq("id", user_id).execute()
                    referral_cache[user_id] = user_row.data[0].get("referred_by") if user_row.data else None
                    # Cap cache size
                    if len(referral_cache) > 5000:
                        keys = list(referral_cache.keys())[:2500]
                        for k in keys:
                            del referral_cache[k]
                referrer_id = referral_cache.get(user_id)
                if referrer_id:
                    commission = round(fee * 0.20, 6)
                    referrer = supabase.table("users").select("referral_earnings").eq("id", referrer_id).execute()
                    if referrer.data:
                        current = float(referrer.data[0].get("referral_earnings") or 0)
                        supabase.table("users").update({
                            "referral_earnings": round(current + commission, 6)
                        }).eq("id", referrer_id).execute()
            except Exception as e:
                print(f"[referral] commission error: {e}")
        return fee
    except Exception as e:
        print(f"Fee error: {e}")
        return 0

def send_usdc(private_key, to_address, amount_usdc):
    w3 = get_w3()
    account = w3.eth.account.from_key(private_key)
    contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    amount_raw = int(amount_usdc * 1_000_000)
    nonce = w3.eth.get_transaction_count(account.address)
    tx = {
        'from': account.address,
        'to': USDC_E,
        'nonce': nonce,
        'gas': 100000,
        'gasPrice': 150_000_000_000,
        'data': contract.encode_abi('transfer', [Web3.to_checksum_address(to_address), amount_raw]),
        'chainId': 137,
    }
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()

def setup_wallet_approvals(private_key):
    w3 = get_w3()
    account = w3.eth.account.from_key(private_key)
    usdc = w3.eth.contract(address=USDC_E, abi=APPROVE_ABI)
    ctf = w3.eth.contract(address=CTF, abi=CTF_ABI)
    nonce = w3.eth.get_transaction_count(account.address)
    count = 0
    for spender in SPENDERS:
        if usdc.functions.allowance(account.address, Web3.to_checksum_address(spender)).call() > 0:
            continue
        tx = {
            'from': account.address, 'to': USDC_E, 'nonce': nonce, 'gas': 100000,
            'gasPrice': 150_000_000_000, 'chainId': 137,
            'data': usdc.encode_abi('approve', [Web3.to_checksum_address(spender), 2**256-1]),
        }
        w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, private_key).raw_transaction)
        nonce += 1
        count += 1
    if not ctf.functions.isApprovedForAll(account.address, EXCHANGE).call():
        tx = {
            'from': account.address, 'to': CTF, 'nonce': nonce, 'gas': 100000,
            'gasPrice': 150_000_000_000, 'chainId': 137,
            'data': ctf.encode_abi('setApprovalForAll', [EXCHANGE, True]),
        }
        w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, private_key).raw_transaction)
        count += 1
    return count

def get_usdc_balance(wallet_address):
    try:
        w3 = get_w3()
        contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
        return contract.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call() / 1_000_000
    except:
        return 0

# ─── Market helpers ──────────────────────────────────────────

def get_clob_tokens(condition_id):
    try:
        r = requests.get(f"https://clob.polymarket.com/markets/{condition_id}", timeout=5)
        return r.json().get("tokens", []) if r.ok else []
    except:
        return []

def get_tag_id(tag_label):
    """Fetch the numeric tag ID for a category label from Polymarket's /tags endpoint."""
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/tags?label={tag_label}&limit=5", timeout=5)
        if r.ok:
            tags = r.json()
            if tags:
                return tags[0].get("id")
    except:
        pass
    return None

# Official Polymarket tag IDs from their own codebase
CATEGORY_TAG_IDS = {
    "politics": 2,
    "crypto": 21,
    "sports": 100639,
    "entertainment": 596,
    "science": 1401,
    "economics": 120,
}

def get_markets(search=None, limit=5, category=None):
    try:
        if search:
            today = datetime.now(timezone.utc)
            keyword = search.lower()
            results = []
            # Paginate through markets to find keyword matches
            for offset in [0, 100, 200, 300]:
                url = (
                    f"https://gamma-api.polymarket.com/markets"
                    f"?active=true&closed=false&limit=100&offset={offset}"
                    f"&order=volume24hr&ascending=false"
                )
                r = requests.get(url, timeout=8)
                if not r.ok:
                    break
                data = r.json() if isinstance(r.json(), list) else []
                if not data:
                    break
                for m in data:
                    question = (m.get("question") or m.get("title") or "").lower()
                    if keyword not in question:
                        continue
                    end_date = m.get("endDate", "")
                    if end_date:
                        try:
                            end = datetime.strptime(end_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            if end < today:
                                continue
                        except:
                            pass
                    results.append(m)
                if len(results) >= limit:
                    break
            print(f"[search] query={search} found={len(results)}")
            return results[:limit]
        elif category:
            tag_id = CATEGORY_TAG_IDS.get(category.lower())
            if tag_id:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                url = (
                    f"https://gamma-api.polymarket.com/events"
                    f"?tag_id={tag_id}&related_tags=true&closed=false"
                    f"&active=true&end_date_min={today}"
                    f"&limit=20&order=volume24hr&ascending=false"
                )
                r = requests.get(url, timeout=5)
                if r.ok:
                    events = r.json()
                    markets = []
                    for event in events:
                        event_title = event.get("title", "")
                        event_volume = event.get("volume24hr", 0)
                        event_end = event.get("endDate", "")
                        for m in event.get("markets", []):
                            # Skip resolved/closed markets
                            if m.get("closed") or m.get("archived"):
                                continue
                            # Use market question if available, else event title
                            q = m.get("question") or event_title
                            if not q:
                                continue
                            m["question"] = q
                            m["volume24hr"] = m.get("volume24hr") or event_volume
                            m["endDate"] = m.get("endDate") or event_end
                            # Ensure conditionId is present
                            if not m.get("conditionId"):
                                m["conditionId"] = m.get("condition_id", "")
                            markets.append(m)
                        if len(markets) >= limit:
                            break
                    print(f"[markets] category={category} tag_id={tag_id} events={len(events)} markets={len(markets)}")
                    return markets[:limit]
            url = f"https://gamma-api.polymarket.com/markets?limit={limit}&active=true&order=volume24hr&ascending=false"
            r = requests.get(url, timeout=5)
        else:
            url = f"https://gamma-api.polymarket.com/markets?limit={limit}&active=true&order=volume24hr&ascending=false"
            r = requests.get(url, timeout=5)
        print(f"[markets] URL: {url} | status: {r.status_code} | results: {len(r.json()) if r.ok else 0}")
        return r.json() if r.ok else []
    except Exception as e:
        print(f"[markets] error: {e}")
        return []

def get_market_of_day():
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?limit=1&active=true&order=volume&ascending=false", timeout=5)
        data = r.json()
        return data[0] if data else None
    except:
        return None

def get_positions(wallet_address):
    try:
        r = requests.get(f"https://data-api.polymarket.com/positions?user={wallet_address}&sizeThreshold=0.01", timeout=5)
        return r.json() if r.ok else []
    except:
        return []

def get_recent_trades(wallet_address, limit=10):
    try:
        r = requests.get(f"https://data-api.polymarket.com/activity?user={wallet_address}&limit={limit}", timeout=5)
        return r.json() if r.ok else []
    except:
        return []

def get_open_orders(private_key):
    try:
        client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        return client.get_orders()
    except Exception as e:
        print(f"Get orders error: {e}")
        return []

def cancel_order(private_key, order_id):
    try:
        client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        return client.cancel_order(order_id)
    except Exception as e:
        print(f"Cancel order error: {e}")
        return None

def get_trade_keyboard(tokens, show_quick_bet=True, bal=0, condition_id=None):
    yes_token = no_token = None
    for t in tokens:
        if t.get("outcome") == "Yes": yes_token = t.get("token_id")
        if t.get("outcome") == "No": no_token = t.get("token_id")
    if not yes_token or not no_token: return None
    yk = str(uuid.uuid4())[:8]
    nk = str(uuid.uuid4())[:8]
    token_store[yk] = yes_token
    token_store[nk] = no_token
    buttons = [[
        InlineKeyboardButton(text="🟢 Buy YES", callback_data=f"t:y:{yk}"),
        InlineKeyboardButton(text="🔴 Buy NO", callback_data=f"t:n:{nk}")
    ]]
    if show_quick_bet:
        quick = []
        for amt in [1, 5, 10, 25]:
            if bal == 0 or bal >= amt:
                quick.append(InlineKeyboardButton(text=f"${amt}", callback_data=f"quickbet:{amt}:{yk}"))
        if quick:
            buttons.append(quick)
    # Store condition_id with a short key to stay within Telegram's 64-byte callback limit
    if condition_id:
        ck = str(uuid.uuid4())[:8]
        token_store[f"cid:{ck}"] = condition_id
        buttons.append([
            InlineKeyboardButton(text="📈 Price History", callback_data=f"chart:{ck}"),
            InlineKeyboardButton(text="👥 Who's Buying", callback_data=f"holders:{ck}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── DB helpers ──────────────────────────────────────────────

def get_user(user_id):
    r = supabase.table("users").select("*").eq("id", user_id).execute()
    return r.data[0] if r.data else None

def decrypt_key(enc): return fernet.decrypt(enc.encode()).decode()

def is_trade_seen(user_id, trade_id):
    r = supabase.table("seen_trades").select("id").eq("user_id", user_id).eq("trade_id", trade_id).execute()
    return len(r.data) > 0

def mark_trade_seen(user_id, trade_id):
    try: supabase.table("seen_trades").insert({"user_id": user_id, "trade_id": trade_id, "created_at": datetime.now(timezone.utc).isoformat()}).execute()
    except: pass

def get_daily_pnl(wallet_address):
    try:
        # Get trades from the last 24h only
        since = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
        r = requests.get(
            f"https://data-api.polymarket.com/activity?user={wallet_address}&limit=50"
            f"&type=TRADE&start={since}",
            timeout=5
        )
        trades = r.json() if r.ok else []
        pnl = 0
        for t in trades:
            size = float(t.get("usdcSize", 0) or 0)
            side = t.get("side", "")
            if side == "SELL":
                pnl += size
            elif side == "BUY":
                pnl -= size
        return pnl
    except:
        return 0

# ─── /start ──────────────────────────────────────────────────

@router.message(Command("start"))
async def start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name or "trader"
    existing = supabase.table("users").select("*").eq("id", user_id).execute()

    # Parse referral code from /start REF123456
    ref_code = None
    args = message.text.split()
    if len(args) > 1:
        ref_code = args[1].strip()

    if existing.data:
        user = existing.data[0]
        asyncio.create_task(maybe_relay_gas(user))
        bal, positions, pnl = await get_home_stats(user["wallet_address"])
        text = format_home_text(first_name, user, bal, positions, pnl)
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu())
        return


    account = Account.create()
    encrypted_key = fernet.encrypt(account.key.hex().encode()).decode()

    # Resolve referrer from code
    referred_by = None
    referrer_username = None
    if ref_code:
        try:
            ref_user_id = int(ref_code.replace("REF", ""))
            ref_result = supabase.table("users").select("id, username").eq("id", ref_user_id).execute()
            if ref_result.data and ref_user_id != user_id:
                referred_by = ref_user_id
                referrer_username = ref_result.data[0].get("username") or None
        except:
            pass

    supabase.table("users").insert({
        "id": user_id, "username": username,
        "wallet_address": account.address, "encrypted_key": encrypted_key,
        "referred_by": referred_by, "referral_earnings": 0,
        "referral_bonus_paid": False
    }).execute()

    # Notify referrer someone joined
    if referred_by:
        try:
            await bot.send_message(
                referred_by,
                f"🎉 *New referral!*\n\n"
                f"*{first_name}* just joined PolyRift using your link.\n"
                f"You'll earn *20% of PolyRift's fees* from every trade they make — forever.\n\n"
                f"_They need to deposit $10+ to unlock their $1 bonus._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📣 My Referrals", callback_data="menu:referral")]
                ])
            )
        except:
            pass

    # Mention referral bonus if they came via link
    bonus_str = ""
    if referred_by:
        ref_name = f"@{referrer_username}" if referrer_username else "a friend"
        bonus_str = (
            f"\n\n🎁 *You were invited by {ref_name}!*\n"
            f"Deposit $10+ USDC.e to receive a *$1 USDC.e bonus* automatically."
        )

    await message.answer(
        f"🌊 *Welcome to PolyRift, {first_name}!*\n\n"
        f"Your personal trading wallet has been created.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Your Wallet Address*\n"
        f"`{account.address}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*To get started:*\n"
        f"1️⃣ Send *USDC.e* on Polygon to your wallet\n"
        f"2️⃣ Tap ⚙️ *Settings → Activate Wallet* below\n\n"
        f"_Gas fees are covered by PolyRift ⛽_"
        f"{bonus_str}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Activate Wallet", callback_data="menu:settings")],
            [InlineKeyboardButton(text="📈 Browse Markets", callback_data="menu:markets")]
        ])
    )

# ─── Main menu ────────────────────────────────────────────────

async def get_home_stats(wallet_address: str) -> tuple:
    """Fetch all home screen stats concurrently."""
    loop = asyncio.get_event_loop()
    try:
        bal, positions, pnl = await asyncio.gather(
            loop.run_in_executor(None, get_usdc_balance, wallet_address),
            loop.run_in_executor(None, get_positions, wallet_address),
            loop.run_in_executor(None, get_daily_pnl, wallet_address),
        )
    except:
        bal, positions, pnl = 0.0, [], 0.0
    return bal or 0.0, positions or [], pnl or 0.0

def format_home_text(first_name: str, user: dict, bal: float, positions: list, pnl: float) -> str:
    positions_value = sum(float(p.get("currentValue") or 0) for p in positions)
    active_count = len(positions)
    net_worth = round(bal + positions_value, 2)
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    streak = user.get("win_streak", 0) or 0
    streak_str = f"\n🔥 *Win Streak: {streak}*" if streak >= 2 else ""
    return (
        f"👋 *Welcome back, {first_name}!*\n\n"
        f"📊 *Current Positions:* ${positions_value:.2f}\n"
        f"💰 *Available Balance:* ${bal:.2f}\n"
        f"📋 *Active Positions:* {active_count}\n"
        f"🏦 *Total Net Worth:* ${net_worth:.2f}\n"
        f"{pnl_emoji} *Todays PnL:* {pnl_str}"
        f"{streak_str}\n\n"
        f"Open /wallet to top up your balance."
    )

@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user = get_user(callback.from_user.id)
    first_name = callback.from_user.first_name or "trader"
    if user:
        bal, positions, pnl = await get_home_stats(user["wallet_address"])
        text = format_home_text(first_name, user, bal, positions, pnl)
    else:
        text = "👋 *Welcome to PolyRift!*\n\nCreate a wallet to get started."
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu())
    except:
        pass

# ─── Balance ─────────────────────────────────────────────────

@router.callback_query(F.data == "menu:balance")
async def cb_balance(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await safe_answer(callback)
    loop = asyncio.get_event_loop()
    bal, pol_bal, pnl = await asyncio.gather(
        loop.run_in_executor(None, get_usdc_balance, user["wallet_address"]),
        loop.run_in_executor(None, get_pol_balance, user["wallet_address"]),
        loop.run_in_executor(None, get_daily_pnl, user["wallet_address"]),
    )
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    gas_str = f"⛽ POL (gas): *{pol_bal:.4f}*\n" if pol_bal < RELAY_THRESHOLD_POL else f"⛽ POL (gas): *{pol_bal:.4f}* ✅\n"
    await callback.message.edit_text(
        f"💰 *Your Balance*\n\n"
        f"USDC.e: *${bal:.2f}*\n"
        f"{gas_str}"
        f"{pnl_emoji} Total PnL: *{pnl_str}*\n\n"
        f"📍 Wallet:\n`{user['wallet_address']}`\n\n"
        f"Deposit USDC.e on Polygon, bridge from another chain, or buy with card.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Buy with Card (MoonPay)", url=moonpay_url(user["wallet_address"]))],
            [InlineKeyboardButton(text="🌉 Bridge from Another Chain", url=f"https://jumper.exchange/?toAddress={user['wallet_address']}&toChain=137&toToken=0x2791bca1f2de4661ed88a30c99a7a9449aa84174")],
            [InlineKeyboardButton(text="🔄 Swap POL → USDC.e", callback_data="swap:pol_to_usdc")],
            [InlineKeyboardButton(text="📤 Withdraw", callback_data="menu:withdraw")],
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )

# ─── Withdraw ────────────────────────────────────────────────

@router.callback_query(F.data == "menu:withdraw")
async def cb_withdraw(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    bal = get_usdc_balance(user["wallet_address"])
    if bal < 0.01:
        await callback.message.edit_text(
            "📤 *Withdraw*\n\n❌ No balance to withdraw.",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
        await safe_answer(callback)
        return
    await state.update_data(bal=bal)
    await state.set_state(TradeStates.waiting_for_withdraw_address)
    await callback.message.edit_text(
        f"📤 *Withdraw USDC.e*\n\n"
        f"Available: *${bal:.2f} USDC.e*\n\n"
        f"Enter the destination wallet address on *Polygon*:\n\n"
        f"⚠️ _Only send to Polygon addresses. Sending to wrong network = lost funds._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_withdraw_address)
async def handle_withdraw_address(message: Message, state: FSMContext):
    if is_rate_limited(message.from_user.id, cooldown_seconds=3):
        await message.answer("⏳ Slow down — try again in a moment.")
        return
    address = message.text.strip()
    if not address.startswith("0x") or len(address) != 42:
        await message.answer("❌ Invalid address. Must be a valid 0x Polygon address.")
        return
    try:
        address = Web3.to_checksum_address(address)
    except Exception:
        await message.answer("❌ Invalid address checksum. Double-check and try again.")
        return
    data = await state.get_data()
    bal = data["bal"]
    await state.update_data(to_address=address)
    await state.set_state(TradeStates.waiting_for_withdraw_amount)
    await message.answer(
        f"📤 *Withdraw Amount*\n\n"
        f"Destination: `{address[:8]}...{address[-6:]}`\n"
        f"Available: *${bal:.2f} USDC.e*\n\n"
        f"How much would you like to withdraw?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="50%", callback_data=f"wamt:{round(bal * 0.5, 2)}"),
                InlineKeyboardButton(text="Max", callback_data=f"wamt:{round(bal, 2)}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")]
        ])
    )

@router.callback_query(F.data.startswith("wamt:"))
async def handle_withdraw_quick_amount(callback: CallbackQuery, state: FSMContext):
    amount = float(callback.data.split(":")[1])
    data = await state.get_data()
    await state.update_data(amount=amount)
    await state.set_state(TradeStates.waiting_for_withdraw_confirm)
    to_address = data["to_address"]
    await callback.message.edit_text(
        f"📤 *Confirm Withdrawal*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Amount: *${amount:.2f} USDC.e*\n"
        f"To: `{to_address}`\n"
        f"Network: *Polygon*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚠️ *This cannot be undone. Double-check the address!*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm Withdraw", callback_data="action:confirm_withdraw"),
                InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")
            ]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_withdraw_amount)
async def handle_withdraw_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    bal = data["bal"]
    try:
        amount = float(message.text.strip())
        if amount < 0.01 or amount > bal:
            raise ValueError
    except:
        await message.answer(f"Enter a valid amount between $0.01 and ${bal:.2f}")
        return
    await state.update_data(amount=amount)
    await state.set_state(TradeStates.waiting_for_withdraw_confirm)
    to_address = data["to_address"]
    await message.answer(
        f"📤 *Confirm Withdrawal*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Amount: *${amount:.2f} USDC.e*\n"
        f"To: `{to_address}`\n"
        f"Network: *Polygon*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚠️ *This cannot be undone. Double-check the address!*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm Withdraw", callback_data="action:confirm_withdraw"),
                InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")
            ]
        ])
    )

@router.callback_query(F.data == "action:confirm_withdraw")
async def handle_confirm_withdraw(callback: CallbackQuery, state: FSMContext):
    if is_rate_limited(callback.from_user.id, cooldown_seconds=5):
        await safe_answer(callback, "⏳ Please wait before retrying.", show_alert=True)
        return
    data = await state.get_data()
    to_address = data.get("to_address")
    amount = data.get("amount")
    if not to_address or not amount:
        await safe_answer(callback, "Session expired.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    user = get_user(callback.from_user.id)
    private_key = decrypt_key(user["encrypted_key"])
    bal = get_usdc_balance(user["wallet_address"])
    if amount > bal:
        await callback.message.edit_text(
            f"❌ *Insufficient balance.*\n\nYou have ${bal:.2f} but tried to withdraw ${amount:.2f}.",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
        await safe_answer(callback)
        return
    await callback.message.edit_text("⏳ *Processing withdrawal...*", parse_mode="Markdown")
    try:
        tx_hash = send_usdc(private_key, to_address, amount)
        await callback.message.edit_text(
            f"✅ *Withdrawal Sent!*\n\n"
            f"Amount: *${amount:.2f} USDC.e*\n"
            f"To: `{to_address[:8]}...{to_address[-6:]}`\n\n"
            f"🔗 [View on PolygonScan](https://polygonscan.com/tx/{tx_hash})",
            parse_mode="Markdown",
            reply_markup=back_to_menu()
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ *Withdrawal Failed*\n\n`{str(e)}`",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
    await safe_answer(callback)

# ─── Help ────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery):
    await safe_answer(callback)
    await callback.message.edit_text(
        "❓ <b>PolyRift Help</b>\n\n"
        "<b>Getting Started</b>\n"
        "1. Deposit USDC.e on Polygon to your wallet\n"
        "2. Activate your wallet in Settings\n"
        "3. Browse markets and place trades\n\n"
        "<b>Key Features</b>\n"
        "📈 Markets — Browse and trade live Polymarket markets\n"
        "🤖 Copy Trade — Mirror top traders automatically\n"
        "🛩 Smart Pilot — AI monitors your copied wallets\n"
        "📋 Limit Orders — Set automatic buy/sell triggers\n"
        "💼 Portfolio — Track your positions and PnL\n"
        "📣 Referrals — Earn 20% of fees from your referrals\n\n"
        "<b>Commands</b>\n"
        "/start — Open the main menu\n"
        "/wallet — Quick wallet info\n\n"
        "<b>Support</b>\n"
        "Having issues? Contact us at @polyrift_support",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )

@router.callback_query(F.data == "menu:limit_orders")
async def cb_limit_orders_menu(callback: CallbackQuery):
    await safe_answer(callback)
    # Redirect to the existing limit orders flow
    existing = supabase.table("auto_sells").select("*").execute()
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    orders = supabase.table("auto_sells").select("*").eq("user_id", user["id"]).eq("active", True).execute()
    if not orders.data:
        text = "📋 *Limit Orders*\n\n_No active limit orders._\n\nSet automatic buy or sell triggers on any market."
    else:
        text = f"📋 *Limit Orders*\n\n*{len(orders.data)} active order(s)*\n\n"
        for o in orders.data[:5]:
            direction = "📈 Buy" if o.get("direction") == "buy" else "📉 Sell"
            text += f"{direction} at {o.get('target_price', '?')}¢\n"
    await callback.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )

# ─── Settings ────────────────────────────────────────────────

@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    display = user.get("display_name") or callback.from_user.first_name or "Not set"
    opt_in = user.get("leaderboard_opt_in", False)
    lb_str = "✅ Visible" if opt_in else "❌ Hidden"
    pol_bal = get_pol_balance(user["wallet_address"])
    gas_str = f"⛽ Gas: *{pol_bal:.4f} POL*"
    await callback.message.edit_text(
        f"⚙️ *Settings*\n\n"
        f"📍 Wallet: `{user['wallet_address']}`\n"
        f"👤 Display name: *{display}*\n"
        f"🏆 Leaderboard: *{lb_str}*\n"
        f"{gas_str}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔧 Activate Wallet", callback_data="action:setup")],
            [InlineKeyboardButton(text="👤 Set Display Name", callback_data="action:setname")],
            [InlineKeyboardButton(text="🏆 Toggle Leaderboard", callback_data="action:togglelb")],
            [InlineKeyboardButton(text="🔑 Export Private Key", callback_data="action:exportkey")],
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data == "action:togglelb")
async def cb_toggle_lb(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    current = user.get("leaderboard_opt_in", False)
    supabase.table("users").update({"leaderboard_opt_in": not current}).eq("id", user["id"]).execute()
    status = "visible on" if not current else "hidden from"
    await safe_answer(callback, f"You are now {status} the leaderboard!", show_alert=True)
    await cb_settings(callback)

@router.callback_query(F.data == "action:exportkey")
async def cb_export_key(callback: CallbackQuery):
    if is_rate_limited(callback.from_user.id, cooldown_seconds=10):
        await safe_answer(callback, "⏳ Please wait before retrying.", show_alert=True)
        return
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await safe_answer(callback)
    private_key = decrypt_key(user["encrypted_key"])
    sent = await callback.message.answer(
        f"🔑 *Your Private Key*\n\n"
        f"`{private_key}`\n\n"
        f"⚠️ *WARNING — Keep this secret!*\n"
        f"Anyone with this key has full access to your wallet and funds.\n\n"
        f"_Use this to import your wallet into MetaMask or any Web3 wallet._\n\n"
        f"⏱ _This message will self-delete in 60 seconds._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Back to Settings", callback_data="menu:settings")]
        ])
    )
    await asyncio.sleep(60)
    try:
        await sent.delete()
    except:
        pass

@router.callback_query(F.data == "action:setname")
async def cb_setname(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TradeStates.waiting_for_display_name)
    await callback.message.edit_text(
        "👤 *Set Display Name*\n\nThis name appears on the leaderboard.\n\nType your name:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:settings")]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_display_name)
async def handle_display_name(message: Message, state: FSMContext):
    await state.clear()
    name = message.text.strip()[:30]
    supabase.table("users").update({"display_name": name}).eq("id", message.from_user.id).execute()
    await message.answer(f"✅ Display name set to *{name}*!", parse_mode="Markdown", reply_markup=back_to_menu())

@router.callback_query(F.data == "action:setup")
async def cb_setup(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚙️ *Activating your wallet...*\n\n_This may take up to 60 seconds._",
        parse_mode="Markdown"
    )
    private_key = decrypt_key(user["encrypted_key"])
    await maybe_relay_gas(user)
    try:
        count = setup_wallet_approvals(private_key)
        if count > 0:
            await callback.message.edit_text(
                f"✅ *Wallet Activated!*\n\n_{count} approval(s) confirmed._\n\nYou're ready to trade!",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📈 Browse Markets", callback_data="menu:markets")],
                    [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
                ])
            )
        else:
            await callback.message.edit_text(
                "✅ *Wallet Already Active!*", parse_mode="Markdown", reply_markup=back_to_menu()
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ *Setup Failed*\n\n`{str(e)}`\n\n_Gas is being topped up — try again in 30 seconds._",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
    await safe_answer(callback)

# ─── Markets ─────────────────────────────────────────────────

@router.callback_query(F.data == "menu:markets")
async def cb_markets(callback: CallbackQuery):
    await callback.message.edit_text("📈 *Fetching markets...*", parse_mode="Markdown")
    motd = get_market_of_day()
    if motd:
        condition_id = motd.get("conditionId")
        tokens = get_clob_tokens(condition_id) if condition_id else []
        card, _, _ = format_market_card(motd, tokens)
        await callback.message.edit_text(
            f"🌟 *Market of the Day*\n\n{card}",
            parse_mode="Markdown", reply_markup=get_trade_keyboard(tokens)
        )
    markets = get_markets(limit=5)
    if not markets:
        await callback.message.answer("😕 No markets found.", reply_markup=back_to_menu())
        await safe_answer(callback)
        return
    await callback.message.answer("📈 *Trending Markets* — _Tap Yes or No to trade_", parse_mode="Markdown")
    for m in markets:
        condition_id = m.get("conditionId")
        tokens = get_clob_tokens(condition_id) if condition_id else []
        card, _, _ = format_market_card(m, tokens)
        await callback.message.answer(card, parse_mode="Markdown", reply_markup=get_trade_keyboard(tokens))
    await callback.message.answer("_Top 5 by 24h volume_", reply_markup=back_to_menu())
    await safe_answer(callback)

@router.callback_query(F.data == "menu:search")
async def cb_search_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TradeStates.waiting_for_search)
    await callback.message.edit_text(
        "🔍 *Search Markets*\n\nType a keyword:\n\n_Examples: bitcoin, trump, fed, oscar_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_search)
async def handle_search(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    await message.answer(f"🔍 Searching *{query}*...", parse_mode="Markdown")
    results = get_markets(search=query, limit=5)
    if not results:
        await message.answer(f"😕 No results for *{query}*.", parse_mode="Markdown", reply_markup=back_to_menu())
        return
    for m in results:
        condition_id = m.get("conditionId")
        tokens = get_clob_tokens(condition_id) if condition_id else []
        card, _, _ = format_market_card(m, tokens)
        await message.answer(card, parse_mode="Markdown", reply_markup=get_trade_keyboard(tokens))
    await message.answer("_Tap Yes or No to trade_", reply_markup=back_to_menu())

# ─── Portfolio ───────────────────────────────────────────────

@router.callback_query(F.data == "menu:portfolio")
async def cb_portfolio(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.edit_text("💼 *Fetching positions...*", parse_mode="Markdown")
    pos = get_positions(user["wallet_address"])
    if not pos:
        await callback.message.edit_text(
            "💼 *Portfolio*\n\nNo open positions yet.\n\n_Start trading to see positions here._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📈 Browse Markets", callback_data="menu:markets")],
                [InlineKeyboardButton(text="📋 Activity", callback_data="menu:activity")],
                [InlineKeyboardButton(text="📑 Open Orders", callback_data="menu:open_orders")],
                [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
            ])
        )
        await safe_answer(callback)
        return

    total_value = sum(float(p.get("currentValue", 0)) for p in pos)
    total_cost = sum(float(p.get("initialValue", 0)) for p in pos)
    total_pnl = total_value - total_cost
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    await callback.message.edit_text(
        f"💼 *Portfolio Summary*\n\n"
        f"Total Value: *${total_value:.2f}*\n"
        f"{pnl_emoji} Total PnL: *{pnl_str}*\n"
        f"Positions: *{len(pos)}*",
        parse_mode="Markdown"
    )

    for p in pos:
        title = p.get("title", "Unknown")
        outcome = p.get("outcome", "")
        size = float(p.get("size", 0))
        current_value = float(p.get("currentValue", 0))
        avg_price = float(p.get("avgPrice", 0))
        cur_price = float(p.get("curPrice", 0))
        pnl = current_value - (size * avg_price)
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        outcome_emoji = "🟢" if outcome == "Yes" else "🔴"
        bar = sentiment_bar(cur_price)

        sell_key = str(uuid.uuid4())[:8]
        position_store[sell_key] = {
            "token_id": p.get("asset"), "size": size,
            "title": title, "outcome": outcome, "cur_price": cur_price,
            "owner_id": callback.from_user.id
        }

        expiry_str = ""
        end_date = p.get("endDate", "")
        if end_date:
            try:
                end = datetime.strptime(end_date[:10], "%Y-%m-%d")
                days_left = (end - datetime.now()).days
                if days_left <= 1: expiry_str = "\n⚠️ *Expires tomorrow!*"
                elif days_left <= 3: expiry_str = f"\n⚠️ _Expires in {days_left} days_"
            except: pass

        text = (
            f"*{title}*\n\n"
            f"{bar}\n"
            f"{outcome_emoji} *{outcome}* — Avg: *{avg_price:.2f}¢* → Now: *{cur_price:.2f}¢*\n"
            f"📦 *{size:.2f} shares* = *${current_value:.2f}*  {pnl_emoji} *{pnl_str}*{expiry_str}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💸 Sell", callback_data=f"sell:{sell_key}"),
                InlineKeyboardButton(text="🎯 Auto-Sell", callback_data=f"autosell:{sell_key}"),
            ]
        ])
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=keyboard)

    await callback.message.answer(
        "_Tap Sell or Auto-Sell on any position_",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Activity", callback_data="menu:activity")],
            [InlineKeyboardButton(text="📑 Open Orders", callback_data="menu:open_orders")],
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

# ─── Open Orders ─────────────────────────────────────────────

@router.callback_query(F.data == "menu:open_orders")
async def cb_open_orders(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.answer("📑 *Fetching open orders...*", parse_mode="Markdown")
    private_key = decrypt_key(user["encrypted_key"])
    orders = get_open_orders(private_key)
    if not orders:
        await callback.message.answer(
            "📑 *Open Orders*\n\nNo open limit orders.",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
        await safe_answer(callback)
        return
    await callback.message.answer(f"📑 *Open Limit Orders* — {len(orders)} active", parse_mode="Markdown")
    for o in orders:
        order_id = o.get("id", "")
        side = o.get("side", "")
        price = float(o.get("price", 0))
        size = float(o.get("size", 0) or o.get("original_size", 0))
        size_matched = float(o.get("size_matched", 0))
        remaining = size - size_matched
        side_emoji = "🟢 BUY" if side == "BUY" else "🔴 SELL"
        text = (
            f"{side_emoji} limit @ *{round(price * 100)}¢*\n"
            f"Size: *{remaining:.2f}* shares remaining\n"
            f"Order ID: `{order_id[:12]}...`"
        )
        await callback.message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Cancel Order", callback_data=f"cancelorder:{order_id}")]
            ])
        )
    await callback.message.answer("_Tap Cancel to remove an order_", reply_markup=back_to_menu())
    await safe_answer(callback)

@router.callback_query(F.data.startswith("cancelorder:"))
async def cb_cancel_order(callback: CallbackQuery):
    order_id = callback.data.split(":", 1)[1]
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    private_key = decrypt_key(user["encrypted_key"])
    result = cancel_order(private_key, order_id)
    if result:
        await callback.message.edit_text(
            f"✅ *Order Cancelled*\n\n`{order_id[:12]}...`",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
    else:
        await safe_answer(callback, "❌ Failed to cancel order.", show_alert=True)
    await safe_answer(callback)

# ─── Activity ────────────────────────────────────────────────

@router.callback_query(F.data == "menu:activity")
async def cb_activity(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.answer("📋 *Fetching activity...*", parse_mode="Markdown")
    trades = get_recent_trades(user["wallet_address"], limit=15)
    if not trades:
        await callback.message.answer(
            "📋 *Activity*\n\nNo recent trades found.",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
        await safe_answer(callback)
        return
    text = "📋 *Recent Activity*\n\n"
    for t in trades:
        trade_type = t.get("type", "")
        title = t.get("title", "Unknown")[:35]
        outcome = t.get("outcome", "")
        size = float(t.get("shares", 0) or 0)
        usdc = float(t.get("usdcSize", 0) or 0)
        price = float(t.get("price", 0) or 0)
        timestamp = t.get("timestamp", "")
        date_str = ""
        if timestamp:
            try:
                dt = datetime.fromtimestamp(int(timestamp))
                date_str = dt.strftime("%b %d %H:%M")
            except: pass
        type_emoji = "🟢" if trade_type == "BUY" else "🔴"
        outcome_emoji = "✅" if outcome == "Yes" else "❌"
        text += f"{type_emoji} *{trade_type}* {outcome_emoji} *{outcome}*\n"
        text += f"_{title}_\n"
        text += f"{size:.2f} shares @ {round(price * 100)}¢ = *${usdc:.2f}*"
        if date_str:
            text += f"\n_{date_str}_"
        text += "\n\n"
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=back_to_menu())
    await safe_answer(callback)

# ─── Leaderboard ─────────────────────────────────────────────

@router.callback_query(F.data == "menu:leaderboard")
async def cb_leaderboard(callback: CallbackQuery):
    result = supabase.table("users").select("*").eq("leaderboard_opt_in", True).execute()
    users = result.data or []
    if not users:
        await callback.message.edit_text(
            "🏆 *Leaderboard*\n\nNo traders yet!\n\n_Enable in ⚙️ Settings to appear here._",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
        await safe_answer(callback)
        return
    users.sort(key=lambda u: (u.get("win_streak", 0) or 0, u.get("total_wins", 0) or 0), reverse=True)
    text = "🏆 *PolyRift Leaderboard*\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, u in enumerate(users[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = u.get("display_name") or u.get("username") or "Anonymous"
        streak = u.get("win_streak", 0) or 0
        wins = u.get("total_wins", 0) or 0
        trades = u.get("total_trades", 0) or 0
        streak_str = f" 🔥{streak}" if streak >= 2 else ""
        text += f"{medal} *{name}*{streak_str} — {wins}/{trades} wins\n"
    text += "\n_Enable leaderboard in ⚙️ Settings to appear here._"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_menu())
    await safe_answer(callback)

# ─── Categories ───────────────────────────────────────────────

@router.callback_query(F.data == "menu:categories")
async def cb_categories(callback: CallbackQuery):
    await callback.message.edit_text(
        "🗂 *Browse by Category*\n\nWhat are you interested in?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🏛 Politics", callback_data="cat:politics"),
                InlineKeyboardButton(text="₿ Crypto", callback_data="cat:crypto"),
            ],
            [
                InlineKeyboardButton(text="⚽ Sports", callback_data="cat:sports"),
                InlineKeyboardButton(text="🎬 Entertainment", callback_data="cat:entertainment"),
            ],
            [
                InlineKeyboardButton(text="🔬 Science", callback_data="cat:science"),
                InlineKeyboardButton(text="📉 Economics", callback_data="cat:economics"),
            ],
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("cat:"))
async def cb_category_markets(callback: CallbackQuery):
    category = callback.data.split(":")[1]
    emoji_map = {
        "politics": "🏛", "crypto": "₿", "sports": "⚽",
        "entertainment": "🎬", "science": "🔬", "economics": "📉"
    }
    emoji = emoji_map.get(category, "📈")
    await callback.message.edit_text(f"{emoji} *Fetching {category.title()} markets...*", parse_mode="Markdown")
    markets = get_markets(category=category, limit=5)
    if not markets:
        await callback.message.edit_text(
            f"😕 No {category} markets found right now.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Categories", callback_data="menu:categories")]
            ])
        )
        await safe_answer(callback)
        return
    await callback.message.edit_text(
        f"{emoji} *{category.title()} Markets* — _Tap Yes or No to trade_",
        parse_mode="Markdown"
    )
    for m in markets:
        condition_id = m.get("conditionId")
        tokens = get_clob_tokens(condition_id) if condition_id else []
        card, _, _ = format_market_card(m, tokens)
        await callback.message.answer(card, parse_mode="Markdown", reply_markup=get_trade_keyboard(tokens))
    await callback.message.answer(
        f"_Top 5 {category} markets by 24h volume_",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Categories", callback_data="menu:categories")],
            [InlineKeyboardButton(text="← Menu", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

# ─── Quick Bet ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("quickbet:"))
async def handle_quick_bet(callback: CallbackQuery, state: FSMContext):
    if is_rate_limited(callback.from_user.id):
        await safe_answer(callback, "⏳ Slow down!", show_alert=True)
        return
    parts = callback.data.split(":")
    amount = float(parts[1])
    token_key = parts[2]
    token_id = token_store.get(token_key)
    if not token_id:
        await safe_answer(callback, "⏰ Session expired. Refresh markets.", show_alert=True)
        return
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found. Send /start first.", show_alert=True)
        return
    bal = get_usdc_balance(user["wallet_address"])
    if bal < amount:
        await safe_answer(callback, f"Insufficient balance (${bal:.2f}). Need ${amount:.2f}.", show_alert=True)
        return
    asyncio.create_task(maybe_relay_gas(user))
    # Ask YES or NO for quick bet
    await callback.message.answer(
        f"⚡ *Quick Bet — ${amount:.0f}*\n\n"
        f"💰 Balance: *${bal:.2f} USDC.e*\n\n"
        f"Which side?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 YES", callback_data=f"qbside:y:{amount}:{token_key}"),
                InlineKeyboardButton(text="🔴 NO", callback_data=f"qbside:n:{amount}:{token_key}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("qbside:"))
async def handle_quick_bet_side(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    outcome = parts[1]
    amount = float(parts[2])
    token_key = parts[3]
    token_id = token_store.get(token_key)
    if not token_id:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    fee = round(amount * FEE_PERCENT, 4)
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    await state.update_data(outcome=outcome, token_id=token_id, amount=amount)
    await callback.message.edit_text(
        f"⚡ *Confirm Quick Bet*\n\n"
        f"Direction: *{outcome_label}*\n"
        f"Amount: *${amount:.2f} USDC.e*\n"
        f"Fee (1%): *${fee:.4f}*\n"
        f"Net trade: *${amount - fee:.4f}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data="action:confirm_trade"),
                InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")
            ]
        ])
    )
    await state.set_state(TradeStates.waiting_for_confirm)
    await safe_answer(callback)

# ─── Stats Card ───────────────────────────────────────────────

def generate_stats_card(name, bal, pnl, wins, losses, streak, win_rate):
    """Generate a simple ASCII-style stats card as text (no PIL needed)."""
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    total = wins + losses
    bar_filled = round(win_rate / 10) if total > 0 else 0
    bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
    streak_str = f"🔥 {streak} win streak" if streak >= 2 else "No streak yet"
    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *PolyRift Stats — {name}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Balance: *${bal:.2f} USDC.e*\n"
        f"{pnl_emoji} Total PnL: *{pnl_str}*\n\n"
        f"🎯 Win Rate:\n"
        f"{bar} *{win_rate:.0f}%*\n\n"
        f"✅ Wins: *{wins}*  ❌ Losses: *{losses}*  📋 Total: *{total}*\n"
        f"{streak_str}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Trade on PolyRift → @PolyRiftBot_"
    )

@router.callback_query(F.data == "menu:stats")
async def cb_stats(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.edit_text("📊 *Generating your stats...*", parse_mode="Markdown")
    name = user.get("display_name") or user.get("username") or "Trader"
    bal = get_usdc_balance(user["wallet_address"])
    pnl = get_daily_pnl(user["wallet_address"])
    wins = user.get("total_wins", 0) or 0
    losses = user.get("total_trades", 0) - wins if user.get("total_trades") else 0
    losses = max(0, losses)
    streak = user.get("win_streak", 0) or 0
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    card = generate_stats_card(name, bal, pnl, wins, losses, streak, win_rate)
    await callback.message.edit_text(
        card,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

# ─── Auto-sell ───────────────────────────────────────────────

@router.callback_query(F.data.startswith("autosell:"))
async def cb_autosell(callback: CallbackQuery, state: FSMContext):
    sell_key = callback.data.split(":")[1]
    position = position_store.get(sell_key)
    if not position:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    await state.update_data(**position)
    await state.set_state(TradeStates.waiting_for_autosell_price)
    cur = position.get("cur_price", 0.5)
    outcome_emoji = "🟢" if position["outcome"] == "Yes" else "🔴"
    await callback.message.answer(
        f"🎯 *Set Auto-Sell Target*\n\n"
        f"_{position['title']}_\n"
        f"{outcome_emoji} *{position['outcome']}* — Current: *{round(cur * 100)}¢*\n\n"
        f"Enter target price in cents (1–99):\n"
        f"_e.g. `75` = sell when price hits 75¢_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:portfolio")]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_autosell_price)
async def handle_autosell_price(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        target = float(message.text.strip())
        if target < 1 or target > 99: raise ValueError
    except:
        await message.answer("Please enter a price between 1 and 99.")
        return
    await state.clear()
    # Cap at 20 auto-sells per user
    existing_count = supabase.table("auto_sells").select("id").eq("user_id", message.from_user.id).eq("active", True).execute()
    if len(existing_count.data) >= 20:
        await message.answer("❌ Maximum 20 auto-sells allowed. Cancel some first.", reply_markup=back_to_menu())
        return
    supabase.table("auto_sells").insert({
        "user_id": message.from_user.id,
        "token_id": data["token_id"],
        "title": data["title"],
        "outcome": data["outcome"],
        "target_price": target / 100,
        "size": data["size"],
        "active": True
    }).execute()
    await message.answer(
        f"✅ *Auto-Sell Set!*\n\n"
        f"Will sell *{data['size']:.2f} shares* when price hits *{target:.0f}¢*",
        parse_mode="Markdown", reply_markup=back_to_menu()
    )

# ─── Trade flow ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("t:"))
async def handle_trade_click(callback: CallbackQuery, state: FSMContext):
    if is_rate_limited(callback.from_user.id):
        await safe_answer(callback, "⏳ Slow down!", show_alert=True)
        return
    parts = callback.data.split(":")
    outcome = parts[1]
    token_key = parts[2]
    token_id = token_store.get(token_key)
    if not token_id:
        await safe_answer(callback, "⏰ Session expired. Refresh markets.", show_alert=True)
        return
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found. Send /start first.", show_alert=True)
        return
    bal = get_usdc_balance(user["wallet_address"])
    if bal < 0.5:
        await safe_answer(callback, f"Insufficient balance (${bal:.2f}). Deposit USDC.e first.", show_alert=True)
        return
    asyncio.create_task(maybe_relay_gas(user))
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    await state.update_data(outcome=outcome, token_id=token_id, bal=bal, token_key=token_key)
    # Show market vs limit choice
    await callback.message.answer(
        f"*{outcome_label} — Choose Order Type*\n\n"
        f"💰 Balance: *${bal:.2f} USDC.e*\n\n"
        f"🟩 *Market Order* — fills instantly at current price\n"
        f"📋 *Limit Order* — set your own price, waits to fill",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟩 Market Order", callback_data=f"ordertype:market:{outcome}:{token_key}"),
                InlineKeyboardButton(text="📋 Limit Order", callback_data=f"ordertype:limit:{outcome}:{token_key}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

# ─── Order type selection ─────────────────────────────────────

@router.callback_query(F.data.startswith("ordertype:market:"))
async def handle_ordertype_market(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    outcome = parts[2]
    token_key = parts[3]
    token_id = token_store.get(token_key)
    if not token_id:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    data = await state.get_data()
    bal = data.get("bal", 0)
    await state.update_data(outcome=outcome, token_id=token_id)
    await state.set_state(TradeStates.waiting_for_amount)
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    await callback.message.edit_text(
        f"*Market Order — {outcome_label}*\n\n"
        f"💰 Available: *${bal:.2f} USDC.e*\n\n"
        f"Select amount or type custom:\n"
        f"_Min: $1  •  1% fee applies_",
        parse_mode="Markdown",
        reply_markup=dynamic_amount_buttons(bal, outcome, token_key)
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("ordertype:limit:"))
async def handle_ordertype_limit(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    outcome = parts[2]
    token_key = parts[3]
    token_id = token_store.get(token_key)
    if not token_id:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    await state.update_data(outcome=outcome, token_id=token_id, token_key=token_key)
    await state.set_state(TradeStates.waiting_for_limit_price)
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    await callback.message.edit_text(
        f"📋 *Limit Order — {outcome_label}*\n\n"
        f"Enter the price you want to buy at in cents (1–99):\n\n"
        f"_e.g. `45` = buy when price drops to 45¢_\n"
        f"_Order stays open until filled or cancelled_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="25¢", callback_data=f"lprice:25:{outcome}:{token_key}"),
                InlineKeyboardButton(text="40¢", callback_data=f"lprice:40:{outcome}:{token_key}"),
                InlineKeyboardButton(text="50¢", callback_data=f"lprice:50:{outcome}:{token_key}"),
                InlineKeyboardButton(text="60¢", callback_data=f"lprice:60:{outcome}:{token_key}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("lprice:"))
async def handle_limit_price_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    price_cents = int(parts[1])
    outcome = parts[2]
    token_key = parts[3]
    token_id = token_store.get(token_key)
    if not token_id:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    await state.update_data(limit_price=price_cents / 100, outcome=outcome, token_id=token_id)
    await state.set_state(TradeStates.waiting_for_limit_amount)
    data = await state.get_data()
    bal = data.get("bal", 0)
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    await callback.message.edit_text(
        f"📋 *Limit Order — {outcome_label} @ {price_cents}¢*\n\n"
        f"💰 Available: *${bal:.2f} USDC.e*\n\n"
        f"How much USDC.e to spend?\n"
        f"_Min: $1  •  1% fee applies_",
        parse_mode="Markdown",
        reply_markup=dynamic_amount_buttons(bal, outcome, token_key)
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_limit_price)
async def handle_limit_price_text(message: Message, state: FSMContext):
    try:
        price_cents = float(message.text.strip())
        if price_cents < 1 or price_cents > 99: raise ValueError
    except:
        await message.answer("Enter a price between 1 and 99 cents.")
        return
    await state.update_data(limit_price=price_cents / 100)
    await state.set_state(TradeStates.waiting_for_limit_amount)
    data = await state.get_data()
    bal = data.get("bal", 0)
    outcome = data.get("outcome", "y")
    token_key = data.get("token_key", "")
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    await message.answer(
        f"📋 *Limit Order — {outcome_label} @ {price_cents:.0f}¢*\n\n"
        f"💰 Available: *${bal:.2f} USDC.e*\n\n"
        f"How much USDC.e to spend?\n"
        f"_Min: $1  •  1% fee applies_",
        parse_mode="Markdown",
        reply_markup=dynamic_amount_buttons(bal, outcome, token_key)
    )

@router.message(TradeStates.waiting_for_limit_amount)
async def handle_limit_amount_text(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount < 1: raise ValueError
    except:
        await message.answer("Please enter a valid amount of at least $1")
        return
    data = await state.get_data()
    await state.update_data(amount=amount)
    outcome = data["outcome"]
    limit_price = data["limit_price"]
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    fee = round(amount * FEE_PERCENT, 4)
    shares = round((amount - fee) / limit_price, 2)
    await message.answer(
        f"📋 *Confirm Limit Order*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Direction: *{outcome_label}*\n"
        f"Limit price: *{round(limit_price * 100)}¢*\n"
        f"Amount: *${amount:.2f} USDC.e*\n"
        f"Fee (1%): *${fee:.4f}*\n"
        f"Est. shares: *~{shares}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"_Order stays open until price hits {round(limit_price * 100)}¢_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Place Limit Order", callback_data="action:confirm_limit"),
                InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")
            ]
        ])
    )

@router.callback_query(F.data == "action:confirm_limit")
async def handle_confirm_limit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    token_id = data.get("token_id")
    outcome = data.get("outcome")
    amount = data.get("amount")
    limit_price = data.get("limit_price")
    if not all([token_id, outcome, amount, limit_price]):
        await safe_answer(callback, "Session expired.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    user = get_user(callback.from_user.id)
    private_key = decrypt_key(user["encrypted_key"])
    await callback.message.edit_text("⏳ *Placing limit order...*", parse_mode="Markdown")
    try:
        client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        fee = round(amount * FEE_PERCENT, 4)
        net_amount = amount - fee
        shares = net_amount / limit_price
        order_args = OrderArgs(
            token_id=token_id,
            price=limit_price,
            size=shares,
            side="BUY"
        )
        signed_order = client.create_limit_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        if resp.get("success") or resp.get("orderID"):
            collect_fee(private_key, amount, user_id=user["id"] if isinstance(user, dict) else getattr(user, "id", None))
            outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
            order_id = resp.get("orderID", "")
            await callback.message.edit_text(
                f"✅ *Limit Order Placed!*\n\n"
                f"Direction: *{outcome_label}*\n"
                f"Price: *{round(limit_price * 100)}¢*\n"
                f"Size: *~{round(shares, 2)} shares*\n"
                f"Fee: *${fee:.4f}*\n\n"
                f"_Order ID: `{order_id[:12]}...`_\n"
                f"_Will fill when market reaches {round(limit_price * 100)}¢_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📑 View Open Orders", callback_data="menu:open_orders")],
                    [InlineKeyboardButton(text="📈 More Markets", callback_data="menu:markets")],
                    [InlineKeyboardButton(text="🏠 Menu", callback_data="menu:main")]
                ])
            )
        else:
            await callback.message.edit_text(
                f"⚠️ *Order Failed*\n\n`{resp.get('errorMsg', 'Unknown error')}`",
                parse_mode="Markdown", reply_markup=back_to_menu()
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ *Error*\n\n`{str(e)}`", parse_mode="Markdown", reply_markup=back_to_menu()
        )
    await safe_answer(callback)

# ─── Market order confirm ─────────────────────────────────────

@router.callback_query(F.data.startswith("amt:"))
async def handle_quick_amount(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    amount = float(parts[1])
    outcome = parts[2]
    token_key = parts[3]
    token_id = token_store.get(token_key)
    if not token_id:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    data = await state.get_data()
    current_state = await state.get_state()

    # If we're in limit amount state, route to limit confirm
    if current_state == TradeStates.waiting_for_limit_amount:
        await state.update_data(amount=amount)
        limit_price = data.get("limit_price", 0.5)
        outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
        fee = round(amount * FEE_PERCENT, 4)
        shares = round((amount - fee) / limit_price, 2)
        await callback.message.edit_text(
            f"📋 *Confirm Limit Order*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Direction: *{outcome_label}*\n"
            f"Limit price: *{round(limit_price * 100)}¢*\n"
            f"Amount: *${amount:.2f} USDC.e*\n"
            f"Fee (1%): *${fee:.4f}*\n"
            f"Est. shares: *~{shares}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"_Order stays open until price hits {round(limit_price * 100)}¢_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Place Limit Order", callback_data="action:confirm_limit"),
                    InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")
                ]
            ])
        )
        await safe_answer(callback)
        return

    # Otherwise normal market order
    await state.update_data(outcome=outcome, token_id=token_id, amount=amount)
    await state.set_state(TradeStates.waiting_for_confirm)
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    fee = round(amount * FEE_PERCENT, 4)
    await callback.message.edit_text(
        f"*Confirm Trade*\n\n"
        f"Direction: *{outcome_label}*\n"
        f"Amount: *${amount:.2f} USDC.e*\n"
        f"Fee (1%): *${fee:.4f}*\n"
        f"Net trade: *${amount - fee:.4f}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data="action:confirm_trade"),
                InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")
            ]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_amount)
async def handle_amount_text(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount < 1: raise ValueError
    except:
        await message.answer("Please enter a valid amount of at least $1")
        return
    data = await state.get_data()
    outcome = data["outcome"]
    await state.update_data(amount=amount)
    await state.set_state(TradeStates.waiting_for_confirm)
    outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
    fee = round(amount * FEE_PERCENT, 4)
    await message.answer(
        f"*Confirm Trade*\n\n"
        f"Direction: *{outcome_label}*\n"
        f"Amount: *${amount:.2f} USDC.e*\n"
        f"Fee (1%): *${fee:.4f}*\n"
        f"Net trade: *${amount - fee:.4f}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data="action:confirm_trade"),
                InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")
            ]
        ])
    )

@router.callback_query(F.data == "action:confirm_trade")
async def handle_confirm_trade(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    token_id = data.get("token_id")
    outcome = data.get("outcome")
    amount = data.get("amount")
    if not all([token_id, outcome, amount]):
        await safe_answer(callback, "Session expired.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    user = get_user(callback.from_user.id)
    private_key = decrypt_key(user["encrypted_key"])
    await callback.message.edit_text("⏳ *Placing order...*", parse_mode="Markdown")
    try:
        client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        order_args = MarketOrderArgs(token_id=token_id, amount=amount, side="BUY")
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        if resp.get("success"):
            fee = collect_fee(private_key, amount, user_id=user["id"] if isinstance(user, dict) else getattr(user, "id", None))
            outcome_label = "🟢 YES" if outcome == "y" else "🔴 NO"
            streak = user.get("win_streak", 0) or 0
            streak_str = f"\n🔥 *Win streak: {streak + 1}!*" if streak >= 1 else ""
            yk = str(uuid.uuid4())[:8]
            token_store[yk] = token_id
            await callback.message.edit_text(
                f"✅ *Trade Successful!*\n\n"
                f"Direction: *{outcome_label}*\n"
                f"Amount: *${amount:.2f} USDC.e*\n"
                f"Fee: *${fee:.4f}*{streak_str}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔥 Double Down", callback_data=f"t:{outcome}:{yk}")],
                    [InlineKeyboardButton(text="💼 View Portfolio", callback_data="menu:portfolio")],
                    [InlineKeyboardButton(text="📈 More Markets", callback_data="menu:markets")]
                ])
            )
        else:
            await callback.message.edit_text(
                f"⚠️ *Order Failed*\n\n`{resp.get('errorMsg', 'Unknown error')}`",
                parse_mode="Markdown", reply_markup=back_to_menu()
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ *Error*\n\n`{str(e)}`", parse_mode="Markdown", reply_markup=back_to_menu()
        )
    await safe_answer(callback)

# ─── Sell flow ───────────────────────────────────────────────

@router.callback_query(F.data.startswith("sell:"))
async def handle_sell_click(callback: CallbackQuery, state: FSMContext):
    sell_key = callback.data.split(":")[1]
    position = position_store.get(sell_key)
    if not position:
        await safe_answer(callback, "Session expired. Open Portfolio again.", show_alert=True)
        return
    if position.get("owner_id") != callback.from_user.id:
        await safe_answer(callback, "❌ Unauthorized.", show_alert=True)
        return
    outcome_emoji = "🟢" if position["outcome"] == "Yes" else "🔴"
    cur_price = position.get("cur_price", 0)
    await state.update_data(**position)
    await callback.message.answer(
        f"💸 *Sell Position — Choose Order Type*\n\n"
        f"_{position['title']}_\n"
        f"{outcome_emoji} *{position['outcome']}* — *{position['size']:.2f} shares*\n"
        f"Current price: *{round(cur_price * 100)}¢*\n\n"
        f"🟩 *Market Sell* — sells instantly at current price\n"
        f"📋 *Limit Sell* — set your target price, waits to fill",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟩 Market Sell", callback_data=f"selltype:market:{sell_key}"),
                InlineKeyboardButton(text="📋 Limit Sell", callback_data=f"selltype:limit:{sell_key}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:portfolio")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("selltype:market:"))
async def handle_selltype_market(callback: CallbackQuery, state: FSMContext):
    sell_key = callback.data.split(":", 2)[2]
    position = position_store.get(sell_key)
    if not position:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    if position.get("owner_id") != callback.from_user.id:
        await safe_answer(callback, "❌ Unauthorized.", show_alert=True)
        return
    outcome_emoji = "🟢" if position["outcome"] == "Yes" else "🔴"
    await state.update_data(**position)
    await state.set_state(TradeStates.waiting_for_sell_amount)
    await callback.message.edit_text(
        f"💸 *Market Sell*\n\n"
        f"_{position['title']}_\n"
        f"{outcome_emoji} *{position['outcome']}* — *{position['size']:.2f} shares*\n\n"
        f"How many shares to sell?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Sell All", callback_data=f"sellall:{sell_key}")],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:portfolio")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("selltype:limit:"))
async def handle_selltype_limit(callback: CallbackQuery, state: FSMContext):
    sell_key = callback.data.split(":", 2)[2]
    position = position_store.get(sell_key)
    if not position:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    if position.get("owner_id") != callback.from_user.id:
        await safe_answer(callback, "❌ Unauthorized.", show_alert=True)
        return
    outcome_emoji = "🟢" if position["outcome"] == "Yes" else "🔴"
    cur_price = position.get("cur_price", 0)
    await state.update_data(**position, sell_key=sell_key)
    await state.set_state(TradeStates.waiting_for_limit_sell_price)
    await callback.message.edit_text(
        f"📋 *Limit Sell*\n\n"
        f"_{position['title']}_\n"
        f"{outcome_emoji} *{position['outcome']}* — *{position['size']:.2f} shares*\n"
        f"Current price: *{round(cur_price * 100)}¢*\n\n"
        f"Enter target sell price in cents (1–99):\n"
        f"_e.g. `80` = sell when price hits 80¢_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="70¢", callback_data=f"lsellprice:70:{sell_key}"),
                InlineKeyboardButton(text="75¢", callback_data=f"lsellprice:75:{sell_key}"),
                InlineKeyboardButton(text="80¢", callback_data=f"lsellprice:80:{sell_key}"),
                InlineKeyboardButton(text="90¢", callback_data=f"lsellprice:90:{sell_key}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:portfolio")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("lsellprice:"))
async def handle_limit_sell_price_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    price_cents = int(parts[1])
    sell_key = parts[2]
    position = position_store.get(sell_key)
    if not position:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    if position.get("owner_id") != callback.from_user.id:
        await safe_answer(callback, "❌ Unauthorized.", show_alert=True)
        return
    await state.update_data(limit_sell_price=price_cents / 100)
    await state.set_state(TradeStates.waiting_for_limit_sell_amount)
    outcome_emoji = "🟢" if position["outcome"] == "Yes" else "🔴"
    await callback.message.edit_text(
        f"📋 *Limit Sell @ {price_cents}¢*\n\n"
        f"_{position['title']}_\n"
        f"{outcome_emoji} *{position['outcome']}* — *{position['size']:.2f} shares* available\n\n"
        f"How many shares to sell?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Sell All", callback_data=f"lsellall:{price_cents}:{sell_key}")],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:portfolio")]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_limit_sell_price)
async def handle_limit_sell_price_text(message: Message, state: FSMContext):
    try:
        price_cents = float(message.text.strip())
        if price_cents < 1 or price_cents > 99: raise ValueError
    except:
        await message.answer("Enter a price between 1 and 99 cents.")
        return
    await state.update_data(limit_sell_price=price_cents / 100)
    await state.set_state(TradeStates.waiting_for_limit_sell_amount)
    data = await state.get_data()
    await message.answer(
        f"📋 *Limit Sell @ {price_cents:.0f}¢*\n\n"
        f"How many shares to sell? (max {data.get('size', 0):.2f})",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:portfolio")]
        ])
    )

@router.callback_query(F.data.startswith("lsellall:"))
async def handle_limit_sell_all(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    price_cents = int(parts[1])
    sell_key = parts[2]
    position = position_store.get(sell_key)
    if not position:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    if position.get("owner_id") != callback.from_user.id:
        await safe_answer(callback, "❌ Unauthorized.", show_alert=True)
        return
    await state.clear()
    await _execute_limit_sell(callback.message, position["token_id"], position["size"], price_cents / 100, callback.from_user.id)
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_limit_sell_amount)
async def handle_limit_sell_amount_text(message: Message, state: FSMContext):
    data = await state.get_data()
    max_size = data.get("size", 0)
    limit_sell_price = data.get("limit_sell_price", 0)
    try:
        size = float(message.text.strip())
        if size <= 0 or size > max_size: raise ValueError
    except:
        await message.answer(f"Enter a valid amount between 0 and {max_size:.2f}")
        return
    await state.clear()
    await _execute_limit_sell(message, data["token_id"], size, limit_sell_price, message.from_user.id)

async def _execute_limit_sell(message, token_id, size, price, user_id):
    user = get_user(user_id)
    private_key = decrypt_key(user["encrypted_key"])
    await message.answer(f"⏳ *Placing limit sell @ {round(price * 100)}¢...*", parse_mode="Markdown")
    try:
        client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="SELL"
        )
        signed_order = client.create_limit_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        if resp.get("success") or resp.get("orderID"):
            order_id = resp.get("orderID", "")
            await message.answer(
                f"✅ *Limit Sell Placed!*\n\n"
                f"Size: *{size:.2f} shares*\n"
                f"Target: *{round(price * 100)}¢*\n\n"
                f"_Order ID: `{order_id[:12]}...`_\n"
                f"_Will fill when price hits {round(price * 100)}¢_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📑 Open Orders", callback_data="menu:open_orders")],
                    [InlineKeyboardButton(text="💼 Portfolio", callback_data="menu:portfolio")],
                    [InlineKeyboardButton(text="🏠 Menu", callback_data="menu:main")]
                ])
            )
        else:
            await message.answer(
                f"⚠️ *Limit Sell Failed*\n\n`{resp.get('errorMsg', 'Unknown error')}`",
                parse_mode="Markdown", reply_markup=back_to_menu()
            )
    except Exception as e:
        await message.answer(f"❌ *Error*\n\n`{str(e)}`", parse_mode="Markdown", reply_markup=back_to_menu())

@router.callback_query(F.data.startswith("sellall:"))
async def handle_sell_all(callback: CallbackQuery, state: FSMContext):
    sell_key = callback.data.split(":")[1]
    position = position_store.get(sell_key)
    if not position:
        await safe_answer(callback, "Session expired.", show_alert=True)
        return
    if position.get("owner_id") != callback.from_user.id:
        await safe_answer(callback, "❌ Unauthorized.", show_alert=True)
        return
    await state.clear()
    await _execute_sell(callback.message, position["token_id"], position["size"], callback.from_user.id)
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_sell_amount)
async def handle_sell_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    max_size = data.get("size", 0)
    try:
        size = float(message.text.strip())
        if size <= 0 or size > max_size: raise ValueError
    except:
        await message.answer(f"Enter a valid amount between 0 and {max_size:.2f}")
        return
    await state.clear()
    await _execute_sell(message, data["token_id"], size, message.from_user.id)

async def _execute_sell(message, token_id, size, user_id):
    user = get_user(user_id)
    private_key = decrypt_key(user["encrypted_key"])
    await message.answer(f"⏳ *Selling {size:.2f} shares...*", parse_mode="Markdown")
    try:
        client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        order_args = MarketOrderArgs(token_id=token_id, amount=size, side="SELL")
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        if resp.get("success"):
            await message.answer(
                f"✅ *Sold {size:.2f} shares!*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💼 Portfolio", callback_data="menu:portfolio")],
                    [InlineKeyboardButton(text="🏠 Menu", callback_data="menu:main")]
                ])
            )
        else:
            await message.answer(
                f"⚠️ *Sell Failed*\n\n`{resp.get('errorMsg', 'Unknown error')}`",
                parse_mode="Markdown", reply_markup=back_to_menu()
            )
    except Exception as e:
        await message.answer(f"❌ *Error*\n\n`{str(e)}`", parse_mode="Markdown", reply_markup=back_to_menu())

# ─── Copy Trade Menu ─────────────────────────────────────────

@router.callback_query(F.data == "menu:copy")
async def cb_copy_menu(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    active = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).eq("active", True).execute()
    text = "🤖 *Copy Trading*\n\n"
    buttons = []
    if active.data:
        text += f"*Following {len(active.data)} trader(s):*\n\n"
        for c in active.data:
            w = c["target_wallet"]
            mode = c.get("copy_mode") or "percent"
            paused = c.get("paused", False)
            status = "⏸" if paused else "▶️"
            if mode == "fixed":
                mode_str = f"Fixed ${c.get('fixed_amount') or 10}"
            else:
                pct = int((c.get("copy_percent") or 0.10) * 100)
                max_t = c.get("max_per_trade") or 50
                mode_str = f"{pct}% max ${max_t}"
            text += f"{status} `{w[:8]}...{w[-6:]}` — {mode_str}\n"
            buttons.append([InlineKeyboardButton(text=f"⚙️ Manage {w[:8]}...", callback_data=f"copy:manage:{c['id']}")])
    else:
        text += "_Not copying anyone yet._\n\nMirror trades from top Polymarket wallets automatically.\n"
    buttons.append([
        InlineKeyboardButton(text="➕ Follow a Trader", callback_data="action:copy_prompt"),
        InlineKeyboardButton(text="🏆 Top Traders", callback_data="copy:top_traders"),
    ])
    buttons.append([
        InlineKeyboardButton(text="📡 Live Feed", callback_data="copy:feed"),
        InlineKeyboardButton(text="📜 History", callback_data="copy:history"),
    ])
    buttons.append([
        InlineKeyboardButton(text="💰 Set Budget", callback_data="copy:budget"),
        InlineKeyboardButton(text="👁 Watch Wallet", callback_data="copy:watch_prompt"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🏅 My Rankings", callback_data="copy:my_leaderboard"),
        InlineKeyboardButton(text="📤 Share Stats", callback_data="copy:share"),
    ])
    buttons.append([InlineKeyboardButton(text="🛩 Smart Pilot", callback_data="smart_pilot:menu")])
    buttons.append([InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:manage:"))
async def cb_copy_manage(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    user = get_user(callback.from_user.id)
    result = supabase.table("copy_trades").select("*").eq("id", copy_id).execute()
    if not result.data:
        await safe_answer(callback, "Not found.", show_alert=True)
        return
    # Ownership check — prevent managing another user's copy trade
    if user and result.data[0].get("user_id") != user["id"]:
        await safe_answer(callback, "Not authorized.", show_alert=True)
        return
    c = result.data[0]
    w = c["target_wallet"]
    mode = c.get("copy_mode") or "percent"
    paused = c.get("paused", False)
    status = "Paused" if paused else "Active"
    pause_btn = "▶️ Resume" if paused else "⏸ Pause"
    pause_action = f"copy:resume:{copy_id}" if paused else f"copy:pause:{copy_id}"
    fade_mode = mode == "fade"
    fade_btn = "🔄 Switch to Copy" if fade_mode else "↩️ Switch to Fade"
    sell_mode = c.get("sell_mode") or "mirror"
    sell_str = {"mirror": "Mirror %", "fixed": "Fixed $", "full": "Full position"}.get(sell_mode, "Mirror %")
    max_odds = c.get("max_odds") or 0
    cat_filter = c.get("category_filter") or "All"
    if mode == "fixed":
        mode_str = f"Fixed *${c.get('fixed_amount') or 10}* per trade"
    elif fade_mode:
        mode_str = "*Fade mode* (betting opposite)"
    else:
        pct = int((c.get("copy_percent") or 0.10) * 100)
        max_t = c.get("max_per_trade") or 50
        mode_str = f"*{pct}% of balance* (max ${max_t})"
    await callback.message.edit_text(
        f"*Manage Copy Trade*\n\n"
        f"Wallet: `{w}`\n"
        f"Status: *{status}*\n"
        f"Mode: {mode_str}\n"
        f"Sell mode: *{sell_str}*\n"
        f"Category: *{cat_filter}*"
        + (f"\nMax odds: *{int(max_odds*100)}c*" if max_odds else ""),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Trader Profile", callback_data=f"copy:profile:{c['id']}")],
            [InlineKeyboardButton(text=pause_btn, callback_data=pause_action)],
            [
                InlineKeyboardButton(text="📊 % of Balance", callback_data=f"copy:setpct:{copy_id}"),
                InlineKeyboardButton(text="💵 Fixed Amount", callback_data=f"copy:setfixed:{copy_id}"),
            ],
            [InlineKeyboardButton(text="📈 Set Max $", callback_data=f"copy:setmax:{copy_id}")],
            [InlineKeyboardButton(text="🎯 Min Win Rate Filter", callback_data=f"copy:setwinrate:{copy_id}")],
            [InlineKeyboardButton(text="📏 Min Trade Size", callback_data=f"copy:setminsize:{copy_id}")],
            [InlineKeyboardButton(text="💰 Manage Copy Sells", callback_data=f"copy:sellmode:{copy_id}")],
            [InlineKeyboardButton(text="🏷 Category Filter", callback_data=f"copy:setcat:{copy_id}")],
            [InlineKeyboardButton(text="🎲 Max Odds Filter", callback_data=f"copy:setmaxodds:{copy_id}")],
            [InlineKeyboardButton(text=fade_btn, callback_data=f"copy:togglefade:{copy_id}")],
            [InlineKeyboardButton(text="🛑 Set Stop Loss", callback_data=f"copy:stoploss:{copy_id}")],
            [InlineKeyboardButton(text="🗑 Stop & Remove", callback_data=f"copy:stop:{copy_id}")],
            [InlineKeyboardButton(text="← Back", callback_data="menu:copy")]
        ])
    )
    await safe_answer(callback)


# ─── Copy Sells Handler ───────────────────────────────────────

@router.callback_query(F.data.startswith("copy:sellmode:"))
async def cb_copy_sellmode(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    await safe_answer(callback)
    await callback.message.edit_text(
        "💰 *Manage Copy Sells*\n\n"
        "Choose how to handle SELL orders from the trader you are copying:\n\n"
        "*Mirror %* — When they sell 20% of their position, you sell 20% of yours. Recommended.\n"
        "*Fixed $* — Always sell a fixed dollar amount regardless of their sell size.\n"
        "*Full position* — Sell your entire position whenever they sell anything.\n"
        "*Ignore* — Never copy sells, only copy buys.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Mirror % (recommended)", callback_data=f"copy:setsell:{copy_id}:mirror")],
            [InlineKeyboardButton(text="Fixed dollar amount", callback_data=f"copy:setsell:{copy_id}:fixed")],
            [InlineKeyboardButton(text="Full position", callback_data=f"copy:setsell:{copy_id}:full")],
            [InlineKeyboardButton(text="Ignore sells", callback_data=f"copy:setsell:{copy_id}:ignore")],
            [InlineKeyboardButton(text="← Back", callback_data=f"copy:manage:{copy_id}")]
        ])
    )

@router.callback_query(F.data.startswith("copy:setsell:"))
async def cb_copy_setsell(callback: CallbackQuery):
    parts = callback.data.split(":")
    copy_id = int(parts[2])
    sell_mode = parts[3]
    user = get_user(callback.from_user.id)
    result = supabase.table("copy_trades").select("user_id").eq("id", copy_id).execute()
    if not result.data or result.data[0]["user_id"] != user["id"]:
        await safe_answer(callback, "Not authorized.", show_alert=True)
        return
    supabase.table("copy_trades").update({"sell_mode": sell_mode}).eq("id", copy_id).execute()
    labels = {"mirror": "Mirror %", "fixed": "Fixed $", "full": "Full position", "ignore": "Ignore sells"}
    await safe_answer(callback, f"Sell mode set to: {labels.get(sell_mode, sell_mode)}", show_alert=True)
    await cb_copy_manage(callback)

# ─── Category Filter Handler ──────────────────────────────────

@router.callback_query(F.data.startswith("copy:setcat:"))
async def cb_copy_setcat(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    await safe_answer(callback)
    await callback.message.edit_text(
        "🏷 *Category Filter*\n\nOnly copy trades in specific market categories.\nSelect a category or All to copy everything:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="All categories", callback_data=f"copy:cat:{copy_id}:All")],
            [
                InlineKeyboardButton(text="Politics", callback_data=f"copy:cat:{copy_id}:politics"),
                InlineKeyboardButton(text="Crypto", callback_data=f"copy:cat:{copy_id}:crypto"),
            ],
            [
                InlineKeyboardButton(text="Sports", callback_data=f"copy:cat:{copy_id}:sports"),
                InlineKeyboardButton(text="Science", callback_data=f"copy:cat:{copy_id}:science"),
            ],
            [
                InlineKeyboardButton(text="Business", callback_data=f"copy:cat:{copy_id}:business"),
                InlineKeyboardButton(text="Entertainment", callback_data=f"copy:cat:{copy_id}:entertainment"),
            ],
            [InlineKeyboardButton(text="← Back", callback_data=f"copy:manage:{copy_id}")]
        ])
    )

@router.callback_query(F.data.startswith("copy:cat:"))
async def cb_copy_cat(callback: CallbackQuery):
    parts = callback.data.split(":")
    copy_id = int(parts[2])
    cat = parts[3]
    user = get_user(callback.from_user.id)
    result = supabase.table("copy_trades").select("user_id").eq("id", copy_id).execute()
    if not result.data or result.data[0]["user_id"] != user["id"]:
        await safe_answer(callback, "Not authorized.", show_alert=True)
        return
    supabase.table("copy_trades").update({"category_filter": cat}).eq("id", copy_id).execute()
    await safe_answer(callback, f"Category filter set to: {cat}", show_alert=True)
    await cb_copy_manage(callback)

# ─── Max Odds Filter Handler ──────────────────────────────────

@router.callback_query(F.data.startswith("copy:setmaxodds:"))
async def cb_copy_setmaxodds(callback: CallbackQuery, state: FSMContext):
    copy_id = int(callback.data.split(":")[2])
    await state.set_state(TradeStates.waiting_for_copy_max_odds)
    await state.update_data(copy_id=copy_id)
    await safe_answer(callback)
    await callback.message.edit_text(
        "🎲 *Max Odds Filter*\n\n"
        "Skip copying trades where YES is already above this price.\n\n"
        "Example: set to 80 to avoid copying trades where YES costs more than 80c\n\n"
        "Tap a preset or type a number (1-99):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="60c", callback_data=f"copy:maxodds_q:{copy_id}:60"),
                InlineKeyboardButton(text="70c", callback_data=f"copy:maxodds_q:{copy_id}:70"),
                InlineKeyboardButton(text="80c", callback_data=f"copy:maxodds_q:{copy_id}:80"),
                InlineKeyboardButton(text="90c", callback_data=f"copy:maxodds_q:{copy_id}:90"),
            ],
            [InlineKeyboardButton(text="No limit", callback_data=f"copy:maxodds_q:{copy_id}:0")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"copy:manage:{copy_id}")]
        ])
    )

@router.callback_query(F.data.startswith("copy:maxodds_q:"))
async def cb_copy_maxodds_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    copy_id = int(parts[2])
    val = int(parts[3])
    await state.clear()
    supabase.table("copy_trades").update({"max_odds": val / 100 if val > 0 else 0}).eq("id", copy_id).execute()
    msg = f"Max odds set to {val}c" if val > 0 else "Max odds filter removed"
    await safe_answer(callback, msg, show_alert=True)
    await cb_copy_manage(callback)

@router.message(TradeStates.waiting_for_copy_max_odds)
async def handle_copy_max_odds(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        val = int(message.text.strip())
        if val < 0 or val > 99: raise ValueError
    except:
        await message.answer("Enter a number between 1 and 99 (e.g. 80 for 80c).")
        return
    await state.clear()
    supabase.table("copy_trades").update({"max_odds": val / 100}).eq("id", data["copy_id"]).execute()
    await message.answer(f"Max odds set to *{val}c*", parse_mode="Markdown", reply_markup=back_to_copy())

# ─── Fade Mode Toggle ─────────────────────────────────────────

@router.callback_query(F.data.startswith("copy:togglefade:"))
async def cb_copy_togglefade(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    user = get_user(callback.from_user.id)
    result = supabase.table("copy_trades").select("*").eq("id", copy_id).execute()
    if not result.data or result.data[0]["user_id"] != user["id"]:
        await safe_answer(callback, "Not authorized.", show_alert=True)
        return
    current_mode = result.data[0].get("copy_mode") or "percent"
    if current_mode == "fade":
        # Switch back to percent
        supabase.table("copy_trades").update({"copy_mode": "percent"}).eq("id", copy_id).execute()
        await safe_answer(callback, "Switched to Copy mode - mirroring trades.", show_alert=True)
    else:
        # Switch to fade
        supabase.table("copy_trades").update({"copy_mode": "fade"}).eq("id", copy_id).execute()
        await safe_answer(callback, "Switched to Fade mode - betting opposite.", show_alert=True)
    await cb_copy_manage(callback)

@router.callback_query(F.data.startswith("copy:pause:"))
async def cb_copy_pause(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    supabase.table("copy_trades").update({"paused": True}).eq("id", copy_id).eq("user_id", user_id).execute()
    await safe_answer(callback, "⏸ Paused.", show_alert=True)
    await cb_copy_manage(callback)

@router.callback_query(F.data.startswith("copy:resume:"))
async def cb_copy_resume(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    supabase.table("copy_trades").update({"paused": False}).eq("id", copy_id).eq("user_id", user_id).execute()
    await safe_answer(callback, "▶️ Resumed.", show_alert=True)
    await cb_copy_manage(callback)

@router.callback_query(F.data.startswith("copy:stop:"))
async def cb_copy_stop(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    supabase.table("copy_trades").update({"active": False}).eq("id", copy_id).eq("user_id", user_id).execute()
    await callback.message.edit_text("🗑 *Copy trade removed.*", parse_mode="Markdown", reply_markup=back_to_copy())
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:setpct:"))
async def cb_copy_setpct(callback: CallbackQuery, state: FSMContext):
    copy_id = int(callback.data.split(":")[2])
    await state.update_data(copy_id=copy_id)
    await state.set_state(TradeStates.waiting_for_copy_percent)
    await callback.message.edit_text(
        "📊 *% of Balance Mode*\n\nCopy a percentage of your balance per trade.\n\n_e.g. 10% of $100 = $10 per copy trade_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="5%", callback_data=f"copy:pct:5:{copy_id}"),
                InlineKeyboardButton(text="10%", callback_data=f"copy:pct:10:{copy_id}"),
                InlineKeyboardButton(text="25%", callback_data=f"copy:pct:25:{copy_id}"),
                InlineKeyboardButton(text="50%", callback_data=f"copy:pct:50:{copy_id}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data=f"copy:manage:{copy_id}")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:pct:"))
async def cb_copy_pct_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    pct = int(parts[2])
    copy_id = int(parts[3])
    await state.clear()
    supabase.table("copy_trades").update({"copy_mode": "percent", "copy_percent": pct / 100}).eq("id", copy_id).execute()
    await safe_answer(callback, f"✅ Set to {pct}% of balance", show_alert=True)
    await cb_copy_manage(callback)

@router.message(TradeStates.waiting_for_copy_percent)
async def handle_copy_percent(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        pct = float(message.text.strip())
        if pct < 1 or pct > 100: raise ValueError
    except:
        await message.answer("Enter a number between 1 and 100.")
        return
    await state.clear()
    supabase.table("copy_trades").update({"copy_mode": "percent", "copy_percent": pct / 100}).eq("id", data["copy_id"]).execute()
    await message.answer(f"✅ Set to *{pct:.0f}%* of balance per trade.", parse_mode="Markdown", reply_markup=back_to_copy())

@router.callback_query(F.data.startswith("copy:setfixed:"))
async def cb_copy_setfixed(callback: CallbackQuery, state: FSMContext):
    copy_id = int(callback.data.split(":")[2])
    await state.update_data(copy_id=copy_id)
    await state.set_state(TradeStates.waiting_for_copy_fixed)
    await callback.message.edit_text(
        "💵 *Fixed Amount Mode*\n\nUse the same dollar amount on every copied trade.\n\n_e.g. $10 = always copy with exactly $10_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="$5", callback_data=f"copy:fixed:5:{copy_id}"),
                InlineKeyboardButton(text="$10", callback_data=f"copy:fixed:10:{copy_id}"),
                InlineKeyboardButton(text="$25", callback_data=f"copy:fixed:25:{copy_id}"),
                InlineKeyboardButton(text="$50", callback_data=f"copy:fixed:50:{copy_id}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data=f"copy:manage:{copy_id}")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:fixed:"))
async def cb_copy_fixed_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    amount = int(parts[2])
    copy_id = int(parts[3])
    await state.clear()
    supabase.table("copy_trades").update({"copy_mode": "fixed", "fixed_amount": amount}).eq("id", copy_id).execute()
    await safe_answer(callback, f"✅ Fixed at ${amount} per trade", show_alert=True)
    await cb_copy_manage(callback)

@router.message(TradeStates.waiting_for_copy_fixed)
async def handle_copy_fixed(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        amount = float(message.text.strip())
        if amount < 1: raise ValueError
    except:
        await message.answer("Enter a valid dollar amount (e.g. `10`)", parse_mode="Markdown")
        return
    await state.clear()
    supabase.table("copy_trades").update({"copy_mode": "fixed", "fixed_amount": amount}).eq("id", data["copy_id"]).execute()
    await message.answer(f"✅ Fixed at *${amount:.2f}* per trade.", parse_mode="Markdown", reply_markup=back_to_copy())

@router.callback_query(F.data.startswith("copy:setmax:"))
async def cb_copy_setmax(callback: CallbackQuery, state: FSMContext):
    copy_id = int(callback.data.split(":")[2])
    await state.update_data(copy_id=copy_id)
    await state.set_state(TradeStates.waiting_for_copy_max)
    await callback.message.edit_text(
        "📈 *Set Max Per Trade*\n\nCap the maximum USD per copy trade (applies to % mode).",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="$10", callback_data=f"copy:max:10:{copy_id}"),
                InlineKeyboardButton(text="$25", callback_data=f"copy:max:25:{copy_id}"),
                InlineKeyboardButton(text="$50", callback_data=f"copy:max:50:{copy_id}"),
                InlineKeyboardButton(text="$100", callback_data=f"copy:max:100:{copy_id}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data=f"copy:manage:{copy_id}")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:max:"))
async def cb_copy_max_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    max_t = int(parts[2])
    copy_id = int(parts[3])
    await state.clear()
    supabase.table("copy_trades").update({"max_per_trade": max_t}).eq("id", copy_id).execute()
    await safe_answer(callback, f"✅ Max set to ${max_t}", show_alert=True)
    await cb_copy_manage(callback)

@router.message(TradeStates.waiting_for_copy_max)
async def handle_copy_max(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        max_t = float(message.text.strip())
        if max_t < 1: raise ValueError
    except:
        await message.answer("Enter a valid dollar amount.")
        return
    await state.clear()
    supabase.table("copy_trades").update({"max_per_trade": max_t}).eq("id", data["copy_id"]).execute()
    await message.answer(f"✅ Max set to *${max_t:.0f}*", parse_mode="Markdown", reply_markup=back_to_copy())

@router.callback_query(F.data.startswith("copy:setwinrate:"))
async def cb_copy_setwinrate(callback: CallbackQuery, state: FSMContext):
    copy_id = int(callback.data.split(":")[2])
    await state.update_data(copy_id=copy_id)
    await state.set_state(TradeStates.waiting_for_copy_min_win_rate)
    await callback.message.edit_text(
        "🎯 *Minimum Win Rate Filter*\n\n"
        "Only copy trades where the trader's win rate is above this threshold.\n\n"
        "_e.g. set 55% = only copy if their win rate is above 55%_\n"
        "_Set to 0% to copy all trades regardless_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="0% (All)", callback_data=f"copy:winrate:0:{copy_id}"),
                InlineKeyboardButton(text="50%", callback_data=f"copy:winrate:50:{copy_id}"),
                InlineKeyboardButton(text="55%", callback_data=f"copy:winrate:55:{copy_id}"),
                InlineKeyboardButton(text="60%", callback_data=f"copy:winrate:60:{copy_id}"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data=f"copy:manage:{copy_id}")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:winrate:"))
async def cb_copy_winrate_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    min_wr = int(parts[2])
    copy_id = int(parts[3])
    await state.clear()
    supabase.table("copy_trades").update({"min_win_rate": min_wr}).eq("id", copy_id).execute()
    label = "all trades" if min_wr == 0 else f"win rate ≥{min_wr}%"
    await safe_answer(callback, f"✅ Now copying {label}", show_alert=True)
    await cb_copy_manage(callback)

@router.message(TradeStates.waiting_for_copy_min_win_rate)
async def handle_copy_min_win_rate(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        min_wr = float(message.text.strip())
        if min_wr < 0 or min_wr > 100: raise ValueError
    except:
        await message.answer("Enter a number between 0 and 100.")
        return
    await state.clear()
    supabase.table("copy_trades").update({"min_win_rate": min_wr}).eq("id", data["copy_id"]).execute()
    await message.answer(f"✅ Min win rate set to *{min_wr:.0f}%*", parse_mode="Markdown", reply_markup=back_to_copy())

@router.callback_query(F.data.startswith("copy:setminsize:"))
async def cb_copy_setminsize(callback: CallbackQuery, state: FSMContext):
    copy_id = int(callback.data.split(":")[2])
    await state.set_state(TradeStates.waiting_for_copy_min_size)
    await state.update_data(copy_id=copy_id)
    await safe_answer(callback)
    await callback.message.edit_text(
        "📏 *Minimum Trade Size Filter*\n\n"
        "Only copy trades where the original trader's position is at least this amount.\n\n"
        "Enter minimum trade size in USD (e.g. `50` to only copy trades ≥$50):\n\n"
        "_Set to 0 to copy all trades regardless of size._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="$10", callback_data=f"copy:minsize_quick:{copy_id}:10"),
                InlineKeyboardButton(text="$25", callback_data=f"copy:minsize_quick:{copy_id}:25"),
                InlineKeyboardButton(text="$50", callback_data=f"copy:minsize_quick:{copy_id}:50"),
                InlineKeyboardButton(text="$100", callback_data=f"copy:minsize_quick:{copy_id}:100"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data=f"copy:manage:{copy_id}")]
        ])
    )

@router.callback_query(F.data.startswith("copy:minsize_quick:"))
async def cb_copy_minsize_quick(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    copy_id = int(parts[2])
    size = float(parts[3])
    await state.clear()
    supabase.table("copy_trades").update({"min_trade_size": size}).eq("id", copy_id).execute()
    await safe_answer(callback, f"✅ Min trade size set to ${size:.0f}", show_alert=True)
    await cb_copy_manage(callback)

@router.message(TradeStates.waiting_for_copy_min_size)
async def handle_copy_min_size(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        size = float(message.text.strip())
        if size < 0: raise ValueError
    except:
        await message.answer("Enter a valid dollar amount (e.g. 50).")
        return
    await state.clear()
    supabase.table("copy_trades").update({"min_trade_size": size}).eq("id", data["copy_id"]).execute()
    await message.answer(f"✅ Min trade size set to *${size:.0f}*", parse_mode="Markdown", reply_markup=back_to_copy())

@router.callback_query(F.data == "action:copy_prompt")
async def cb_copy_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TradeStates.waiting_for_copy_wallet)
    await callback.message.edit_text(
        "🤖 *Follow a Trader*\n\nPaste their Polymarket wallet address:\n\n_Find top traders at polymarket.com/leaderboard_\n\n"
        "⚡ PolyRift will automatically check their win rate before you follow them.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:copy")]
        ])
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_copy_wallet)
async def handle_copy_wallet(message: Message, state: FSMContext):
    await state.clear()
    target = message.text.strip()
    user = get_user(message.from_user.id)
    if not target.startswith("0x") or len(target) != 42:
        await message.answer("❌ Invalid wallet address.", reply_markup=back_to_copy())
        return
    # Cap at 10 copy trades per user
    existing_count = supabase.table("copy_trades").select("id").eq("user_id", user["id"]).eq("active", True).execute()
    if len(existing_count.data) >= 10:
        await message.answer("❌ Maximum 10 copy trades allowed.", reply_markup=back_to_copy())
        return
    existing = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).eq("target_wallet", target.lower()).eq("active", True).execute()
    if existing.data:
        await message.answer("⚠️ Already copying this wallet!", reply_markup=back_to_copy())
        return
    # Smart copy — check win rate
    checking_msg = await message.answer("🔍 *Analysing trader...*", parse_mode="Markdown")
    win_rate = get_wallet_win_rate(target)
    win_rate_str = ""
    warning_str = ""
    if win_rate is not None:
        win_rate_str = f"📊 Win rate: *{win_rate:.0f}%* (last 50 trades)\n"
        if win_rate < 40:
            warning_str = "\n⚠️ *Warning: This trader has a low win rate. Copy at your own risk.*\n"
        elif win_rate >= 60:
            win_rate_str = f"📊 Win rate: *{win_rate:.0f}%* ✅ (last 50 trades)\n"
    else:
        win_rate_str = "📊 Win rate: *Not enough data*\n"
    supabase.table("copy_trades").insert({
        "user_id": user["id"], "target_wallet": target.lower(),
        "active": True, "copy_percent": 0.10, "max_per_trade": 50,
        "copy_mode": "percent", "min_win_rate": 0
    }).execute()
    try:
        await checking_msg.delete()
    except:
        pass
    await message.answer(
        f"✅ *Now Copying!*\n\n"
        f"`{target[:8]}...{target[-6:]}`\n\n"
        f"{win_rate_str}"
        f"{warning_str}\n"
        f"Default: *10% of balance* per trade (max $50)\n\n"
        f"_Tap Manage to adjust settings or set a minimum win rate filter._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Manage Settings", callback_data="menu:copy")],
            [InlineKeyboardButton(text="← Menu", callback_data="menu:main")]
        ])
    )

# ─── Price Chart (Dome candlesticks) ─────────────────────────

@router.callback_query(F.data.startswith("chart:"))
async def cb_price_chart(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    # Resolve short key back to full condition_id
    condition_id = token_store.get(f"cid:{key}", key)
    await callback.message.answer("📈 *Fetching price history...*", parse_mode="Markdown")
    await safe_answer(callback)
    if not dome:
        await callback.message.answer("❌ Price history unavailable — Dome API not configured.", reply_markup=back_to_menu())
        return
    try:
        from datetime import timezone as tz
        end_time = int(datetime.now(tz.utc).timestamp())
        start_time = end_time - (7 * 24 * 3600)  # 7 days
        candles = dome.polymarket.markets.get_candlesticks({
            "condition_id": condition_id,
            "start_time": start_time,
            "end_time": end_time,
            "interval": 1440  # daily candles
        })
        points = getattr(candles, "candlesticks", [])
        if not points:
            await callback.message.answer("😕 No price history available for this market.", reply_markup=back_to_menu())
            return
        # Build ASCII chart
        prices = []
        for p in points:
            close = getattr(getattr(p, "price", None), "close", None)
            if close is not None:
                prices.append(float(close))
        if not prices:
            await callback.message.answer("😕 No price data available.", reply_markup=back_to_menu())
            return
        min_p = min(prices)
        max_p = max(prices)
        chart_lines = []
        for i, price in enumerate(prices[-7:]):  # last 7 days
            bar_len = int((price - min_p) / (max_p - min_p + 0.001) * 15)
            bar = "█" * bar_len + "░" * (15 - bar_len)
            day = f"Day -{len(prices[-7:]) - i}"
            chart_lines.append(f"`{day:6}` {bar} *{round(price*100)}¢*")
        current = prices[-1]
        change = prices[-1] - prices[0]
        change_str = f"+{change*100:.1f}¢" if change >= 0 else f"{change*100:.1f}¢"
        trend = "📈" if change >= 0 else "📉"
        chart_text = (
            f"📊 *7-Day Price History*\n\n"
            f"{chr(10).join(chart_lines)}\n\n"
            f"{trend} 7d change: *{change_str}*\n"
            f"Current: *{round(current*100)}¢*  "
            f"Range: *{round(min_p*100)}¢ – {round(max_p*100)}¢*"
        )
        await callback.message.answer(chart_text, parse_mode="Markdown", reply_markup=back_to_menu())
    except Exception as e:
        print(f"[chart] Error: {e}")
        await callback.message.answer("❌ Failed to load price history.", reply_markup=back_to_menu())

# ─── Top Traders Discovery (Dome) ────────────────────────────

@router.callback_query(F.data == "copy:top_traders")
async def cb_top_traders(callback: CallbackQuery):
    await callback.message.edit_text("🔍 *Finding top Polymarket traders...*", parse_mode="Markdown")
    await safe_answer(callback)
    try:
        top_markets = get_markets(limit=3)
        seen_wallets = {}
        for m in top_markets:
            condition_id = m.get("conditionId")
            if not condition_id:
                continue
            r = requests.get(
                f"https://data-api.polymarket.com/holders?market={condition_id}&limit=5",
                timeout=6
            )
            if not r.ok:
                continue
            data = r.json()
            # Normalise all possible response shapes
            holder_list = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        if "holders" in item and isinstance(item["holders"], list):
                            holder_list.extend(item["holders"])
                        elif "proxyWallet" in item:
                            holder_list.append(item)
            for holder in holder_list:
                if not isinstance(holder, dict):
                    continue
                wallet = holder.get("proxyWallet", "")
                if not wallet or wallet in seen_wallets:
                    continue
                name = holder.get("name") or holder.get("pseudonym") or f"{wallet[:8]}...{wallet[-6:]}"
                amount = holder.get("amount", 0)
                try:
                    amount = float(amount)
                except:
                    amount = 0
                seen_wallets[wallet] = {"wallet": wallet, "name": name, "amount": amount}

        top = sorted(seen_wallets.values(), key=lambda x: x["amount"], reverse=True)[:8]
        if not top:
            await callback.message.edit_text("😕 No trader data available right now.", reply_markup=back_to_copy())
            return

        text = "🏆 *Top Active Traders*\n\n_Traders with the biggest positions right now:_\n\n"
        buttons = []
        medals = ["🥇", "🥈", "🥉"]
        for i, t in enumerate(top):
            w = t["wallet"]
            name = t["name"]
            amount_str = f"${t['amount']:,.0f}"
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} *{name}* — {amount_str} in positions\n"
            buttons.append([InlineKeyboardButton(
                text=f"{medal} {name} ({amount_str})",
                callback_data=f"copy:follow_top:{w}"
            )])
        buttons.append([InlineKeyboardButton(text="← Back", callback_data="menu:copy")])
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        print(f"[top_traders] Error: {e}")
        await callback.message.edit_text("❌ Failed to load top traders.", reply_markup=back_to_copy())

@router.callback_query(F.data.startswith("copy:follow_top:"))
async def cb_follow_top_trader(callback: CallbackQuery, state: FSMContext):
    wallet = callback.data.split(":", 2)[2]
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    existing = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).eq("target_wallet", wallet.lower()).eq("active", True).execute()
    if existing.data:
        await safe_answer(callback, "Already copying this wallet!", show_alert=True)
        return
    existing_count = supabase.table("copy_trades").select("id").eq("user_id", user["id"]).eq("active", True).execute()
    if len(existing_count.data) >= 10:
        await safe_answer(callback, "Max 10 copy trades reached.", show_alert=True)
        return
    win_rate = get_wallet_win_rate(wallet)
    win_rate_str = f"{win_rate:.0f}%" if win_rate is not None else "N/A"
    supabase.table("copy_trades").insert({
        "user_id": user["id"], "target_wallet": wallet.lower(),
        "active": True, "copy_percent": 0.10, "max_per_trade": 50,
        "copy_mode": "percent", "min_win_rate": 0
    }).execute()
    await callback.message.edit_text(
        f"✅ *Now Following!*\n\n"
        f"`{wallet[:8]}...{wallet[-6:]}`\n"
        f"📊 Win rate: *{win_rate_str}*\n\n"
        f"Default: *10% of balance* per trade (max $50)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Manage", callback_data="menu:copy")],
            [InlineKeyboardButton(text="← Menu", callback_data="menu:main")]
        ])
    )
    await safe_answer(callback)

# ─── Who's Buying (holders from market) ──────────────────────

@router.callback_query(F.data.startswith("holders:"))
async def cb_holders(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    # Resolve short key back to full condition_id
    condition_id = token_store.get(f"cid:{key}", key)
    await callback.message.answer("👥 *Finding top holders...*", parse_mode="Markdown")
    await safe_answer(callback)
    try:
        r = requests.get(f"https://data-api.polymarket.com/holders?market={condition_id}&limit=8", timeout=6)
        if not r.ok:
            await callback.message.answer("😕 Holder data unavailable.", reply_markup=back_to_menu())
            return
        data = r.json()
        if data and isinstance(data[0], dict) and "holders" in data[0]:
            holder_list = [h for group in data for h in group.get("holders", [])]
        else:
            holder_list = data
        if not holder_list:
            await callback.message.answer("😕 No holder data found.", reply_markup=back_to_menu())
            return
        # Dedupe by wallet
        seen = {}
        for h in holder_list:
            w = h.get("proxyWallet", "")
            if w and w not in seen:
                seen[w] = h
        top = sorted(seen.values(), key=lambda x: float(x.get("amount", 0)), reverse=True)[:8]
        text = "👥 *Top Holders*\n\n_Tap to follow their trades:_\n\n"
        buttons = []
        for i, h in enumerate(top):
            w = h.get("proxyWallet", "")
            name = h.get("name") or h.get("pseudonym") or f"{w[:8]}...{w[-6:]}"
            amount = float(h.get("amount", 0))
            outcome = "Yes" if h.get("outcomeIndex", 0) == 0 else "No"
            outcome_emoji = "🟢" if outcome == "Yes" else "🔴"
            text += f"{i+1}. *{name}* — {outcome_emoji} {outcome} ({amount:,.0f} shares)\n"
            buttons.append([InlineKeyboardButton(
                text=f"👁 Follow {name}",
                callback_data=f"copy:follow_top:{w}"
            )])
        buttons.append([InlineKeyboardButton(text="← Back", callback_data="menu:main")])
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        print(f"[holders] Error: {e}")
        await callback.message.answer("❌ Failed to load holders.", reply_markup=back_to_menu())

# ─── Trader Profile ───────────────────────────────────────────

@router.callback_query(F.data.startswith("copy:profile:"))
async def cb_trader_profile(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    result = supabase.table("copy_trades").select("*").eq("id", copy_id).execute()
    if not result.data:
        await safe_answer(callback, "Not found.", show_alert=True)
        return
    wallet = result.data[0]["target_wallet"]
    await callback.message.edit_text("📊 *Loading trader profile...*", parse_mode="Markdown")
    await safe_answer(callback)
    try:
        # Get positions
        pos_r = requests.get(f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=1&limit=20&sortBy=CURRENT", timeout=6)
        positions = pos_r.json() if pos_r.ok else []
        # Get recent trades
        trades_r = requests.get(f"https://data-api.polymarket.com/activity?user={wallet}&limit=30&type=TRADE", timeout=6)
        trades = trades_r.json() if trades_r.ok else []
        # Compute stats
        total_positions = len(positions)
        total_value = sum(float(p.get("currentValue", 0)) for p in positions)
        total_pnl = sum(float(p.get("cashPnl", 0)) for p in positions)
        wins = sum(1 for p in positions if float(p.get("cashPnl", 0)) > 0)
        win_rate = (wins / total_positions * 100) if total_positions > 0 else 0
        # Best trade
        best = max(positions, key=lambda x: float(x.get("cashPnl", 0)), default=None)
        best_str = ""
        if best and float(best.get("cashPnl", 0)) > 0:
            best_str = f"\n🏆 Best: _{best.get('title', '')[:40]}_ (+${float(best['cashPnl']):.0f})"
        # Recent activity
        recent = []
        for t in trades[:5]:
            side = t.get("side", "")
            title = t.get("title", "")[:35]
            size = float(t.get("usdcSize", 0))
            side_emoji = "🟢" if side == "BUY" else "🔴"
            recent.append(f"{side_emoji} {title} — ${size:.0f}")
        recent_str = "\n".join(recent) if recent else "_No recent activity_"
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        pnl_str = f"+${total_pnl:.0f}" if total_pnl >= 0 else f"-${abs(total_pnl):.0f}"
        text = (
            f"📊 *Trader Profile*\n\n"
            f"`{wallet[:8]}...{wallet[-6:]}`\n\n"
            f"💰 Portfolio value: *${total_value:,.0f}*\n"
            f"{pnl_emoji} Total PnL: *{pnl_str}*\n"
            f"🎯 Win rate: *{win_rate:.0f}%* ({wins}/{total_positions} positions)\n"
            f"{best_str}\n\n"
            f"*Recent Trades:*\n{recent_str}"
        )
        buttons = [[InlineKeyboardButton(text="← Back", callback_data=f"copy:manage:{copy_id}")]]
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        print(f"[profile] Error: {e}")
        await callback.message.edit_text("❌ Failed to load profile.", reply_markup=back_to_copy())

# ─── Live Copy Trade Feed ─────────────────────────────────────

@router.callback_query(F.data == "copy:feed")
async def cb_copy_feed(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.edit_text("📡 *Loading live feed...*", parse_mode="Markdown")
    await safe_answer(callback)
    try:
        active = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).eq("active", True).execute()
        if not active.data:
            await callback.message.edit_text(
                "📡 *Live Feed*\n\n_Follow some traders to see their activity here._",
                parse_mode="Markdown", reply_markup=back_to_copy()
            )
            return
        all_trades = []
        for copy in active.data:
            wallet = copy["target_wallet"]
            r = requests.get(f"https://data-api.polymarket.com/activity?user={wallet}&limit=10&type=TRADE", timeout=5)
            if not r.ok: continue
            for t in r.json():
                t["_wallet"] = wallet
                t["_copy_id"] = copy["id"]
                all_trades.append(t)
        # Sort by timestamp newest first
        all_trades.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        if not all_trades:
            await callback.message.edit_text("📡 *Live Feed*\n\n_No recent activity from traders you follow._", parse_mode="Markdown", reply_markup=back_to_copy())
            return
        text = "📡 *Live Feed* — _Traders you follow_\n\n"
        for t in all_trades[:15]:
            w = t["_wallet"]
            name = t.get("name") or t.get("pseudonym") or f"{w[:6]}..."
            side = t.get("side", "")
            title = t.get("title", "Unknown")[:40]
            size = float(t.get("usdcSize", 0))
            outcome = t.get("outcome", "")
            side_emoji = "🟢" if side == "BUY" else "🔴"
            ts = t.get("timestamp", 0)
            try:
                from datetime import timezone as tz
                dt = datetime.fromtimestamp(ts, tz=tz.utc)
                time_str = dt.strftime("%H:%M")
            except:
                time_str = ""
            text += f"{side_emoji} *{name}* {side} {outcome} — ${size:.0f}\n_{title}_\n`{time_str}`\n\n"
        await callback.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Refresh", callback_data="copy:feed")],
                [InlineKeyboardButton(text="← Back", callback_data="menu:copy")]
            ])
        )
    except Exception as e:
        print(f"[feed] Error: {e}")
        await callback.message.edit_text("❌ Failed to load feed.", reply_markup=back_to_copy())

# ─── Copy Trade History ───────────────────────────────────────

@router.callback_query(F.data == "copy:history")
async def cb_copy_history(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.edit_text("📜 *Loading copy history...*", parse_mode="Markdown")
    await safe_answer(callback)
    try:
        # Get copy trades log from seen_trades + activity
        active = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).execute()
        if not active.data:
            await callback.message.edit_text("📜 *Copy History*\n\n_No copy trades yet._", parse_mode="Markdown", reply_markup=back_to_copy())
            return
        # Get user's own recent trades to match against copy trades
        my_trades_r = requests.get(
            f"https://data-api.polymarket.com/activity?user={user['wallet_address']}&limit=50&type=TRADE",
            timeout=6
        )
        my_trades = my_trades_r.json() if my_trades_r.ok else []
        # Build wallet name map
        wallet_map = {c["target_wallet"]: c for c in active.data}
        text = "📜 *Copy Trade History*\n\n"
        count = 0
        total_pnl = 0
        for t in my_trades:
            if count >= 15: break
            side = t.get("side", "")
            title = t.get("title", "")[:38]
            size = float(t.get("usdcSize", 0))
            pnl = float(t.get("cashPnl", 0) or 0)
            side_emoji = "🟢" if side == "BUY" else "🔴"
            pnl_str = f"+${pnl:.2f}" if pnl > 0 else (f"-${abs(pnl):.2f}" if pnl < 0 else "")
            pnl_emoji = "📈" if pnl > 0 else ("📉" if pnl < 0 else "")
            text += f"{side_emoji} *{side}* — ${size:.0f} {pnl_emoji}{pnl_str}\n_{title}_\n\n"
            total_pnl += pnl
            count += 1
        if count == 0:
            text += "_No trades found._\n"
        else:
            total_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
            text += f"━━━━━━━━━━━━━━\n📊 Total PnL: *{total_str}*"
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_to_copy())
    except Exception as e:
        print(f"[history] Error: {e}")
        await callback.message.edit_text("❌ Failed to load history.", reply_markup=back_to_copy())

# ─── Copy Trade Budget ────────────────────────────────────────

@router.callback_query(F.data == "copy:budget")
async def cb_copy_budget(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    current = user.get("copy_budget") or 0
    current_str = f"*${current:.0f}/week*" if current > 0 else "*No limit set*"
    await callback.message.edit_text(
        f"💰 *Weekly Copy Trade Budget*\n\n"
        f"Current: {current_str}\n\n"
        f"Set a weekly spending cap across all copy trades.\n"
        f"Bot will pause all copy trades once the limit is hit.\n\n"
        f"_Enter amount in USD, or 0 to remove limit:_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="$50", callback_data="copy:budget_quick:50"),
                InlineKeyboardButton(text="$100", callback_data="copy:budget_quick:100"),
                InlineKeyboardButton(text="$250", callback_data="copy:budget_quick:250"),
            ],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:copy")]
        ])
    )
    await state.set_state(TradeStates.waiting_for_copy_budget)
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:budget_quick:"))
async def cb_copy_budget_quick(callback: CallbackQuery, state: FSMContext):
    amount = int(callback.data.split(":")[2])
    user = get_user(callback.from_user.id)
    supabase.table("users").update({"copy_budget": amount, "copy_budget_used": 0}).eq("id", user["id"]).execute()
    await state.clear()
    await callback.message.edit_text(
        f"✅ *Weekly budget set to ${amount}*\n\nCopy trades will pause when you hit this limit.",
        parse_mode="Markdown", reply_markup=back_to_copy()
    )
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_copy_budget)
async def handle_copy_budget(message: Message, state: FSMContext):
    await state.clear()
    try:
        amount = float(message.text.strip())
        if amount < 0: raise ValueError
    except:
        await message.answer("❌ Enter a valid amount (e.g. 100) or 0 to remove limit.", reply_markup=back_to_copy())
        return
    user = get_user(message.from_user.id)
    supabase.table("users").update({"copy_budget": amount, "copy_budget_used": 0}).eq("id", user["id"]).execute()
    if amount == 0:
        await message.answer("✅ *Budget limit removed.*", parse_mode="Markdown", reply_markup=back_to_copy())
    else:
        await message.answer(f"✅ *Weekly budget set to ${amount:.0f}*\n\nCopy trades will pause when you hit this limit.", parse_mode="Markdown", reply_markup=back_to_copy())

# ─── Watch Wallet (alerts without copying) ────────────────────

@router.callback_query(F.data == "copy:watch_prompt")
async def cb_watch_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "👁 *Watch a Wallet*\n\n"
        "Get notified when a wallet makes a trade — without copying them.\n\n"
        "Paste the Polymarket wallet address to watch:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:copy")]
        ])
    )
    await state.set_state(TradeStates.waiting_for_alert_wallet)
    await safe_answer(callback)

@router.message(TradeStates.waiting_for_alert_wallet)
async def handle_alert_wallet(message: Message, state: FSMContext):
    await state.clear()
    wallet = message.text.strip()
    if not wallet.startswith("0x") or len(wallet) != 42:
        await message.answer("❌ Invalid wallet address.", reply_markup=back_to_copy())
        return
    user = get_user(message.from_user.id)
    # Store as a copy trade with copy_mode = "watch" (no actual trades placed)
    existing = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).eq("target_wallet", wallet.lower()).eq("active", True).execute()
    if existing.data:
        await message.answer("⚠️ Already following or watching this wallet!", reply_markup=back_to_copy())
        return
    existing_count = supabase.table("copy_trades").select("id").eq("user_id", user["id"]).eq("active", True).execute()
    if len(existing_count.data) >= 10:
        await message.answer("❌ Maximum 10 copy/watch slots reached.", reply_markup=back_to_copy())
        return
    supabase.table("copy_trades").insert({
        "user_id": user["id"], "target_wallet": wallet.lower(),
        "active": True, "copy_percent": 0, "max_per_trade": 0,
        "copy_mode": "watch", "min_win_rate": 0
    }).execute()
    await message.answer(
        f"👁 *Now Watching!*\n\n"
        f"`{wallet[:8]}...{wallet[-6:]}`\n\n"
        f"You'll get notified when this wallet makes a trade.\n"
        f"_No trades will be copied — alerts only._",
        parse_mode="Markdown", reply_markup=back_to_copy()
    )

# ─── Copied Traders Leaderboard ──────────────────────────────

@router.callback_query(F.data == "copy:my_leaderboard")
async def cb_my_leaderboard(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await callback.message.edit_text("📊 *Loading your traders...*", parse_mode="Markdown")
    await safe_answer(callback)
    try:
        active = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).eq("active", True).execute()
        if not active.data:
            await callback.message.edit_text("📊 No traders followed yet.", parse_mode="Markdown", reply_markup=back_to_copy())
            return
        ranked = []
        for c in active.data:
            w = c["target_wallet"]
            wr = get_wallet_win_rate(w)
            ranked.append({"wallet": w, "win_rate": wr or 0, "copy_id": c["id"], "mode": c.get("copy_mode", "percent")})
        ranked.sort(key=lambda x: x["win_rate"], reverse=True)
        text = "📊 *Your Traders — Ranked by Win Rate*\n\n"
        buttons = []
        medals = ["🥇", "🥈", "🥉"]
        for i, t in enumerate(ranked):
            w = t["wallet"]
            wr = t["win_rate"]
            medal = medals[i] if i < 3 else f"{i+1}."
            mode = "👁 Watch" if t["mode"] == "watch" else "🤖 Copy"
            wr_bar = "█" * int(wr / 10) + "░" * (10 - int(wr / 10))
            text += f"{medal} `{w[:8]}...{w[-6:]}` {mode}\n`{wr_bar}` *{wr:.0f}%*\n\n"
            buttons.append([InlineKeyboardButton(text=f"⚙️ Manage {w[:8]}...", callback_data=f"copy:manage:{t['copy_id']}")])
        buttons.append([InlineKeyboardButton(text="← Back", callback_data="menu:copy")])
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        print(f"[my_leaderboard] Error: {e}")
        await callback.message.edit_text("❌ Failed to load.", reply_markup=back_to_copy())

# ─── Stop Loss per Copy Trade ─────────────────────────────────

@router.callback_query(F.data.startswith("copy:stoploss:"))
async def cb_copy_stoploss(callback: CallbackQuery):
    copy_id = int(callback.data.split(":")[2])
    result = supabase.table("copy_trades").select("*").eq("id", copy_id).execute()
    if not result.data:
        await safe_answer(callback, "Not found.", show_alert=True)
        return
    current = result.data[0].get("stop_loss_pct") or 0
    current_str = f"*{current}% loss*" if current > 0 else "*Not set*"
    await callback.message.edit_text(
        f"🛑 *Stop Loss*\n\n"
        f"Current: {current_str}\n\n"
        f"Auto-sell your copied position if it drops by this % from entry price.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="20%", callback_data=f"copy:sl_quick:{copy_id}:20"),
                InlineKeyboardButton(text="30%", callback_data=f"copy:sl_quick:{copy_id}:30"),
                InlineKeyboardButton(text="50%", callback_data=f"copy:sl_quick:{copy_id}:50"),
            ],
            [InlineKeyboardButton(text="❌ Remove Stop Loss", callback_data=f"copy:sl_quick:{copy_id}:0")],
            [InlineKeyboardButton(text="← Back", callback_data=f"copy:manage:{copy_id}")]
        ])
    )
    await safe_answer(callback)

@router.callback_query(F.data.startswith("copy:sl_quick:"))
async def cb_sl_quick(callback: CallbackQuery):
    parts = callback.data.split(":")
    copy_id = int(parts[2])
    pct = int(parts[3])
    user_id = callback.from_user.id
    supabase.table("copy_trades").update({"stop_loss_pct": pct}).eq("id", copy_id).eq("user_id", user_id).execute()
    if pct == 0:
        await safe_answer(callback, "Stop loss removed.", show_alert=True)
    else:
        await safe_answer(callback, f"Stop loss set to {pct}%.", show_alert=True)
    await cb_copy_manage(callback)

# ─── Social Share ─────────────────────────────────────────────

@router.callback_query(F.data == "copy:share")
async def cb_copy_share(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await safe_answer(callback)
    try:
        pos = get_positions(user["wallet_address"])
        total_pnl = sum(float(p.get("cashPnl", 0)) for p in pos)
        active = supabase.table("copy_trades").select("*").eq("user_id", user["id"]).eq("active", True).execute()
        following = len([c for c in (active.data or []) if c.get("copy_mode") != "watch"])
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        emoji = "🚀" if total_pnl >= 0 else "📉"
        card = (
            f"{emoji} *My PolyRift Stats*\n\n"
            f"💰 PnL: *{pnl_str}*\n"
            f"🤖 Copying: *{following} trader(s)*\n"
            f"📊 Positions: *{len(pos)}*\n\n"
            f"_Trade smarter on Polymarket 👇_\n"
            f"t.me/polyrift\\_bot"
        )
        await callback.message.answer(
            f"📤 *Share your stats:*\n\n{card}\n\n_Copy the text above to share!_",
            parse_mode="Markdown", reply_markup=back_to_menu()
        )
    except Exception as e:
        print(f"[share] Error: {e}")
        await callback.message.answer("❌ Failed to generate stats.", reply_markup=back_to_menu())

# ─── POL → USDC.e Swap via 1inch ─────────────────────────────

POL_TOKEN  = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"  # native POL
USDC_E_1INCH = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"  # USDC.e on Polygon
ONEINCH_ROUTER = "0x1111111254eeb25477b68fb85ed929f73a960582"  # 1inch v5 router Polygon
ONEINCH_CHAIN = 137  # Polygon

def oneinch_quote(amount_wei: int) -> dict | None:
    """Get a quote for swapping POL → USDC.e via 1inch."""
    try:
        headers = {"Authorization": f"Bearer {ONEINCH_API_KEY}"} if ONEINCH_API_KEY else {}
        r = requests.get(
            f"https://api.1inch.dev/swap/v6.0/{ONEINCH_CHAIN}/quote",
            params={
                "src": POL_TOKEN,
                "dst": USDC_E_1INCH,
                "amount": str(amount_wei),
            },
            headers=headers,
            timeout=6
        )
        if r.ok:
            return r.json()
        print(f"[1inch] quote error: {r.status_code} {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[1inch] quote exception: {e}")
        return None

def oneinch_swap_tx(wallet_address: str, amount_wei: int, slippage: float = 1.0) -> dict | None:
    """Get swap calldata from 1inch for POL → USDC.e."""
    try:
        headers = {"Authorization": f"Bearer {ONEINCH_API_KEY}"} if ONEINCH_API_KEY else {}
        r = requests.get(
            f"https://api.1inch.dev/swap/v6.0/{ONEINCH_CHAIN}/swap",
            params={
                "src": POL_TOKEN,
                "dst": USDC_E_1INCH,
                "amount": str(amount_wei),
                "from": wallet_address,
                "slippage": slippage,
                "disableEstimate": "true",
            },
            headers=headers,
            timeout=8
        )
        if r.ok:
            return r.json()
        print(f"[1inch] swap error: {r.status_code} {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[1inch] swap exception: {e}")
        return None

@router.callback_query(F.data == "swap:pol_to_usdc")
async def cb_swap_pol(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    pol_bal = get_pol_balance(user["wallet_address"])
    # Reserve 0.05 POL for gas
    swappable = pol_bal - 0.05
    if swappable < 0.1:
        await callback.message.edit_text(
            "⚠️ *Not enough POL to swap*\n\n"
            "You need at least 0.15 POL (0.1 to swap + 0.05 kept for gas).",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Back", callback_data="menu:balance")]
            ])
        )
        await safe_answer(callback)
        return
    await callback.message.edit_text("🔄 *Getting quote...*", parse_mode="Markdown")
    await safe_answer(callback)
    amount_wei = int(swappable * 1e18)
    quote = oneinch_quote(amount_wei)
    if not quote:
        await callback.message.edit_text(
            "❌ *Swap unavailable right now.*\n\nTry again in a moment.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Back", callback_data="menu:balance")]
            ])
        )
        return
    dst_amount = int(quote.get("dstAmount", 0))
    usdc_out = dst_amount / 1_000_000
    await callback.message.edit_text(
        f"🔄 *Swap POL → USDC.e*\n\n"
        f"You send: *{swappable:.4f} POL*\n"
        f"You receive: *~${usdc_out:.2f} USDC.e*\n"
        f"Slippage: *1%*\n"
        f"Gas reserved: *0.05 POL*\n\n"
        f"_Tap confirm to execute the swap._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Confirm Swap ${usdc_out:.2f}", callback_data=f"swap:confirm:{amount_wei}")],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:balance")]
        ])
    )

@router.callback_query(F.data.startswith("swap:confirm:"))
async def cb_swap_confirm(callback: CallbackQuery):
    if is_rate_limited(callback.from_user.id, cooldown_seconds=5):
        await safe_answer(callback, "⏳ Please wait before retrying.", show_alert=True)
        return
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    amount_wei = int(callback.data.split(":")[2])
    await callback.message.edit_text("⏳ *Executing swap...*", parse_mode="Markdown")
    await safe_answer(callback)
    try:
        private_key = decrypt_key(user["encrypted_key"])
        wallet = user["wallet_address"]
        swap_data = oneinch_swap_tx(wallet, amount_wei)
        if not swap_data or "tx" not in swap_data:
            await callback.message.edit_text(
                "❌ *Swap failed — could not build transaction.*\n\nTry again later.",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="← Back", callback_data="menu:balance")]
                ])
            )
            return
        tx = swap_data["tx"]
        w3 = get_w3()
        account = w3.eth.account.from_key(private_key)
        nonce = w3.eth.get_transaction_count(account.address)
        base_fee = get_base_fee(w3)
        built_tx = {
            "from": account.address,
            "to": Web3.to_checksum_address(tx["to"]),
            "data": tx["data"],
            "value": int(tx.get("value", amount_wei)),
            "nonce": nonce,
            "gas": int(int(tx.get("gas", 300000)) * 1.2),
            "gasPrice": 150_000_000_000,  # 150 gwei
            "chainId": ONEINCH_CHAIN,
        }
        signed = w3.eth.account.sign_transaction(built_tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        # Estimate output
        dst_amount = int(swap_data.get("dstAmount", 0))
        usdc_out = dst_amount / 1_000_000
        await callback.message.edit_text(
            f"✅ *Swap Submitted!*\n\n"
            f"Swapping POL → *~${usdc_out:.2f} USDC.e*\n\n"
            f"[View on PolygonScan](https://polygonscan.com/tx/{tx_hash})\n\n"
            f"_Your USDC.e balance will update in ~30 seconds._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Check Balance", callback_data="menu:balance")],
                [InlineKeyboardButton(text="← Menu", callback_data="menu:main")]
            ])
        )
    except Exception as e:
        print(f"[swap] Error: {e}")
        await callback.message.edit_text(
            "❌ *Swap failed.*\n\nYour funds are safe — nothing was sent.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Back", callback_data="menu:balance")]
            ])
        )

# ─── Referral System ─────────────────────────────────────────

@router.callback_query(F.data == "menu:referral")
async def cb_referral(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await safe_answer(callback)
    ref_code = f"REF{user['id']}"
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={ref_code}"
    # Count referrals
    refs = supabase.table("users").select("id, referral_bonus_paid").eq("referred_by", user["id"]).execute()
    total_refs = len(refs.data) if refs.data else 0
    active_refs = sum(1 for r in (refs.data or []) if r.get("referral_bonus_paid"))
    earnings = float(user.get("referral_earnings") or 0)
    earnings_str = f"${earnings:.4f}" if earnings < 1 else f"${earnings:.2f}"
    text = (
        f"📣 *Refer & Earn*\n\n"
        f"Share your link and earn *20% of PolyRift's fees* from every trade your referrals make — forever.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total referrals: *{total_refs}*\n"
        f"✅ Active traders: *{active_refs}*\n"
        f"💰 Total earned: *{earnings_str} USDC.e*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 *New users get $1 bonus* on their first $10+ deposit\n\n"
        f"*Your referral link:*\n"
        f"`{ref_link}`\n\n"
        f"_Earnings are paid to your wallet daily (min $1)._"
    )
    await callback.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Copy Link", switch_inline_query=ref_link)],
            [InlineKeyboardButton(text="📤 Share Link", url=f"https://t.me/share/url?url={ref_link}&text=Trade%20prediction%20markets%20on%20PolyRift%20%F0%9F%8C%8A")],
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ])
    )

async def pay_referral_earnings():
    """Pay out accumulated referral earnings daily to each referrer's wallet."""
    try:
        if not RELAY_PRIVATE_KEY:
            return
        users = supabase.table("users").select("id, wallet_address, referral_earnings").gt("referral_earnings", 1).execute()
        for user in (users.data or []):
            try:
                earnings = float(user.get("referral_earnings") or 0)
                if earnings < 1:
                    continue
                w3 = get_w3()
                relay_account = w3.eth.account.from_key(RELAY_PRIVATE_KEY)
                contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
                amount_raw = int(earnings * 1_000_000)
                nonce = w3.eth.get_transaction_count(relay_account.address)
                base_fee = get_base_fee(w3)
                tx = {
                    'from': relay_account.address, 'to': USDC_E, 'nonce': nonce, 'gas': 100000,
                    'gasPrice': 150_000_000_000, 'chainId': 137,
                    'data': contract.encode_abi('transfer', [Web3.to_checksum_address(user["wallet_address"]), amount_raw]),
                }
                signed = w3.eth.account.sign_transaction(tx, RELAY_PRIVATE_KEY)
                w3.eth.send_raw_transaction(signed.raw_transaction)
                # Reset earnings
                supabase.table("users").update({"referral_earnings": 0}).eq("id", user["id"]).execute()
                # Notify
                try:
                    await bot.send_message(
                        user["id"],
                        f"💰 *Referral Earnings Paid!*\n\n"
                        f"*${earnings:.4f} USDC.e* has been sent to your wallet from referral commissions.\n\n"
                        f"Keep sharing your link to earn more!",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="📣 My Referrals", callback_data="menu:referral")]
                        ])
                    )
                except:
                    pass
                print(f"[referral] Paid ${earnings:.4f} to user {user['id']}")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"[referral] payout error for user {user['id']}: {e}")
    except Exception as e:
        print(f"[referral] payout loop error: {e}")

async def sweep_fee_wallet():
    """Sweep accumulated USDC.e from fee wallet into relay wallet to fund referral payouts."""
    try:
        if not FEE_PRIVATE_KEY or not RELAY_PRIVATE_KEY:
            print("[sweep] Skipping — FEE_PRIVATE_KEY or RELAY_PRIVATE_KEY not set")
            return
        w3 = get_w3()
        fee_account = w3.eth.account.from_key(FEE_PRIVATE_KEY)
        relay_account = w3.eth.account.from_key(RELAY_PRIVATE_KEY)
        contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
        balance_raw = contract.functions.balanceOf(fee_account.address).call()
        balance = balance_raw / 1_000_000
        # Only sweep if more than $2 sitting in fee wallet
        if balance < 2:
            print(f"[sweep] Fee wallet ${balance:.4f} — below threshold, skipping")
            return
        # Keep $0.50 as buffer, sweep the rest
        sweep_amount = balance - 0.50
        sweep_raw = int(sweep_amount * 1_000_000)
        nonce = w3.eth.get_transaction_count(fee_account.address)
        base_fee = get_base_fee(w3)
        tx = {
            'from': fee_account.address, 'to': USDC_E, 'nonce': nonce, 'gas': 100000,
            'gasPrice': 150_000_000_000, 'chainId': 137,
            'data': contract.encode_abi('transfer', [relay_account.address, sweep_raw]),
        }
        signed = w3.eth.account.sign_transaction(tx, FEE_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        print(f"[sweep] Swept ${sweep_amount:.4f} USDC.e from fee wallet to relay wallet — tx: {tx_hash}")
    except Exception as e:
        print(f"[sweep] Error: {e}")

async def check_expiring_markets():
    """Alert users when markets they hold are expiring within 24 hours."""
    try:
        all_users = supabase.table("users").select("id, wallet_address").execute()
        for user in (all_users.data or []):
            try:
                positions = get_positions(user["wallet_address"])
                if not positions:
                    continue
                alerts = []
                for p in positions:
                    end_date = p.get("endDate", "")
                    if not end_date:
                        continue
                    try:
                        end = datetime.strptime(end_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        now = datetime.now(timezone.utc)
                        hours_left = (end - now).total_seconds() / 3600
                        if 0 < hours_left <= 24:
                            title = p.get("title") or p.get("question") or "Unknown market"
                            size = float(p.get("size") or 0)
                            value = float(p.get("currentValue") or 0)
                            alerts.append((title, hours_left, size, value))
                    except:
                        continue
                if alerts:
                    text = "⏰ *Expiring Markets Alert!*\n\nYou have positions expiring within 24 hours:\n\n"
                    for title, hours, size, value in alerts:
                        text += f"• _{title}_\n  ⏳ {hours:.0f}h left — *{size:.2f} shares* (${value:.2f})\n\n"
                    text += "_Review your positions and decide to hold or sell before resolution._"
                    try:
                        await bot.send_message(
                            user["id"], text, parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💼 View Portfolio", callback_data="menu:portfolio")]
                            ])
                        )
                    except:
                        pass
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"[expiry] user error: {e}")
    except Exception as e:
        print(f"[expiry] loop error: {e}")

# Track alerted arbitrage markets to avoid repeat alerts
arb_alerted = set()


# ═══════════════════════════════════════════════════════════════
# 🛩 SMART PILOT — AI Auto-Copy Trading
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# 🛩 SMART PILOT — AI Wallet Manager
# ═══════════════════════════════════════════════════════════════

def get_wallet_recent_pnl(wallet_address: str, days: int = 7) -> dict:
    """Fetch a wallet's recent PnL and trade stats from data-api."""
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/positions?user={wallet_address}&limit=50",
            timeout=8
        )
        if not r.ok:
            return {"pnl": 0, "win_rate": 0, "trade_count": 0}
        positions = r.json() if isinstance(r.json(), list) else []

        total_cash_pnl = 0
        wins = 0
        losses = 0
        for p in positions:
            cash_pnl = float(p.get("cashPnl") or 0)
            total_cash_pnl += cash_pnl
            if cash_pnl > 0:
                wins += 1
            elif cash_pnl < 0:
                losses += 1

        total = wins + losses
        win_rate = (wins / total) if total > 0 else 0
        return {
            "pnl": round(total_cash_pnl, 2),
            "win_rate": round(win_rate, 3),
            "trade_count": total,
            "wins": wins,
            "losses": losses,
        }
    except Exception as e:
        print(f"[smart_pilot] get_wallet_recent_pnl error: {e}")
        return {"pnl": 0, "win_rate": 0, "trade_count": 0}

async def run_smart_pilot(user_id: int = None):
    """Smart Pilot — scores all followed wallets and drops underperformers."""
    try:
        query = supabase.table("users").select("*").eq("smart_pilot_enabled", True)
        if user_id:
            query = query.eq("id", user_id)
        users = query.execute()
        if not (users.data):
            return

        print(f"[smart_pilot] running for {len(users.data)} user(s)")

        for user in (users.data or []):
            try:
                risk = user.get("smart_pilot_risk") or "balanced"
                # Drop thresholds by risk profile
                drop_pnl = {"conservative": -50, "balanced": -150, "aggressive": -300}.get(risk, -150)
                drop_winrate = {"conservative": 0.40, "balanced": 0.30, "aggressive": 0.20}.get(risk, 0.30)
                min_trades = 3  # ignore wallets with too few trades to judge

                # Get all active non-smart-pilot copy trades for this user
                copies = supabase.table("copy_trades").select("*")                    .eq("user_id", user["id"]).eq("active", True)                    .neq("copy_mode", "watch").execute()

                if not copies.data:
                    continue

                dropped = []
                kept = []

                for c in copies.data:
                    w = c["target_wallet"]
                    stats = get_wallet_recent_pnl(w)
                    pnl = stats["pnl"]
                    wr = stats["win_rate"]
                    tc = stats["trade_count"]

                    # Skip if not enough data
                    if tc < min_trades:
                        kept.append((w, pnl, wr, "not enough data"))
                        continue

                    # Drop if both PnL and win rate are below threshold
                    if pnl < drop_pnl and wr < drop_winrate:
                        supabase.table("copy_trades").update({"active": False, "paused": True})                            .eq("id", c["id"]).execute()
                        dropped.append((w, pnl, wr))
                    else:
                        kept.append((w, pnl, wr, "ok"))

                # Send notification if wallets were dropped
                if dropped:
                    msg = "🛩 *Smart Pilot Update*\n\n"
                    msg += f"❌ *Removed {len(dropped)} underperforming wallet(s):*\n\n"
                    for w, pnl, wr in dropped:
                        pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
                        msg += f"`{w[:8]}...{w[-6:]}` — PnL: {pnl_str} | Win rate: {round(wr*100)}%\n"
                    msg += f"\n_Dropped because PnL < ${drop_pnl} and win rate < {round(drop_winrate*100)}% ({risk} profile)._"
                    msg += "\n\n_To re-add manually, go to Copy Trade → Follow a Trader._"
                    try:
                        await bot.send_message(
                            user["id"], msg, parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🛩 Smart Pilot", callback_data="smart_pilot:menu")],
                                [InlineKeyboardButton(text="🤖 Copy Trade Menu", callback_data="menu:copy")]
                            ])
                        )
                    except: pass
                else:
                    # Only send "all good" on manual run (user_id provided)
                    if user_id:
                        msg = "🛩 *Smart Pilot Scan Complete*\n\n"
                        if kept:
                            msg += f"✅ *All {len(kept)} wallet(s) performing within threshold:*\n\n"
                            for w, pnl, wr, reason in kept:
                                pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
                                msg += f"`{w[:8]}...{w[-6:]}` — PnL: {pnl_str} | Win rate: {round(wr*100)}%\n"
                        else:
                            msg += "_No wallets to evaluate yet. Add wallets to copy trade first._"
                        try:
                            await bot.send_message(
                                user["id"], msg, parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="🛩 Smart Pilot", callback_data="smart_pilot:menu")]
                                ])
                            )
                        except: pass

            except Exception as e:
                print(f"[smart_pilot] user {user['id']} error: {e}")

    except Exception as e:
        print(f"[smart_pilot] loop error: {e}")

# ─── Smart Pilot UI Handlers ──────────────────────────────────

@router.callback_query(F.data == "smart_pilot:menu")
async def cb_smart_pilot_menu(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await safe_answer(callback)
    enabled = user.get("smart_pilot_enabled", False)
    risk = user.get("smart_pilot_risk") or "balanced"
    risk_emoji = {"conservative": "🟢", "balanced": "🟡", "aggressive": "🔴"}.get(risk, "🟡")
    drop_pnl = {"conservative": -50, "balanced": -150, "aggressive": -300}.get(risk, -150)
    drop_wr = {"conservative": 40, "balanced": 30, "aggressive": 20}.get(risk, 30)
    copies = supabase.table("copy_trades").select("id").eq("user_id", user["id"])        .eq("active", True).neq("copy_mode", "watch").execute()
    wallet_count = len(copies.data or [])
    status_str = "ON" if enabled else "OFF"
    toggle_label = "\u2705 Turn OFF" if enabled else "\u2b55 Turn ON"
    menu_text = (
        "\U0001f6e9 *Smart Pilot*\n\n"
        "_Smart Pilot monitors all wallets you copy trade and automatically removes underperformers "
        "before they cost you more money \u2014 no manual tracking needed._\n\n"
        f"Status: *{status_str}*\n"
        f"Risk profile: {risk_emoji} *{risk.capitalize()}*\n"
        f"Drop threshold: PnL < *${abs(drop_pnl)}* AND win rate < *{drop_wr}%*\n"
        f"Watching: *{wallet_count} wallet(s)*\n\n"
        "_Runs automatically every 6 hours._"
    )
    await callback.message.edit_text(
        menu_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data="smart_pilot:toggle")],
            [InlineKeyboardButton(text=f"{risk_emoji} Risk Profile: {risk.capitalize()}", callback_data="smart_pilot:set_risk")],
            [InlineKeyboardButton(text="\U0001f504 Run Scan Now", callback_data="smart_pilot:run_now")],
            [InlineKeyboardButton(text="\U0001f4cb View Wallet Scores", callback_data="smart_pilot:view")],
            [InlineKeyboardButton(text="\u2190 Back", callback_data="menu:copy")]
        ])
    )

@router.callback_query(F.data == "smart_pilot:toggle")
async def cb_smart_pilot_toggle(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    enabled = not user.get("smart_pilot_enabled", False)
    supabase.table("users").update({"smart_pilot_enabled": enabled}).eq("id", user["id"]).execute()
    msg = "🛩 Smart Pilot *activated!* Scanning wallets every 6 hours." if enabled else "Smart Pilot *deactivated.*"
    await safe_answer(callback, msg, show_alert=True)
    await cb_smart_pilot_menu(callback)

@router.callback_query(F.data == "smart_pilot:set_risk")
async def cb_smart_pilot_set_risk(callback: CallbackQuery):
    await safe_answer(callback)
    await callback.message.edit_text(
        "🛩 *Smart Pilot — Risk Profile*\n\n"
        "Controls how aggressively Smart Pilot drops underperforming wallets:\n\n"
        "🟢 *Conservative* — Drops if PnL < -$50 AND win rate < 40%. Strict.\n\n"
        "🟡 *Balanced* — Drops if PnL < -$150 AND win rate < 30%. Recommended.\n\n"
        "🔴 *Aggressive* — Drops if PnL < -$300 AND win rate < 20%. Tolerant.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Conservative", callback_data="smart_pilot:risk:conservative")],
            [InlineKeyboardButton(text="🟡 Balanced (recommended)", callback_data="smart_pilot:risk:balanced")],
            [InlineKeyboardButton(text="🔴 Aggressive", callback_data="smart_pilot:risk:aggressive")],
            [InlineKeyboardButton(text="← Back", callback_data="smart_pilot:menu")]
        ])
    )

@router.callback_query(F.data.startswith("smart_pilot:risk:"))
async def cb_smart_pilot_risk(callback: CallbackQuery):
    risk = callback.data.split(":")[2]
    user = get_user(callback.from_user.id)
    supabase.table("users").update({"smart_pilot_risk": risk}).eq("id", user["id"]).execute()
    await safe_answer(callback, f"Risk profile set to {risk.capitalize()}", show_alert=True)
    await cb_smart_pilot_menu(callback)

@router.callback_query(F.data == "smart_pilot:run_now")
async def cb_smart_pilot_run_now(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    await safe_answer(callback, "Scanning all wallets now...", show_alert=True)
    asyncio.create_task(run_smart_pilot(user_id=user["id"]))

@router.callback_query(F.data == "smart_pilot:view")
async def cb_smart_pilot_view(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    await safe_answer(callback)
    copies = supabase.table("copy_trades").select("*")        .eq("user_id", user["id"]).eq("active", True)        .neq("copy_mode", "watch").execute()
    if not (copies.data):
        await callback.message.edit_text(
            "🛩 *Smart Pilot — Wallet Scores*\n\n_No wallets being copied yet. Add wallets via Copy Trade first._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Back", callback_data="smart_pilot:menu")]
            ])
        )
        return
    await callback.message.edit_text(
        "🛩 *Fetching live scores...*\n\n_This may take a few seconds._",
        parse_mode="Markdown"
    )
    text = "🛩 *Smart Pilot — Live Wallet Scores*\n\n"
    risk = user.get("smart_pilot_risk") or "balanced"
    drop_pnl = {"conservative": -50, "balanced": -150, "aggressive": -300}.get(risk, -150)
    drop_wr = {"conservative": 0.40, "balanced": 0.30, "aggressive": 0.20}.get(risk, 0.30)
    buttons = []
    for c in copies.data:
        w = c["target_wallet"]
        stats = get_wallet_recent_pnl(w)
        pnl = stats["pnl"]
        wr = stats["win_rate"]
        tc = stats["trade_count"]
        pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
        at_risk = pnl < drop_pnl and wr < drop_wr
        status = "⚠️" if at_risk else "✅"
        text += f"{status} `{w[:8]}...{w[-6:]}`\n"
        text += f"   PnL: {pnl_str} | Win: {round(wr*100)}% | Trades: {tc}\n\n"
        buttons.append([InlineKeyboardButton(text=f"⚙️ Manage {w[:8]}...", callback_data=f"copy:manage:{c['id']}")])
    text += f"_⚠️ = at risk of being dropped ({risk} profile)_"
    buttons.append([InlineKeyboardButton(text="← Back", callback_data="smart_pilot:menu")])
    try:
        await callback.message.edit_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except: pass

async def check_arbitrage():
    """Alert users when YES + NO prices on a market don't sum to $1 (free money)."""
    try:
        markets = get_markets(limit=20)
        opps = []
        for m in markets:
            condition_id = m.get("conditionId")
            if not condition_id:
                continue
            try:
                tokens = get_clob_tokens(condition_id)
                if len(tokens) < 2:
                    continue
                yes_price = no_price = None
                for t in tokens:
                    p = float(t.get("price") or 0)
                    if t.get("outcome") == "Yes": yes_price = p
                    if t.get("outcome") == "No": no_price = p
                if yes_price is None or no_price is None:
                    continue
                total = yes_price + no_price
                # Arbitrage if total < 0.97 (buy both sides for profit) or > 1.03 (sell both)
                if total < 0.97:
                    profit_pct = round((1 - total) * 100, 2)
                    opps.append((m, yes_price, no_price, total, profit_pct, "buy_both"))
                elif total > 1.03:
                    profit_pct = round((total - 1) * 100, 2)
                    opps.append((m, yes_price, no_price, total, profit_pct, "overpriced"))
            except:
                continue

        if not opps:
            return

        # Find users with arb alerts enabled (all users for now)
        all_users = supabase.table("users").select("id").execute()
        for opp in opps[:3]:  # max 3 alerts per scan
            m, yes_p, no_p, total, profit_pct, opp_type = opp
            market_key = m.get("conditionId", "")
            if market_key in arb_alerted:
                continue
            arb_alerted.add(market_key)
            # Cap set size
            if len(arb_alerted) > 500:
                arb_alerted.clear()
            title = m.get("question") or m.get("title") or "Unknown market"
            if opp_type == "buy_both":
                detail = (
                    f"YES: *{round(yes_p*100)}¢* + NO: *{round(no_p*100)}¢* = *{round(total*100)}¢*\n"
                    f"Buy BOTH sides for a guaranteed *+{profit_pct}%* profit at resolution."
                )
            else:
                detail = (
                    f"YES: *{round(yes_p*100)}¢* + NO: *{round(no_p*100)}¢* = *{round(total*100)}¢*\n"
                    f"Market is overpriced by *{profit_pct}%* — prices should normalise soon."
                )
            text = (
                f"⚡ *Arbitrage Opportunity!*\n\n"
                f"_{title}_\n\n"
                f"{detail}\n\n"
                f"_Act fast — these gaps close quickly._"
            )
            for user in (all_users.data or []):
                try:
                    await bot.send_message(
                        user["id"], text, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="📈 Browse Markets", callback_data="menu:markets")]
                        ])
                    )
                    await asyncio.sleep(0.05)
                except:
                    pass
    except Exception as e:
        print(f"[arb] scan error: {e}")

# ─── Background jobs ──────────────────────────────────────────

async def check_auto_sells():
    try:
        active = supabase.table("auto_sells").select("*").eq("active", True).execute()
        for a in (active.data or []):
            try:
                r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={a['token_id']}", timeout=5)
                if not r.ok: continue
                mid = float(r.json().get("mid", 0) or 0)
                if mid <= 0 or mid < a["target_price"]: continue
                user = get_user(a["user_id"])
                if not user: continue
                private_key = decrypt_key(user["encrypted_key"])
                client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
                order_args = MarketOrderArgs(token_id=a["token_id"], amount=a["size"], side="SELL")
                signed_order = client.create_market_order(order_args)
                resp = client.post_order(signed_order, OrderType.FOK)
                if resp.get("success"):
                    supabase.table("auto_sells").update({"active": False}).eq("id", a["id"]).execute()
                    await bot.send_message(
                        user["id"],
                        f"🎯 *Auto-Sell Triggered!*\n\n"
                        f"_{a['title']}_\n"
                        f"Sold *{a['size']:.2f} shares* at *{round(mid * 100)}¢*\n"
                        f"Target was *{round(a['target_price'] * 100)}¢*",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💼 Portfolio", callback_data="menu:portfolio")]
                        ])
                    )
            except Exception as e:
                print(f"Auto-sell error: {e}")
    except Exception as e:
        print(f"Auto-sell loop error: {e}")

async def check_price_alerts():
    try:
        all_users = supabase.table("users").select("id, wallet_address").execute()
        for user in (all_users.data or []):
            try:
                pos = get_positions(user["wallet_address"])
                for p in pos:
                    token_id = p.get("asset")
                    if not token_id: continue
                    cur_price = float(p.get("curPrice", 0))
                    prev = last_prices.get(token_id)
                    if prev and prev > 0:
                        change = abs(cur_price - prev) / prev
                        if change >= 0.10:
                            direction = "📈 UP" if cur_price > prev else "📉 DOWN"
                            try:
                                await bot.send_message(
                                    user["id"],
                                    f"🔔 *Price Alert!*\n\n"
                                    f"_{p.get('title', 'Unknown')}_\n"
                                    f"Your *{p.get('outcome', '')}* moved {direction} {round(change * 100)}%\n"
                                    f"Now: *{round(cur_price * 100)}¢* (was {round(prev * 100)}¢)",
                                    parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                        [InlineKeyboardButton(text="💼 Portfolio", callback_data="menu:portfolio")]
                                    ])
                                )
                            except: pass
                    last_prices[token_id] = cur_price
                # Cap last_prices size to avoid unbounded memory growth
                if len(last_prices) > 2000:
                    oldest_keys = list(last_prices.keys())[:500]
                    for k in oldest_keys:
                        del last_prices[k]
                await asyncio.sleep(0.5)  # avoid hammering Polymarket API
            except Exception as e:
                print(f"Price alert user error: {e}")
    except Exception as e:
        print(f"Price alert error: {e}")

async def gas_relay_loop():
    try:
        if not RELAY_PRIVATE_KEY:
            return
        # Only fetch users who haven't been relayed recently — filter in DB
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=RELAY_COOLDOWN_HRS)).isoformat()
        all_users = supabase.table("users").select("*").or_(
            f"last_relay_at.is.null,last_relay_at.lt.{cutoff}"
        ).execute()
        for user in (all_users.data or []):
            try:
                # Quick POL balance check before doing anything
                pol = get_pol_balance(user["wallet_address"])
                if pol >= RELAY_THRESHOLD_POL:
                    continue
                await maybe_relay_gas(user)
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Relay loop user error: {e}")
    except Exception as e:
        print(f"Gas relay loop error: {e}")

async def execute_copy_trade(user, trade, copy_config):
    try:
        private_key = decrypt_key(user["encrypted_key"])
        bal = get_usdc_balance(user["wallet_address"])
        if bal < 1: return
        trade_type = trade.get("type", "")
        if trade_type not in ["BUY", "SELL"]: return
        asset = trade.get("asset")
        if not asset: return

        mode = copy_config.get("copy_mode") or "percent"
        w = copy_config["target_wallet"]

        # Watch mode — alert only, no trade
        if mode == "watch":
            title = trade.get("title", "Unknown market")
            side_emoji = "🟢" if trade_type == "BUY" else "🔴"
            try:
                await bot.send_message(
                    user["id"],
                    f"👁 *Wallet Alert!*\n\n"
                    f"`{w[:8]}...{w[-6:]}` just made a trade:\n\n"
                    f"{side_emoji} *{trade_type}* on _{title}_\n\n"
                    f"_You're watching this wallet — no trade was copied._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🤖 Copy Trade Menu", callback_data="menu:copy")]
                    ])
                )
            except: pass
            return

        # Category filter — skip trades not in selected category
        cat_filter = (copy_config.get("category_filter") or "All").lower()
        if cat_filter != "all":
            trade_cat = (trade.get("category") or trade.get("marketCategory") or "").lower()
            if trade_cat and cat_filter not in trade_cat:
                return

        # Max odds filter — skip if YES price is too high
        max_odds = float(copy_config.get("max_odds") or 0)
        if max_odds > 0 and trade_type == "BUY":
            trade_price = float(trade.get("price") or trade.get("avgPrice") or 0)
            if trade_price > max_odds:
                return

        # Fade mode — bet the opposite side
        actual_side = trade_type
        if mode == "fade":
            actual_side = "SELL" if trade_type == "BUY" else "BUY"

        # Handle SELL trades based on sell_mode setting
        sell_mode = copy_config.get("sell_mode") or "mirror"
        if trade_type == "SELL":
            if sell_mode == "ignore":
                return
            # For sell modes, we need to handle differently — sell from our own position
            if sell_mode in ["mirror", "full", "fixed"] and mode != "fade":
                positions = get_positions(user["wallet_address"])
                matching = [p for p in positions if p.get("asset") == asset]
                if not matching:
                    return  # We don't hold this position
                pos = matching[0]
                our_size = float(pos.get("size") or 0)
                if our_size <= 0:
                    return
                if sell_mode == "full":
                    sell_size = our_size
                elif sell_mode == "fixed":
                    sell_size = min(float(copy_config.get("fixed_amount") or 10) / max(float(pos.get("curPrice") or 0.5), 0.01), our_size)
                else:  # mirror — sell same % they sold
                    their_size = float(trade.get("size") or 0)
                    their_prev = their_size + float(trade.get("amount") or their_size)
                    sell_pct = (their_size / their_prev) if their_prev > 0 else 0.5
                    sell_size = our_size * sell_pct
                sell_size = max(round(sell_size, 4), 0.01)
                client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
                order_args = MarketOrderArgs(token_id=asset, amount=sell_size, side="SELL")
                signed_order = client.create_market_order(order_args)
                resp = client.post_order(signed_order, OrderType.FOK)
                if resp.get("success"):
                    await bot.send_message(
                        user["id"],
                        f"🤖 *Copy Sell!*\n\n"
                        f"Copying sell from: `{w[:8]}...{w[-6:]}`\n"
                        f"_{trade.get('title', 'Unknown')}_\n"
                        f"*SELL* — *{sell_size:.2f} shares*",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💼 Portfolio", callback_data="menu:portfolio")]
                        ])
                    )
                return

        # Smart copy — check win rate filter
        min_wr = copy_config.get("min_win_rate") or 0
        if min_wr > 0:
            win_rate = get_wallet_win_rate(copy_config["target_wallet"])
            if win_rate is not None and win_rate < min_wr:
                return

        # Minimum trade size filter
        min_size = float(copy_config.get("min_trade_size") or 0)
        if min_size > 0:
            trade_size = float(trade.get("size") or trade.get("amount") or 0)
            if trade_size < min_size:
                return

        # Weekly budget check
        budget = float(user.get("copy_budget") or 0)
        if budget > 0:
            used = float(user.get("copy_budget_used") or 0)
            if used >= budget:
                try:
                    await bot.send_message(
                        user["id"],
                        f"⚠️ *Weekly copy trade budget of ${budget:.0f} reached.*\n\nCopy trades paused until next week.",
                        parse_mode="Markdown"
                    )
                except: pass
                return

        if mode == "fixed":
            copy_amount = float(copy_config.get("fixed_amount") or 10)
        else:
            pct = copy_config.get("copy_percent") or 0.10
            max_t = copy_config.get("max_per_trade") or 50
            copy_amount = min(bal * pct, max_t)
        if copy_amount < 1 or copy_amount > bal: return
        client = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        order_args = MarketOrderArgs(token_id=asset, amount=copy_amount, side=actual_side)
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        if resp.get("success"):
            collect_fee(private_key, copy_amount, user_id=user["id"] if isinstance(user, dict) else None)
            # Update weekly budget usage
            if budget > 0:
                new_used = float(user.get("copy_budget_used") or 0) + copy_amount
                supabase.table("users").update({"copy_budget_used": new_used}).eq("id", user["id"]).execute()
            trade_label = "Fade Trade" if mode == "fade" else "Copy Trade"
            side_label = actual_side
            await bot.send_message(
                user["id"],
                f"🤖 *{trade_label}!*\n\n"
                f"{'Fading' if mode == 'fade' else 'Copying'}: `{w[:8]}...{w[-6:]}`\n"
                f"_{trade.get('title', 'Unknown')}_\n"
                f"*{side_label}* — *${copy_amount:.2f}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💼 Portfolio", callback_data="menu:portfolio")],
                    [InlineKeyboardButton(text="📤 Share", callback_data="copy:share")]
                ])
            )
    except Exception as e:
        print(f"Copy trade error: {e}")

# ─── Dome WebSocket copy trading ──────────────────────────────
dome_ws_active = False

async def start_dome_websocket():
    """Start real-time copy trading via Dome WebSocket. Only runs if copy trades are active."""
    global dome_ws_active
    if not dome:
        print("[dome] No Dome client, falling back to polling")
        return

    while True:
        try:
            # Check if there are any active copy trades before connecting
            active = supabase.table("copy_trades").select("id").eq("active", True).eq("paused", False).execute()
            if not active.data:
                await asyncio.sleep(60)  # nothing to watch, check again in 1 min
                continue

            ws_client = dome.polymarket.websocket
            await ws_client.connect()
            dome_ws_active = True
            print("[dome] WebSocket connected")

            async def refresh_subscriptions():
                try:
                    active = supabase.table("copy_trades").select("*").eq("active", True).eq("paused", False).execute()
                    wallets = list({c["target_wallet"] for c in (active.data or [])})
                    if not wallets:
                        return
                    def on_trade(event):
                        asyncio.create_task(handle_dome_trade_event(event))
                    await ws_client.subscribe(users=wallets, on_event=on_trade)
                    print(f"[dome] Subscribed to {len(wallets)} wallets")
                except Exception as e:
                    print(f"[dome] Subscription error: {e}")

            await refresh_subscriptions()

            # Refresh every 5 min to pick up new copy trades
            while dome_ws_active:
                await asyncio.sleep(300)
                await refresh_subscriptions()

        except Exception as e:
            dome_ws_active = False
            print(f"[dome] WebSocket error: {e} — reconnecting in 60s")
            await asyncio.sleep(60)

async def handle_dome_trade_event(event):
    """Handle real-time trade event from Dome WebSocket."""
    try:
        data = event.data
        wallet = getattr(data, "user", None)
        if not wallet:
            return
        trade_id = getattr(data, "order_hash", None) or getattr(data, "tx_hash", None)
        if not trade_id:
            return
        side = getattr(data, "side", None) or getattr(data, "type", "BUY")
        side = side.upper() if side else "BUY"
        if side not in ["BUY", "SELL"]:
            return
        token_id = getattr(data, "token_id", None)
        title = getattr(data, "market_slug", "Unknown market")
        if not token_id:
            return
        # Find all users copying this wallet
        active = supabase.table("copy_trades").select("*").eq("target_wallet", wallet.lower()).eq("active", True).eq("paused", False).execute()
        for copy in (active.data or []):
            uid = f"{copy['user_id']}_{trade_id}"
            if is_trade_seen(copy["user_id"], uid):
                continue
            mark_trade_seen(copy["user_id"], uid)
            user = get_user(copy["user_id"])
            if not user:
                continue
            trade = {"type": side, "asset": token_id, "title": title}
            await execute_copy_trade(user, trade, copy)
    except Exception as e:
        print(f"[dome] Trade event error: {e}")

async def copy_trade_loop():
    """Fallback polling loop — only runs if Dome WebSocket is not active."""
    if dome_ws_active:
        return  # WebSocket is handling it in real-time
    try:
        active = supabase.table("copy_trades").select("*").eq("active", True).eq("paused", False).execute()
        for copy in (active.data or []):
            user = get_user(copy["user_id"])
            if not user: continue
            trades = get_recent_trades(copy["target_wallet"], limit=5)
            for trade in trades:
                trade_id = trade.get("id") or trade.get("transactionHash")
                if not trade_id: continue
                uid = f"{copy['user_id']}_{trade_id}"
                if is_trade_seen(copy["user_id"], uid): continue
                mark_trade_seen(copy["user_id"], uid)
                await execute_copy_trade(user, trade, copy)
    except Exception as e:
        print(f"[copy] Loop error: {e}")

# ─── Slash shortcuts ──────────────────────────────────────────

@router.message(Command("menu"))
async def cmd_menu(message: Message):
    user = get_user(message.from_user.id)
    bal = get_usdc_balance(user["wallet_address"]) if user else 0
    pnl = get_daily_pnl(user["wallet_address"]) if user else 0
    pnl_str = f"📈 +${pnl:.2f}" if pnl >= 0 else f"📉 -${abs(pnl):.2f}"
    await message.answer(
        f"💰 *${bal:.2f}* USDC.e  {pnl_str} PnL\n\nWhat would you like to do?",
        parse_mode="Markdown", reply_markup=main_menu()
    )

@router.message(Command("markets"))
async def cmd_markets(message: Message):
    await message.answer("📈 *Fetching markets...*", parse_mode="Markdown")
    markets = get_markets(limit=5)
    for m in markets:
        condition_id = m.get("conditionId")
        tokens = get_clob_tokens(condition_id) if condition_id else []
        card, _, _ = format_market_card(m, tokens)
        await message.answer(card, parse_mode="Markdown", reply_markup=get_trade_keyboard(tokens))
    await message.answer("_Tap Yes or No to trade_", reply_markup=back_to_menu())

# ─── Wallet Analytics Report ─────────────────────────────────

@router.callback_query(F.data == "menu:analytics")
async def cb_analytics_prompt(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(TradeStates.waiting_for_analytics_wallet)
    await callback.message.edit_text(
        "📊 *Wallet Analytics*\n\n"
        "Paste any Polymarket wallet address to get a full performance report:\n\n"
        "_Win rate, PnL, best categories, trade history and more._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Analyse My Wallet", callback_data="analytics:self")],
            [InlineKeyboardButton(text="✕ Cancel", callback_data="menu:main")]
        ])
    )

@router.callback_query(F.data == "analytics:self")
async def cb_analytics_self(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = get_user(callback.from_user.id)
    if not user:
        await safe_answer(callback, "No wallet found.", show_alert=True)
        return
    await safe_answer(callback)
    await _run_wallet_analytics(callback.message, user["wallet_address"], edit=True)

@router.message(TradeStates.waiting_for_analytics_wallet)
async def handle_analytics_wallet(message: Message, state: FSMContext):
    await state.clear()
    wallet = message.text.strip()
    if not wallet.startswith("0x") or len(wallet) != 42:
        await message.answer("❌ Invalid wallet address. Must start with 0x and be 42 characters.")
        return
    await _run_wallet_analytics(message, wallet, edit=False)

async def _run_wallet_analytics(msg, wallet: str, edit: bool = False):
    loading_text = f"📊 *Analysing wallet...*\n\n`{wallet[:8]}...{wallet[-6:]}`\n\n_This may take a moment._"
    if edit:
        await msg.edit_text(loading_text, parse_mode="Markdown")
    else:
        await msg.answer(loading_text, parse_mode="Markdown")
    try:
        # Fetch trades
        trades = []
        try:
            r = requests.get(f"https://data-api.polymarket.com/activity?user={wallet}&limit=100", timeout=8)
            if r.ok: trades = r.json() or []
        except: pass

        # Fetch positions
        positions = []
        try:
            r2 = requests.get(f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.01", timeout=8)
            if r2.ok: positions = r2.json() or []
        except: pass

        if not trades and not positions:
            text = f"😕 *No data found*\n\n`{wallet[:8]}...{wallet[-6:]}`\n\nThis wallet has no recorded activity on Polymarket."
            if edit:
                await msg.edit_text(text, parse_mode="Markdown", reply_markup=back_to_menu())
            else:
                await msg.answer(text, parse_mode="Markdown", reply_markup=back_to_menu())
            return

        # Compute stats
        buys = [t for t in trades if t.get("type") == "BUY"]
        sells = [t for t in trades if t.get("type") == "SELL"]
        total_trades = len(trades)

        # PnL from positions
        total_invested = 0
        total_current = 0
        wins = losses = 0
        category_pnl = {}
        for p in positions:
            try:
                invested = float(p.get("cashBalanceDelta") or 0)
                current = float(p.get("currentValue") or 0)
                pnl = current - abs(invested)
                total_invested += abs(invested)
                total_current += current
                if pnl > 0: wins += 1
                else: losses += 1
                cat = p.get("marketCategory") or "Other"
                category_pnl[cat] = category_pnl.get(cat, 0) + pnl
            except: pass

        total_pnl = total_current - total_invested
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        # Average position size
        amounts = []
        for t in buys:
            try: amounts.append(float(t.get("usdcSize") or t.get("size") or 0))
            except: pass
        avg_size = sum(amounts) / len(amounts) if amounts else 0

        # Best and worst categories
        sorted_cats = sorted(category_pnl.items(), key=lambda x: x[1], reverse=True)
        best_cat = sorted_cats[0] if sorted_cats else None
        worst_cat = sorted_cats[-1] if len(sorted_cats) > 1 else None

        # Open positions value
        open_value = sum(float(p.get("currentValue") or 0) for p in positions)

        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"

        text = (
            f"📊 *Wallet Analytics*\n\n"
            f"`{wallet[:8]}...{wallet[-6:]}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Total trades: *{total_trades}*\n"
            f"🟢 Winning positions: *{wins}*\n"
            f"🔴 Losing positions: *{losses}*\n"
            f"🎯 Win rate: *{win_rate:.1f}%*\n"
            f"💵 Avg trade size: *${avg_size:.2f}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{pnl_emoji} Total PnL: *{pnl_str}*\n"
            f"💼 Open positions value: *${open_value:.2f}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        if best_cat:
            text += f"🏆 Best category: *{best_cat[0]}* (+${best_cat[1]:.2f})\n"
        if worst_cat:
            text += f"📉 Worst category: *{worst_cat[0]}* (${worst_cat[1]:.2f})\n"
        text += f"\n_Based on last 100 trades & open positions._"

        buttons = [
            [InlineKeyboardButton(text="🤖 Copy This Trader", callback_data=f"copy:follow_top:{wallet}")],
            [InlineKeyboardButton(text="← Back to Menu", callback_data="menu:main")]
        ]
        if edit:
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        else:
            await msg.answer(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        print(f"[analytics] Error: {e}")
        err = "❌ Failed to load analytics. Try again later."
        if edit:
            await msg.edit_text(err, reply_markup=back_to_menu())
        else:
            await msg.answer(err, reply_markup=back_to_menu())

def extract_polymarket_slug(text: str) -> str | None:
    """Extract market slug from a Polymarket URL."""
    import re
    # Matches: polymarket.com/event/some-slug or polymarket.com/market/some-slug
    match = re.search(r'polymarket\.com/(?:event|market)/([a-zA-Z0-9_-]+)', text)
    return match.group(1) if match else None

async def fetch_market_by_slug(slug: str) -> dict | None:
    """Fetch a market from Gamma API by slug."""
    try:
        # Try event endpoint first
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}&limit=1", timeout=6)
        if r.ok and r.json():
            event = r.json()[0]
            markets = event.get("markets", [])
            if markets:
                m = markets[0]
                if not m.get("conditionId"):
                    m["conditionId"] = m.get("condition_id", "")
                if not m.get("question"):
                    m["question"] = event.get("title", "Unknown")
                return m
        # Fallback: try market search by slug
        r2 = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}&limit=1", timeout=6)
        if r2.ok and r2.json():
            return r2.json()[0]
        return None
    except Exception as e:
        print(f"[paste-to-trade] fetch error: {e}")
        return None

@router.message(F.text.contains("polymarket.com"))
async def handle_polymarket_url(message: Message, state: FSMContext):
    """Detect pasted Polymarket URLs and instantly render the trade card."""
    # Don't intercept if user is in a state waiting for input
    current_state = await state.get_state()
    if current_state:
        return
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("⚠️ Please /start first to set up your wallet.")
        return
    slug = extract_polymarket_slug(message.text)
    if not slug:
        return
    loading = await message.answer("🔍 *Loading market...*", parse_mode="Markdown")
    market = await fetch_market_by_slug(slug)
    try:
        await loading.delete()
    except:
        pass
    if not market:
        await message.answer(
            "❌ *Market not found.*\n\n_Make sure it's an active Polymarket event URL._",
            parse_mode="Markdown",
            reply_markup=back_to_menu()
        )
        return
    condition_id = market.get("conditionId", "")
    tokens = get_clob_tokens(condition_id) if condition_id else []
    if not tokens:
        await message.answer(
            "❌ *Market not tradeable.*\n\n_This market may be closed or resolved._",
            parse_mode="Markdown",
            reply_markup=back_to_menu()
        )
        return
    bal = get_usdc_balance(user["wallet_address"])
    card, _, _ = format_market_card(market, tokens)
    await message.answer(
        f"🔗 *Paste-to-Trade*\n\n{card}",
        parse_mode="Markdown",
        reply_markup=get_trade_keyboard(tokens, show_quick_bet=True, bal=bal, condition_id=condition_id)
    )

async def check_deposits():
    """Detect new USDC.e deposits and notify users."""
    try:
        all_users = supabase.table("users").select("id, wallet_address, last_known_balance, referred_by, referral_bonus_paid").execute()
        for user in (all_users.data or []):
            try:
                current_bal = get_usdc_balance(user["wallet_address"])
                last_bal = float(user.get("last_known_balance") or 0)
                if current_bal > last_bal + 0.5:
                    deposited = current_bal - last_bal
                    try:
                        await bot.send_message(
                            user["id"],
                            f"💸 *Deposit Received!*\n\n"
                            f"*+${deposited:.2f} USDC.e* has arrived in your wallet.\n"
                            f"New balance: *${current_bal:.2f} USDC.e*\n\n"
                            f"Ready to trade!",
                            parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📈 Browse Markets", callback_data="menu:markets")],
                                [InlineKeyboardButton(text="🏠 Menu", callback_data="menu:main")]
                            ])
                        )
                    except:
                        pass
                    # First deposit bonus — $1 USDC.e for referred users who deposit $10+
                    if (
                        current_bal >= 10 and
                        user.get("referred_by") and
                        not user.get("referral_bonus_paid")
                    ):
                        try:
                            # Send $1 bonus from relay wallet
                            relay_account = get_w3().eth.account.from_key(RELAY_PRIVATE_KEY) if RELAY_PRIVATE_KEY else None
                            if relay_account:
                                w3 = get_w3()
                                contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
                                nonce = w3.eth.get_transaction_count(relay_account.address)
                                base_fee = get_base_fee(w3)
                                tx = {
                                    'from': relay_account.address, 'to': USDC_E, 'nonce': nonce, 'gas': 100000,
                                    'gasPrice': 150_000_000_000, 'chainId': 137,
                                    'data': contract.encode_abi('transfer', [Web3.to_checksum_address(user["wallet_address"]), 1_000_000]),
                                }
                                signed = w3.eth.account.sign_transaction(tx, RELAY_PRIVATE_KEY)
                                w3.eth.send_raw_transaction(signed.raw_transaction)
                                supabase.table("users").update({"referral_bonus_paid": True}).eq("id", user["id"]).execute()
                                await bot.send_message(
                                    user["id"],
                                    "🎁 *Referral Bonus!*\n\n"
                                    "*$1.00 USDC.e* has been added to your wallet as a welcome gift!\n\n"
                                    "_Thanks for joining via a referral link._",
                                    parse_mode="Markdown"
                                )
                        except Exception as e:
                            print(f"[referral] bonus error: {e}")
                supabase.table("users").update({"last_known_balance": current_bal}).eq("id", user["id"]).execute()
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"Deposit check error for user: {e}")
    except Exception as e:
        print(f"Deposit loop error: {e}")

async def cleanup_stale_data():
    """Purge old seen_trades rows and cap in-memory stores to prevent unbounded growth."""
    try:
        # Delete seen_trades older than 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        supabase.table("seen_trades").delete().lt("created_at", cutoff).execute()
    except Exception as e:
        print(f"[cleanup] seen_trades error: {e}")
    try:
        # Reset weekly copy budget used every Monday
        if datetime.now(timezone.utc).weekday() == 0:
            supabase.table("users").update({"copy_budget_used": 0}).gt("copy_budget", 0).execute()
            print("[cleanup] Weekly copy budgets reset")
    except Exception as e:
        print(f"[cleanup] budget reset error: {e}")
    try:
        # Cap token_store at 1000 entries
        if len(token_store) > 1000:
            keys = list(token_store.keys())[:len(token_store) - 1000]
            for k in keys:
                del token_store[k]
    except: pass
    try:
        # Cap position_store at 500 entries
        if len(position_store) > 500:
            keys = list(position_store.keys())[:len(position_store) - 500]
            for k in keys:
                del position_store[k]
    except: pass
    print(f"[cleanup] done — token_store={len(token_store)} position_store={len(position_store)}")

def get_wallet_win_rate(wallet_address):
    """Get accurate win rate using Dome's wallet PnL endpoint."""
    try:
        if dome:
            pnl_data = dome.polymarket.wallet.get_wallet_pnl({"wallet_address": wallet_address})
            points = getattr(pnl_data, "pnl_over_time", [])
            if points:
                wins = sum(1 for p in points if float(getattr(p, "pnl", 0) or 0) > 0)
                total = len(points)
                return (wins / total * 100) if total > 0 else None
        # Fallback to raw activity API
        trades = get_recent_trades(wallet_address, limit=50)
        if not trades:
            return None
        wins = sum(1 for t in trades if float(t.get("cashPnl", 0) or 0) > 0)
        total = len(trades)
        return (wins / total * 100) if total > 0 else None
    except:
        return None

async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Open PolyRift"),
        BotCommand(command="menu", description="Main menu"),
        BotCommand(command="markets", description="Trending markets"),
    ])
    scheduler.add_job(copy_trade_loop, 'interval', minutes=1, misfire_grace_time=30)
    scheduler.add_job(check_auto_sells, 'interval', minutes=2, misfire_grace_time=30)
    scheduler.add_job(check_price_alerts, 'interval', minutes=10, misfire_grace_time=60)
    scheduler.add_job(gas_relay_loop, 'interval', minutes=30, misfire_grace_time=120)
    scheduler.add_job(check_deposits, 'interval', minutes=3, misfire_grace_time=60)
    scheduler.add_job(cleanup_stale_data, 'interval', hours=6, misfire_grace_time=300)
    scheduler.add_job(pay_referral_earnings, 'interval', hours=24, misfire_grace_time=300)
    scheduler.add_job(sweep_fee_wallet, 'interval', hours=12, misfire_grace_time=300)
    scheduler.add_job(check_expiring_markets, 'interval', hours=1, misfire_grace_time=300)
    scheduler.add_job(check_arbitrage, 'interval', minutes=30, misfire_grace_time=120)
    scheduler.add_job(run_smart_pilot, 'interval', hours=6, misfire_grace_time=600)
    scheduler.start()
    # Start Dome WebSocket for real-time copy trading
    asyncio.create_task(start_dome_websocket())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
