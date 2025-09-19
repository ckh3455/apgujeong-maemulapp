# -*- coding: utf-8 -*-
# 허위매물없는 압구정동 매매 · 임대 실시간 검색 + 매물 알림/의뢰 접수
# 실행: streamlit run "압구정동_매물앱_단계형.py"

import os, re, time
import numpy as np
import pandas as pd
from datetime import datetime
from urllib.parse import quote

import streamlit as st

# ===== 페이지 설정(반드시 최상단 한 번만) =====
# app.py (맨 위 set_page_config 줄만 교체)
st.set_page_config(
    page_title="허위매물없는 압구정동 매매 · 임대 실시간 검색",
    page_icon="assets/thumbnail.png",   # ← 이미지 파일을 아이콘으로
    layout="wide"
)
# 썸네일 강제 생성용: URL에 ?thumb=1 로 접속하면 이미지만 보여주고 종료
from urllib.parse import parse_qs, urlparse
import streamlit as st

qs = st.query_params  # Streamlit 1.32+ (1.29 이하는: st.experimental_get_query_params())
if qs.get("thumb") == "1":
    st.image("thumbnail.png", use_container_width=True)  # 레포 루트에 thumbnail.png
    st.stop()


# 타이틀 아래에 배너 이미지 표시 (선택)
st.title("🏠 허위매물없는 압구정동 매매 · 임대 실시간 검색")

# ===== 앱 설정 및 시트 정보 =====
SHEET_ID = "1QP56lm5kPBdsUhrgcgY2U-JdmukXIkKCSxefd1QExKE"
CSV_BASE = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"
SALE_SHEET_NAME = "매매물건 목록"
RENT_SHEET_NAME = "임대물건 목록"

# (선택) 로컬 엑셀 테스트 경로(없으면 무시)
EXCEL_PATH = ""  # 예) r"D:\OneDrive\office work\00 압구정동 실시간 매물앱\원부동산 매물장.xlsx"

# ===== 유틸 =====
def parse_first_number(val):
    """문자열에서 첫 숫자(정수/실수) 추출 → float, 없으면 NaN"""
    s = str(val)
    if s.strip() == "" or s.lower() == "nan":
        return float('nan')
    m = re.findall(r"\d+\.?\d*", s.replace(",", ""))
    return float(m[0]) if m else float('nan')

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """시트 컬럼 표준화(공개여부/요약내용/층/구역/동/평형/평형대, 금액 컬럼 등)"""
    df = df.copy()
    df.columns = (df.columns.astype(str)
                  .str.replace('\u00a0', ' ', regex=False)
                  .str.replace('\ufeff', '', regex=False)
                  .str.strip())
    alias, cols = {}, df.columns.tolist()

    for c in cols:
        c0 = c.strip()
        lc = c0.lower()

        # 공통
        if "공개" in c0: alias[c0] = "공개여부"
        if any(k in c0 for k in ["요약","특징","메모","비고"]) or ("요역" in c0):
            alias[c0] = "요약내용"
        if c0 in ["층/호","층호","층수"] and "층" not in cols:
            alias[c0] = "층"
        if "구역" in c0 and "구역" not in cols:
            alias[c0] = "구역"
        if c0 in ["동호수"] and "동" not in cols:
            alias[c0] = "동"

        # 매매 금액(만원) 계열 → 가격(만원)
        if (("매매" in c0 or "가격" in c0) and "만원" in c0) and "가격(만원)" not in alias.values():
            alias[c0] = "가격(만원)"

        # 임대 전세금(=금액(억)) 별칭
        if any(k in c0 for k in ["전세금","전세가","전세"]) and ("억" in c0):
            alias[c0] = "금액(억)"
        if c0 in ["가격(억)","임대금액(억)","전세(억)"]:
            alias[c0] = "금액(억)"

        # 임대 보증금/월세 (만원/억 혼재 대응)
        if "보증금" in c0 and "억" in c0:
            alias[c0] = "보증금(억)"
        if "보증금" in c0 and ("만" in c0 or "만원" in c0):
            alias[c0] = "보증금(만원)"
        if ("월세" in c0) and (("만" in c0) or ("만원" in c0) or c0 == "월세"):
            alias[c0] = "월세(만)"

        # 평형대 컬럼(문자열)은 그대로 사용
        if "평형대" in c0.replace(" ", "") and "평형대" not in df.columns:
            alias[c0] = "평형대"

        # 평형(문자열) 별칭
        if "평형" not in cols and c0.replace(" ", "") in ["평형(평)","평수","전용(평)","전용평","전용면적(평)"]:
            alias[c0] = "평형"
        elif "평형" not in cols and ("평" in c0.replace(" ", "")) and ("평형대" not in c0) and ("평당" not in c0) and ("가격" not in c0):
            alias[c0] = "평형"

    if alias:
        df = df.rename(columns=alias)

    # 필수 텍스트 컬럼 보강
    for c in ["구역","동","층","요약내용","평형","평형대"]:
        if c not in df.columns:
            df[c] = ""

    return df

def normalize_zone(z):
    s = str(z).strip()
    if s == "" or s.lower() == "nan":
        return ""
    if s.isdigit():
        return f"{int(s)}구역"
    m = re.match(r"(\d+)\s*구역", s)
    return f"{int(m.group(1))}구역" if m else s

def safe_int_text(x):
    try:
        return str(int(float(str(x))))
    except Exception:
        return str(x)

# ===== 전처리(매매/임대) =====
def enrich_sale(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_columns(df)

    # 금액(억)
    price_col = next((c for c in ["매매가(만원)","가격(만원)","매매(만원)","거래금액(만원)","금액(만원)"] if c in df.columns), None)
    if "금액(억)" in df.columns:
        df["금액(억)"] = pd.to_numeric(df["금액(억)"].apply(parse_first_number), errors="coerce")
    elif price_col:
        df[price_col] = df[price_col].apply(parse_first_number)
        df["금액(억)"] = (df[price_col] / 10000).round(1)
    else:
        df["금액(억)"] = np.nan

    # 평형/평형대
    df["평형"] = df["평형"].astype(str).str.strip()
    df["평형대"] = df["평형대"].astype(str).str.replace(" ", "").str.strip()

    # 구역/동
    df["구역"] = df["구역"].apply(normalize_zone)
    df["동"] = df["동"].apply(safe_int_text)

    # 공개여부 표준화
    if "공개여부" in df.columns:
        df["공개여부_norm"] = df["공개여부"].astype(str).str.strip().str.lower().map(
            {"y":"y","yes":"y","true":"y","1":"y","공개":"y"}
        ).fillna("n")
    else:
        df["공개여부_norm"] = "y"

    # 요약 기본값
    df["요약내용"] = df["요약내용"].fillna("").apply(lambda x: "상태 보통" if str(x).strip()=="" else x)
    return df

def enrich_rent(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_columns(df)

    # 전세: 금액(억)
    if "금액(억)" in df.columns:
        df["금액(억)"] = pd.to_numeric(df["금액(억)"].apply(parse_first_number), errors="coerce")
    else:
        won_cols = [c for c in df.columns if ("금액" in c and "만원" in c)]
        if won_cols:
            base = won_cols[0]
            df["금액(억)"] = pd.to_numeric(df[base].apply(parse_first_number), errors="coerce")/10000
        else:
            df["금액(억)"] = np.nan

    # 보증금(억) / 월세(만)
    if "보증금(억)" in df.columns:
        df["보증금(억)"] = pd.to_numeric(df["보증금(억)"].apply(parse_first_number), errors="coerce")
    else:
        dep_col = next((c for c in ["보증금(만원)","보증금(만)"] if c in df.columns), None)
        if dep_col:
            df["보증금(억)"] = pd.to_numeric(df[dep_col].apply(parse_first_number), errors="coerce")/10000
        else:
            df["보증금(억)"] = np.nan

    if "월세(만)" in df.columns:
        df["월세(만)"] = pd.to_numeric(df["월세(만)"].apply(parse_first_number), errors="coerce")
    else:
        if "월세" in df.columns:
            df["월세(만)"] = pd.to_numeric(df["월세"].apply(parse_first_number), errors="coerce")
        else:
            df["월세(만)"] = np.nan

    # 평형/평형대
    df["평형"] = df["평형"].astype(str).str.strip()
    df["평형대"] = df["평형대"].astype(str).str.replace(" ", "").str.strip()

    # 구역/동
    df["구역"] = df["구역"].apply(normalize_zone)
    df["동"] = df["동"].apply(safe_int_text)

    # 공개여부 표준화
    if "공개여부" in df.columns:
        df["공개여부_norm"] = df["공개여부"].astype(str).str.strip().str.lower().map(
            {"y":"y","yes":"y","true":"y","1":"y","공개":"y"}
        ).fillna("n")
    else:
        df["공개여부_norm"] = "y"

    df["요약내용"] = df["요약내용"].fillna("").apply(lambda x: "상태 보통" if str(x).strip()=="" else x)
    return df

@st.cache_data(ttl=60)
def load_sheet(sheet_name: str, kind: str, nonce: int | None = None):
    """kind: 'sale' or 'rent'"""
    url = f"{CSV_BASE}&sheet={quote(sheet_name)}"
    if nonce is not None:
        url += f"&cacheBust={nonce}"
    try:
        df = pd.read_csv(url)
        if kind == "sale":
            return enrich_sale(df), f"csv:{sheet_name}"
        else:
            return enrich_rent(df), f"csv:{sheet_name}"
    except Exception as e:
        st.warning(f"{sheet_name} 시트를 CSV로 불러오지 못했습니다: {e}. 엑셀로 시도합니다.")

    if EXCEL_PATH:
        if not os.path.exists(EXCEL_PATH):
            st.error(f"엑셀 파일을 찾을 수 없습니다: {EXCEL_PATH}")
            st.stop()
        try:
            df = pd.read_excel(EXCEL_PATH, sheet_name=sheet_name)
            if kind == "sale":
                return enrich_sale(df), f"excel:{sheet_name}"
            else:
                return enrich_rent(df), f"excel:{sheet_name}"
        except Exception as e:
            st.error(f"엑셀 '{sheet_name}' 로드 실패: {e}")
            st.stop()

    st.error(f"데이터 소스를 찾을 수 없습니다. 시트 '{sheet_name}' 접근 권한/이름을 확인하세요.")
    st.stop()

# ===== 사이드바: 업소 홍보 + 캐시 버튼 =====
with st.sidebar:
    st.markdown("### 🏢 압구정 원 부동산중개")
    st.markdown(
        "- 압구정동 허위매물 없는 실매물/임대 실시간 업데이트\n"
        "- 가격변동이나 거래발생시  즉시 문자 알림\n"
        "- 신속/정확/투명한 중개 서비스"
    )
    st.markdown("**대표번호:** **02-540-3334**  \n**모바일(최이사):** **010-3065-1780**")

    if "refresh_nonce" not in st.session_state:
        st.session_state["refresh_nonce"] = None

    if st.button("🔁 시트 다시 읽기 / 캐시 비우기", use_container_width=True):
        st.cache_data.clear()
        st.session_state["refresh_nonce"] = int(time.time())
        st.success("캐시를 비웠습니다. 다시 조회해 주세요.")

# ===== 알림/의뢰 접수용 웹훅 =====
def _get_secret(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        return st.secrets[name]
    except Exception:
        return default

try:
    import requests
except ImportError:
    requests = None

GAS_WEBHOOK_URL = _get_secret("GAS_WEBHOOK_URL", "")

def _clean_phone(s: str) -> str:
    if s is None:
        return ""
    return re.sub(r"[^\d\-]", "", str(s)).strip()

def _post_to_gas(values):
    if not GAS_WEBHOOK_URL:
        return False, "NO_URL"
    if requests is None:
        return False, "requests 미설치"

    try:
        payload = {"sheet": "발송명단", "values": values}
        r = requests.post(GAS_WEBHOOK_URL, json=payload, timeout=10)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code == 200 and data.get("ok"):
            return True, data
        return False, f"HTTP {r.status_code} / {data}"
    except Exception as e:
        return False, str(e)

# ===== 매물카드 표시 =====
def card_sale_row(r):
    title = f"**{r['구역']} · {r['평형']} · {r['동']}동 {r['층']}층**"
    price = "—" if pd.isna(r["금액(억)"]) else f"**{float(r['금액(억)']):.1f}억**"
    summary = str(r["요약내용"])
    short = (summary[:60] + "…") if len(summary) > 60 else summary
    st.markdown(f"{title}  \n{price} — {short}")
    st.divider()

def card_rent_row(r):
    title = f"**{r['구역']} · {r['평형']} · {r['동']}동 {r['층']}층**"
    has_jeonse = not pd.isna(r.get("금액(억)", np.nan))
    has_depo   = not pd.isna(r.get("보증금(억)", np.nan))
    if has_jeonse:
        price = f"**(전세)** {float(r['금액(억)']):.1f}(억)"
    elif has_depo:
        wol = r.get("월세(만)", np.nan)
        parts = [f"보증금 {float(r['보증금(억)']):.1f}(억)"]
        if not pd.isna(wol) and float(wol) != 0:
            parts.append(f"월 {int(float(wol))}(만)")
        price = f"**(월세)** " + " / ".join(parts)
    else:
        price = ""
    summary = str(r["요약내용"])
    short = (summary[:60] + "…") if len(summary) > 60 else summary
    st.markdown(f"{title}  \n{price} — {short}")
    st.divider()

# ===== 세션 기본값 =====
if "dataset" not in st.session_state: st.session_state.dataset = None  # 'sale' or 'rent'
if "mode" not in st.session_state: st.session_state.mode = None
if "page" not in st.session_state: st.session_state.page = 1
if "out_df" not in st.session_state: st.session_state.out_df = None
if "source_kind" not in st.session_state: st.session_state.source_kind = ""
if "results_ready" not in st.session_state: st.session_state.results_ready = False

def reset_all():
    st.session_state.dataset = None
    st.session_state.mode = None
    st.session_state.page = 1
    st.session_state.out_df = None
    st.session_state.results_ready = False

def to_dataset(d):
    st.session_state.dataset = d
    st.session_state.mode = None
    st.session_state.page = 1
    st.session_state.out_df = None
    st.session_state.results_ready = False

def to_mode(m):
    st.session_state.mode = m
    st.session_state.page = 1
    st.session_state.out_df = None
    st.session_state.results_ready = False

# ===== 최상위 선택 =====
if st.session_state.dataset is None:
    st.subheader("검색 대상을 선택하세요")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🏷️ 매매물건 검색", use_container_width=True):
            to_dataset("sale")
            st.rerun()
    with c2:
        if st.button("🧾 임대물건 검색", use_container_width=True):
            to_dataset("rent")
            st.rerun()
    # --- [추가] 모바일에서도 보이는 업소 홍보 문구 (버튼 아래) ---
    st.markdown(
        """
        <div style="
            margin-top:14px;
            padding:12px 16px;
            border-radius:12px;
            background:rgba(255,75,75,0.06);
            text-align:center;
            line-height:1.45;
            font-size:15px;
        ">
            <b>🏢 압구정 원 부동산중개</b><br/>
            허위매물 없는 실매물/임대 실시간 업데이트<br/>
            가격 변동·거래 발생 시 즉시 문자 알림<br/>
            <span style="opacity:.9;">대표번호</span> <b>02-540-3334</b> ·
            <span style="opacity:.9;">모바일(최이사)</span> <b>010-3065-1780</b>
        </div>
        """,
        unsafe_allow_html=True,
    )
  
    st.stop()
else:
    st.button("⬅ 처음으로", on_click=reset_all)

# ===== 데이터 로드 =====
nonce = st.session_state.get("refresh_nonce")
if st.session_state.dataset == "sale":
    df, st.session_state.source_kind = load_sheet(SALE_SHEET_NAME, "sale", nonce)
elif st.session_state.dataset == "rent":
    df, st.session_state.source_kind = load_sheet(RENT_SHEET_NAME, "rent", nonce)
else:
    st.stop()

data = df[df["공개여부_norm"] == "y"].copy()

# ===== 검색유형 선택 =====
if st.session_state.mode is None:
    if st.session_state.dataset == "sale":
        st.subheader("검색 유형을 선택하세요 (매매)")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("💰 금액대별 검색", use_container_width=True): to_mode("price_sale"); st.rerun()
        with c2:
            if st.button("🗺️ 구역별 검색", use_container_width=True): to_mode("zone_sale"); st.rerun()
        with c3:
            if st.button("📐 평형대별 검색", use_container_width=True): to_mode("pyeong_sale"); st.rerun()
    else:
        st.subheader("검색 유형을 선택하세요 (임대)")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("💵 금액별 검색(전세/보증금)", use_container_width=True): to_mode("amount_rent"); st.rerun()
        with c2:
            if st.button("🗺️ 구역별 검색", use_container_width=True): to_mode("zone_rent"); st.rerun()
        with c3:
            if st.button("📐 평형대별 검색", use_container_width=True): to_mode("pyeong_rent"); st.rerun()
    st.stop()

# ===== 검색 입력 위젯 & 조회 =====
BAND_ORDER = ["20평형대","30평형대","40평형대","50평형대","60평형대","70평형대","80평형대"]

def run_query_and_store(out_df):
    st.session_state.out_df = out_df
    st.session_state.results_ready = True
    st.session_state.page = 1

# --- 매매
if st.session_state.mode == "price_sale":
    st.subheader("💰 금액대별 검색 (매매)")
    st.caption("최소금액과 최대금액을 (억) 단위로 선택하시고 조회버튼을 눌러주세요")
    st.caption("매물검색 후 ‘조회’를 누르면 하단에 검색결과가 표시됩니다.")

    v = pd.to_numeric(data["금액(억)"], errors="coerce").dropna()
    min_eok, max_eok = (0.0, 100.0) if v.empty else (float(np.floor(v.min())), float(np.ceil(v.max())))
    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        min_in = st.number_input("최소(억)", min_value=0.0, max_value=max_eok, value=min_eok, step=0.1, format="%.1f", key="sale_min")
    with c2:
        max_in = st.number_input("최대(억)", min_value=min_in, max_value=max_eok, value=max_eok, step=0.1, format="%.1f", key="sale_max")
    with c3:
        run = st.button("조회", type="primary", use_container_width=True, key="sale_price_btn")
    if run:
        q = data[pd.to_numeric(data["금액(억)"], errors="coerce").between(min_in, max_in)]
        out = q[["평형대","구역","평형","동","층","금액(억)","요약내용"]].copy()
        out["금액(억)"] = pd.to_numeric(out["금액(억)"], errors="coerce")
        out = out.sort_values(by="금액(억)", ascending=True, na_position="last")
        run_query_and_store(out)

elif st.session_state.mode == "zone_sale":
    st.subheader("🗺️ 구역별 검색 (매매)")
    zones = ["1구역","2구역","3구역","4구역","5구역","6구역"]
    c1, c2 = st.columns([2,1])
    with c1:
        zone = st.selectbox("구역", zones, index=0, key="sale_zone")
    with c2:
        run = st.button("조회", type="primary", use_container_width=True, key="sale_zone_btn")
    if run:
        q = data[data["구역"].astype(str) == zone]
        out = q[["평형대","구역","평형","동","층","금액(억)","요약내용"]].copy()
        out["금액(억)"] = pd.to_numeric(out["금액(억)"], errors="coerce")
        out = out.sort_values(by="금액(억)", ascending=True, na_position="last")
        run_query_and_store(out)

elif st.session_state.mode == "pyeong_sale":
    st.subheader("📐 평형대별 검색 (매매)")
    c1, c2 = st.columns([2,1])
    with c1:
        band = st.selectbox("평형대", BAND_ORDER, index=2, key="sale_band")
    with c2:
        run = st.button("조회", type="primary", use_container_width=True, key="sale_pyeong_btn")
    if run:
        q = data[data["평형대"].astype(str).str.replace(" ", "") == band]
        out = q[["평형대","구역","평형","동","층","금액(억)","요약내용"]].copy()
        out["금액(억)"] = pd.to_numeric(out["금액(억)"], errors="coerce")
        out = out.sort_values(by="금액(억)", ascending=True, na_position="last")
        run_query_and_store(out)

# --- 임대
elif st.session_state.mode == "amount_rent":
    st.subheader("💵 금액별 검색 (임대: 전세 ‘금액(억)’ + 월세 ‘보증금(억)’ 포함)")
    v_j = pd.to_numeric(data["금액(억)"], errors="coerce")
    v_d = pd.to_numeric(data["보증금(억)"], errors="coerce")
    base = pd.concat([v_j.dropna(), v_d.dropna()])
    min_eok, max_eok = (0.0, 50.0) if base.empty else (float(np.floor(base.min())), float(np.ceil(base.max())))
    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        min_in = st.number_input("최소(억)", min_value=0.0, max_value=max_eok, value=min_eok, step=0.1, format="%.1f", key="rent_min")
    with c2:
        max_in = st.number_input("최대(억)", min_value=min_in, max_value=max_eok, value=max_eok, step=0.1, format="%.1f", key="rent_max")
    with c3:
        run = st.button("조회", type="primary", use_container_width=True, key="rent_amt_btn")
    if run:
        m = (v_j.between(min_in, max_in, inclusive="both")) | (v_d.between(min_in, max_in, inclusive="both"))
        q = data[m].copy()
        q["_정렬액"] = v_j.where(~v_j.isna(), v_d)
        q["월세(만)"] = pd.to_numeric(q["월세(만)"], errors="coerce")
        q = q.sort_values(by=["_정렬액","월세(만)"], ascending=[True, True], na_position="last")
        out = q[["평형대","구역","평형","동","층","금액(억)","보증금(억)","월세(만)","요약내용"]].copy()
        run_query_and_store(out)

elif st.session_state.mode == "zone_rent":
    st.subheader("🗺️ 구역별 검색 (임대)")
    zones = ["1구역","2구역","3구역","4구역","5구역","6구역"]
    c1, c2 = st.columns([2,1])
    with c1:
        zone = st.selectbox("구역", zones, index=0, key="rent_zone")
    with c2:
        run = st.button("조회", type="primary", use_container_width=True, key="rent_zone_btn")
    if run:
        q = data[data["구역"].astype(str) == zone].copy()
        q["_정렬액"] = pd.to_numeric(q["금액(억)"], errors="coerce").where(
            ~pd.to_numeric(q["금액(억)"], errors="coerce").isna(),
            pd.to_numeric(q["보증금(억)"], errors="coerce")
        )
        q["월세(만)"] = pd.to_numeric(q["월세(만)"], errors="coerce")
        q = q.sort_values(by=["_정렬액","월세(만)"], ascending=[True, True], na_position="last")
        out = q[["평형대","구역","평형","동","층","금액(억)","보증금(억)","월세(만)","요약내용"]].copy()
        run_query_and_store(out)

elif st.session_state.mode == "pyeong_rent":
    st.subheader("📐 평형대별 검색 (임대)")
    c1, c2 = st.columns([2,1])
    with c1:
        band = st.selectbox("평형대", BAND_ORDER, index=2, key="rent_band")
    with c2:
        run = st.button("조회", type="primary", use_container_width=True, key="rent_pyeong_btn")
    if run:
        q = data[data["평형대"].astype(str).str.replace(" ", "") == band].copy()
        q["_정렬액"] = pd.to_numeric(q["금액(억)"], errors="coerce").where(
            ~pd.to_numeric(q["금액(억)"], errors="coerce").isna(),
            pd.to_numeric(q["보증금(억)"], errors="coerce")
        )
        q["월세(만)"] = pd.to_numeric(q["월세(만)"], errors="coerce")
        q = q.sort_values(by=["_정렬액","월세(만)"], ascending=[True, True], na_position="last")
        out = q[["평형대","구역","평형","동","층","금액(억)","보증금(억)","월세(만)","요약내용"]].copy()
        run_query_and_store(out)

# ===== 알림/의뢰 접수(폼) — 결과 위에 배치 =====
st.markdown("---")
st.subheader("📩 매물 알림/의뢰 접수")

st.info(
    "압구정동 현직 중개업자의 매물장입니다. 변동되는 가격과 거래내역 그리고   \n매물내역을 받아보길 원하시는분들께  **하루1회 실시간 문자**로 알려드립니다. "
    "입력하신 전화번호는 매수/임차 희망자의 경우에 알림 발송 용도로만 사용됩니다."
)

req_type = st.radio(
    "의뢰를 원하시는 항목을 골라주세요",
    ["매도의뢰", "매수의뢰", "임대의뢰", "임차의뢰"],
    horizontal=True,
    key="req_type_radio",
)

# 공통 연락처
phone = st.text_input("연락처 (숫자/하이픈 가능)", key="req_phone", placeholder="010-1234-5678")
phone_clean = _clean_phone(phone)

# 분기: 폼 구성
if req_type in ("매도의뢰", "임대의뢰"):
    colz1, colz2 = st.columns([1, 2])
    with colz1:
        zone = st.selectbox("구역 선택", ["1구역","2구역","3구역","4구역","5구역","6구역"], index=0, key="req_zone")
    with colz2:
        dong = st.text_input("동 (예: 101)", key="req_dong", placeholder="예) 101")

    floorho = st.text_input("층/호 (예: 1203호)", key="req_floorho", placeholder="예) 1203호")
    memo = st.text_area("특별히 원하시는 내용", key="req_memo_sale", height=80, placeholder="예) 희망금액,지불조건, 리모델링 여부 등")
    spec = f"구역:{zone} / 동:{dong.strip()} / 층·호:{floorho.strip()}"
    if memo.strip():
        spec += f" / 요청:{memo.strip()}"

elif req_type == "매수의뢰":
    amount_possible = st.text_input("매수희망액 (대출포함, 억 단위)", key="req_buy_cap", placeholder="예) 50")
    memo = st.text_area("특별히 원하시는 내용", key="req_memo_buy", height=80, placeholder="예) 선호 구역/평형/조건 등")
    show_amount = amount_possible.strip()
    if show_amount and not show_amount.endswith("억"):
        show_amount = f"{show_amount}억"
    spec = f"매수희망액:{show_amount}"
    if memo.strip():
        spec += f" / 요청:{memo.strip()}"

else:  # 임차의뢰
    wish_pyeong = st.text_input("희망평형", key="req_rent_pyeong", placeholder="예) 30평대")
    wish_amount = st.text_input("희망금액", key="req_rent_amt", placeholder="예) 전세 12억  or  보증 3억 / 월 200만")
    memo = st.text_area("특별히 원하시는 지역이나 내용이 있으시면 적어주세요", key="req_memo_rent", height=80, placeholder="예) 선호 구역/단지/조건/입주일 등")
    spec = f"희망평형:{wish_pyeong.strip()} / 희망금액:{wish_amount.strip()}"
    if memo.strip():
        spec += f" / 요청:{memo.strip()}"

submit = st.button("접수하기", type="primary", use_container_width=True, key="req_submit_btn")

if submit:
    if not phone_clean:
        st.error("연락처를 입력해 주세요.")
    else:
        now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [now_txt, req_type, phone_clean, spec]
        ok, result = _post_to_gas(row)
        if ok:
            st.success("정상 접수되었습니다. 빠르게 연락드리겠습니다.")
        else:
            st.error(f"전송 실패: {result}")

# ===== 검색 결과 영역 =====
st.markdown("---")
st.caption(f"데이터 소스: {st.session_state.source_kind}")

st.markdown("### 🔎 검색 결과")
if not st.session_state.results_ready or st.session_state.out_df is None or len(st.session_state.out_df) == 0:
    st.info("조건에 맞는 매물이 없습니다. 범위를 넓혀 다시 조회해 보세요.")
else:
    out = st.session_state.out_df

    # 페이지네이션
    PAGE_SIZE = 25
    total = max(1, int(np.ceil(len(out) / PAGE_SIZE)))
    cur = max(1, min(st.session_state.page, total))
    start = (cur - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    view_df = out.iloc[start:end]

    # 카드 표시
    if st.session_state.dataset == "sale":
        for _, r in view_df.iterrows():
            card_sale_row(r)
    else:
        for _, r in view_df.iterrows():
            card_rent_row(r)

    # 페이지 컨트롤
    colp1, colp2, colp3 = st.columns([1,1,1])
    with colp1:
        if st.button("◀ 이전", use_container_width=True) and st.session_state.page > 1:
            st.session_state.page -= 1
            st.rerun()
    with colp2:
        st.write(f"페이지 {cur} / {total}")
    with colp3:
        if st.button("다음 ▶", use_container_width=True) and st.session_state.page < total:
            st.session_state.page += 1
            st.rerun()

# ===== 내비게이션 버튼 =====
cback1, cback2 = st.columns([1,1])
with cback1:
    if st.button("◀ 검색유형으로 돌아가기", use_container_width=True):
        st.session_state.mode = None
        st.session_state.page = 1
        st.rerun()
with cback2:
    if st.button("⬅ 처음으로", use_container_width=True):
        reset_all()
        st.rerun()





