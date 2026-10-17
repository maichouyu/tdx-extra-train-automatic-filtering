import json
import os
import sys
import requests

# 強制 Log 即時印出
sys.stdout.reconfigure(line_buffering=True)

# 讀取環境變數金鑰
CLIENT_ID = os.environ.get("TDX_CLIENT_ID")
CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET")


def get_tdx_token(client_id, client_secret):
  """取得 TDX API Token"""
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
      print(f"[!] Token 取得失敗: {response.text}")
      return None
  except Exception as e:
    print(f"[!] 連線錯誤: {e}")
    return None


def fetch_day_timetable(api_date_str, token):
  """抓取指定日期（YYYY-MM-DD）的完整車次時刻表，並整理成以 TrainNo 為 Key 的字典"""
  api_url = f"https://tdx.transportdata.tw/api/basic/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{api_date_str}?$format=JSON"
  headers = {"authorization": f"Bearer {token}", "accept": "json"}

  print(f"正在向 TDX 請求 {api_date_str} 的時刻表資料...")
  response = requests.get(api_url, headers=headers, timeout=30)

  if response.status_code != 200:
    print(f"❌ 抓取 {api_date_str} 失敗 [HTTP {response.status_code}]")
    return {}

  data = response.json()
  trains_map = {}

  for item in data.get("TrainTimetables", []):
    train_info = item.get("TrainInfo", {})
    train_no = train_info.get("TrainNo")

    if not train_no:
      continue

    # 取得車種名稱
    train_type_dict = train_info.get("TrainTypeName", {})
    train_type = (
        train_type_dict.get("Zh_tw", "")
        if isinstance(train_type_dict, dict)
        else str(train_type_dict)
    )

    # 取得備註
    raw_note = train_info.get("Note", "")
    note = (
        raw_note.get("Zh_tw", "")
        if isinstance(raw_note, dict)
        else str(raw_note or "")
    )

    # 檢查是否整車停駛 (SuspendedFlag == 1 或者是 Note 包含停駛)
    is_suspended = train_info.get("SuspendedFlag", 0) == 1 or "停駛" in note

    stop_times = item.get("StopTimes", [])
    start_station, end_station, route_str = "", "", "未知區間"

    if stop_times:
      first_stop = stop_times[0]
      last_stop = stop_times[-1]

      s_name = first_stop.get("StationName", {})
      start_station = (
          s_name.get("Zh_tw", "") if isinstance(s_name, dict) else str(s_name)
      )
      start_time = first_stop.get("DepartureTime", "")[:5]

      e_name = last_stop.get("StationName", {})
      end_station = (
          e_name.get("Zh_tw", "") if isinstance(e_name, dict) else str(e_name)
      )
      end_time = last_stop.get("ArrivalTime", "")[:5]

      start_part = (
          f"{start_station} {start_time}" if start_time else start_station
      )
      end_part = f"{end_station} {end_time}" if end_time else end_station
      route_str = (
          f"{start_part} -> {end_part}"
          if start_station and end_station
          else "未知區間"
      )

    trains_map[train_no] = {
        "train_no": train_no,
        "train_type": train_type,
        "route": route_str,
        "start_station": start_station,
        "end_station": end_station,
        "is_suspended": is_suspended,
        "note": note,
        "stop_count": len(stop_times),
    }

  print(f"✅ {api_date_str} 解析完成，共有效車次 {len(trains_map)} 筆")
  return trains_map


def compare_two_dates(date_a, date_b):
  """比對基準日 (Date A，例如正常日) 與目標日 (Date B，例如減班日)"""
  token = get_tdx_token(CLIENT_ID, CLIENT_SECRET)
  if not token:
    return

  trains_a = fetch_day_timetable(date_a, token)
  trains_b = fetch_day_timetable(date_b, token)

  if not trains_a or not trains_b:
    print("❌ 資料獲取不完全，無法比對。")
    return

  set_a = set(trains_a.keys())
  set_b = set(trains_b.keys())

  # 1. 停駛車次：在 A 存在，但在 B 完全消失、或是 B 的 is_suspended 為 True
  suspended_list = []
  for t_no in set_a:
    if t_no not in set_b:
      suspended_list.append((t_no, trains_a[t_no]["route"], "完全自時刻表中移除"))
    elif trains_b[t_no]["is_suspended"]:
      suspended_list.append((t_no, trains_b[t_no]["route"], "狀態標示為停駛"))

  # 2. 新增/加開車次：A 沒有，B 新出現
  added_list = []
  for t_no in set_b:
    if t_no not in set_a:
      added_list.append((t_no, trains_b[t_no]["route"]))

  # 3. 區間調整車次：兩天都有開，但起訖站（route）或 Note 有變動
  modified_list = []
  common_trains = set_a.intersection(set_b)
  for t_no in common_trains:
    info_a = trains_a[t_no]
    info_b = trains_b[t_no]
    if not info_b["is_suspended"] and info_a["route"] != info_b["route"]:
      modified_list.append(
          (t_no, info_a["route"], info_b["route"], info_b["note"])
      )

  # --- 印出比對報告 ---
  print("\n" + "=" * 40)
  print(f"📊 臺鐵時刻表異動比對報告")
  print(f"基準日（正常）：{date_a} | 目標日（調整）：{date_b}")
  print("=" * 40)

  print(f"\n【🚫 停駛車次總計：{len(suspended_list)} 筆】")
  for t_no, route, reason in sorted(
      suspended_list, key=lambda x: int(x[0]) if x[0].isdigit() else 0
  ):
    print(f"• {t_no}次 ({route}) - 原因: {reason}")

  print(f"\n【✨ 新增/加開車次總計：{len(added_list)} 筆】")
  for t_no, route in sorted(
      added_list, key=lambda x: int(x[0]) if x[0].isdigit() else 0
  ):
    print(f"• {t_no}次 ({route})")

  print(f"\n【🔄 區間調整行駛車次總計：{len(modified_list)} 筆】")
  for t_no, old_r, new_r, note in sorted(
      modified_list, key=lambda x: int(x[0]) if x[0].isdigit() else 0
  ):
    print(f"• {t_no}次")
    print(f"  原區間: {old_r}")
    print(f"  新區間: {new_r}")
    if note:
      print(f"  備註: {note}")
  print("=" * 40)


if __name__ == "__main__":
  # 測試比對：你可以將日期換成你要的星期一 (例如 11/2 與 11/9)
  # 由於是未來或過去的日期，請確保 TDX 資料庫中有該日資料
  date_normal = "2026-11-02"  # 基準日 (一)
  date_target = "2026-11-09"  # 異動日 (一)

  compare_two_dates(date_normal, date_target)
