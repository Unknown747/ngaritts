import os
import json
import time
import random
import threading
import requests
from eth_account import Account
from flask import Flask, render_template, jsonify
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ============ KONFIGURASI ============
RPC_LIST = [
    "https://eth.drpc.org",
    "https://ethereum.publicnode.com",
    "https://mainnet.gateway.tenderly.co",
    "https://eth-mainnet.public.blastapi.io",
    "https://1rpc.io/eth",
    "https://rpc.flashbots.net",
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_DELAY = float(os.getenv("CHECK_DELAY", "0"))
NUM_THREADS = int(os.getenv("NUM_THREADS", "20"))

# ============ VARIABEL GLOBAL ============
total_checked = 0
total_found = 0
total_eth_found = 0.0
start_time = datetime.now()
rpc_status = f"Active ({len(RPC_LIST)} RPCs)"
last_logs = []
found_wallets = []
last_found = None
running = True
lock = threading.Lock()

# Sliding window untuk speed real-time (10 detik)
speed_window = []
speed_lock = threading.Lock()

def record_check():
    with speed_lock:
        speed_window.append(time.time())

def get_current_speed():
    now = time.time()
    with speed_lock:
        recent = [t for t in speed_window if now - t <= 10]
        speed_window.clear()
        speed_window.extend(recent)
        return round(len(recent) / 10, 1)

# ============ PER-THREAD RPC MANAGER ============
class RPCManager:
    def __init__(self):
        self._rpcs = list(RPC_LIST)
        self._index = random.randint(0, len(self._rpcs) - 1)
        self._errors = 0
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    @property
    def current(self):
        return self._rpcs[self._index]

    def next(self):
        self._index = (self._index + 1) % len(self._rpcs)
        self._errors = 0
        self._update_global_status()

    def on_error(self):
        self._errors += 1
        if self._errors >= 3:
            self.next()

    def _update_global_status(self):
        global rpc_status
        rpc_status = f"Active ({len(self._rpcs)} RPCs)"

    def request(self, method, params):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": random.randint(1, 10000)
        }
        for _ in range(len(self._rpcs)):
            try:
                resp = self._session.post(self.current, json=payload, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if "error" not in data:
                        self._errors = 0
                        return data.get("result")
                    self.on_error()
                elif resp.status_code == 429:
                    self.next()
                    time.sleep(0.5)
                else:
                    self.on_error()
            except Exception:
                self.on_error()
        return None

# Load saved data
FOUND_FILE = "found_wallets.json"
PROGRESS_FILE = "progress.json"

def load_saved_data():
    global total_checked, total_found, total_eth_found, found_wallets
    try:
        if os.path.exists(FOUND_FILE):
            with open(FOUND_FILE, 'r') as f:
                found_wallets = json.load(f)
                total_found = len(found_wallets)
                total_eth_found = sum(w.get('balance_eth', 0) for w in found_wallets)
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, 'r') as f:
                data = json.load(f)
                total_checked = data.get('total_checked', 0)
    except:
        pass

def save_found():
    with lock:
        snapshot = list(found_wallets[-100:])
    with open(FOUND_FILE, 'w') as f:
        json.dump(snapshot, f, indent=2)

def save_progress():
    with lock:
        checked = total_checked
    with open(PROGRESS_FILE, 'w') as f:
        json.dump({'total_checked': checked}, f)

def add_log(message, is_error=False):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    with lock:
        last_logs.insert(0, log_entry)
        if len(last_logs) > 20:
            last_logs.pop()
    print(log_entry)

def send_telegram_notification(wallet_data):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        add_log("⚠️ Telegram not configured! Found wallet but no notification sent.")
        return
    
    message = f"""
🚨 *ETH WALLET FOUND!* 🚨

📍 *Address:* `{wallet_data['address']}`
🔑 *Private Key:* `{wallet_data['private_key']}`
💰 *Balance:* `{wallet_data['balance_eth']:.8f} ETH`
💵 *Value:* `${wallet_data['balance_usd']:.2f}` (est)
⏰ *Time:* `{wallet_data['found_time']}`

🔗 [View on Etherscan](https://etherscan.io/address/{wallet_data['address']})

⚠️ *Keep this private key secure!*
"""
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            add_log(f"✅ Telegram notification sent for {wallet_data['address'][:10]}...")
        else:
            add_log(f"❌ Telegram failed: {response.text}")
    except Exception as e:
        add_log(f"❌ Telegram error: {str(e)}")

def get_eth_price():
    try:
        response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", timeout=5)
        if response.status_code == 200:
            return response.json()['ethereum']['usd']
    except:
        pass
    return 3000  # fallback price

def generate_random_wallet():
    private_key_bytes = os.urandom(32)
    private_key_hex = private_key_bytes.hex()
    acct = Account.from_key(private_key_bytes)
    address = acct.address
    return private_key_hex, address

def check_balance(address, rpc):
    result = rpc.request("eth_getBalance", [address, "latest"])
    if result is not None:
        try:
            return int(result, 16) / 10**18
        except:
            return None
    return None

eth_price_cache = {"price": 3000, "updated": 0}

def get_cached_eth_price():
    now = time.time()
    if now - eth_price_cache["updated"] > 60:
        price = get_eth_price()
        eth_price_cache["price"] = price
        eth_price_cache["updated"] = now
    return eth_price_cache["price"]

def brute_worker(thread_id):
    global total_checked, total_found, total_eth_found, last_found, running

    rpc = RPCManager()
    consecutive_errors = 0

    while running:
        try:
            private_key, address = generate_random_wallet()
            balance = check_balance(address, rpc)

            with lock:
                total_checked += 1
                checked_now = total_checked

            record_check()

            if balance is not None and balance > 0:
                eth_price = get_cached_eth_price()
                add_log(f"💰 FOUND! {address[:10]}... | Balance: {balance:.8f} ETH")

                wallet_data = {
                    "address": address,
                    "private_key": private_key,
                    "balance_eth": balance,
                    "balance_usd": balance * eth_price,
                    "found_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }

                with lock:
                    found_wallets.insert(0, wallet_data)
                    total_found += 1
                    total_eth_found += balance
                    last_found = wallet_data

                send_telegram_notification(wallet_data)
                save_found()
                consecutive_errors = 0
            else:
                if checked_now % 500 == 0:
                    add_log(f"🔍 Checked {checked_now} wallets | Found: {total_found}")

            if checked_now % 500 == 0:
                save_progress()

            if CHECK_DELAY > 0:
                time.sleep(CHECK_DELAY)

        except Exception as e:
            add_log(f"Worker[{thread_id}] error: {str(e)}", True)
            consecutive_errors += 1
            if consecutive_errors > 10:
                consecutive_errors = 0
            time.sleep(1)

# ============ FLASK ROUTES ============
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stats')
def get_stats():
    elapsed = datetime.now() - start_time
    speed = get_current_speed()

    with lock:
        return jsonify({
            'total_checked': total_checked,
            'total_found': total_found,
            'total_eth': round(total_eth_found, 8),
            'uptime': str(elapsed).split('.')[0],
            'speed': speed,
            'rpc_status': rpc_status,
            'current_rpc': f"{len(RPC_LIST)} RPCs active",
            'logs': last_logs[:10],
            'last_found': last_found,
            'recent_found': found_wallets[:5]
        })

# ============ MAIN ============
if __name__ == '__main__':
    load_saved_data()
    add_log(f"📊 Loaded: {total_checked} checked, {total_found} found")
    
    # Start multiple worker threads for higher speed
    add_log(f"⚡ Starting {NUM_THREADS} worker threads...")
    for i in range(NUM_THREADS):
        t = threading.Thread(target=brute_worker, args=(i,), daemon=True)
        t.start()
    
    # Run Flask app
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)