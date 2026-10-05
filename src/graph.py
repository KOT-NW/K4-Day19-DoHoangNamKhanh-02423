"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    if not name:
        return None
    target = normalize(name)
    if not target:
        return None
    canonical = {}
    for original in known:
        canonical.setdefault(normalize(original), original)
    if target in canonical:
        return canonical[target]
    close = difflib.get_close_matches(target, list(canonical), n=1, cutoff=0.8)
    return canonical[close[0]] if close else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# DrugKG-2 — canonical substances with synonyms, penalties and quantity thresholds
# ----------------------------------------------------------------------------------------------

SUBSTANCE_ALIASES: dict[str, list[str]] = {
    "Heroine": ["heroin", "bạch phiến", "hêrôin"],
    "Cocaine": ["cocain", "cocaine"],
    "Methamphetamine": ["methamphetamine", "meth", "ma túy đá", "ma tuý đá", "hàng đá"],
    "Amphetamine": ["amphetamine", "amphetamin"],
    "MDMA": ["mdma", "thuốc lắc", "ma túy kẹo", "kẹo", "ecstasy", "nước vui"],
    "XLR-11": ["xlr-11", "xlr11"],
    "Ketamine": ["ketamine", "ketamin"],
    "cần sa": ["cần sa", "cannabis"],
    "thuốc phiện": ["thuốc phiện", "nhựa thuốc phiện", "quả thuốc phiện", "opium"],
    "côca": ["côca", "cao côca", "lá côca"],
    "Etomidate": ["etomidate", "pod chill", "podchill"],
}
CANONICAL_SUBSTANCES = list(SUBSTANCE_ALIASES)

def normalize_substance(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())

def link_substance(name: str) -> str | None:
    """Map a free-text substance mention (news wording or law wording) onto a canonical name."""
    target = normalize_substance(name)
    if not target:
        return None
    for canonical in CANONICAL_SUBSTANCES:
        if target == normalize_substance(canonical):
            return canonical
    for canonical, aliases in SUBSTANCE_ALIASES.items():
        if any(normalize_substance(a) in target for a in aliases):
            return canonical
    return link_entity(target, CANONICAL_SUBSTANCES, normalize=normalize_substance)

NUMBER_YEARS = re.compile(r"(\d+)\s*năm")

def parse_penalty(clause_text: str) -> dict | None:
    """Turn 'thì bị phạt tù từ 07 năm đến 15 năm' into a structured Penalty map."""
    first = clause_text.strip().splitlines()[0] if clause_text.strip() else ""
    match = re.search(r"bị\s+(.+?)\s*[:.]?$", first)
    if not match:
        return None
    detail = match.group(1).strip()
    years = [int(y) for y in NUMBER_YEARS.findall(detail)] if "tù" in detail else []
    return {
        "kind": "tù" if "tù" in detail else ("tiền" if "tiền" in detail else "khác"),
        "min_years": years[0] if years else None,
        "max_years": years[1] if len(years) > 1 else None,
        "life": "chung thân" in detail,
        "death": "tử hình" in detail,
        "text": detail,
    }

RANGE_RE = re.compile(r"từ\s+([\d.,]+)\s*(gam|kilôgam|mililít|mililit)\s+đến dưới\s+([\d.,]+)\s*(gam|kilôgam|mililít|mililit)", re.IGNORECASE)
FROM_RE = re.compile(r"([\d.,]+)\s*(gam|kilôgam|mililít|mililit)\s+trở lên", re.IGNORECASE)
UNIT_FACTOR = {"gam": (1.0, "g"), "kilôgam": (1000.0, "g"), "mililít": (1.0, "ml"), "mililit": (1.0, "ml")}

def _to_base(value: str, unit: str) -> tuple[float, str]:
    number = float(value.replace(".", "").replace(",", "."))
    factor, base = UNIT_FACTOR[unit.lower()]
    return number * factor, base

def parse_thresholds(clause_text: str, clause_id: str) -> list[dict]:
    """Extract per-substance quantity ranges (grams/ml) from the lettered points of one clause."""
    thresholds: list[dict] = []
    for point in re.split(r"(?m)^(?=[a-zđ]\)\s)", clause_text):
        substances = [s for s in (link_substance(x) for x in find_substances(point)) if s]
        if not substances:
            continue
        match, lower, upper, base = None, None, None, "g"
        if (match := RANGE_RE.search(point)):
            lower, base = _to_base(match.group(1), match.group(2))
            upper, _ = _to_base(match.group(3), match.group(4))
        elif (match := FROM_RE.search(point)):
            lower, base = _to_base(match.group(1), match.group(2))
        else:
            continue
        for substance in dict.fromkeys(substances):
            thresholds.append({
                "id": f"{clause_id}#th{len(thresholds) + 1}",
                "substance": substance,
                "min_g": lower, "max_g": upper, "unit": base,
                "raw": re.sub(r"\s+", " ", point).strip()[:200],
            })
    return thresholds

def format_penalty(penalty: dict | None) -> str:
    if not penalty:
        return ""
    if penalty.get("life") or penalty.get("death"):
        parts = []
        if penalty.get("min_years") is not None:
            parts.append(f"tù {penalty['min_years']} năm")
        if penalty.get("life"):
            parts.append("tù chung thân")
        if penalty.get("death"):
            parts.append("tử hình")
        return ", ".join(parts)
    if penalty.get("min_years") is not None and penalty.get("max_years") is not None:
        return f"tù {penalty['min_years']} năm đến {penalty['max_years']} năm"
    return penalty.get("text", "")

MASS_UNITS = {"g", "gam", "gram", "gr", "kg", "kilogam", "kilogram", "mg", "miligam",
              "ml", "mililit", "mililít", "lit", "lít"}

def parse_amount_to_g(amount: str, unit: str = "") -> float | None:
    """'9,6' + 'kg' -> 9600.0 ; '406' + 'g' -> 406.0 ; '5' + 'viên' -> None (not a mass)."""
    text = f"{amount or ''} {unit or ''}".lower().replace(",", ".")
    unit_norm = normalize_substance(unit)
    has_mass = bool(re.search(r"(kg|gam|milig|mg|\bg\b|\bgr\b)", text))
    if unit_norm not in MASS_UNITS and not has_mass:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    value = float(match.group(1))
    if "kg" in text or "kilogam" in text or "ki lô gam" in text:
        value *= 1000
    elif "mg" in text or "miligam" in text:
        value *= 0.001
    return value

def question_substances(question: str) -> list[str]:
    lowered = question.lower()
    found = []
    for canonical, aliases in SUBSTANCE_ALIASES.items():
        names = [normalize_substance(canonical)] + [normalize_substance(a) for a in aliases]
        if any(n and n in lowered for n in names):
            found.append(canonical)
    return found

def threshold_matches(amount_g: float | None, threshold: dict) -> bool:
    if amount_g is None or threshold.get("min_g") is None:
        return False
    if amount_g < threshold["min_g"]:
        return False
    return threshold.get("max_g") is None or amount_g < threshold["max_g"]


# ----------------------------------------------------------------------------------------------
# DrugKG-2 — extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' into Article/Clause/Penalty/Threshold data."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    number = re.search(r"(\d+)", article_id)
    clauses = []
    starts = list(CLAUSE_START.finditer(body))
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        clause_id = f"{article_id} khoản {start.group(1)}"
        penalty = parse_penalty(text)
        if penalty:
            penalty["id"] = f"{clause_id}#penalty"
        substances = list(dict.fromkeys(s for s in (link_substance(x) for x in find_substances(text)) if s))
        clauses.append({
            "id": clause_id,
            "number": int(start.group(1)),
            "text": text,
            "penalty": penalty,
            "thresholds": parse_thresholds(text, clause_id),
            "substances": [{"name": s, "aliases": SUBSTANCE_ALIASES.get(s, [])} for s in substances],
        })
    return {
        "id": article_id,
        "number": number.group(1) if number else "",
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "stage": "giai đoạn tố tụng chính: bắt giữ | khởi tố | xét xử sơ thẩm | xét xử phúc thẩm | tuyên án | điều tra, hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "court": "tên tòa án xét xử, ví dụ TAND TP.HCM, hoặc chuỗi rỗng",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp",
                   "amount": "khối lượng, ví dụ 9,6", "unit": "g hoặc kg"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}],
  "events": [{{"type": "bắt giữ | khởi tố | xét xử | tuyên án | thu giữ", "date": "chuỗi rỗng nếu không rõ"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges/substances/people are re-linked to canonical names in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(CANONICAL_SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt, json_mode=True)).get("cases", [])
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []
    out = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_id = f"{doc.id}#{index}"
        charges = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        substances = []
        for item in case.get("substances", []):
            if not isinstance(item, dict):
                continue
            name = link_substance(item.get("name", ""))
            if name:
                substances.append({"name": name, "amount": str(item.get("amount", "") or ""),
                                   "unit": str(item.get("unit", "") or "")})
        people = []
        for person in case.get("people", []):
            if isinstance(person, dict) and person.get("name"):
                people.append({
                    "name": str(person["name"]).strip(),
                    "aliases": [str(a) for a in (person.get("aliases") or []) if a],
                    "role": str(person.get("role", "") or ""),
                    "charge": link_entity(person.get("charge") or "", known_crimes) or "",
                    "sentence": str(person.get("sentence", "") or ""),
                })
        events = []
        for j, event in enumerate(case.get("events", [])):
            if isinstance(event, dict) and event.get("type"):
                events.append({"id": f"{case_id}#ev{j}", "type": str(event["type"]),
                               "date": str(event.get("date", "") or "")})
        out.append({
            "id": case_id,
            "name": case.get("name") or doc.metadata.get("title", doc.id),
            "summary": case.get("summary", ""), "date": case.get("date", ""),
            "stage": case.get("stage", ""), "location": case.get("location", ""),
            "court": case.get("court", ""),
            "charges": charges, "substances": substances, "people": people, "events": events,
        })
    return out

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- DrugKG-2: writes

    LABEL_KEYS = [("Article", "id"), ("Clause", "id"), ("Penalty", "id"), ("Threshold", "id"),
                  ("Crime", "name"), ("Substance", "name"), ("Case", "id"), ("Person", "name"),
                  ("Location", "name"), ("Court", "name"), ("Event", "id")]

    def create_constraints(self) -> None:
        for label, key in self.LABEL_KEYS:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id})
              SET a.number = $number, a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:CRIMINALIZES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (p IN CASE WHEN clause.penalty IS NULL THEN [] ELSE [clause.penalty] END |
                MERGE (pe:Penalty {id: p.id})
                SET pe.kind = p.kind, pe.min_years = p.min_years, pe.max_years = p.max_years,
                    pe.life = p.life, pe.death = p.death, pe.text = p.text, pe.doc_id = $doc_id
                MERGE (cl)-[:PRESCRIBES]->(pe))
            FOREACH (s IN clause.substances |
                MERGE (sub:Substance {name: s.name})
                SET sub.aliases = s.aliases
                MERGE (cl)-[:MENTIONS]->(sub))
            FOREACH (t IN clause.thresholds |
                MERGE (th:Threshold {id: t.id})
                SET th.min_g = t.min_g, th.max_g = t.max_g, th.unit = t.unit, th.raw = t.raw, th.doc_id = $doc_id
                MERGE (cl)-[:HAS_THRESHOLD]->(th)
                MERGE (tsub:Substance {name: t.substance})
                MERGE (th)-[:FOR_SUBSTANCE]->(tsub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {id: $id})
              SET k.name = $name, k.summary = $summary, k.date = $date, k.stage = $stage,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:OCCURRED_AT]->(l))
            FOREACH (ct IN CASE WHEN $court = '' THEN [] ELSE [$court] END |
                MERGE (co:Court {name: ct}) MERGE (k)-[:TRIED_BY]->(co))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances |
                MERGE (sub:Substance {name: s.name})
                MERGE (k)-[r:SEIZES]->(sub)
                SET r.amount = s.amount, r.unit = s.unit)
            FOREACH (p IN $people |
                MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:PARTY_TO]->(k)
                SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            FOREACH (e IN $events |
                MERGE (ev:Event {id: e.id})
                SET ev.type = e.type, ev.date = e.date, ev.doc_id = $doc_id
                MERGE (k)-[:HAS_EVENT]->(ev))
            """,
            doc_id=doc.id, title=doc.metadata.get("title", ""), **case,
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """DrugKG-2 multi-hop retrieval: seeds -> cases -> crime/article/clauses/penalties/thresholds."""
        seed_ids, seed_facts = self.seed_facts(
            question, doc_ids, skip_labels=("Clause", "Threshold", "Penalty"), limit=max_facts)
        ids = list(seed_ids)
        facts: list[str] = []
        q_substances = question_substances(question)
        q_max = bool(re.search(r"tối đa|cao nhất|khung cao", question, re.IGNORECASE))
        q_articles = set(re.findall(r"[Đđ]iều\s+(\d+)", question))
        # Substance seeds drive aggregation, not the legal-basis expansion (avoids pulling every MDMA case).
        primary_ids = [row["id"] for row in self.run(
            "MATCH (n) WHERE elementId(n) IN $ids AND NOT n:Substance RETURN elementId(n) AS id", ids=ids)]
        person_charges = [row["charge"] for row in self.run(
            """
            MATCH (p:Person)-[r:PARTY_TO]->(:Case)
            WHERE elementId(p) IN $ids AND r.charge IS NOT NULL AND r.charge <> ''
            RETURN DISTINCT r.charge AS charge
            """,
            ids=ids,
        )]

        # 1) cases reached from the non-substance seeds (seed itself or 1 hop away)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary, k.stage AS stage
            LIMIT 40
            """,
            ids=primary_ids,
        )
        case_ids = [c["id"] for c in cases]
        case_name = {c["id"]: c["name"] for c in cases}
        for c in cases:
            bits = [f"Vụ việc '{c['name']}'"]
            if c.get("stage"):
                bits.append(f"giai đoạn {c['stage']}")
            if c.get("summary"):
                bits.append(c["summary"])
            facts.append(" — ".join(bits))

        # 2) substances each case seizes (for threshold matching + aggregation)
        case_amounts: dict[str, list[tuple[str, float | None]]] = {}
        for row in self.run(
            """
            MATCH (k:Case)-[r:SEIZES]->(s:Substance)
            WHERE elementId(k) IN $case_ids
            RETURN elementId(k) AS cid, s.name AS substance, r.amount AS amount, r.unit AS unit
            """,
            case_ids=case_ids,
        ):
            amount_g = parse_amount_to_g(row["amount"], row["unit"])
            case_amounts.setdefault(row["cid"], []).append((row["substance"], amount_g))
            shown = " ".join(str(x) for x in (row["amount"], row["unit"]) if x)
            facts.append(f"Vụ '{case_name.get(row['cid'], '')}' thu giữ {row['substance']} {shown}".strip())

        # 3) legal basis: case -> CHARGED_WITH -> Crime <- CRIMINALIZES -> Article -> Clause -> Penalty/Threshold
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(c:Crime)<-[:CRIMINALIZES]-(a:Article)
            MATCH (a)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids AND ($charges = [] OR c.name IN $charges)
            OPTIONAL MATCH (cl)-[:PRESCRIBES]->(pe:Penalty)
            OPTIONAL MATCH (cl)-[:HAS_THRESHOLD]->(th:Threshold)-[:FOR_SUBSTANCE]->(tsub:Substance)
            RETURN elementId(k) AS cid, a.id AS article, a.title AS title, cl.number AS number, cl.text AS text,
                   pe.min_years AS min_years, pe.max_years AS max_years, pe.life AS life, pe.death AS death,
                   collect(DISTINCT {sub: tsub.name, min_g: th.min_g, max_g: th.max_g}) AS thresholds
            ORDER BY cid, article, number
            """,
            case_ids=case_ids, charges=person_charges,
        ):
            groups.setdefault((row["cid"], row["article"]), []).append(row)
        for (cid, _), rows in groups.items():
            best = None
            if q_max:
                best = max(rows, key=lambda r: (bool(r["death"]), bool(r["life"]),
                                                r["max_years"] or 0, r["min_years"] or 0))
            for row in rows:
                thresholds = [t for t in row["thresholds"] if t.get("sub")]
                keep = row["number"] == 1
                if not keep:
                    for substance, amount_g in case_amounts.get(cid, []):
                        if any(t["sub"] == substance and threshold_matches(amount_g, t) for t in thresholds):
                            keep = True
                            break
                if not keep and best is not None and row["number"] == best["number"]:
                    keep = True
                if keep:
                    facts.append(self._clause_fact(row))

        # 4) seeded law articles (doc_id) or "Điều N" named in the question
        single_hop_law = not case_ids
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.doc_id IN $doc_ids
               OR any(n IN $numbers WHERE a.id STARTS WITH ('Điều ' + n + ' '))
            OPTIONAL MATCH (cl)-[:PRESCRIBES]->(pe:Penalty)
            OPTIONAL MATCH (cl)-[:HAS_THRESHOLD]->(th:Threshold)-[:FOR_SUBSTANCE]->(tsub:Substance)
            RETURN a.id AS article, a.title AS title, cl.number AS number, cl.text AS text,
                   pe.min_years AS min_years, pe.max_years AS max_years, pe.life AS life, pe.death AS death,
                   collect(DISTINCT {sub: tsub.name, min_g: th.min_g, max_g: th.max_g}) AS thresholds
            ORDER BY article, number
            """,
            doc_ids=doc_ids, numbers=list(q_articles),
        ):
            if single_hop_law or row["number"] == 1:
                facts.append(self._clause_fact(row))

        # 5) aggregation: every case that seizes a substance named in the question
        if q_substances:
            for row in self.run(
                """
                MATCH (k:Case)-[:SEIZES]->(s:Substance)
                WHERE s.name IN $subs
                OPTIONAL MATCH (p:Person)-[:PARTY_TO]->(k)
                RETURN k.name AS name, collect(DISTINCT s.name) AS substances,
                       collect(DISTINCT p.name) AS people
                LIMIT 30
                """,
                subs=q_substances,
            ):
                who = ", ".join(n for n in row["people"] if n)
                facts.append(f"Vụ '{row['name']}' liên quan {', '.join(row['substances'])}"
                             + (f"; người liên quan: {who}" if who else ""))

        seen, out = set(), []
        facts.extend(seed_facts)
        for fact in facts:
            if fact and fact not in seen:
                seen.add(fact)
                out.append(fact)
            if len(out) >= max_facts:
                break
        return out

    @staticmethod
    def _clause_fact(row: dict) -> str:
        penalty = format_penalty({"min_years": row["min_years"], "max_years": row["max_years"],
                                  "life": row["life"], "death": row["death"]})
        suffix = f" [{penalty}]" if penalty else ""
        return f"[{row['article']} - {row['title']}] khoản {row['number']}{suffix}: {row['text']}"

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.create_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article(article)
    known_crimes = [a["crime"] for a in articles if a["crime"]]
    for doc in news_docs:
        for case in extract_news_cases(doc, llm_fn, known_crimes):
            graph.add_news_case(case, doc)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(chunk["metadata"]["doc_id"] for chunk in chunks))
        facts = self.graph.context(question, doc_ids)
        fact_text = "\n".join(f"- {fact}" for fact in facts) or "(không có)"
        chunk_text = "\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, 1))
        prompt = GRAPH_PROMPT.format(facts=fact_text, chunks=chunk_text, question=question)
        return self.llm_fn(prompt)
