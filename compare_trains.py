import collections
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
import requests

# 強制 Log 即時印出，不等待 Buffer
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# 從 GitHub Secrets 讀取設定（支援多金鑰備援與多 Chat ID）
# ==========================================
KEY_PAIRS = []

# 第一組金鑰（主 Key）
if os.environ.get("TDX_CLIENT_ID") and os.environ.get("TDX_CLIENT_SECRET"):
    KEY_PAIRS.append({
        "id": os.environ.get("TDX_CLIENT_ID").strip(),
        "secret": os.environ.get("TDX_CLIENT_SECRET").strip(),
        "name": "主 Key (Key 1)",
    })

# 第二組金鑰（備援 Key 2）
if os.environ.get("TDX_CLIENT_ID_2") and os.environ.get("TDX_CLIENT_SECRET_2"):
    KEY_PAIRS.append({
        "id": os.environ.get("TDX_CLIENT_ID_2").strip(),
        "secret": os.environ.get("TDX_CLIENT_SECRET_2").strip(),
        "name": "備用 Key (Key 2)",
    })

# 第三組金鑰（備援 Key 3）
if os.environ.get("TDX_CLIENT_ID_3") and os.environ.get("TDX_CLIENT_SECRET_3"):
    KEY_PAIRS.append({
        "id": os.environ.get("TDX_CLIENT_ID_3").strip(),
        "secret": os.environ.get("TDX_CLIENT_SECRET_3").strip(),
        "name": "備用 Key (Key 3)",
    })

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

# 讀取並彙整多個 Telegram Chat ID
tg_ids = []
if os.environ.get("TG_CHAT_ID"):
    tg_ids.append(os.environ.get("TG_CHAT_ID").strip())
if os.environ.get("TG_CHAT_ID_2"):
    tg_ids.append(os.environ.get("TG_CHAT_ID_2").strip())
TG_CHAT_ID = ",".join(tg_ids) if tg_ids else None

CACHE_FILE = "http_cache.json"

current_key_index = 0
current_token = None
DEAD_KEYS = set()
REQUEST_TIMESTAMPS = collections.deque(maxlen=5)


def wait_for_rate_limit():
    """精準控制器：確保 60 秒內最多只發送 5 次 Request"""
    now = time.time()
    if len(REQUEST_TIMESTAMPS) == 5:
        elapsed = now - REQUEST_TIMESTAMPS[0]
        if elapsed < 60:
            sleep_time = (60 - elapsed) + 0.5
            print(f"[Rate Limit] 動態冷卻 {sleep_time:.1f} 秒...")
            time.sleep(sleep_time)
    REQUEST_TIMESTAMPS.append(time.time())


def load_http_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] 讀取 HTTP Cache 失敗: {e}")
    return {}


def save_http_cache(cache_data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] 儲存 HTTP Cache 失敗: {e}")


def get_tdx_token(client_id, client_secret):
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    headers = {"content-type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        response = requests.post(auth_url, headers=headers, data=data, timeout=12)
        if response.status_code == 200:
            return response.json().get("access_token")
        else:
            print(f"[!] 【HTTP {response.status_code}】金鑰驗證失敗: {response.text}")
            return None
    except Exception as e:
        print(f"[!] 取得 Token 時發生連線錯誤: {e}")
        return None


def get_valid_token():
    global current_key_index, current_token
    alive_indices = [i for i in range(len(KEY_PAIRS)) if i not in DEAD_KEYS]

    if not alive_indices:
        print("[Fatal Error] 所有 TDX 金鑰皆已被標記為無效！")
        sys.exit(1)

    for idx in alive_indices:
        key_info = KEY_PAIRS[idx]
        print(f"[Key System] 嘗試驗證【{key_info['name']}】...")
        token = get_tdx_token(key_info["id"], key_info["secret"])
        if token:
            current_key_index = idx
            current_token = token
            print(f"[Key System] 【{key_info['name']}】驗證成功並啟用！")
            return current_token
        else:
            print(f"[Key System] 【{key_info['name']}】驗證失敗！")
            DEAD_KEYS.add(idx)

    print("[Fatal Error] 無可用金鑰！")
    sys.exit(1)


def fetch_single_day_timetable(api_date_str, http_cache):
    """具備多金鑰備援與 304 快取機制的單日時刻表抓取"""
    global current_token, current_key_index
    api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{api_date_str}?$format=JSON"

    while True:
        try:
            wait_for_rate_limit()
            headers = {"authorization": f"Bearer {current_token}", "accept": "json"}

            cached_info = http_cache.get(api_date_str, {})
            last_modified = cached_info.get("last_modified")
            if last_modified:
                headers["If-Modified-Since"] = last_modified

            response = requests.get(api_url, headers=headers, timeout=15)

            if response.status_code == 304:
                print(f"[304 Cache Hit] {api_date_str} 資料未更新，使用快取")
                return cached_info.get("results", {})

            elif response.status_code == 200:
                data = response.json()
                new_last_modified = response.headers.get("Last-Modified")
                
                # 解析並轉成以 train_no 為 Key 的對照字典
                trains_map = {}
                for item in data.get("TrainTimetables", []):
                    train_info = item.get("TrainInfo", {})
                    train_no = train_info.get("TrainNo")
                    if not train_no:
                        continue

                    raw_note = train_info.get("Note", "")
                    note = raw_note.get("Zh_tw", "") if isinstance(raw_note, dict) else str(raw_note or "")
                    is_suspended = train_info.get("SuspendedFlag", 0) == 1 or "停駛" in note

                    stop_times = item.get("StopTimes", [])
                    route_str = "未知區間"
                    if stop_times:
                        s_name = stop_times[0].get("StationName", {})
                        start_station = s_name.get("Zh_tw", "") if isinstance(s_name, dict) else str(s_name)
                        start_time = stop_times[0].get("DepartureTime", "")[:5]
                        
                        e_name = stop_times[-1].get("StationName", {})
                        end_station = e_name.get("Zh_tw", "") if isinstance(e_name, dict) else str(e_name)
                        end_time = stop_times[-1].get("ArrivalTime", "")[:5]

                        route_str = f"{start_station} {start_time} -> {end_station} {end_time}"

                    trains_map[train_no] = {
                        "route": route_str,
                        "is_suspended": is_suspended,
                        "note": note
                    }

                http_cache[api_date_str] = {"last_modified": new_last_modified, "results": trains_map}
                print(f"[V] {api_date_str} 抓取成功，共 {len(trains_map)} 筆車次")
                return trains_map

            elif response.status_code in [401, 403]:
                print(f"[X] {api_date_str} Token 失效，切換 Key 重試...")
                DEAD_KEYS.add(current_key_index)
                current_token = get_valid_token()
                time.sleep(0.5)

            elif response.status_code == 429:
                print(f"[-] 觸發 429 限速，等待 60 秒...")
                time.sleep(60)

            else:
                print(f"[X] HTTP {response.status_code}，8秒後重試...")
                time.sleep(8.0)

        except Exception as e:
            print(f"[!] 連線例外: {e}，8秒後重試...")
            time.sleep(8.0)


def send_telegram_messages(messages):
    """支援多 Chat ID 與自動分段訊息發送"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG Warning] 未設定 Telegram Bot Token 或 Chat ID，跳過。")
        return

    chat_ids = [c.strip() for c in TG_CHAT_ID.replace(";", ",").replace(" ", ",").split(",") if c.strip()]
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    for target_id in chat_ids:
        print(f"[TG] 發送訊息至 Chat ID: {target_id}")
        for msg in messages:
            payload = {
                "chat_id": target_id,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }
            try:
                res = requests.post(url, json=payload, timeout=8)
                if res.status_code == 200:
                    print(f"[TG] 發送至 {target_id} 成功！")
                else:
                    print(f"[TG Error] 發送至 {target_id} 失敗: {res.text}")
            except Exception as e:
                print(f"[TG Exception] 發送錯誤: {e}")
            time.sleep(0.3)


def main():
    if not KEY_PAIRS:
        print("[Fatal Error] 缺少 TDX 金鑰環境變數！")
        sys.exit(1)

    date_normal = os.environ.get("DATE_NORMAL", "2026-11-02")
    date_target = os.environ.get("DATE_TARGET", "2026-11-09")

    print(f"開始進行時刻表比對：基準日 [{date_normal}] vs 目標日 [{date_target}]")

    http_cache = load_http_cache()
    current_token = get_valid_token()

    trains_a = fetch_single_day_timetable(date_normal, http_cache)
    trains_b = fetch_single_day_timetable(date_target, http_cache)
    save_http_cache(http_cache)

    if not trains_a or not trains_b:
        print("資料獲取不完全，無法比對。")
        sys.exit(1)

    set_a = set(trains_a.keys())
    set_b = set(trains_b.keys())

    # 1. 停駛車次
    suspended_list = []
    for t_no in set_a:
        if t_no not in set_b:
            suspended_list.append((t_no, trains_a[t_no]["route"], "自時刻表中移除"))
        elif trains_b[t_no]["is_suspended"]:
            suspended_list.append((t_no, trains_b[t_no]["route"], "標示為停駛"))

    # 2. 新增車次
    added_list = []
    for t_no in set_b:
        if t_no not in set_a:
            added_list.append((t_no, trains_b[t_no]["route"]))

    # 3. 區間調整車次
    modified_list = []
    for t_no in set_a.intersection(set_b):
        if not trains_b[t_no]["is_suspended"] and trains_a[t_no]["route"] != trains_b[t_no]["route"]:
            modified_list.append((t_no, trains_a[t_no]["route"], trains_b[t_no]["route"]))

    # 準備逐行資料
    lines_suspended = [f"• *{t_no}次* ({route}) - {reason}" for t_no, route, reason in sorted(suspended_list, key=lambda x: int(x[0]) if x[0].isdigit() else 0)]
    lines_added = [f"• *{t_no}次* ({route})" for t_no, route in sorted(added_list, key=lambda x: int(x[0]) if x[0].isdigit() else 0)]
    lines_modified = [f"• *{t_no}次*\n  原: {old_r}\n  新: {new_r}" for t_no, old_r, new_r in sorted(modified_list, key=lambda x: int(x[0]) if x[0].isdigit() else 0)]

    header = (
        f"*臺鐵時刻表兩天比對報告*\n"
        f"基準日（正常）：`{date_normal}`\n"
        f"目標日（調整）：`{date_target}`\n"
        f"------------------------------------\n"
    )

    messages = []
    current_msg = header + f"*{f'停駛車次 ({len(suspended_list)} 筆)'}*\n"

    if lines_suspended:
        for line in lines_suspended:
            if len(current_msg) + len(line) + 1 > 3500:
                current_msg += "------------------------------------"
                messages.append(current_msg)
                current_msg = "*臺鐵時刻表兩天比對報告（停駛續集）*\n" + line + "\n"
            else:
                current_msg += line + "\n"
    else:
        current_msg += "• 無\n"

    added_section = f"\n*{f'新增車次 ({len(added_list)} 筆)'}*\n"
    if lines_added:
        for line in lines_added:
            added_section += line + "\n"
    else:
        added_section += "• 無\n"

    modified_section = f"\n*{f'區間調整車次 ({len(modified_list)} 筆)'}*\n"
    if lines_modified:
        for line in lines_modified:
            modified_section += line + "\n"
    else:
        modified_section += "• 無\n"

    footer = "\n------------------------------------"

    remaining_content = added_section + modified_section + footer
    if len(current_msg) + len(remaining_content) <= 3800:
        current_msg += remaining_content
        messages.append(current_msg)
    else:
        current_msg += "------------------------------------"
        messages.append(current_msg)
        messages.append("*臺鐵時刻表兩天比對報告（新增與調整）*\n" + remaining_content)

    print(f"\n總共切分成 {len(messages)} 則 Telegram 訊息發送...")
    send_telegram_messages(messages)


if __name__ == "__main__":
    main()
