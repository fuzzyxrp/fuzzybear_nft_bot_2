import os
import time
import requests
import json
import urllib.parse
import html

# === Configuration (from environment variables) ===
BITHOMP_API_TOKEN = os.getenv("BITHOMP_API_TOKEN")
BITHOMP_API_KEY = os.getenv("BITHOMP_API_KEY")  # optional
XRPL_NFT_ISSUER = os.getenv("FUZZYBEAR_ISSUER_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GROUP_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
XRPL_RPC_URL = os.getenv("XRPL_RPC_URL") or "https://s1.ripple.com:51234/"

# === Bithomp API endpoint for NFT sales ===
BITHOMP_API_URL = "https://xrplexplorer.com/api/v2/nft-sales"
sales_params = {
    "list": "lastSold",
    "issuer": XRPL_NFT_ISSUER,
    "saleType": "all",
    "period": "all"
}
# (If you need to restrict to a specific taxon, add: sales_params["taxon"] = "YOUR_TAXON")

# === Global state variables ===
last_seen_sale_tx = None
last_seen_mint_tx = None

# === Utility Functions ===
def decode_uri(hex_uri: str) -> str:
    """
    Decode a hex-encoded URI, URL-encode it so that special characters (like spaces and #)
    are properly escaped, and for IPFS URIs convert to a gateway URL.
    """
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


def fetch_metadata(uri: str) -> dict:
    """
    Fetch JSON metadata from a URI.
    Returns the metadata dictionary if successful.
    """
    try:
        r = requests.get(uri, timeout=10)
        if "application/json" in r.headers.get("Content-Type", "") or r.text.strip().startswith("{"):
            return r.json()
    except Exception as e:
        print(f"Error fetching metadata from {uri}: {e}")
    return None


def validate_image_url(url: str) -> bool:
    """
    Use a HEAD request to check if the URL returns an image.
    """
    try:
        r = requests.head(url, timeout=5)
        content_type = r.headers.get("Content-Type", "")
        return content_type.startswith("image/")
    except Exception as e:
        print(f"Error validating image URL {url}: {e}")
        return False


def fetch_image_bytes(url: str) -> bytes:
    """
    Download the image from the given URL and return its bytes.
    """
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"Error fetching image from {url}: {e}")
        return None


def send_telegram_message(text: str, image_url: str = None):
    """
    Send a Telegram message.
      - If image_url is provided, download the image bytes and send via sendPhoto (so that the image appears at the top).
      - Otherwise, send a text-only message.
    """
    if image_url:
        # Replace any literal '#' with '%23' so the URL is valid
        image_url = image_url.replace("#", "%23")
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        image_bytes = fetch_image_bytes(image_url)
        if image_bytes:
            files = {"photo": ("nft_image.jpg", image_bytes)}
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": text,
                "parse_mode": "HTML"
            }
            try:
                resp = requests.post(api_url, data=payload, files=files, timeout=10)
                if resp.status_code != 200:
                    print(f"Telegram API error (sendPhoto): {resp.status_code} - {resp.text}")
                return
            except Exception as e:
                print(f"Error sending Telegram photo: {e}")
        else:
            print("Failed to download image; sending text-only message.")
    # Fallback: send text-only message with link preview disabled
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(api_url, data=payload, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram API error (sendMessage): {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"Error sending Telegram message: {e}")


def abbr(text, length=5):
    """Return an abbreviated version of text (first `length` characters followed by '...')."""
    if text and len(text) > length:
        return text[:length] + "..."
    return text or "N/A"


# === Polling Functions ===
def poll_sales():
    global last_seen_sale_tx
    try:
        resp = requests.get(BITHOMP_API_URL, params=sales_params,
                            headers={"x-bithomp-token": BITHOMP_API_TOKEN},
                            timeout=10)
        if resp.status_code != 200:
            print(f"Bithomp sales API error: {resp.status_code} - {resp.text}")
            return
        data = resp.json()
        sales = data.get("sales", [])
        if not sales:
            return
        new_sales = []
        if last_seen_sale_tx is None:
            last_seen_sale_tx = sales[0].get("acceptedTxHash")
            print(f"Initialized backlog: {len(sales)} sales skipped.")
            return
        for sale in sales:
            tx_hash = sale.get("acceptedTxHash")
            if not tx_hash:
                continue
            if tx_hash == last_seen_sale_tx:
                break
            new_sales.append(sale)
        for sale in reversed(new_sales):
            tx_hash = sale.get("acceptedTxHash")
            nft = sale.get("nftoken", {})
            buyer = sale.get("buyer")
            seller = sale.get("seller")
            amount_str = sale.get("amount")
            try:
                amount_drops = int(amount_str) if amount_str else 0
                price_xrp = amount_drops / 1000000
            except Exception:
                price_xrp = 0
            price_str = f"{int(price_xrp)} XRP" if price_xrp.is_integer() else f"{price_xrp:.2f} XRP"
            accepted_at = sale.get("acceptedAt")
            if accepted_at:
                try:
                    tx_timestamp = int(accepted_at)
                    utc_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(tx_timestamp))
                except Exception:
                    utc_time = "N/A"
            else:
                utc_time = "N/A"
            # Retrieve metadata from NFT URI.
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
                            if img_link.startswith("ipfs://"):
                                image_url = f"https://ipfs.io/ipfs/{img_link[len('ipfs://'):]}"
                            else:
                                image_url = img_link
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
            # Replace '#' with '%23' in the image URL if needed.
            if image_url and "#" in image_url:
                image_url = image_url.replace("#", "%23")
            message = (
                "🚀 <b>!YUB TFN WEN</b>\n\n" +
                f"🏷️ <b>METI:</b> <a href=\"{nft_link}\">{safe_item_name}</a>\n" +
                f"💰 <b>ROF DLOS:</b> {price_str}\n" +
                f"🔄 <b>RELLES:</b> <a href=\"{seller_link}\">{seller_abbr}</a>\n" +
                f"➡️ <b>REYUB:</b> <a href=\"{buyer_link}\">{buyer_abbr}</a>\n" +
                f"⏱️ <b>EMIT NOITCASNART:</b> {utc_time}\n" +
                f"📑 <b>DI NOITCASNART:</b> <a href=\"{tx_link}\">{tx_abbr}</a>"
            )
            send_telegram_message(message, image_url=image_url)
            print(f"Notified sale {tx_hash}: {price_str}, buyer {buyer_abbr}, seller {seller_abbr}")
            last_seen_sale_tx = tx_hash
    except Exception as e:
        print(f"Error processing sales: {e}")


def fetch_nft_info(nft_id: str):
    """Fetch NFT info from Bithomp API for a given NFT token id."""
    url = f"https://xrplexplorer.com/api/v2/nft/{nft_id}"
    params = {
        "sellOffers": "true",
        "buyOffers": "true",
        "uri": "true",
        "history": "true",
        "assets": "true"
    }
    try:
        resp = requests.get(url, params=params, headers={"x-bithomp-token": BITHOMP_API_TOKEN}, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching NFT info for {nft_id}: {e}")
        return None


def poll_mints():
    global last_seen_mint_tx
    payload = {
        "method": "account_tx",
        "params": [
            {
                "account": XRPL_NFT_ISSUER,
                "ledger_index_min": -1,
                "ledger_index_max": -1,
                "limit": 50,
                "forward": False
            }
        ]
    }
    try:
        resp = requests.post(XRPL_RPC_URL, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        txs = data.get("result", {}).get("transactions", [])
        if not txs:
            return
        new_mints = []
        if last_seen_mint_tx is None:
            for entry in txs:
                tx_obj = entry.get("tx", {})
                if tx_obj.get("TransactionType") == "NFTokenMint":
                    last_seen_mint_tx = tx_obj.get("hash")
                    print(f"Initialized mint backlog: {len(txs)} transactions skipped. Last seen mint tx: {last_seen_mint_tx}")
                    break
            return
        for entry in txs:
            tx_obj = entry.get("tx", {})
            if tx_obj.get("TransactionType") == "NFTokenMint":
                tx_hash = tx_obj.get("hash")
                if tx_hash == last_seen_mint_tx:
                    break
                new_mints.append(tx_obj)
        for tx in reversed(new_mints):
            tx_hash = tx.get("hash")
            timestamp = tx.get("date")
            utc_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp + 946684800)) if timestamp else "N/A"
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
                            if img_link.startswith("ipfs://"):
                                image_url = f"https://ipfs.io/ipfs/{img_link[len('ipfs://'):]}"
                            else:
                                image_url = img_link
                    else:
                        image_url = uri
            if not item_name:
                item_name = "Unknown NFT"
            safe_item_name = html.escape(item_name)
            # Force mint price to be 32 XRP as required.
            mint_price_str = "32 XRP"
            nft_link = f"https://bithomp.com/en/nft/{nft_id}" if nft_id else ""
            tx_link = f"https://bithomp.com/explorer/{tx_hash}" if tx_hash else ""
            tx_abbr = abbr(tx_hash)
            # Replace '#' with '%23' in the image URL if needed.
            if image_url and "#" in image_url:
                image_url = image_url.replace("#", "%23")
            message = (
                "🚀 <b>!TNIM TFN WEN</b>\n\n" +
                "🖼️ <b>EMAN NOITCELLOC:</b> sraebyzzuF\n" +
                f"🏷️ <b>METI:</b> <a href=\"{nft_link}\">{safe_item_name}</a>\n" +
                f"⏱️ <b>EMIT NOITCASNART:</b> {utc_time}\n" +
                f"📑 <b>DI NOITCASNART:</b> <a href=\"{tx_link}\">{tx_abbr}</a>"
            )
            send_telegram_message(message, image_url=image_url)
            print(f"Notified mint {tx_hash}: {mint_price_str}, item name: {safe_item_name}")
            last_seen_mint_tx = tx_hash
    except Exception as e:
        print(f"Error polling mints: {e}")


# === Initialization: Skip backlog so only new events trigger messages ===
print("Starting NFT sales tracker...")
print(f"Tracking events for issuer: {XRPL_NFT_ISSUER}")

# Initialize sales backlog: skip all current sales.
try:
    init_resp = requests.get(BITHOMP_API_URL, params=sales_params,
                             headers={"x-bithomp-token": BITHOMP_API_TOKEN},
                             timeout=10)
    data = init_resp.json()
    sales = data.get("sales", [])
    if sales:
        last_seen_sale_tx = sales[0].get("acceptedTxHash")
        print(f"Initialized backlog: {len(sales)} sales skipped.")
    else:
        print("No sales found during initialization.")
except Exception as e:
    print(f"Failed to initialize sales data: {e}")


def init_mints():
    global last_seen_mint_tx
    payload = {
        "method": "account_tx",
        "params": [
            {
                "account": XRPL_NFT_ISSUER,
                "ledger_index_min": -1,
                "ledger_index_max": -1,
                "limit": 50,
                "forward": False
            }
        ]
    }
    try:
        resp = requests.post(XRPL_RPC_URL, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        txs = data.get("result", {}).get("transactions", [])
        if txs:
            for entry in txs:
                tx_obj = entry.get("tx", {})
                if tx_obj.get("TransactionType") == "NFTokenMint":
                    last_seen_mint_tx = tx_obj.get("hash")
                    print(f"Initialized mint backlog: {len(txs)} transactions skipped. Last seen mint tx: {last_seen_mint_tx}")
                    break
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
