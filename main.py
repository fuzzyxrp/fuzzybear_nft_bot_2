import os
import time
import requests
import json
import urllib.parse
import html
from collections import deque
from datetime import datetime, timezone, timedelta

# === Configuration (from environment variables) ===
BITHOMP_API_TOKEN = os.getenv("BITHOMP_API_TOKEN")
XRPL_NFT_ISSUER = os.getenv("SECOND_COLLECTION_ISSUER")  # <-- Collection B (IMPORTANT)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GROUP_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
XRPL_RPC_URL = os.getenv("XRPL_RPC_URL") or "https://s1.ripple.com:51234/"

# Guardrails (tweak via env)
AGE_CUTOFF_MIN = int(os.getenv("AGE_CUTOFF_MIN", "120"))
ALLOW_BACKFILL = os.getenv("ALLOW_BACKFILL", "0") == "1"
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
MAX_DEDUP = int(os.getenv("MAX_DEDUP", "8000"))
MAX_ERRORS = int(os.getenv("MAX_ERRORS", "5"))

# === Bithomp API endpoint for NFT sales ===
BITHOMP_API_URL = "https://xrplexplorer.com/api/v2/nft-sales"
sales_params = {
    "list": "lastSold",
    "issuer": XRPL_NFT_ISSUER,
    "saleType": "all",
    "period": "all"
}

# === Global state ===
last_seen_sale_tx = None
last_seen_mint_tx = None

# Rolling de-dup
seen_sales = deque(maxlen=MAX_DEDUP)
seen_sales_set = set()
seen_mints = deque(maxlen=MAX_DEDUP)
seen_mints_set = set()

errors_sales = 0
errors_mints = 0

def _remember(dq, s, h):
    if not h or h in s:
        return
    if len(dq) == dq.maxlen and dq.maxlen > 0:
        old = dq.popleft()
        if old in s:
            s.remove(old)
    dq.append(h)
    s.add(h)

def _now():
    return datetime.now(timezone.utc)

def _maybe_sleep(error_count):
    if error_count >= MAX_ERRORS:
        time.sleep(60)
    else:
        time.sleep(min(60, 2 ** error_count))

# === Utils ===
def decode_uri(hex_uri: str) -> str | None:
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

def fetch_metadata(uri: str) -> dict | None:
    try:
        r = requests.get(uri, timeout=REQUEST_TIMEOUT)
        if "application/json" in r.headers.get("Content-Type", "") or r.text.strip().startswith("{"):
            return r.json()
    except Exception as e:
        print(f"Error fetching metadata from {uri}: {e}")
    return None

def fetch_image_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"Error fetching image from {url}: {e}")
        return None

def send_telegram_message(text: str, image_url: str = None):
    if image_url:
        image_url = image_url.replace("#", "%23")
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        image_bytes = fetch_image_bytes(image_url)
        if image_bytes:
            files = {"photo": ("nft_image.jpg", image_bytes)}
            payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": text, "parse_mode": "HTML"}
            try:
                resp = requests.post(api_url, data=payload, files=files, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    print(f"Telegram API error (sendPhoto): {resp.status_code} - {resp.text}")
                return
            except Exception as e:
                print(f"Error sending Telegram photo: {e}")
        else:
            print("Failed to download image; sending text-only message.")
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(api_url, data=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"Telegram API error (sendMessage): {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def abbr(text, length=5):
    if text and len(text) > length:
        return text[:length] + "..."
    return text or "N/A"

# === Pollers ===
def poll_sales():
    global last_seen_sale_tx, errors_sales
    try:
        resp = requests.get(
            BITHOMP_API_URL, params=sales_params,
            headers={"x-bithomp-token": BITHOMP_API_TOKEN},
            timeout=REQUEST_TIMEOUT
        )
        if resp.status_code != 200:
            print(f"Bithomp sales API error: {resp.status_code} - {resp.text}")
            errors_sales += 1
            _maybe_sleep(errors_sales)
            return

        data = resp.json()
        sales = data.get("sales", [])
        if not sales:
            errors_sales = 0
            return

        def _t(s):
            try:
                return int(s.get("acceptedAt", 0))
            except Exception:
                return 0
        sales.sort(key=_t)

        if last_seen_sale_tx is None:
            head = sales[-1] if sales else None
            if head:
                last_seen_sale_tx = head.get("acceptedTxHash")
                _remember(seen_sales, seen_sales_set, last_seen_sale_tx)
                print(f"Initialized sales backlog: {len(sales)} sales skipped. Anchor={last_seen_sale_tx}")
            errors_sales = 0
            return

        age_cutoff = timedelta(minutes=AGE_CUTOFF_MIN)
        now = _now()

        new_events = []
        for s in sales:
            h = s.get("acceptedTxHash")
            if h == last_seen_sale_tx:
                new_events = []
                continue
            new_events.append(s)

        for sale in new_events:
            tx_hash = sale.get("acceptedTxHash") or ""
            if not tx_hash or tx_hash in seen_sales_set:
                continue

            accepted_at = sale.get("acceptedAt")
            event_dt = datetime.fromtimestamp(int(accepted_at), tz=timezone.utc) if accepted_at else None
            too_old = bool(event_dt and (now - event_dt) > age_cutoff)
            if too_old and not ALLOW_BACKFILL:
                _remember(seen_sales, seen_sales_set, tx_hash)
                last_seen_sale_tx = tx_hash
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

            utc_time_str = event_dt.strftime("%Y-%m-%d %H:%M:%S") if event_dt else "N/A"

            # Metadata
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
                            image_url = f"https://ipfs.io/ipfs/{img_link[len('ipfs://'):]}" if img_link.startswith("ipfs://") else img_link
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

            # Mirrored copy for this bot
            message = (
                "🚀 <b>!YUB TFN WEN</b>\n\n"
                f"🏷️ <b>METI:</b> <a href=\"{nft_link}\">{safe_item_name}</a>\n"
                f"💰 <b>ROF DLOS:</b> {price_str}\n"
                f"🔄 <b>RELLES:</b> <a href=\"{seller_link}\">{seller_abbr}</a>\n"
                f"➡️ <b>REYUB:</b> <a href=\"{buyer_link}\">{buyer_abbr}</a>\n"
                f"⏱️ <b>EMIT NOITCASNART:</b> {utc_time_str}\n"
                f"📑 <b>DI NOITCASNART:</b> <a href=\"{tx_link}\">{tx_abbr}</a>"
            )
            send_telegram_message(message, image_url=image_url)
            print(f"Notified sale {tx_hash}: {price_str}, buyer {buyer_abbr}, seller {seller_abbr}")

            _remember(seen_sales, seen_sales_set, tx_hash)
            last_seen_sale_tx = tx_hash

        errors_sales = 0

    except requests.Timeout:
        errors_sales += 1
        print("Error processing sales: read timeout.")
        _maybe_sleep(errors_sales)
    except Exception as e:
        errors_sales += 1
        print(f"Error processing sales: {e}")
        _maybe_sleep(errors_sales)

def poll_mints():
    global last_seen_mint_tx, errors_mints
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
        resp = requests.post(XRPL_RPC_URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        txs = data.get("result", {}).get("transactions", [])
        if not txs:
            errors_mints = 0
            return

        mints = []
        for entry in txs:
            tx_obj = entry.get("tx", {})
            if tx_obj.get("TransactionType") == "NFTokenMint":
                mints.append(tx_obj)
        mints.sort(key=lambda t: t.get("date", 0))

        if last_seen_mint_tx is None:
            head = mints[-1] if mints else None
            if head:
                last_seen_mint_tx = head.get("hash")
                _remember(seen_mints, seen_mints_set, last_seen_mint_tx)
                print(f"Initialized mint backlog: {len(mints)} txs skipped. Anchor={last_seen_mint_tx}")
            errors_mints = 0
            return

        age_cutoff = timedelta(minutes=AGE_CUTOFF_MIN)
        now = _now()

        new_events = []
        for tx in mints:
            h = tx.get("hash")
            if h == last_seen_mint_tx:
                new_events = []
                continue
            new_events.append(tx)

        for tx in new_events:
            tx_hash = tx.get("hash") or ""
            if not tx_hash or tx_hash in seen_mints_set:
                continue

            ts_ripple = tx.get("date")
            event_dt = datetime.fromtimestamp(ts_ripple + 946684800, tz=timezone.utc) if ts_ripple else None
            too_old = bool(event_dt and (now - event_dt) > age_cutoff)
            if too_old and not ALLOW_BACKFILL:
                _remember(seen_mints, seen_mints_set, tx_hash)
                last_seen_mint_tx = tx_hash
                continue

            utc_time = event_dt.strftime("%Y-%m-%d %H:%M:%S") if event_dt else "N/A"
            nft_id = tx.get("NFTokenID")
            uri_hex = tx.get("URI")

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
                            image_url = f"https://ipfs.io/ipfs/{img_link[len('ipfs://'):]}" if img_link.startswith("ipfs://") else img_link
                    else:
                        image_url = uri

            if not item_name:
                item_name = "Unknown NFT"
            safe_item_name = html.escape(item_name)

            nft_link = f"https://bithomp.com/en/nft/{nft_id}" if nft_id else ""
            tx_link = f"https://bithomp.com/explorer/{tx_hash}" if tx_hash else ""
            tx_abbr = abbr(tx_hash)

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
            send_telegram_message(message, image_url=image_url)
            print(f"Notified mint {tx_hash}: item name: {safe_item_name}")

            _remember(seen_mints, seen_mints_set, tx_hash)
            last_seen_mint_tx = tx_hash

        errors_mints = 0

    except requests.Timeout:
        errors_mints += 1
        print("Error polling mints: read timeout.")
        _maybe_sleep(errors_mints)
    except Exception as e:
        errors_mints += 1
        print(f"Error polling mints: {e}")
        _maybe_sleep(errors_mints)

# === Initialization ===
print("Starting NFT sales tracker (Telegram Bot 2)...")
print(f"Tracking issuer: {XRPL_NFT_ISSUER}")

try:
    init_resp = requests.get(
        BITHOMP_API_URL, params=sales_params,
        headers={"x-bithomp-token": BITHOMP_API_TOKEN},
        timeout=REQUEST_TIMEOUT
    )
    data = init_resp.json()
    sales = data.get("sales", [])
    if sales:
        head = max(sales, key=lambda s: int(s.get("acceptedAt", 0)))
        last_seen_sale_tx = head.get("acceptedTxHash")
        _remember(seen_sales, seen_sales_set, last_seen_sale_tx)
        print(f"Initialized sales backlog: {len(sales)} sales skipped. Anchor={last_seen_sale_tx}")
    else:
        print("No sales found during initialization.")
except Exception as e:
    print(f"Failed to initialize sales data: {e}")

def init_mints():
    global last_seen_mint_tx
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
        resp = requests.post(XRPL_RPC_URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        txs = data.get("result", {}).get("transactions", [])
        mints = [e.get("tx", {}) for e in txs if e.get("tx", {}).get("TransactionType") == "NFTokenMint"]
        if mints:
            head = max(mints, key=lambda t: t.get("date", 0))
            last_seen_mint_tx = head.get("hash")
            _remember(seen_mints, seen_mints_set, last_seen_mint_tx)
            print(f"Initialized mint backlog: {len(mints)} txs skipped. Anchor={last_seen_mint_tx}")
        else:
            print("No mint transactions found during initialization.")
    except Exception as e:
        print(f"Failed to initialize mint data: {e}")

init_mints()

# === Main Loop ===
while True:
    poll_sales()
    poll_mints()
    time.sleep(POLL_INTERVAL)
