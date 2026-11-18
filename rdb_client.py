"""실측 데이터 RDB조회 (읽기 전용)

현재 : SQLite 샘플 DB (data/sample/*.db 파일 하나가 스키마 하나 -> mfg.db = mfg 스키마)
나중 : MSSQL (RDB_TYPE = mssql, RDB_MSSQL_CONN=ODBC 연결 문자열) -> 사내망 환경에서 검증 필요

안전장치
1. SELECT/WITH 로 시작하는 단일 문장만 허용, 쓰기/관리 키워드 차단
2. FROM/JOIN 대상은 지식 그래프에 :TABLE 로 등록한 테이블만 허용
   (노드 예: (:TABLE {name: "mfg.MEAS_DATA"}) - name은 "스키마.테이블", 대소문자 무관)
3. 읽기 전용 연결 (SQLite: mode=ro + query_only), 값은 바인딩 파라메터로만 전달
4. 행 수(MAX_ROWS) 및 실행 시간(TIMEOUT_SEC) 제한

SQLite는 1, 2를 authorizer로 한 번 더 막는다 (정규식 검사를 우회해도 DB가 거부).

사용 예)
    from rdb_client import run_sql
    run_sql("SELECT * FROM mfg.MEAS_DATA WHERE PARAM = ? LIMIT 10", ["금형 온도"])
"""
import os
import re
import sqlite3
import time
from pathlib import Path

RDB_TYPE = os.getenv("RDB_TYPE", "sqlite").lower()
SQLITE_DIR = Path(os.getenv("RDB_SQLITE_DIR", Path(__file__).parent / "data" / "sample"))
MAX_ROWS = int(os.getenv("RDB_MAX_ROWS", "100"))
TIMEOUT_SEC = int(os.getenv("RDB_TIMEOUT_SEC", "10"))
TABLE_LABEL = "TABLE"  # 허용 테이블을 등록하는 지식 그래프 노드 라벨

DIALECT = {
    "sqlite": "SQLite(행 제한은 LIMIT n, 날짜는 'YYYY-MM-DD HH:MM:SS' 문자열이며 "
              "date(MEAS_DT), strftime('%Y-%m-%d', 컬럼) 사용 가능)",
    "mssql": "Microsoft SQL Server T-SQL(행 제한은 SELECT TOP n, 날짜는 CAST(컬럼 AS DATE))",
}.get(RDB_TYPE, RDB_TYPE)

# 쓰기/관리 키워드 (주석·문자열을 지운 뒤 검사하므로 WHERE NOTE = 'delete' 같은 값은 통과)
# 컬럼명 UPDATE_DT 등은 \b 경계 때문에 걸리지 않음. replace() 함수는 허용하고 REPLACE INTO만 차단
_BLOCKED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|CREATE|ALTER|DROP|TRUNCATE|RENAME"
    r"|GRANT|REVOKE|DENY|EXEC|EXECUTE|CALL|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE"
    r"|BACKUP|RESTORE|BULK|OPENROWSET|OPENQUERY|OPENDATASOURCE|SHUTDOWN|KILL|DBCC"
    r"|USE|DECLARE|INTO|WAITFOR|LOAD_EXTENSION|XP_\w+|SP_\w+)\b"
    r"|\bREPLACE\s+INTO\b",
    re.IGNORECASE)

_IDENT = r'(?:\[[^\]]+\]|"[^"]+"|`[^`]+`|\w+)'          # 이름, [이름], "이름", `이름`
_TABLE_REF = rf"{_IDENT}(?:\s*\.\s*{_IDENT})*"           # 스키마.테이블 (점으로 연결)


def _strip(sql):
    """주석 제거 + 문자열 리터럴을 ''로 치환 (키워드·테이블 검사용)"""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def _norm(name):
    """[mfg].[MEAS_DATA], "mfg"."MEAS_DATA" -> mfg.meas_data"""
    parts = re.findall(_IDENT, name)
    return ".".join(p.strip('[]"`') for p in parts).lower()


def _tables(clean):
    """FROM/JOIN 뒤의 테이블 이름 (쉼표 조인 포함, 서브쿼리 '(' 는 제외)"""
    found = []
    stop = r"(?:WHERE|GROUP|ORDER|HAVING|LIMIT|UNION|EXCEPT|INTERSECT|ON|USING|WINDOW" \
           r"|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|NATURAL|OUTER|OFFSET|FETCH|FOR|OPTION)\b"
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+", clean, re.IGNORECASE):
        pos = m.end()
        while True:
            t = re.match(rf"\s*({_TABLE_REF})", clean[pos:])
            if not t or clean[pos:].lstrip().startswith("("):
                break
            found.append(_norm(t.group(1)))
            pos += t.end()
            # 별칭(AS x 또는 x) 건너뛰기
            alias = re.match(rf"\s+(?:AS\s+)?(?!{stop}){_IDENT}", clean[pos:], re.IGNORECASE)
            if alias:
                pos += alias.end()
            comma = re.match(r"\s*,", clean[pos:])
            if not comma:
                break
            pos += comma.end()
    return found


def _cte_names(clean):
    """WITH x AS (...), y AS (...) 의 x, y (FROM에서 참조해도 허용)"""
    if not re.match(r"\s*WITH\b", clean, re.IGNORECASE):
        return set()
    return {_norm(n) for n in re.findall(
        rf"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)({_IDENT})\s*(?:\([^)]*\))?\s*AS\s*\(",
        clean, re.IGNORECASE)}


def check_sql(sql, allowed):
    """안전장치 1, 2 검사. 문제가 있으면 오류 메시지, 없으면 None"""
    clean = _strip(sql).strip()
    clean = re.sub(r";\s*$", "", clean)  # 끝의 세미콜론 하나는 허용
    if ";" in clean:
        return "여러 문장은 허용되지 않습니다. SELECT 문 하나만 사용하세요."
    if not re.match(r"(SELECT|WITH)\b", clean, re.IGNORECASE):
        return "SELECT 또는 WITH로 시작하는 조회 쿼리만 허용됩니다."
    m = _BLOCKED.search(clean)
    if m:
        return f"허용되지 않는 키워드입니다: {m.group(0).upper()}"
    allowed = {a.lower() for a in allowed}
    ctes = _cte_names(clean)
    denied = sorted({t for t in _tables(clean) if t not in allowed and t not in ctes})
    if denied:
        return (f"등록되지 않은 테이블입니다: {', '.join(denied)}. "
                f"허용 테이블: {', '.join(sorted(allowed)) or '(없음)'}")
    return None


def allowed_tables(store=None):
    """지식 그래프에 :TABLE 로 등록된 테이블 이름 목록 (소문자)"""
    from graph_store import GraphStore
    own = store is None
    store = store or GraphStore()
    try:
        with store.driver.session() as s:
            rows = s.execute_read(lambda tx: tx.run(
                f"MATCH (t:`{TABLE_LABEL}`) WHERE t.name IS NOT NULL RETURN t.name AS name").data())
        return {r["name"].lower() for r in rows}
    finally:
        if own:
            store.close()


# ---------- SQLite ----------
def _sqlite_connect(allowed):
    # 메모리 DB에 각 .db 파일을 읽기 전용(mode=ro)으로 ATTACH -> 파일 이름이 스키마 이름
    conn = sqlite3.connect("file::memory:", uri=True, check_same_thread=False)
    for db in sorted(SQLITE_DIR.glob("*.db")):
        if re.fullmatch(r"\w+", db.stem):
            conn.execute(f"ATTACH DATABASE ? AS {db.stem}", (db.resolve().as_uri() + "?mode=ro",))
    conn.execute("PRAGMA query_only = ON")

    # 2차 방어: 조회(SELECT/READ/함수)만 허용하고, 읽는 테이블도 허용 목록으로 제한
    def auth(action, arg1, arg2, dbname, _trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if dbname is None:  # CTE 등 실제 테이블이 아닌 대상 (실제 테이블은 항상 스키마 이름이 있음)
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_OK if f"{dbname}.{arg1}".lower() in allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    conn.set_authorizer(auth)

    deadline = [0.0]
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline[0] else 0, 10000)
    return conn, deadline


def _run_sqlite(sql, params, allowed):
    conn, deadline = _sqlite_connect(allowed)
    try:
        deadline[0] = time.monotonic() + TIMEOUT_SEC
        try:
            cur = conn.execute(sql, params or [])
            rows = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise TimeoutError(f"실행 시간 {TIMEOUT_SEC}초를 초과했습니다.") from e
            raise
        cols = [d[0] for d in cur.description or []]
        return cols, rows
    finally:
        conn.close()


# ---------- MSSQL (사내망에서 검증 필요) ----------
def _run_mssql(sql, params, allowed):
    import pyodbc
    conn = pyodbc.connect(os.environ["RDB_MSSQL_CONN"], autocommit=False, readonly=True)
    try:
        conn.timeout = TIMEOUT_SEC  # 쿼리 실행 시간 제한(초)
        cur = conn.cursor()
        cur.execute(sql, params or [])
        rows = cur.fetchmany(MAX_ROWS + 1)
        cols = [d[0] for d in cur.description or []]
        return cols, [tuple(r) for r in rows]
    finally:
        conn.rollback()  # 혹시 모를 변경도 반영하지 않음
        conn.close()


def run_sql(sql, params=None, allowed=None):
    """읽기 전용 SQL 실행. 값은 params(? 자리표시자)로 전달
    allowed: 허용 테이블 목록. None이면 지식 그래프의 :TABLE 노드에서 읽어옴
    반환값: {"columns", "row_count", "rows", "truncated"} 또는 {"error"}"""
    allowed = {a.lower() for a in (allowed if allowed is not None else allowed_tables())}
    err = check_sql(sql, allowed)
    if err:
        return {"error": err}
    run = {"sqlite": _run_sqlite, "mssql": _run_mssql}.get(RDB_TYPE)
    if run is None:
        return {"error": f"지원하지 않는 RDB_TYPE: {RDB_TYPE}"}
    try:
        cols, rows = run(sql, params, allowed)
    except Exception as e:  # 문법 오류·시간 초과 등은 LLM이 보고 고칠 수 있도록 그대로 전달
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    truncated = len(rows) > MAX_ROWS
    rows = [dict(zip(cols, r)) for r in rows[:MAX_ROWS]]
    # truncated=True면 row_count는 전체 개수가 아니라 MAX_ROWS (실제로는 그 이상)
    return {"columns": cols, "row_count": len(rows), "rows": rows, "truncated": truncated}
