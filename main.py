import collections
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import requests

# 強制 Log 即時印出，不等待 Buffer
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# 從 GitHub Secrets 讀取設定（支援多金鑰備援與多 Chat ID）
# ==========================================
KEY_PAIRS = []

# 第一組金鑰（主 Key）
client_id_1 = os.environ.get("TDX_CLIENT_ID")
client_secret_1 = os.environ.get("TDX_CLIENT_SECRET")
if client_id_1 and client_secret_1:
    KEY_PAIRS.append({
        "id": client_id_1.strip(),
        "secret": client_secret_1.strip(),
        "name": "主 Key (Key 1)",
    })

# 第二組金鑰（備援 Key 2）
client_id_2 = os.environ.get("TDX_CLIENT_ID_2")
client_secret_2 = os.environ.get("TDX_CLIENT_SECRET_2")
if client_id_2 and client_secret_2:
    KEY_PAIRS.append({
        "id": client_id_2.strip(),
        "secret": client_secret_2.strip(),
        "name": "備用 Key (Key 2)",
    })

# 第三組金鑰（備援 Key 3）
client_id_3 = os.environ.get("TDX_CLIENT_ID_3")
client_secret_3 = os.environ.get("TDX_CLIENT_SECRET_3")
if client_id_3 and client_secret_3:
    KEY_PAIRS.append({
        "id": client_id_3.strip(),
        "secret": client_secret_3.strip(),
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

DAYS_AHEAD = 45  # 統計天數設定為 45 天
EXCLUDE_TRAINS = []
STATE_FILE = "last_trains.json"
CACHE_FILE = "http_cache.json"  # 儲存 HTTP Cache (304 機制)

# ==========================================
# 金鑰與 Token 狀態管理
# ==========================================
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
            print(
                f"[Rate Limit] 60 秒內已達 5 次限制，動態冷卻 {sleep_time:.1f} 秒..."
            )
            time.sleep(sleep_time)
    REQUEST_TIMESTAMPS.append(time.time())


def load_http_cache():
    """載入本地 HTTP 快取"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] 讀取 HTTP Cache 失敗: {e}")
            return {}
    return {}


def save_http_cache(cache_data):
    """儲存 HTTP 快取至檔案"""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] 儲存 HTTP Cache 失敗: {e}")


def get_tdx_token(client_id, client_secret):
    """跟 TDX 拿 Token"""
    auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
    headers = {"content-type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        response = requests.post(
            auth_url, headers=headers, data=data, timeout=12
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        else:
            print(
                f"[!] 【HTTP {response.status_code}】金鑰驗證失敗:"
                f" {response.text}"
            )
            return None
    except Exception as e:
        print(f"[!] 取得 Token 時發生連線錯誤: {e}")
        return None


def get_valid_token():
    global current_key_index, current_token
    alive_indices = [i for i in range(len(KEY_PAIRS)) if i not in DEAD_KEYS]

    if not alive_indices:
        print("[Fatal Error] ❌ 所有 TDX 金鑰皆已被標記為無效！")
        sys.exit(1)

    for idx in alive_indices:
        key_info = KEY_PAIRS[idx]
        print(f"[Key System] 嘗試驗證【{key_info['name']}】...")
        token = get_tdx_token(key_info["id"], key_info["secret"])

        if token:
            current_key_index = idx
            current_token = token
            print(f"[Key System] 🎉 【{key_info['name']}】驗證成功並啟用！")
            return current_token
        else:
            print(f"[Key System] ❌ 【{key_info['name']}】驗證失敗！")
            DEAD_KEYS.add(idx)

    print("[Fatal Error] ❌ 無可用金鑰！")
    sys.exit(1)


def is_extra_train(train_type, note, train_num):
    if "專開" in train_type:
        return False
    # 檢查備註中是否有「?月?日」格式（例如 12月5日），或車次在 6000~6999 之間
    has_date = bool(re.search(r"\d+月\d+日", note))
    return has_date or (6000 <= train_num <= 6999)


def process_note(raw_note):
    """
    處理備註條件：
    - 如果備註中有「行駛」，就把「行駛」以及它前面的所有文字去掉。
    - 清理殘留的標點符號或空白。
    """
    if not raw_note:
        return ""

    note = raw_note.strip()

    if "行駛" in note:
        # 切割「行駛」字眼，取其右側的文字
        parts = note.split("行駛", 1)
        if len(parts) > 1:
            note = parts[1].strip()

    # 清理開頭或結尾殘留的句號、逗號、空格等
    cleaned = re.sub(r"^([。，、\s]+)", "", note).strip()
    return cleaned


def get_line_type(stops):
    """根據停靠站清單判斷經由線路"""
    station_names = [s.get("station", "") for s in stops]

    # 海線指標站
    coast_indicators = [
        "大甲",
        "清水",
        "沙鹿",
        "龍井",
        "大肚",
        "後龍",
        "通霄",
        "苑裡",
    ]
    if any(stn in station_names for stn in coast_indicators):
        return "海線"

    # 山線指標站
    mountain_indicators = [
        "豐原",
        "潭子",
        "臺中",
        "大慶",
        "成功",
        "三義",
        "銅鑼",
    ]
    if any(stn in station_names for stn in mountain_indicators):
        return "山線"

    # 南迴線指標站（嚴格限縮為知本、加祿、內獅、枋山）
    nanhuai_indicators = ["知本", "加祿", "內獅", "枋山"]
    if any(stn in station_names for stn in nanhuai_indicators):
        return "南迴線"

    # 東部幹線指標站
    east_indicators = [
        "花蓮",
        "臺東",
        "宜蘭",
        "羅東",
        "蘇澳",
        "玉里",
        "關山",
        "池上",
        "瑞穗",
    ]
    if any(stn in station_names for stn in east_indicators):
        return "東部幹線"

    return "西部幹線"


def parse_train_data(data, date_str):
    """解析 API 回傳資料為加班車資訊（含車種、處理後備註、站點與線路）"""
    results = []
    for item in data.get("TrainTimetables", []):
        train_info = item.get("TrainInfo", {})
        train_no = train_info.get("TrainNo")

        if train_no and train_no.isdigit():
            train_num = int(train_no)
            raw_note = train_info.get("Note", "")
            note_str = (
                raw_note.get("Zh_tw", "")
                if isinstance(raw_note, dict)
                else str(raw_note or "")
            )

            train_type_dict = train_info.get("TrainTypeName", {})
            train_type = (
                train_type_dict.get("Zh_tw", "")
                if isinstance(train_type_dict, dict)
                else str(train_type_dict)
            )

            if (
                is_extra_train(train_type, note_str, train_num)
                and train_no not in EXCLUDE_TRAINS
            ):
                status_sign = "-" if "停駛" in note_str else "+"

                processed_note = process_note(note_str)
                stop_times = item.get("StopTimes", [])

                start_station, end_station, start_time, end_time = (
                    "",
                    "",
                    "",
                    "",
                )
                stops = []

                if stop_times:
                    first_stop = stop_times[0]
                    last_stop = stop_times[-1]
                    s_name = first_stop.get("StationName", {})
                    start_station = (
                        s_name.get("Zh_tw", "")
                        if isinstance(s_name, dict)
                        else str(s_name)
                    )
                    start_time = first_stop.get("DepartureTime", "")[:5]

                    e_name = last_stop.get("StationName", {})
                    end_station = (
                        e_name.get("Zh_tw", "")
                        if isinstance(e_name, dict)
                        else str(e_name)
                    )
                    end_time = last_stop.get("ArrivalTime", "")[:5]

                    for st in stop_times:
                        st_name_dict = st.get("StationName", {})
                        st_name = (
                            st_name_dict.get("Zh_tw", "")
                            if isinstance(st_name_dict, dict)
                            else str(st_name_dict)
                        )
                        arr_time = st.get("ArrivalTime", "")[:5]
                        dep_time = st.get("DepartureTime", "")[:5]
                        stops.append({
                            "station": st_name,
                            "arrival": arr_time,
                            "departure": dep_time,
                        })

                line_type = get_line_type(stops)

                results.append([
                    train_no,
                    train_num,
                    date_str,
                    start_station,
                    start_time,
                    end_station,
                    end_time,
                    status_sign,
                    stops,
                    train_type,
                    processed_note,
                    line_type,
                ])
    return results


def fetch_single_day(date_obj, http_cache):
    global current_token, current_key_index
    api_date_str = date_obj.strftime("%Y-%m-%d")
    date_str = api_date_str  # 一律使用含年份的 ISO 格式，避免跨年錯亂
    api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{api_date_str}?$format=JSON"

    attempt = 0

    while True:
        try:
            attempt += 1
            wait_for_rate_limit()

            headers = {
                "authorization": f"Bearer {current_token}",
                "accept": "json",
            }

            cached_info = http_cache.get(api_date_str, {})
            last_modified = cached_info.get("last_modified")
            if last_modified:
                headers["If-Modified-Since"] = last_modified

            response = requests.get(api_url, headers=headers, timeout=15)

            if response.status_code == 304:
                print(f"[304 Cache Hit] {api_date_str} 資料未更新，使用快取")
                cached_results = cached_info.get("results", [])
                # 舊版快取的日期欄位是 MM/DD，這裡統一補成 ISO 日期
                for cached_item in cached_results:
                    cached_item[2] = api_date_str
                return cached_results

            elif response.status_code == 200:
                data = response.json()
                new_last_modified = response.headers.get("Last-Modified")
                results = parse_train_data(data, date_str)

                http_cache[api_date_str] = {
                    "last_modified": new_last_modified,
                    "results": results,
                }

                print(f"[V] {api_date_str} 抓取成功，找到 {len(results)} 筆")
                return results

            elif response.status_code in [401, 403]:
                print(f"[X] {api_date_str} Token 失效，重新驗證 Key...")
                DEAD_KEYS.add(current_key_index)
                current_token = get_valid_token()
                time.sleep(0.5)

            elif response.status_code == 429:
                print(f"[-] {api_date_str} 觸發 429 限速，等待 60 秒...")
                time.sleep(60)

            else:
                print(f"[X] {api_date_str} HTTP {response.status_code}，重試...")
                time.sleep(8.0)

        except Exception as e:
            print(f"[!] {api_date_str} 發生例外: {e}，重試...")
            time.sleep(8.0)


def normalize_date_entry(entry, ref_dt):
    """把舊格式 'MM/DD+' 轉成 'YYYY-MM-DD+'（升級過渡用）；新格式原樣回傳。"""
    m = re.match(r"^(\d{2})/(\d{2})([+-]?)$", entry)
    if not m:
        return entry
    mon, day, sign = int(m.group(1)), m.group(2), m.group(3)
    year = ref_dt.year
    if ref_dt.month - mon > 6:      # 例如上次更新在 12 月、日期是 01 月 → 明年
        year += 1
    elif mon - ref_dt.month > 6:    # 例如上次更新在 1 月、日期是 12 月 → 去年
        year -= 1
    return f"{year}-{mon:02d}-{day}{sign}"


def fmt_date(entry):
    """'2026-12-20+' → '12/20+'，僅用於 Telegram 顯示。"""
    m = re.match(r"^\d{4}-(\d{2})-(\d{2})([+-]?)$", entry)
    return f"{m.group(1)}/{m.group(2)}{m.group(3)}" if m else entry


def fmt_dates(entries):
    return ", ".join(fmt_date(d) for d in sorted(entries))


def load_previous_trains():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] 讀取上一期 JSON 失敗: {e}")
            return {}
    return {}


def save_current_trains(train_dates, start_str=None, end_str=None):
    """儲存含線路、車種、過濾後備註與站點的 JSON"""
    tz_taipei = timezone(timedelta(hours=8))
    now_str = datetime.now(tz_taipei).strftime("%Y-%m-%d %H:%M:%S")

    serializable = {}
    for dir_key, trains in train_dates.items():
        serializable[dir_key] = {}
        for train_no, info in trains.items():
            serializable[dir_key][train_no] = {
                "type": info.get("type", ""),
                "note": info.get("note", ""),
                "line_type": info.get("line_type", "西部幹線"),
                "dates": sorted(list(info["dates"])),
                "route": info["route"],
                "stops": info.get("stops", []),
            }

    output_data = {"last_updated": now_str, "data": serializable}
    if start_str and end_str:
        output_data["start_date"] = start_str
        output_data["end_date"] = end_str

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print("[System] 成功儲存擴充狀態至 last_trains.json")
        return True
    except Exception as e:
        print(f"[!] 儲存 JSON 失敗: {e}")
        return False


def compare_new_trains(prev_data, current_train_dates):
    if not prev_data:
        return []

    prev_trains_data = prev_data.get("data", prev_data)
    new_items = []

    try:
        ref_dt = datetime.strptime(
            prev_data.get("last_updated", ""), "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        ref_dt = datetime.now(timezone(timedelta(hours=8)))

    for dir_key in ["shun", "ni"]:
        current_dict = current_train_dates.get(dir_key, {})
        prev_dict = prev_trains_data.get(dir_key, {})

        for train_no, curr_info in current_dict.items():
            curr_dates = curr_info["dates"]
            route = curr_info["route"]

            if train_no not in prev_dict:
                new_items.append(
                    f"*{train_no}次* ({route}) [全新車次]：{fmt_dates(curr_dates)}"
                )
            else:
                prev_dates = {
                    normalize_date_entry(d, ref_dt)
                    for d in prev_dict[train_no].get("dates", [])
                }
                added_dates = curr_dates - prev_dates
                if added_dates:
                    new_items.append(
                        f"*{train_no}次* ({route}) 新增日期：{fmt_dates(added_dates)}"
                    )

    return new_items


def send_telegram_messages(bot_token, chat_id, messages):
    if not bot_token:
        print("[TG Warning] 未設定 Telegram Bot Token，跳過。")
        return False

    if isinstance(chat_id, str):
        chat_ids = [
            c.strip()
            for c in chat_id.replace(";", ",").replace(" ", ",").split(",")
            if c.strip()
        ]
    elif isinstance(chat_id, list):
        chat_ids = chat_id
    else:
        chat_ids = [str(chat_id)] if chat_id else []

    if not chat_ids:
        print("[TG Warning] 未提供有效的 Chat ID，跳過。")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    any_chat_ok = False  # 至少有一個 Chat 完整收到所有訊息，就視為通知成功

    for target_id in chat_ids:
        print(f"[TG] 發送訊息至 Chat ID: {target_id}")
        chat_ok = True
        for msg in messages:
            payload = {
                "chat_id": target_id,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            try:
                res = requests.post(url, json=payload, timeout=8)
                if res.status_code == 200:
                    print(f"[TG] 發送至 {target_id} 成功！")
                else:
                    chat_ok = False
                    print(f"[TG Error] 發送至 {target_id} 失敗: {res.text}")
            except Exception as e:
                chat_ok = False
                print(f"[TG Exception] 發送錯誤: {e}")
            time.sleep(0.3)
        any_chat_ok = any_chat_ok or chat_ok

    return any_chat_ok


def format_telegram_report(start_str, end_str, total_found, train_dates, new_items):
    messages = []
    header = ""
    if new_items:
        header += "相較前一報，異動了：\n"
        for item in new_items:
            header += f"• {item}\n"
        header += "------------------------------------\n\n"

    header += (
        f"臺鐵 45 天內加減班車彙整\n"
        f"統計區間：`{start_str}` ~ `{end_str}`\n"
        f"總計抓取：*{total_found}* 筆加班車紀錄\n"
        f"------------------------------------"
    )

    sections = [("順行（雙數車次）", "shun"), ("逆行（單數車次）", "ni")]
    current_msg = header + "\n\n"

    for title, dir_key in sections:
        data_dict = train_dates[dir_key]
        if not data_dict:
            continue

        current_msg += f"*{title}*\n"
        sorted_train_nos = sorted(data_dict.keys(), key=lambda x: int(x))

        for train_no in sorted_train_nos:
            info = data_dict[train_no]
            dates_str = fmt_dates(info["dates"])
            route = info["route"]
            
            line = f"• *{train_no}次* ({route})：{dates_str}\n"

            if len(current_msg) + len(line) > 3800:
                messages.append(current_msg)
                current_msg = f"*{title}（續）*\n" + line
            else:
                current_msg += line
        current_msg += "\n"

    if current_msg.strip():
        messages.append(current_msg)

    return messages


def main():
    missing_vars = []
    if not KEY_PAIRS:
        missing_vars.append("TDX_CLIENT_ID / TDX_CLIENT_SECRET")
    if not TG_BOT_TOKEN:
        missing_vars.append("TG_BOT_TOKEN")
    if not TG_CHAT_ID:
        missing_vars.append("TG_CHAT_ID")

    if missing_vars:
        print(f"[Fatal Error] 缺少環境變數: {', '.join(missing_vars)}")
        sys.exit(1)

    tz_taipei = timezone(timedelta(hours=8))
    today_taipei = datetime.now(tz_taipei).date()
    end_date_taipei = today_taipei + timedelta(days=DAYS_AHEAD - 1)

    start_str = today_taipei.strftime("%Y-%m-%d")
    end_str = end_date_taipei.strftime("%Y-%m-%d")

    prev_data = load_previous_trains()
    http_cache = load_http_cache()

    train_dates = {"shun": {}, "ni": {}}
    total_found = 0

    print(f"開始抓取 TDX 45 天加班車資料 ({start_str} ~ {end_str})...")

    try:
        get_valid_token()
        date_list = [
            today_taipei + timedelta(days=i) for i in range(DAYS_AHEAD)
        ]

        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_date = {
                executor.submit(fetch_single_day, d, http_cache): d
                for d in date_list
            }
            for future in as_completed(future_to_date):
                day_results = future.result()
                if day_results:
                    for item in day_results:
                        (
                            train_no,
                            train_num,
                            date_str,
                            start_station,
                            start_time,
                            end_station,
                            end_time,
                            status_sign,
                            stops,
                            train_type,
                            note,
                            line_type,
                        ) = item
                        total_found += 1
                        dir_key = "shun" if train_num % 2 == 0 else "ni"

                        if train_no not in train_dates[dir_key]:
                            start_part = (
                                f"{start_station} {start_time}"
                                if start_time
                                else start_station
                            )
                            end_part = (
                                f"{end_station} {end_time}"
                                if end_time
                                else end_station
                            )
                            route_str = (
                                f"{start_part} -> {end_part}"
                                if start_station and end_station
                                else "未知區間"
                            )

                            train_dates[dir_key][train_no] = {
                                "type": train_type,
                                "note": note,
                                "line_type": line_type,
                                "dates": set(),
                                "route": route_str,
                                "stops": stops,
                            }

                        date_entry = f"{date_str}{status_sign}"
                        train_dates[dir_key][train_no]["dates"].add(date_entry)

        # 防呆：API 異常回傳空資料時，不覆蓋舊狀態（否則下次全部變「全新車次」）
        prev_has_trains = any(
            prev_data.get("data", prev_data).get(k) for k in ("shun", "ni")
        ) if prev_data else False
        if total_found == 0 and prev_has_trains:
            print("[Fatal Error] 本次抓到 0 筆資料但上一期有資料，疑似 API 異常，保留舊狀態不覆蓋。")
            sys.exit(1)

        save_http_cache(http_cache)
        new_items = compare_new_trains(prev_data, train_dates)

        print("\n[System] 資料比對完成，準備發送 TG 訊息...")
        tg_messages = format_telegram_report(
            start_str, end_str, total_found, train_dates, new_items
        )
        tg_ok = send_telegram_messages(TG_BOT_TOKEN, TG_CHAT_ID, tg_messages)

        # 通知成功後才更新狀態；否則下次執行仍會與「上次已通知的版本」比對
        if tg_ok:
            save_current_trains(train_dates, start_str, end_str)
        else:
            print("[Fatal Error] Telegram 通知失敗，未更新 last_trains.json，下次會重新比對。")
            sys.exit(1)

    except Exception as e:
        print(f"[Fatal Error] 執行過程發生錯誤: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
