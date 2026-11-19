"""검증용 SQLite 샘플 DB 생성 (실제 MSSQL 대신 구조 검증용 가짜 데이터)

생성 파일: data/sample/mfg.db  → rdb_client가 'mfg' 스키마로 연결하므로 mfg.A, mfg.Q 로 조회
  mfg.A : 공정 측정값 (B = 검사항목 코드, MEAS_VAL = 측정값)
          C   : 금형온도 (1·2호기)     C3  : 금형온도 (3호기, 센서 교체로 코드 다름)
          P01 : 사출압력 (bar)         D01 : 건조온도 (℃)
  mfg.Q : 외관검사 결과 (LOT·불량 코드별 1행)
          DEFECT_CD = 불량 코드 (WL: 웰드라인, BR: 파단 불량), DEFECT_QTY = 불량 수,
          INSP_QTY = 검사 수량 → 불량률 = DEFECT_QTY / INSP_QTY
          (같은 LOT의 WL·BR 행에 INSP_QTY가 똑같이 들어 있으므로 LOT 단위 합계 시 중복 주의)

데모 시나리오: 3호기에서 11/10~11/12 금형온도(C3)가 50℃ 미만으로 떨어지고
              (지식 그래프: 금형 온도 50도 미만 → 웰드라인), 같은 기간 3호기의 웰드라인(WL)
              불량이 늘어난다. 파단 불량(BR)은 전 기간·전 호기에 드물게만 발생한다.

실행: python make_sample_db.py
  ※ 실행할 때마다 기존 mfg.db를 지우고 다시 생성한다 (random.seed 고정이라 결과는 매번 같음)
"""
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DB = Path(__file__).parent / "data" / "sample" / "mfg.db"
START = datetime(2026, 11, 1)
DAYS = 17
EQUIPMENT = ["1호기", "2호기", "3호기"]
LOTS_PER_DAY = 4
LOW_TEMP_FROM, LOW_TEMP_TO = date(2026, 11, 10), date(2026, 11, 12)  # 3호기 금형온도 저하 기간
LOW_TEMP_MAX = 49.5                   # 저하 기간 금형온도 상한 (지식 그래프의 웰드라인 조건 50도 미만)


def main():
    random.seed(42)
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE A (
            LOT_NO   TEXT NOT NULL,
            EQP_ID   TEXT NOT NULL,
            B        TEXT NOT NULL,     -- 검사항목 코드
            MEAS_VAL REAL,              -- 측정값
            MEAS_DT  TEXT NOT NULL      -- 'YYYY-MM-DD HH:MM:SS'
        );
        CREATE INDEX IX_A_CODE ON A (B, EQP_ID, MEAS_DT);

        CREATE TABLE Q (
            LOT_NO     TEXT NOT NULL,
            EQP_ID     TEXT NOT NULL,
            DEFECT_CD  TEXT NOT NULL,    -- 불량 코드
            DEFECT_QTY INTEGER NOT NULL, -- 불량 수
            INSP_QTY   INTEGER NOT NULL, -- 검사 수량
            INSP_DT    TEXT NOT NULL
        );
        CREATE INDEX IX_Q_CODE ON Q (DEFECT_CD, EQP_ID, INSP_DT);
    """)

    a_rows, q_rows = [], []
    for d in range(DAYS):
        day = START + timedelta(days=d)
        for e_idx, eqp in enumerate(EQUIPMENT, start=1):
            for n in range(LOTS_PER_DAY):
                t = day + timedelta(hours=2 + n * 6, minutes=random.randint(0, 50))
                lot = f"L{day:%m%d}-{e_idx}{n + 1:02d}"
                ts = t.strftime("%Y-%m-%d %H:%M:%S")
                # 월이 바뀌어도 안전하도록 '일'이 아니라 날짜 범위로 비교
                low = eqp == "3호기" and LOW_TEMP_FROM <= day.date() <= LOW_TEMP_TO

                # 금형온도: 1·2호기는 코드 C, 3호기는 센서 교체로 코드 C3 (관리 기준 60℃)
                # 저하 기간에는 모든 측정값이 50℃ 미만이 되도록 상한을 둠
                mold = min(random.gauss(47.5, 1.0), LOW_TEMP_MAX) if low else random.gauss(60.0, 1.2)
                a_rows.append((lot, eqp, "C3" if eqp == "3호기" else "C", round(mold, 1), ts))
                # 사출압력 P01 (규격 800~1200 bar), 건조온도 D01 (규격 80~90℃)
                a_rows.append((lot, eqp, "P01", round(random.gauss(1000, 40)), ts))
                dry_ts = (t - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                a_rows.append((lot, eqp, "D01", round(random.gauss(85, 1.5), 1), dry_ts))

                # 외관검사: LOT당 검사 수량 480~520개, 불량 코드별로 1행씩
                insp_qty = random.randint(480, 520)
                insp_ts = (t + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
                # 웰드라인 불량: 평소 0~1개, 금형온도 저하 시 3~8개
                wl = random.randint(3, 8) if low else random.choice([0, 0, 0, 1])
                q_rows.append((lot, eqp, "WL", wl, insp_qty, insp_ts))
                # 파단 불량: 금형온도와 무관하게 드물게 발생 (그래프 조건 80도 이상은 이 데이터에 없음)
                br = random.choice([0] * 9 + [1])
                q_rows.append((lot, eqp, "BR", br, insp_qty, insp_ts))

    con.executemany("INSERT INTO A VALUES (?, ?, ?, ?, ?)", a_rows)
    con.executemany("INSERT INTO Q VALUES (?, ?, ?, ?, ?, ?)", q_rows)
    con.commit()
    con.close()
    print(f"생성 완료: {DB}")
    print(f"  mfg.A {len(a_rows)}행 (코드 C, C3, P01, D01)")
    print(f"  mfg.Q {len(q_rows)}행 (코드 WL, BR)")


if __name__ == "__main__":
    main()
