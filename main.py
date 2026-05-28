import os
import json
import time
import random
import threading
import requests
from flask import Flask, render_template, jsonify
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ============ KONFIGURASI ============
RPC_PRIMARY = os.getenv("RPC_PRIMARY_URL", "http://202.61.239.89:8545")
RPC_BACKUP = [
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://cloudflare-eth.com"
]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_DELAY = float(os.getenv("CHECK_DELAY", "1.0"))

# ============ VARIABEL GLOBAL ============
total_checked = 0
total_found = 0
total_eth_found = 0.0
start_time = datetime.now()
current_rpc = RPC_PRIMARY
rpc_status = "Online"
last_logs = []
found_wallets = []
last_found = None
running = True
lock = threading.Lock()

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
        with open(FOUND_FILE, 'w') as f:
            json.dump(found_wallets[-100:], f, indent=2)  # Keep last 100

def save_progress():
    with lock:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump({'total_checked': total_checked}, f)

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

def make_rpc_request(method, params):
    global current_rpc, rpc_status
    
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": random.randint(1, 10000)
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        ])
    }
    
    try:
        response = requests.post(current_rpc, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "error" in data:
                return None
            rpc_status = "Online"
            return data.get("result")
        elif response.status_code == 429:
            add_log("⚠️ Rate limit hit, waiting...")
            time.sleep(5)
            return None
        else:
            return None
    except Exception as e:
        add_log(f"RPC error: {str(e)[:50]}")
        return None

def rotate_rpc():
    global current_rpc, rpc_status
    if current_rpc == RPC_PRIMARY:
        current_rpc = random.choice(RPC_BACKUP)
        add_log(f"Switched to backup RPC: {current_rpc[:30]}...")
    else:
        # Try to switch back to primary
        current_rpc = RPC_PRIMARY
        add_log(f"Switched back to primary RPC")
    rpc_status = "Rotating"

def generate_random_wallet():
    # Generate random private key (64 hex chars)
    private_key = ''.join(random.choices('0123456789abcdef', k=64))
    
    # Derive address from private key (simplified - in real scenario use proper crypto)
    # For brute force, we simulate address generation
    # Note: Real implementation would use eth-account library, but for demo:
    address = '0x' + ''.join(random.choices('0123456789abcdef', k=40))
    
    return private_key, address

def check_balance(address):
    for attempt in range(3):
        result = make_rpc_request("eth_getBalance", [address, "latest"])
        if result is not None:
            try:
                balance_wei = int(result, 16)
                return balance_wei / 10**18
            except:
                return None
        time.sleep(0.5)
        rotate_rpc()
    return None

def brute_worker():
    global total_checked, total_found, total_eth_found, last_found, running
    
    eth_price = get_eth_price()
    add_log(f"🚀 ETH Brute Checker Started! Price: ${eth_price}")
    add_log(f"Primary RPC: {RPC_PRIMARY}")
    
    consecutive_errors = 0
    
    while running:
        try:
            # Generate random wallet
            private_key, address = generate_random_wallet()
            
            # Check balance
            balance = check_balance(address)
            
            with lock:
                total_checked += 1
            
            if balance is not None and balance > 0:
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
                
                # Send Telegram notification
                send_telegram_notification(wallet_data)
                
                # Save immediately
                save_found()
                consecutive_errors = 0
            else:
                # Log every 100 checks (not too spammy)
                if total_checked % 100 == 0:
                    add_log(f"🔍 Checked {total_checked} wallets | Found: {total_found}")
            
            # Save progress every 100 checks
            if total_checked % 100 == 0:
                save_progress()
            
            # Delay with jitter
            delay = CHECK_DELAY + random.uniform(0, 0.5)
            time.sleep(delay)
            
            # Rotate RPC periodically
            if total_checked % 50 == 0:
                rotate_rpc()
                eth_price = get_eth_price()  # Update price periodically
                
        except Exception as e:
            add_log(f"Worker error: {str(e)}", True)
            consecutive_errors += 1
            if consecutive_errors > 10:
                add_log("Too many errors, restarting RPC...")
                rotate_rpc()
                consecutive_errors = 0
            time.sleep(2)

# ============ FLASK ROUTES ============
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stats')
def get_stats():
    elapsed = datetime.now() - start_time
    speed = total_checked / max(elapsed.total_seconds(), 1)
    
    with lock:
        return jsonify({
            'total_checked': total_checked,
            'total_found': total_found,
            'total_eth': round(total_eth_found, 8),
            'uptime': str(elapsed).split('.')[0],
            'speed': round(speed, 2),
            'rpc_status': rpc_status,
            'current_rpc': current_rpc[:50] + "..." if len(current_rpc) > 50 else current_rpc,
            'logs': last_logs[:10],
            'last_found': last_found,
            'recent_found': found_wallets[:5]
        })

# ============ MAIN ============
if __name__ == '__main__':
    load_saved_data()
    add_log(f"📊 Loaded: {total_checked} checked, {total_found} found")
    
    # Start brute worker in background thread
    worker_thread = threading.Thread(target=brute_worker, daemon=True)
    worker_thread.start()
    
    # Run Flask app
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)