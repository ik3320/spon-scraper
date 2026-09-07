import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import os
import sys
from datetime import datetime, timedelta

# GitHub Actions Secrets 환경변수 불러오기
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL")

if not GAS_WEB_APP_URL:
    print("[오류] 구글 웹 앱 URL(GAS_WEB_APP_URL)이 환경변수로 세팅되지 않았습니다.")
    sys.exit(1)

PAGE_DELAY = 0.3
STREAMER_DELAY = 3.0

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
                break
            
            data = res.json()
            if not data or len(data) == 0:
                break
                
            matches.extend(data)
            offset += limit
            
            time.sleep(PAGE_DELAY)
            
        except Exception as e:
            print(f"[Error] API Fetch failed for ELO_ID {elo_id}: {e}")
            break
            
    return matches

def process_spon_data(elo_id, matches):
    if not matches:
        return None

    matches.sort(key=lambda x: x.get('played_on', ''))

    first_date = matches[0].get('played_on')
    last_date = matches[-1].get('played_on')

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

            # --- [팀 정보 분기 처리] ---
            # 대회(college_event), 대학대전(college_war), 미니대전(college_mini)만 팀명 수집
            if cat in ["college_event", "college_war", "college_mini"]:
                my_team_val = (my_p.get('team_name') or '').strip()
                opp_team_val = (opp_p.get('team_name') or '').strip()
            else:
                # 팀리그(team_event), 프로리그(pro_league), 개인전(solo_event) 등은 빈 값 처리
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
        "team_event": json.dumps(cat_data["team_event"], ensure_ascii=False),
        "pro_league": json.dumps(cat_data["pro_league"], ensure_ascii=False),
        "solo_event": json.dumps(cat_data["solo_event"], ensure_ascii=False),
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

def run_sync():
    # URL 파라미터 전달 방식 개선
    params = {"action": "getSponDBTargets", "targetCol": "활성"}
    
    try:
        res = requests.get(GAS_WEB_APP_URL, params=params, headers=HEADERS, timeout=15)
        targets = res.json()
    except Exception as e:
        print(f"[오류] 대상 스트리머 목록을 가져오지 못했습니다: {e}")
        return

    if isinstance(targets, dict) and "error" in targets:
        print(f"[오류 발생] {targets['error']}")
        return
        
    spon_payload = []
    all_opponent_rows = []
    print(f"[활성 모드] 총 {len(targets)}명의 대상 스트리머 데이터를 수집합니다.")

    for t in targets:
        elo_id = t['eloId']
        streamer_name = t['streamerName']
        print(f"[{streamer_name}] (ELO_ID: {elo_id}) 수집 중...")
        matches = fetch_all_matches(elo_id)
        
        processed = process_spon_data(elo_id, matches)
        if processed:
            processed['rowNum'] = t['rowNum']
            spon_payload.append(processed)

        opp_rows = process_opponent_db(elo_id, streamer_name, matches)
        all_opponent_rows.extend(opp_rows)

        time.sleep(STREAMER_DELAY)

    if spon_payload:
        update_res = requests.post(GAS_WEB_APP_URL, json={
            "action": "updateSponDBData",
            "payload": spon_payload
        }, headers=HEADERS)
        print("스폰 DB 업데이트 결과:", update_res.text)

    if all_opponent_rows:
        opp_res = requests.post(GAS_WEB_APP_URL, json={
            "action": "updateOpponentDBData",
            "payload": all_opponent_rows
        }, headers=HEADERS)
        print("상대전적 DB 업데이트 결과:", opp_res.text)

if __name__ == "__main__":
    run_sync()
