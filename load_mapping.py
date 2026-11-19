"""데이터 매핑 정의서(xlsx 또는 CSV) → Neo4j 적재

그래프 구조
  (:Process)-[:HAS_PARAMETER]->(:Parameter)               ← 기존 지식 노드에 연결 (process 컬럼)
  (:Parameter|:Defect)-[:MEASURED_AS {line, valid_from, valid_to, note, sql_template}]->(:InspectionItem)
  (:InspectionItem)-[:STORED_IN]->(:Column)-[:BELONGS_TO]->(:Table)

사용
  python load_mapping.py mapping/data_mapping.xlsx --check           # 검증만 (Neo4j에 쓰지 않음)
  python load_mapping.py mapping/data_mapping.xlsx                   # 검증 후 적재 (전체 교체)
  python load_mapping.py mapping/data_mapping.xlsx --no-db-check     # 실제 DB 컬럼 확인 생략
  python load_mapping.py mapping/data_mapping.xlsx --no-graph-check  # 그래프 노드 이름 확인 생략 (--check 전용)
  python load_mapping.py mapping/data_mapping.xlsx --create-missing  # 그래프에 없는 대상·공정 노드를 새로 만듦

적재 방식: 매번 '전체 교체'. 이전에 이 스크립트로 만든 검사항목·컬럼·테이블 노드(source='mapping_csv')와
           그 관계를 지우고 정의서 내용으로 다시 만든다. 기존 지식 노드(Process·Parameter·Defect)는 지우지 않는다.
           삭제와 적재는 하나의 트랜잭션이라 중간에 실패하면 전부 취소된다.

xlsx는 '매핑정의' 시트(없으면 첫 번째 시트)의 1행 머리글을 읽습니다 (pip install openpyxl 필요).
  item_code 처럼 0으로 시작할 수 있는 코드 열은 Excel에서 '텍스트' 서식으로 지정하세요 (001 → 1 방지).
CSV는 Excel에서 'CSV UTF-8' 형식으로 저장하세요 (cp949로 저장된 파일도 읽지만 경고를 냅니다).
key_columns 는 | 로 구분합니다.

선택 컬럼
  line_column        : 라인(호기)이 저장된 컬럼 (예: EQP_ID). line 값이 있으면 필수 → SQL에 라인 조건 추가
  denominator_column : 비율 계산의 분모 컬럼 (예: INSP_QTY → 불량률 = value_column / denominator_column)
"""
import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path

REQUIRED = ["target_label", "target_name", "item_code", "table", "code_column",
            "value_column", "time_column"]
COLUMNS = REQUIRED + ["process", "line", "line_column", "item_name", "unit", "lsl", "usl",
                      "denominator_column", "system", "key_columns", "valid_from", "valid_to", "note"]
TARGET_LABELS = {"Parameter", "Defect"}
# 검사항목(InspectionItem) 하나에 속하는 값 - 같은 항목을 쓰는 행끼리 값이 같아야 함
ITEM_FIELDS = ["item_name", "unit", "lsl", "usl", "value_column", "time_column", "line_column",
               "denominator_column", "key_columns", "system"]
SOURCE = "mapping_csv"
_TABLE = re.compile(r"^\w+\.\w+$")   # schema.table (rdb_client와 동일하게 두 부분만 허용)
_NAME = re.compile(r"^\w+$")


def _num(v):
    return float(v) if v not in ("", None) else None


def _date(v):
    return date.fromisoformat(v) if v else None


def _keys(r):
    return [k for k in r.get("key_columns", "").split("|") if k]


def _cell_text(v):
    """Excel 셀 값을 문자열로 (날짜 → YYYY-MM-DD, 55.0 → 55)"""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# ---------------------------------------------------------------- 읽기
def _read_xlsx(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb["매핑정의"] if "매핑정의" in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [_cell_text(h) for h in next(it, [])]
    rows = []
    for values in it:
        row = {h: _cell_text(v) for h, v in zip(header, values) if h}
        if any(row.values()):                       # 완전히 빈 행은 건너뜀
            rows.append(row)
    wb.close()
    return header, rows


def _read_csv(path):
    # Excel 'CSV UTF-8'은 utf-8-sig, 한국어 Windows Excel의 기본 'CSV (쉼표로 분리)'는 cp949
    for enc in ("utf-8-sig", "cp949"):
        try:
            with open(path, encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                header = [(h or "").strip() for h in (reader.fieldnames or [])]
                rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
                        for row in reader]
        except UnicodeDecodeError:
            continue
        if enc != "utf-8-sig":
            print(f"[경고] {path.name}을(를) {enc}로 읽었습니다. Excel에서 'CSV UTF-8' 형식으로 저장하는 것을 권장합니다.")
        rows = [r for r in rows if any(r.values())]  # 완전히 빈 행은 건너뜀
        return header, rows
    sys.exit(f"[오류] {path.name}의 문자 인코딩을 읽을 수 없습니다. Excel에서 'CSV UTF-8' 형식으로 저장하세요.")


def read_rows(path):
    path = Path(path)
    header, rows = (_read_xlsx(path) if path.suffix.lower() in (".xlsx", ".xlsm")
                    else _read_csv(path))
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        sys.exit(f"[오류] 머리글에 필수 컬럼이 없습니다: {missing}")
    unknown = [h for h in header if h and h not in COLUMNS]
    if unknown:
        print(f"[경고] 알 수 없는 컬럼은 무시합니다: {unknown}")
    return rows


# ---------------------------------------------------------------- 검증
def item_id(r):
    return f"{r['table']}|{r['code_column']}|{r['item_code']}"


def _row_columns(r):
    cols = [r.get(c) for c in ("code_column", "value_column", "time_column",
                               "line_column", "denominator_column")]
    return [c for c in cols + _keys(r) if c]


def validate(rows, db_check=True):
    """정의서 자체와 실제 DB 컬럼 검사 (Neo4j는 보지 않음). 오류 메시지 목록 반환"""
    errors, seen, items = [], set(), {}
    col_cache = {}
    for i, r in enumerate(rows, start=2):           # 1행은 헤더
        where = f"{i}행({r.get('target_name', '')}/{r.get('item_code', '')})"
        for c in REQUIRED:
            if not r.get(c):
                errors.append(f"{where}: '{c}' 값이 비어 있습니다.")
        if r.get("target_label") and r["target_label"] not in TARGET_LABELS:
            errors.append(f"{where}: target_label은 {sorted(TARGET_LABELS)} 중 하나여야 합니다.")
        if r.get("table") and not _TABLE.match(r["table"]):
            errors.append(f"{where}: table은 schema.table 형식이어야 합니다 (예: mfg.A).")
        cols = _row_columns(r)
        for c in cols:
            if not _NAME.match(c):
                errors.append(f"{where}: 컬럼 이름 '{c}'에 허용되지 않는 문자가 있습니다.")
        if r.get("line") and not r.get("line_column"):
            errors.append(f"{where}: line 값이 있으면 line_column(라인이 저장된 컬럼, 예: EQP_ID)이 필요합니다.")

        nums = {}
        for c in ("lsl", "usl"):
            try:
                nums[c] = _num(r.get(c))
            except ValueError:
                errors.append(f"{where}: {c}는 숫자여야 합니다.")
        if nums.get("lsl") is not None and nums.get("usl") is not None and nums["lsl"] > nums["usl"]:
            errors.append(f"{where}: lsl({r['lsl']})이 usl({r['usl']})보다 큽니다.")
        dates = {}
        for c in ("valid_from", "valid_to"):
            try:
                dates[c] = _date(r.get(c))
            except ValueError:
                errors.append(f"{where}: {c}는 실제 날짜(YYYY-MM-DD)여야 합니다: '{r[c]}'")
        if dates.get("valid_from") and dates.get("valid_to") and dates["valid_from"] > dates["valid_to"]:
            errors.append(f"{where}: valid_from({r['valid_from']})이 valid_to({r['valid_to']})보다 늦습니다.")

        # 같은 대상·라인·코드·테이블이라도 적용 기간(valid_from)이 다르면 별도 행으로 허용
        key = (r.get("target_label"), r.get("target_name"), r.get("line"), r.get("item_code"),
               r.get("table"), r.get("valid_from"))
        if key in seen:
            errors.append(f"{where}: 같은 대상·라인·코드·테이블·적용시작일이 중복되었습니다.")
        seen.add(key)

        # 같은 검사항목(테이블·코드컬럼·코드)을 쓰는 행끼리 항목 속성이 달라지면 나중 행이 덮어쓰므로 오류
        if all(r.get(c) for c in ("table", "code_column", "item_code")):
            values = {f: r.get(f, "") for f in ITEM_FIELDS}
            prev = items.setdefault(item_id(r), (i, values))
            diff = [f for f in ITEM_FIELDS if prev[1][f] != values[f]]
            if diff:
                errors.append(f"{where}: {prev[0]}행과 같은 검사항목({r['item_code']})인데 {diff} 값이 다릅니다.")

        if db_check and r.get("table") and _TABLE.match(r["table"]):
            if r["table"] not in col_cache:
                try:
                    from rdb_client import table_columns
                    col_cache[r["table"]] = {c.lower() for c in table_columns(r["table"])}
                except Exception as e:
                    col_cache[r["table"]] = None
                    errors.append(f"{where}: DB 확인 실패 ({type(e).__name__}: {e})")
            actual = col_cache[r["table"]]
            if actual is not None:
                if not actual:
                    errors.append(f"{where}: DB에 테이블 {r['table']}이(가) 없습니다.")
                else:
                    for c in cols:
                        if c.lower() not in actual:
                            errors.append(f"{where}: {r['table']}에 컬럼 {c}이(가) 없습니다.")
    return errors


def _existing_names(session):
    rows = session.run("MATCH (n) WHERE n:Parameter OR n:Defect OR n:Process "
                       "RETURN labels(n) AS labels, n.name AS name").data()
    return {(lb, r["name"]) for r in rows for lb in r["labels"]}


def graph_missing(rows, session):
    """정의서의 대상(target)·공정(process) 이름 중 그래프에 없는 것 [(라벨, 이름, 행번호들)]"""
    existing = _existing_names(session)
    missing = {}
    for i, r in enumerate(rows, start=2):
        wanted = [(r["target_label"], r["target_name"])]
        if r["target_label"] == "Parameter" and r.get("process"):
            wanted.append(("Process", r["process"]))
        for key in wanted:
            if key not in existing:
                missing.setdefault(key, []).append(i)
    return [(lb, name, lines) for (lb, name), lines in missing.items()]


def _print_missing(missing, existing_hint):
    for lb, name, lines in missing:
        print(f"  - {lb} '{name}' ({', '.join(map(str, lines))}행)")
    if existing_hint:
        print("  그래프에 있는 이름: " + ", ".join(f"{lb}:{n}" for lb, n in sorted(existing_hint)))
    print("  이름을 그래프와 똑같이 맞추거나(띄어쓰기 포함), 새 노드로 만들려면 --create-missing 을 사용하세요.")


# ---------------------------------------------------------------- 적재
def sql_template(r, with_line=False):
    """조회 SQL 틀. with_line=True면 라인 조건(AND line_column = :line) 포함"""
    select = [r["time_column"]] + _keys(r)
    for c in (r.get("line_column"), r.get("denominator_column"), r["value_column"]):
        if c and c not in select:
            select.append(c)
    sql = (f"SELECT {', '.join(select)} FROM {r['table']} "
           f"WHERE {r['code_column']} = :code AND {r['time_column']} >= :from_dt "
           f"AND {r['time_column']} < :to_dt")
    if with_line and r.get("line") and r.get("line_column"):
        sql += f" AND {r['line_column']} = :line"
    return sql


def _load_tx(tx, rows, create_missing):
    # 1) 이전 적재분 삭제 (이 스크립트가 만든 노드만, 관계는 DETACH로 함께 삭제)
    tx.run("MATCH (n) WHERE (n:InspectionItem OR n:Column OR n:Table) AND n.source = $src "
           "DETACH DELETE n", src=SOURCE)
    # 2) 다시 적재
    for r in rows:
        # 기본은 MATCH(기존 지식 노드에만 연결). --create-missing이면 새로 만들고 출처 표시
        node = ("MERGE (x:{0} {{name: $target}}) ON CREATE SET x.source = $src" if create_missing
                else "MATCH (x:{0} {{name: $target}})").format(r["target_label"])
        rec = tx.run(f"""
            {node}
            MERGE (t:Table {{name: $table}})
              SET t.system = $system, t.source = $src
            MERGE (c:Column {{id: $col_id}})
              SET c.name = $code_column, c.table = $table, c.role = 'code', c.source = $src
            MERGE (c)-[:BELONGS_TO]->(t)
            MERGE (i:InspectionItem {{id: $item_id}})
              SET i.code = $code, i.name = $item_name, i.unit = $unit,
                  i.lsl = $lsl, i.usl = $usl, i.table = $table,
                  i.code_column = $code_column, i.value_column = $value_column,
                  i.time_column = $time_column, i.line_column = $line_column,
                  i.denominator_column = $denominator_column, i.key_columns = $key_columns,
                  i.sql_template = $sql_template, i.source = $src
            MERGE (i)-[:STORED_IN]->(c)
            CREATE (x)-[m:MEASURED_AS {{line: $line, valid_from: $valid_from, valid_to: $valid_to,
                                       note: $note, sql_template: $line_sql}}]->(i)
            RETURN count(m) AS n
            """,
                     src=SOURCE, target=r["target_name"],
                     table=r["table"], system=r.get("system") or None,
                     col_id=f"{r['table']}.{r['code_column']}", code_column=r["code_column"],
                     item_id=item_id(r), code=r["item_code"], item_name=r.get("item_name") or None,
                     unit=r.get("unit") or None, lsl=_num(r.get("lsl")), usl=_num(r.get("usl")),
                     value_column=r["value_column"], time_column=r["time_column"],
                     line_column=r.get("line_column") or None,
                     denominator_column=r.get("denominator_column") or None,
                     key_columns=_keys(r), sql_template=sql_template(r),
                     line=r.get("line") or "", valid_from=r.get("valid_from") or None,
                     valid_to=r.get("valid_to") or None, note=r.get("note") or None,
                     line_sql=sql_template(r, with_line=True)).single()
        if not rec or rec["n"] != 1:   # MATCH 실패 시 행이 없어 n=0 → 전체 취소
            raise RuntimeError(f"그래프에 {r['target_label']} '{r['target_name']}' 노드가 없습니다.")
        if r["target_label"] == "Parameter" and r.get("process"):
            node = "MERGE (p:Process {name: $process}) ON CREATE SET p.source = $src" if create_missing \
                else "MATCH (p:Process {name: $process})"
            rec = tx.run(f"{node} "
                         "WITH p MATCH (x:Parameter {name: $target}) "
                         "MERGE (p)-[:HAS_PARAMETER]->(x) RETURN count(*) AS n",
                         process=r["process"], target=r["target_name"], src=SOURCE).single()
            if not rec or rec["n"] != 1:
                raise RuntimeError(f"그래프에 Process '{r['process']}' 노드가 없습니다.")
    return tx.run("""
        MATCH (i:InspectionItem {source: $src}) WITH count(i) AS items
        MATCH (t:Table {source: $src}) WITH items, count(t) AS tables
        MATCH (:InspectionItem {source: $src})<-[m:MEASURED_AS]-() RETURN items, tables, count(m) AS links
        """, src=SOURCE).single().data()


def load(rows, create_missing=False):
    from graph_store import GraphStore
    try:
        g = GraphStore()
    except Exception as e:
        sys.exit(f"[적재 실패] Neo4j에 연결할 수 없습니다 ({type(e).__name__}: {str(e)[:150]})")
    try:
        with g.driver.session() as s:
            # 제약조건은 스키마 변경이라 데이터 트랜잭션과 따로 실행
            for q in ["CREATE CONSTRAINT table_name IF NOT EXISTS FOR (n:Table) REQUIRE n.name IS UNIQUE",
                      "CREATE CONSTRAINT column_id IF NOT EXISTS FOR (n:Column) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT item_id IF NOT EXISTS FOR (n:InspectionItem) REQUIRE n.id IS UNIQUE",
                      "CREATE CONSTRAINT parameter_name IF NOT EXISTS FOR (n:Parameter) REQUIRE n.name IS UNIQUE",
                      "CREATE CONSTRAINT defect_name IF NOT EXISTS FOR (n:Defect) REQUIRE n.name IS UNIQUE"]:
                s.run(q).consume()
            if not create_missing:
                missing = graph_missing(rows, s)
                if missing:
                    print(f"[적재 중단] 그래프에 없는 이름 {len(missing)}개 (아무것도 바꾸지 않음)")
                    _print_missing(missing, None)
                    sys.exit(1)
            # 삭제 + 적재를 하나의 트랜잭션으로 (실패 시 전부 취소)
            return s.execute_write(_load_tx, rows, create_missing)
    finally:
        g.close()


# ---------------------------------------------------------------- 실행
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    path = Path(args[0])
    create_missing = "--create-missing" in sys.argv
    rows = read_rows(path)
    print(f"{path}: {len(rows)}행 읽음")
    errors = validate(rows, db_check="--no-db-check" not in sys.argv)
    if errors:
        print(f"[검증 실패] {len(errors)}건")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print("[검증 통과]")
    for r in rows:
        line = f" ({r['line']})" if r.get("line") else ""
        period = f" [{r.get('valid_from') or '…'} ~ {r.get('valid_to') or '…'}]" \
            if r.get("valid_from") or r.get("valid_to") else ""
        print(f"  {r['target_label']}:{r['target_name']}{line}{period} → "
              f"{r['table']}.{r['code_column']} = '{r['item_code']}'")

    if "--check" in sys.argv:
        if "--no-graph-check" in sys.argv:
            return
        from graph_store import GraphStore
        try:
            g = GraphStore()
        except Exception as e:
            sys.exit(f"[그래프 확인 실패] Neo4j에 연결할 수 없습니다 ({type(e).__name__}). "
                     f"그래프 이름 확인 없이 검사하려면 --no-graph-check 를 사용하세요.")
        try:
            with g.driver.session() as s:
                missing = graph_missing(rows, s)
                existing = _existing_names(s) if missing else set()
        finally:
            g.close()
        if missing and not create_missing:
            print(f"[그래프 확인] 그래프에 없는 이름 {len(missing)}개 (적재 시 오류)")
            _print_missing(missing, existing)
            sys.exit(1)
        if missing:
            print(f"[그래프 확인] 새로 만들 노드 {len(missing)}개 (--create-missing): "
                  + ", ".join(f"{lb}:{n}" for lb, n, _ in missing))
        else:
            print("[그래프 확인] 대상·공정 이름이 모두 그래프에 있습니다.")
        return

    result = load(rows, create_missing=create_missing)
    print(f"[적재 완료] 검사항목 {result.get('items')}개 · 테이블 {result.get('tables')}개 · "
          f"매핑 관계 {result.get('links')}개 (이전 적재분은 교체됨)")


if __name__ == "__main__":
    main()
