import csv
import io
import os
import re
import time
from datetime import datetime
import pandas as pd
import requests
from dotenv import load_dotenv

# 1. Tải API key từ file .env
load_dotenv()
VIETMAP_API_KEY = os.getenv("VIETMAP_API_KEY")

if not VIETMAP_API_KEY:
    raise ValueError("Chưa tìm thấy biến VIETMAP_API_KEY trong file .env!")

# 2. Xóa proxy hệ thống tránh lỗi mạng
for proxy_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(proxy_var, None)

def clean_coord_str(val):
    """Chuẩn hóa chuỗi tọa độ (loại bỏ dấu chấm phân cách hàng nghìn nếu có)"""
    if not val or pd.isna(val) or str(val).lower() == 'nan':
        return None
    s = str(val).strip().replace(' ', '')
    if s.count('.') > 1:
        first_dot = s.find('.')
        s = s[:first_dot+1] + s[first_dot+1:].replace('.', '')
    return s

def load_clean_csv(input_file: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(input_file, encoding='utf-8-sig', sep=None, engine='python', on_bad_lines='skip')
    except Exception:
        df = pd.read_csv(input_file, encoding='utf-8', sep=None, engine='python', on_bad_lines='skip')
    df.columns = [str(c).strip() for c in df.columns]
    return df

def fetch_vietmap_coordinates(query: str, session: requests.Session):
    """Gọi API VietMap v4 kèm cơ chế xử lý lỗi 429 (Rate Limit) và retry"""
    search_url = "https://maps.vietmap.vn/api/search/v4"
    place_url = "https://maps.vietmap.vn/api/place/v4"

    search_params = {
        "apikey": VIETMAP_API_KEY,
        "text": query,
        "admin_new": "false",
        "display_type": "6"
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 1. Tìm ref_id
            resp = session.get(search_url, params=search_params, timeout=15, proxies={'http': None, 'https': None})
            
            if resp.status_code == 429:
                print(f" -> [VietMap Rate Limit 429]: Quá nhanh, chờ 8 giây trước khi thử lại ({attempt+1}/{max_retries})...")
                time.sleep(8)
                continue
            elif resp.status_code == 403:
                print(" -> [VietMap Lỗi 403]: API Key đã hết hạn mức (Quota) hôm nay!")
                return None, None
            elif resp.status_code != 200:
                print(f" -> [VietMap Lỗi HTTP {resp.status_code}]: {resp.text[:80]}")
                return None, None

            data = resp.json()
            items = data if isinstance(data, list) else (data.get("data") or data.get("results") or [])
            if not items:
                return None, None

            first = items[0]
            lat = first.get("lat") or first.get("latitude")
            lon = first.get("lng") or first.get("long") or first.get("longitude")
            if lat and lon:
                return clean_coord_str(lat), clean_coord_str(lon)

            # 2. Lấy tọa độ bằng ref_id qua place/v4
            ref_id = first.get("ref_id")
            if ref_id:
                time.sleep(0.2)  # Giãn cách giữa 2 lượt gọi
                place_params = {"apikey": VIETMAP_API_KEY, "refid": ref_id}
                resp_place = session.get(place_url, params=place_params, timeout=15, proxies={'http': None, 'https': None})
                
                if resp_place.status_code == 200:
                    pdata = resp_place.json()
                    lat = pdata.get("lat") or pdata.get("latitude")
                    lon = pdata.get("lng") or pdata.get("longitude")
                    if lat and lon:
                        return clean_coord_str(lat), clean_coord_str(lon)
            
            return None, None

        except requests.exceptions.RequestException as e:
            print(f" -> Lỗi mạng: {e}, thử lại sau 3s...")
            time.sleep(3)

    return None, None

def geocode_addresses(input_file: str, resume_file: str, output_file: str, address_col: str = 'Địa chỉ'):
    df_raw = load_clean_csv(input_file)
    
    # Đọc lại các kết quả đã quét được trước đó nếu có
    existing_coords = {}
    if resume_file and os.path.exists(resume_file):
        try:
            df_prev = pd.read_csv(resume_file, encoding='utf-8-sig')
            for i, row in df_prev.iterrows():
                if pd.notna(row.get('vĩ độ')) and pd.notna(row.get('kinh độ')):
                    lat_str = clean_coord_str(row['vĩ độ'])
                    lon_str = clean_coord_str(row['kinh độ'])
                    if lat_str and lon_str:
                        existing_coords[i] = (lat_str, lon_str)
            print(f"Đã nạp {len(existing_coords)} tọa độ đã lấy thành công trước đó.")
        except Exception as e:
            print(f"Không thể đọc file resume: {e}")

    session = requests.Session()
    cache = {}

    def get_coords_from_address(addr_str):
        if addr_str in cache:
            return cache[addr_str]

        clean_addr = re.sub(r'Thành Phố Hồ Chí Minh|TP\.?\s*HCM', 'Bình Dương', addr_str, flags=re.IGNORECASE).strip()

        # 1. Tìm nguyên văn
        lat, lon = fetch_vietmap_coordinates(clean_addr, session)
        time.sleep(0.3)  # Giãn cách 300ms an toàn

        # 2. Fallback rút gọn địa chỉ nếu không thấy
        if not lat and ',' in clean_addr:
            parts = [p.strip() for p in clean_addr.split(',') if p.strip()]
            while len(parts) > 1 and not lat:
                parts.pop(0)
                fallback_query = ", ".join(parts)
                lat, lon = fetch_vietmap_coordinates(fallback_query, session)
                time.sleep(0.3)

        cache[addr_str] = (lat, lon)
        return lat, lon

    latitudes = []
    longitudes = []

    total_rows = len(df_raw)
    print(f"Bắt đầu xử lý {total_rows} dòng...")

    for idx, (_, row) in enumerate(df_raw.iterrows()):
        # 1. Nếu dòng này đã có kết quả từ lần chạy trước -> Dùng luôn
        if idx in existing_coords:
            lat, lon = existing_coords[idx]
        else:
            # 2. Chưa có thì mới gọi VietMap
            addr = row.get(address_col, '')
            if pd.notna(addr) and str(addr).strip():
                print(f"[{idx+1}/{total_rows}] Đang tìm: {str(addr).strip()[:35]}...")
                lat, lon = get_coords_from_address(str(addr).strip())
            else:
                lat, lon = None, None

        latitudes.append(lat)
        longitudes.append(lon)

    # Xuất ra file CSV
    df_result = pd.DataFrame({
        'vĩ độ': latitudes,
        'kinh độ': longitudes
    })

    df_result.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"\nHoàn tất! Tổng cộng đã có {df_result['vĩ độ'].notnull().sum()}/{total_rows} dòng có tọa độ.")
    print(f"File kết quả lưu tại: {output_file}")


if __name__ == '__main__':
    FILE_INPUT = input("Nhập đường dẫn file gốc (.csv): ").strip().strip('"').strip("'")
    
    # Nhập file kết quả lần trước (ví dụ: ket_qua_toa_do_15_13_33.csv) để chạy nối tiếp
    RESUME_FILE = input("Nhập file kết quả lần trước (hoặc Enter nếu muốn chạy mới): ").strip().strip('"').strip("'")
    
    FILE_OUTPUT = f'ket_qua_toa_do_{datetime.now().strftime("%H_%M_%S")}.csv'
    geocode_addresses(input_file=FILE_INPUT, resume_file=RESUME_FILE, output_file=FILE_OUTPUT, address_col='Địa chỉ')