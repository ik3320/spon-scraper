import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import os
import sys
import logging
import traceback
from datetime import datetime, timedelta

# ==========================================
# 1. 로깅(Logging) 설정
# ==========================================
timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
log_filename = f"sync_{timestamp_str}.log"
length_txt_filename = f"json_lengths_{timestamp_str}.txt"

logger = logging.getLogger()
logger.setLevel(logging.INFO)

formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

file_handler = logging.FileHandler(log_filename, encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


# ==========================================
# 2. GitHub Actions Secrets 환경변수 불러오기
# ==========================================
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL")

if not GAS_WEB_APP_URL:
    logger.error("[오류] 구글 웹 앱 URL(GAS_WEB_APP_URL)이 환경변수로 세팅되지 않았습니다.")
    sys.exit(1)

PAGE_DELAY = 0.5
STREAMER_DELAY = 3.0
BATCH_SIZE = 10  # 10명씩 묶어서 전송

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_safe_session():
    session = requests.Session()
    retries = Retry(
        total=5, 
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504]
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session

session = get_safe_session()

def fetch_all_matches(elo_id):
    matches = []
    offset = 0
    limit = 200
    
    while True:
        url = f"https://eloboard.co.kr/api/matches?player_id={elo_id}&limit={limit}&offset={offset}"
        try:
            res = session.get(url, headers=HEADERS, timeout=10)
            if res.status_code != 200:
                logger.warning(f"[API 경고] ELO_ID {elo_id} - 응답 코드: {res.status_code}")
                break
            
            data = res.json()
            if not data or len(data) == 0:
                break
                
            matches.extend(data)
            offset += limit
            time.sleep(PAGE_DELAY)
            
        except Exception as e:
            logger.error(f"[API 오류] ELO_ID {elo_id} 데이터 수집 중 예외 발생: {e}")
            break
            
    return matches

def process_spon_data(elo_id, matches):
    if not matches:
        return None

    # played_on 유효한 데이터만 추출 후 정렬
    valid_matches = [m for m in matches if m.get('played_on')]
    if not valid_matches:
        return None

    valid_matches.sort(key=lambda x: x['played_on'])

    first_date = valid_matches[0]['played_on']
    last_date = valid_matches[-1]['played_on']

    today = datetime.now()
    cutoff_date_90 = (today - timedelta(days=90)).strftime('%Y-%m-%d')

    def create_stat_template():
        return {
            "경기수": 0, "승": 0, "패": 0, "승률": 0.0,
            "종족별": {
                "Z": {"경기수": 0, "승": 0, "패": 0, "승률": 0.0},
                "P": {"경기수": 0, "승": 0, "패": 0, "승률": 0.0},
                "T": {"경기수": 0, "승": 0, "패": 0, "승률": 0.0}
            }
        }

    stats = {
        "total": create_stat_template(),
        "90days": create_stat_template()
    }

    categories = ["college_event", "college_war", "college_mini", "team_event", "pro_league", "solo_event"]
    cat_data = {c: {"total": create_stat_template(), "matches": []} for c in categories}

    for m in matches:
        played_on = m.get('played_on', '')
        if not played_on:
            continue
        
        ym = played_on[:7]
        if ym not in stats:
            stats[ym] = create_stat_template()

        participants = m.get('participants', [])
        my_p = next((p for p in participants if str(p.get('player_id')) == str(elo_id)), None)
        opp_p = next((p for p in participants if str(p.get('player_id')) != str(elo_id)), None)

        if not my_p or not opp_p:
            continue

        is_win = (my_p.get('result') == 'win')
        
        opp_race = (opp_p.get('race') or 'Z').upper()
        if opp_race not in ['Z', 'P', 'T']:
            opp_race = 'Z'

        target_scopes = [stats["total"], stats[ym]]
        if played_on >= cutoff_date_90:
            target_scopes.append(stats["90days"])

        for scope in target_scopes:
            scope["경기수"] += 1
            if is_win: scope["승"] += 1
            else: scope["패"] += 1
            
            if opp_race in scope["종족별"]:
                scope["종족별"][opp_race]["경기수"] += 1
                if is_win: scope["종족별"][opp_race]["승"] += 1
                else: scope["종족별"][opp_race]["패"] += 1

        cat = m.get('category')
        if cat in cat_data:
            c_total = cat_data[cat]["total"]
            c_total["경기수"] += 1
            if is_win: c_total["승"] += 1
            else: c_total["패"] += 1

            if opp_race in c_total["종족별"]:
                c_total["종족별"][opp_race]["경기수"] += 1
                if is_win: c_total["종족별"][opp_race]["승"] += 1
                else: c_total["종족별"][opp_race]["패"] += 1

            if cat in ["college_event", "college_war", "college_mini"]:
                my_team_val = (my_p.get('team_name') or '').strip()
                opp_team_val = (opp_p.get('team_name') or '').strip()
            else:
                my_team_val = ""
                opp_team_val = ""

            cat_data[cat]["matches"].append({
                "date": played_on,
                "result": "승" if is_win else "패",
                "opponent": opp_p.get('name', ''),
                "race": opp_race,
                "my_team": my_team_val,
                "opp_team": opp_team_val,
                "matchName": m.get('memo') or m.get('event_name', ''),
                "eventName": m.get('event_name', '')
            })

    def calc_rates(obj):
        tot = obj["경기수"]
        obj["승률"] = round((obj["승"] / tot) * 100, 1) if tot > 0 else 0.0
        for r in ["Z", "P", "T"]:
            rtot = obj["종족별"][r]["경기수"]
            obj["종족별"][r]["승률"] = round((obj["종족별"][r]["승"] / rtot) * 100, 1) if rtot > 0 else 0.0

    for key in stats:
        calc_rates(stats[key])

    for c in categories:
        calc_rates(cat_data[c]["total"])

    return {
        "startDate": first_date,
        "endDate": last_date,
        "sponJson": json.dumps(stats, ensure_ascii=False),
        "college_event": json.dumps(cat_data["college_event"], ensure_ascii=False),
        "college_war": json.dumps(cat_data["college_war"], ensure_ascii=False),
        "college_mini": json.dumps(cat_data["college_mini"], ensure_ascii=False),
        "team_event": json.dumps({"total": cat_data["team_event"]["total"]}, ensure_ascii=False),
        "pro_league": json.dumps({"total": cat_data["pro_league"]["total"]}, ensure_ascii=False),
        "solo_event": json.dumps({"total": cat_data["solo_event"]["total"]}, ensure_ascii=False),
    }

def process_opponent_db(elo_id, streamer_name, matches):
    if not matches:
        return []

    today = datetime.now()
    cutoff_date_90 = (today - timedelta(days=90)).strftime('%Y-%m-%d')

    opponents = {}

    for m in matches:
        played_on = m.get('played_on', '')
        if not played_on:
            continue

        participants = m.get('participants', [])
        my_p = next((p for p in participants if str(p.get('player_id')) == str(elo_id)), None)
        opp_p = next((p for p in participants if str(p.get('player_id')) != str(elo_id)), None)

        if not my_p or not opp_p:
            continue

        opp_name = (opp_p.get('name') or '').strip()
        if not opp_name:
            continue

        opp_race = (opp_p.get('race') or 'Z').upper()
        if opp_race not in ['Z', 'P', 'T']:
            opp_race = 'Z'

        is_win = (my_p.get('result') == 'win')

        if opp_name not in opponents:
            opponents[opp_name] = {
                "race": opp_race,
                "win": 0, "loss": 0,
                "win_90": 0, "loss_90": 0
            }

        opponents[opp_name]["race"] = opp_race

        if is_win:
            opponents[opp_name]["win"] += 1
        else:
            opponents[opp_name]["loss"] += 1

        if played_on >= cutoff_date_90:
            if is_win:
                opponents[opp_name]["win_90"] += 1
            else:
                opponents[opp_name]["loss_90"] += 1

    rows = []
    for opp_name, data in opponents.items():
        w, l = data["win"], data["loss"]
        tot = w + l
        rate = round((w / tot) * 100, 1) if tot > 0 else 0.0

        w90, l90 = data["win_90"], data["loss_90"]
        tot90 = w90 + l90
        rate90 = round((w90 / tot90) * 100, 1) if tot90 > 0 else 0.0

        rows.append([
            elo_id,
            streamer_name,
            opp_name,
            data["race"],
            w,
            l,
            rate,
            w90,
            l90,
            rate90
        ])

    return rows

def flush_batch(spon_batch, opp_batch):
    """배치로 묶인 데이터를 GAS로 전송"""
    if spon_batch:
        try:
            res_spon = requests.post(GAS_WEB_APP_URL, json={
                "action": "updateSponDBData",
                "payload": spon_batch
            }, headers=HEADERS, timeout=60)
            logger.info(f"[배치 전송] 스폰 DB ({len(spon_batch)}명) 업데이트 결과: {res_spon.text}")
        except Exception as e:
            logger.error(f"[배치 오류] 스폰 DB 전송 중 에러 발생: {e}")

    if opp_batch:
        try:
            res_opp = requests.post(GAS_WEB_APP_URL, json={
                "action": "updateOpponentDBData",
                "payload": opp_batch
            }, headers=HEADERS, timeout=60)
            logger.info(f"[배치 전송] 상대전적 DB ({len(opp_batch)}건) 업데이트 결과: {res_opp.text}")
        except Exception as e:
            logger.error(f"[배치 오류] 상대전적 DB 전송 중 에러 발생: {e}")

def run_sync():
    logger.info("=== GitHub Actions 동기화 작업을 시작합니다 ===")
    params = {"action": "getSponDBTargets", "targetCol": "활성"}
    
    try:
        res = requests.get(GAS_WEB_APP_URL, params=params, headers=HEADERS, timeout=15)
        targets = res.json()
    except Exception as e:
        logger.error(f"[초기화 실패] 대상 스트리머 목록을 가져오지 못했습니다: {e}")
        return

    if isinstance(targets, dict) and "error" in targets:
        logger.error(f"[GAS 응답 오류] {targets['error']}")
        return

    total_count = len(targets)
    logger.info(f"[활성 모드] 총 {total_count}명의 대상 스트리머 데이터를 수집합니다 (배치 크기: {BATCH_SIZE}).")

    error_streamers = []
    spon_batch = []
    opp_batch = []

    with open(length_txt_filename, "w", encoding="utf-8") as txt_f:
        txt_f.write("=== 스트리머별 주요 JSON 글자수 기록 ===\n\n")

    for idx, t in enumerate(targets, start=1):
        elo_id = t.get('eloId')
        streamer_name = t.get('streamerName', 'Unknown')
        
        logger.info(f"[{idx}/{total_count}] [{streamer_name}] (ELO_ID: {elo_id}) 수집 시작...")

        try:
            matches = fetch_all_matches(elo_id)
            
            # 1. 스폰 DB 처리 및 배치 추가
            processed = process_spon_data(elo_id, matches)
            if processed:
                processed['rowNum'] = t['rowNum']

                len_spon = len(processed['sponJson'])
                len_event = len(processed['college_event'])
                len_war = len(processed['college_war'])
                len_mini = len(processed['college_mini'])

                log_msg = f"[{streamer_name}] JSON 글자수 -> 스폰: {len_spon}자 | 대회: {len_event}자 | 대학대전: {len_war}자 | 미니대전: {len_mini}자"
                logger.info(log_msg)

                with open(length_txt_filename, "a", encoding="utf-8") as txt_f:
                    txt_f.write(f"[{idx}/{total_count}] {log_msg}\n")

                spon_batch.append(processed)

            # 2. 상대전적 DB 처리 및 배치 추가
            opp_rows = process_opponent_db(elo_id, streamer_name, matches)
            if opp_rows:
                opp_batch.extend(opp_rows)

            logger.info(f"[{idx}/{total_count}] [{streamer_name}] 완료 (경기 수: {len(matches)}건)")

        except Exception as e:
            logger.error(f"[{idx}/{total_count}] [{streamer_name}] (ELO_ID: {elo_id}) 처리 중 에러 발생!")
            logger.error(f"상세 에러 내용: {e}")
            logger.debug(traceback.format_exc())
            error_streamers.append({'name': streamer_name, 'elo_id': elo_id, 'error': str(e)})

        # 배치 크기 도달 시 전송
        if len(spon_batch) >= BATCH_SIZE:
            flush_batch(spon_batch, opp_batch)
            spon_batch = []
            opp_batch = []

        time.sleep(STREAMER_DELAY)

    # 잔여 데이터 전송
    if spon_batch or opp_batch:
        flush_batch(spon_batch, opp_batch)

    if error_streamers:
        logger.warning("==========================================")
        logger.warning(f"총 {len(error_streamers)}명의 스트리머 처리 중 오류가 발생했습니다:")
        for err_info in error_streamers:
            logger.warning(f" - 이름: {err_info['name']} (ELO_ID: {err_info['elo_id']}) | 사유: {err_info['error']}")
        logger.warning("==========================================")
    else:
        logger.info("모든 스트리머의 데이터가 오류 없이 성공적으로 완료되었습니다.")

if __name__ == "__main__":
    run_sync()
