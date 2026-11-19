"""실측 데이터 RDB 조회 (읽기 전용)

지금: SQLite 샘플 DB (data/sample/*.db 파일 하나가 스키마 하나 → mfg.db = mfg 스키마)
나중: MSSQL (RDB_TYPE=mssql, RDB_MSSQL_CONN=ODBC 연결 문자열) - 사내망 환경에서 검증 필요

안전장치
  1. SELECT/WITH 로 시작하는 단일 문장만 허용, 쓰기·관리 키워드 차단
  2. FROM/JOIN 대상은 지식 그래프에 :Table 로 등록된 테이블만 허용
  3. 읽기 전용 연결 (SQLite: mode=ro + query_only), 값은 바인딩 파라미터로만 전달
  4. 행 수(MAX_ROWS)·실행 시간(TIMEOUT_SEC) 제한
  ※ SQLite는 1·2를 authorizer로 한 번 더 막는다. 정규식 검사(validate)는 LLM에게
    알기 쉬운 오류 메시지를 주기 위한 1차 검사이고, 최종 차단은 DB가 실제로 읽는
    테이블을 기준으로 하는 authorizer가 담당한다.
"""
import datetime
import decimal
import os
import re
import sqlite3
import time
import warnings
from pathlib import Path

RDB_TYPE = os.getenv("RDB_TYPE", "sqlite").lower()
SQLITE_DIR = Path(os.getenv("RDB_SQLITE_DIR", Path(__file__).parent / "data" / "sample"))
MAX_ROWS = int(os.getenv("RDB_MAX_ROWS", "100"))
TIMEOUT_SEC = int(os.getenv("RDB_TIMEOUT_SEC", "10"))

DIALECT = {
    "sqlite": "SQLite (행 제한은 LIMIT n, 날짜는 'YYYY-MM-DD HH:MM:SS' 문자열이며 "
              "date(MEAS_DT), strftime('%Y-%m-%d', 컬럼) 사용 가능)",
    "mssql": "Microsoft SQL Server T-SQL (행 제한은 SELECT TOP n, 날짜는 CAST(컬럼 AS date))",
}.get(RDB_TYPE, RDB_TYPE)

# 키워드 검사는 주석을 지우고 문자열을 ''로 바꾼 뒤(_strip) 수행
# (WHERE NOTE = 'delete' 같은 값은 통과). replace() 함수는 허용하고 REPLACE INTO만 차단
_BLOCKED = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|ATTACH|DETACH|PRAGMA|"
    r"VACUUM|REINDEX|GRANT|REVOKE|EXEC|EXECUTE|INTO|OPENROWSET|OPENQUERY|BULK|SHUTDOWN|DBCC)\b"
    r"|\bREPLACE\s+INTO\b",
    re.IGNORECASE)
_SYSTEM_OBJ = re.compile(r"\b(sqlite_\w+|sys\.\w+|information_schema\.\w+|master\.\w+)",
                         re.IGNORECASE)
# 이름 한 조각: name, [name], "name", `name` / 스키마.테이블 (점 앞뒤 공백 허용)
_PART = r"(?:\[\w+\]|\"\w+\"|`\w+`|\w+)"
_NAME = rf"{_PART}(?:\s*\.\s*{_PART})*"
# FROM/JOIN 뒤 테이블 (FROM (mfg.T) 처럼 괄호로 감싼 경우 포함, 서브쿼리는 제외)
_TABLE_REF = re.compile(rf"\b(?:FROM|JOIN)\b\s*(?:\(\s*)*(?!SELECT\b|WITH\b|VALUES\b)({_NAME})",
                        re.IGNORECASE)
# FROM a, b 형태(쉼표 조인)는 두 번째 테이블을 검사하기 어려우므로 JOIN 구문만 허용
#   (FROM 직후가 아닌 위치의 쉼표 조인, 예: JOIN b ON 1=1, c 는 정규식으로 다 잡기 어려우므로
#    SQLite에서는 authorizer가 최종 차단)
_COMMA_JOIN = re.compile(rf"\bFROM\b\s*{_NAME}(?:\s+(?:AS\s+)?(?!WHERE|GROUP|ORDER|LIMIT|JOIN|"
                         r"INNER|LEFT|RIGHT|FULL|CROSS|ON|UNION)\w+)?\s*,", re.IGNORECASE)
# WITH x AS (...), WITH RECURSIVE c(n) AS (...), ..., y AS (...)
_CTE_NAME = re.compile(r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*(\w+)\s*(?:\([^()]*\))?\s*AS\s*\(",
                       re.IGNORECASE)
_STRING_OR_PARAM = re.compile(r"'(?:[^']|'')*'|:(\w+)")


def _strip(sql):
    """주석 제거 + 문자열 리터럴을 ''로 치환 (검사 전용, 실행에는 원문 사용)"""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)   # 문자열 안의 -- 는 주석으로 보지 않도록 먼저 치환
    return re.sub(r"--[^\n]*", " ", sql)


def _norm(name):
    """[mfg] . "MEAS_DATA" -> mfg.meas_data"""
    name = re.sub(r"\s*\.\s*", ".", name)
    return re.sub(r"[\[\]\"`]", "", name).lower()


# ---------------------------------------------------------------- 연결
_IDENT_RE = re.compile(r"\w+")  # 스키마·테이블 이름으로 허용하는 형식 (영문·숫자·_ , 한글 포함)


def _connect_sqlite():
    con = sqlite3.connect("file::memory:", uri=True)  # uri=True 여야 ATTACH에 mode=ro 적용
    for f in sorted(SQLITE_DIR.glob("*.db")):
        # 파일 이름이 그대로 스키마 이름(SQL 식별자)이 되므로 형식이 맞지 않으면 건너뜀
        if not _IDENT_RE.fullmatch(f.stem):
            warnings.warn(f"스키마 이름으로 쓸 수 없는 파일 이름이라 건너뜁니다: {f.name} "
                          f"(영문·숫자·_ 만 사용, 예: mfg_2024.db)")
            continue
        uri = f"file:{f.resolve().as_posix()}?mode=ro"
        con.execute("ATTACH DATABASE ? AS " + f.stem, (uri,))
    con.execute("PRAGMA query_only = ON")
    return con


def _guard_sqlite(con, allowed):
    """2차 방어: 조회(SELECT·함수·재귀 CTE)만 허용하고, 실제로 읽는 테이블을 허용 목록으로 제한
    정규식 검사를 우회한 쿼리(백틱·주석·괄호·쉼표 조인 등)도 여기서 'not authorized' 로 막힘"""
    def auth(action, arg1, arg2, dbname, _trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() == "load_extension" else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if dbname is None:  # CTE 등 실제 테이블이 아닌 대상 (실제 테이블은 항상 스키마 이름이 있음)
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_OK if f"{dbname}.{arg1}".lower() in allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    con.set_authorizer(auth)


def _connect_mssql():  # 미검증: 사내망에서 pyodbc + ODBC Driver 설치 후 사용
    import pyodbc
    # timeout= 은 접속(로그인) 제한 시간. readonly=True 는 드라이버에 따라 무시될 수 있으므로
    # 반드시 조회 권한만 있는 DB 계정을 사용할 것
    con = pyodbc.connect(os.environ["RDB_MSSQL_CONN"], readonly=True, timeout=TIMEOUT_SEC)
    con.timeout = TIMEOUT_SEC  # 쿼리 실행 제한 시간(초)
    return con


def connect():
    if RDB_TYPE == "sqlite":
        if not SQLITE_DIR.exists():
            raise FileNotFoundError(f"샘플 DB 폴더가 없습니다: {SQLITE_DIR} (make_sample_db.py 실행)")
        return _connect_sqlite()
    if RDB_TYPE == "mssql":
        return _connect_mssql()
    raise ValueError(f"지원하지 않는 RDB_TYPE: {RDB_TYPE}")


def table_columns(table):
    """테이블의 컬럼 이름 목록 (매핑 정의서 검증용). 테이블이 없으면 빈 리스트.
    table: "스키마.테이블" 또는 "테이블" ([ ] " ` 로 감싼 이름도 가능)
    스키마를 생략하면 SQLite는 연결된 모든 스키마에서 찾음 (SQLite의 이름 해석 순서와 동일)"""
    # 괄호·따옴표만 제거하고 대소문자는 유지 (MSSQL 대소문자 구분 collation 대비)
    clean = re.sub(r"[\[\]\"`]", "", re.sub(r"\s*\.\s*", ".", table.strip()))
    schema, _, name = clean.rpartition(".")
    # PRAGMA는 바인딩 파라미터를 쓸 수 없으므로 이름 형식을 먼저 검사 (SQL 주입 방지)
    for part in filter(None, (schema, name)):
        if not _IDENT_RE.fullmatch(part):
            raise ValueError(f"테이블 이름 형식이 올바르지 않습니다: {table}")
    con = connect()
    try:
        if RDB_TYPE == "sqlite":
            schemas = [schema] if schema else [r[1] for r in con.execute("PRAGMA database_list")]
            for sc in schemas:
                rows = con.execute(f'PRAGMA "{sc}".table_info("{name}")').fetchall()
                if rows:
                    return [r[1] for r in rows]
            return []
        cur = con.cursor()
        cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", (schema or "dbo", name))
        return [r[0] for r in cur.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------- 검증
def validate(sql, allowed_tables):
    """문제가 있으면 오류 메시지, 없으면 None"""
    s = _strip(sql).strip().rstrip(";").strip()
    if not s:
        return "SQL이 비어 있습니다."
    if ";" in s:
        return "한 번에 하나의 SELECT 문만 실행할 수 있습니다."
    if not re.match(r"^(SELECT|WITH)\b", s, re.IGNORECASE):
        return "SELECT 또는 WITH 로 시작하는 조회 쿼리만 허용됩니다."
    m = _BLOCKED.search(s)
    if m:
        return f"허용되지 않는 키워드입니다: {m.group(0).upper()}"
    m = _SYSTEM_OBJ.search(s)
    if m:
        return f"시스템 객체는 조회할 수 없습니다: {m.group(1)}"
    if _COMMA_JOIN.search(s):
        return "쉼표로 여러 테이블을 나열할 수 없습니다. JOIN ... ON 구문을 사용하세요."
    allowed = {_norm(t) for t in allowed_tables}
    ctes = {_norm(n) for n in _CTE_NAME.findall(s)}
    for ref in _TABLE_REF.findall(s):
        r = _norm(ref)
        if r not in allowed and r not in ctes:
            return (f"등록되지 않은 테이블입니다: {ref}. "
                    f"조회 가능한 테이블: {sorted(allowed_tables)}")
    return None


def _to_qmark(sql, params):
    """MSSQL(pyodbc)용: :name 파라미터를 ? 로 바꾸고 순서대로 값 나열
    문자열 리터럴 안의 :30 등('10:30:00')은 파라미터로 보지 않음"""
    values = []

    def sub(m):
        if m.group(1) is None:  # 문자열 리터럴은 그대로
            return m.group(0)
        if m.group(1) not in params:
            raise ValueError(f"파라미터 값이 없습니다: :{m.group(1)}")
        values.append(params[m.group(1)])
        return "?"
    return _STRING_OR_PARAM.sub(sub, sql), values


# ---------------------------------------------------------------- 실행
def _param_value(v):
    """파라미터 값을 DB에 넘길 수 있는 형태로 변환. 지원하지 않는 형식이면 TypeError
    SQLite는 날짜를 문자열로 저장하므로 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD' 로 변환
    MSSQL(pyodbc)은 date/datetime/Decimal을 그대로 지원"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if RDB_TYPE == "sqlite":
        if isinstance(v, datetime.datetime):  # datetime은 date의 하위 클래스라 먼저 검사
            return v.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(v, datetime.date):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, decimal.Decimal):
            return float(v)
    elif isinstance(v, (datetime.date, decimal.Decimal)):
        return v
    raise TypeError(f"지원하지 않는 파라미터 형식입니다: {type(v).__name__} ({v!r:.50})")


def _prepare_params(params):
    """dict(:name 방식) 또는 list/tuple(? 방식)을 받아 값 형식을 변환"""
    if params is None:
        return {}
    if isinstance(params, dict):
        return {k: _param_value(v) for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        return [_param_value(v) for v in params]
    raise TypeError(f"params는 dict(:name) 또는 list(?) 여야 합니다: {type(params).__name__}")


def run_sql(sql, params=None, allowed_tables=()):
    """읽기 전용 SQL 실행
    params: dict → :name 자리표시자 / list·tuple → ? 자리표시자 (date·datetime 사용 가능)
    반환값: {"columns", "row_count", "rows", "truncated"} 또는 {"error"}"""
    try:
        params = _prepare_params(params)
    except TypeError as e:
        return {"error": str(e)}
    err = validate(sql, allowed_tables)
    if err:
        return {"error": err}
    s = sql.strip().rstrip(";")
    try:
        con = connect()
    except Exception as e:
        return {"error": f"DB 연결 실패: {type(e).__name__}: {str(e)[:200]}"}
    try:
        if RDB_TYPE == "sqlite":
            _guard_sqlite(con, {_norm(t) for t in allowed_tables})
            deadline = time.time() + TIMEOUT_SEC
            con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
            cur = con.execute(s, params)
        else:
            # dict는 :name → ? 변환, list는 이미 ? 방식이므로 그대로
            q, values = _to_qmark(s, params) if isinstance(params, dict) else (s, params)
            cur = con.cursor()
            cur.execute(q, values)
        columns = [c[0] for c in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.DatabaseError as e:  # OperationalError 포함
        msg = str(e)
        if "interrupted" in msg:
            msg = f"실행 시간 초과({TIMEOUT_SEC}초)"
        elif "not authorized" in msg or "prohibited" in msg:  # authorizer 차단
            msg = f"허용되지 않은 테이블 또는 동작입니다. 조회 가능한 테이블: {sorted(allowed_tables)} ({msg[:150]})"
        return {"error": f"SQL 오류: {msg[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        con.close()
    truncated = len(rows) > MAX_ROWS
    rows = [dict(zip(columns, r)) for r in rows[:MAX_ROWS]]
    return {"columns": columns, "row_count": len(rows), "rows": rows, "truncated": truncated}
