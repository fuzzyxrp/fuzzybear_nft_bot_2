# bot2.py
import os
import time
import json
import html
import io
import urllib.parse
from collections import deque

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===============================
# Config (env)
# ===============================
BITHOMP_API_TOKEN = os.getenv("BITHOMP_API_TOKEN")
XRPL_NFT_ISSUER   = os.getenv("FUZZYBEAR_ISSUER_ADDRESS")  # set this to the 2nd collection's issuer in this Railway project
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("GROUP_CHAT_ID")
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL", "30"))
XRPL_RPC_URL       = os.getenv("XRPL_RPC_URL") or "https://s1.ripple.com:51234/"
STATE_PATH         = os.getenv("STATE_PATH", "/mnt/data/state.json")

if not (BITHOMP_API_TOKEN and XRPL_NFT_ISSUER and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
    raise RuntimeError("Missing required env vars: BITHOMP_API_TOKEN, FUZZYBEAR_ISSUER_ADDRESS, TELEGRAM_BOT_TOKEN, GROUP_CHAT_ID")

# ===============================
# Endpoints & session with retry
# ===============================
BITHOMP_SALES_URL = "https://bithomp.com/api/v2/nft-sales"
BITHOMP_NFT_URL   = "https://bithomp.com/api/v2/nft/{}"

def make_session():
    s = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"x-bithomp-token": BITHOMP_API_TOKEN})
    return s

SESSION = make_session()

# ===============================
# State (seen tx hashes, mints)
# ===============================
MAX_SEEN = 2000

def load_state():
    if not STATE_PATH:
        return {"seen_sales": [], "seen_mints": []}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"seen_sales": [], "seen_mints": []}

def save_state(state):
    if not STATE_PATH:
        return
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Warning: failed to persist state: {e}")

STATE = load_state()
seen_sales = deque(STATE.get("seen_sales", []), maxlen=MAX_SEEN)
seen_mints = deque(STATE.get("seen_mints", []), maxlen=MAX_SEEN)
seen_sales_set = set(seen_sales)
seen_mints_set = set(seen_mints)

def remember_sale(tx_hash):
    if tx_hash not in seen_sales_set:
        seen_sales.append(tx_hash)
        seen_sales_set.add(tx_hash)
        if len(seen_sales) > MAX_SEEN:
            while len(seen_sales) > MAX_SEEN:
                popped = seen_sales.popleft()
                seen_sales_set.discard(popped)

def remember_mint(tx_hash):
    if tx_hash not in seen_mints_set:
        seen_mints.append(tx_hash)
        seen_mints_set.add(tx_hash)
        if len(seen_mints) > MAX_SEEN:
            while len(seen_mints) > MAX_SEEN:
                popped = seen_mints.popleft()
                seen_mints_set.discard(popped)

def persist_now():
    STATE["seen_sales"] = list(seen_sales)
    STATE["seen_mints"] = list(seen_mints)
    save_state(STATE)

# ===============================
# Helpers
# ===============================
def abbr(text, length=5):
    if text and len(text) > length:
        return text[:length] + "..."
    return text or "N/A"

def decode_uri(hex_uri: str):
    try:
        uri_bytes = bytes.fromhex(hex_uri)
        uri = uri_bytes.decode("utf-8", errors="ignore").strip("\x00")
    except Exception:
        return None
    uri = urllib.parse.quote(uri, safe=":/?&=%")
    if uri.startswith("ipfs://"):
        cid = uri[len("ipfs://"):]
        return f"https://ipfs.io/ipfs/{cid}"
    return uri

def fetch_metadata(uri: str):
    try:
        r = SESSION.get(uri, timeout=20)
        if "application/json" in r.headers.get("Content-Type", "") or r.text.strip().startswith("{"):
            return r.json()
    except Exception as e:
        print(f"Error fetching metadata from {uri}: {e}")
    return None

def fetch_image_bytes(url: str):
    try:
        r = SESSION.get(url, timeout=25)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"Error fetching image from {url}: {e}")
        return None

def send_telegram(text: str, image_url: str | None = None):
    def _post_json(endpoint, payload, files=None):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{endpoint}"
        try:
            if files:
                resp = requests.post(url, data=payload, files=files, timeout=30)
            else:
                resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 429:
                try:
                    data = resp.json()
                    ra = data.get("parameters", {}).get("retry_after", 10)
                except Exception:
                    ra = 10
                print(f"Telegram 429: retrying after {ra}s")
                time.sleep(int(ra) + 1)
                if files:
                    resp = requests.post(url, data=payload, files=files, timeout=30)
                else:
                    resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code != 200:
                print(f"Telegram API error: {resp.status_code} - {resp.text}")
            return resp
        except Exception as e:
            print(f"Telegram send error: {e}")

    if image_url:
        image_url = image_url.replace("#", "%23")
        img = fetch_image_bytes(image_url)
        if img:
            files = {"photo": ("nft_image.jpg", img)}
            payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": text, "parse_mode": "HTML"}
            _post_json("sendPhoto", payload, files=files)
            return
        else:
            print("Image download failed; sending text only.")

    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    _post_json("sendMessage", payload)

# ===============================
# Core
# ===============================
def seed_seen_sales():
    params = {"list": "lastSold", "issuer": XRPL_NFT_ISSUER, "saleType": "all", "period": "all"}
    try:
        r = SESSION.get(BITHOMP_SALES_URL, params=params, timeout=25)
        r.raise_for_status()
        sales = r.json().get("sales", [])
        for s in sales:
            txh = s.get("acceptedTxHash")
            if txh:
                remember_sale(txh)
        persist_now()
        print(f"Seeded seen_sales with {len(sales)} current sales. (No posts on seed)")
    except Exception as e:
        print(f"Failed to seed seen sales: {e}")

def poll_sales():
    params = {"list": "lastSold", "issuer": XRPL_NFT_ISSUER, "saleType": "all", "period": "all"}
    try:
        r = SESSION.get(BITHOMP_SALES_URL, params=params, timeout=25)
        r.raise_for_status()
        sales = r.json().get("sales", [])
        if not sales:
            return

        for sale in reversed(sales):
            tx_hash = sale.get("acceptedTxHash")
            if not tx_hash:
                continue
            if tx_hash in seen_sales_set:
                continue

            nft = sale.get("nftoken", {})
            buyer = sale.get("buyer")
            seller = sale.get("seller")

            amount_str = sale.get("amount")
            try:
                amount_drops = int(amount_str) if amount_str else 0
                price_xrp = amount_drops / 1_000_000
            except Exception:
                price_xrp = 0
            price_str = f"{int(price_xrp)} XRP" if float(price_xrp).is_integer() else f"{price_xrp:.2f} XRP"

            accepted_at = sale.get("acceptedAt")
            if accepted_at:
                try:
                    tx_timestamp = int(accepted_at)
                    utc_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(tx_timestamp))
                except Exception:
                    utc_time = "N/A"
            else:
                utc_time = "N/A"

            item_name = None
            image_url = None
            uri_hex = nft.get("uri")
            if uri_hex:
                uri = decode_uri(uri_hex)
                if uri:
                    meta = fetch_metadata(uri)
                    if meta:
                        item_name = meta.get("name")
                        img_link = meta.get("image") or meta.get("image_url") or meta.get("imageUrl")
                        if img_link:
                            image_url = f"https://ipfs.io/ipfs/{img_link[7:]}" if img_link.startswith("ipfs://") else img_link
                    else:
                        image_url = uri
            if not item_name:
                item_name = abbr(nft.get("nftokenID"))
            safe_item_name = html.escape(item_name)

            nft_id = nft.get("nftokenID")
            nft_link = f"https://bithomp.com/en/nft/{nft_id}" if nft_id else ""
            buyer_link = f"https://xrpscan.com/account/{buyer}" if buyer else ""
            seller_link = f"https://xrpscan.com/account/{seller}" if seller else ""
            tx_link = f"https://bithomp.com/explorer/{tx_hash}" if tx_hash else ""
            buyer_abbr = abbr(buyer)
            seller_abbr = abbr(seller)
            tx_abbr = abbr(tx_hash)

            if image_url and "#" in image_url:
                image_url = image_url.replace("#", "%23")

            # Mirrored copy
            message = (
                "🚀 <b>!YUB TFN WEN</b>\n\n"
                f"🏷️ <b>METI:</b> <a href=\"{nft_link}\">{safe_item_name}</a>\n"
                f"💰 <b>ROF DLOS:</b> {price_str}\n"
                f"🔄 <b>RELLES:</b> <a href=\"{seller_link}\">{seller_abbr}</a>\n"
                f"➡️ <b>REYUB:</b> <a href=\"{buyer_link}\">{buyer_abbr}</a>\n"
                f"⏱️ <b>EMIT NOITCASNART:</b> {utc_time}\n"
                f"📑 <b>DI NOITCASNART:</b> <a href=\"{tx_link}\">{tx_abbr}</a>"
            )
            send_telegram(message, image_url=image_url)

            remember_sale(tx_hash)
            persist_now()
            print(f"Notified sale {tx_hash}: {price_str}, buyer {buyer_abbr}, seller {seller_abbr}")

    except Exception as e:
        print(f"Error processing sales: {e}")

def poll_mints():
    payload = {
        "method": "account_tx",
        "params": [{
            "account": XRPL_NFT_ISSUER,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "limit": 50,
            "forward": False
        }]
    }
    try:
        r = SESSION.post(XRPL_RPC_URL, json=payload, timeout=25)
        r.raise_for_status()
        txs = r.json().get("result", {}).get("transactions", []) or []
        if not txs:
            return

        for entry in reversed(txs):
            tx_obj = entry.get("tx", {})
            if tx_obj.get("TransactionType") != "NFTokenMint":
                continue
            tx_hash = tx_obj.get("hash")
            if not tx_hash or tx_hash in seen_mints_set:
                continue

            timestamp = tx_obj.get("date")
            utc_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp + 946684800)) if timestamp else "N/A"

            nft_id = tx_obj.get("NFTokenID")
            uri_hex = tx_obj.get("URI")
            item_name = None
            image_url = None
            if uri_hex:
                uri = decode_uri(uri_hex)
                if uri:
                    meta = fetch_metadata(uri)
                    if meta:
                        item_name = meta.get("name")
                        img_link = meta.get("image") or meta.get("image_url") or meta.get("imageUrl")
                        if img_link:
                            image_url = f"https://ipfs.io/ipfs/{img_link[7:]}" if img_link.startswith("ipfs://") else img_link
                    else:
                        image_url = uri
            if not item_name:
                item_name = "Unknown NFT"
            safe_item_name = html.escape(item_name)

            nft_link = f"https://bithomp.com/en/nft/{nft_id}" if nft_id else ""
            tx_link  = f"https://bithomp.com/explorer/{tx_hash}" if tx_hash else ""
            tx_abbr  = abbr(tx_hash)

            if image_url and "#" in image_url:
                image_url = image_url.replace("#", "%23")

            # Mirrored mint copy (no price line)
            message = (
                "🚀 <b>!TNIM TFN WEN</b>\n\n"
                "🖼️ <b>EMAN NOITCELLOC:</b> sraebyzzuF\n"
                f"🏷️ <b>METI:</b> <a href=\"{nft_link}\">{safe_item_name}</a>\n"
                f"⏱️ <b>EMIT NOITCASNART:</b> {utc_time}\n"
                f"📑 <b>DI NOITCASNART:</b> <a href=\"{tx_link}\">{tx_abbr}</a>"
            )
            send_telegram(message, image_url=image_url)

            remember_mint(tx_hash)
            persist_now()
            print(f"Notified mint {tx_hash}: item name: {safe_item_name}")

    except Exception as e:
        print(f"Error polling mints: {e}")

# ===============================
# Boot
# ===============================
print("Starting NFT sales tracker (Telegram Bot #2)...")
print(f"Tracking issuer: {XRPL_NFT_ISSUER}")
seed_seen_sales()

while True:
    poll_sales()
    poll_mints()
    time.sleep(POLL_INTERVAL)
