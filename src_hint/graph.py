"""Suggested-ontology baseline (HINT) for the before/after comparison in report/ONTOLOGY.md.

Wraps the same extraction helpers as DrugKG-2 but writes the suggested ontology:
    (:Article)-[:DEFINES]->(:Crime), (:Clause {penalty: string})-[:MENTIONS]->(:Substance)
    (:Case {name})-[:CHARGED_WITH]->(:Crime), -[:INVOLVES {amount}]->, -[:LOCATED_IN]->
    (:Person)-[:INVOLVED_IN {role, sentence, charge}]->
It intentionally keeps the known weaknesses (no Penalty/Threshold node, Case keyed by LLM name).
"""

from __future__ import annotations

import re

from src.graph import (
    GRAPH_PROMPT,
    GraphRAGAgent,
    Neo4jGraph as _Neo4jGraph,
    extract_news_cases as _extract_news_cases_v2,
    format_penalty,
    link_entity,
    load_markdown_docs,
    normalize_crime,
    parse_law_article as _parse_law_article_v2,
)

__all__ = ["Neo4jGraph", "GraphRAGAgent", "build_graph", "link_entity", "load_markdown_docs",
           "normalize_crime", "parse_law_article", "extract_news_cases", "GRAPH_PROMPT"]


def parse_law_article(doc):
    article = _parse_law_article_v2(doc)
    for clause in article["clauses"]:
        clause["penalty"] = format_penalty(clause["penalty"])
        clause["substances"] = [s["name"] for s in clause["substances"]]
    article.pop("number", None)
    return article


def extract_news_cases(doc, llm_fn, known_crimes):
    cases = _extract_news_cases_v2(doc, llm_fn, known_crimes)
    for case in cases:
        case.pop("events", None)
        case.pop("stage", None)
        case.pop("court", None)
        case.pop("id", None)
    return cases


class Neo4jGraph(_Neo4jGraph):
    """Suggested-ontology graph: same driver/seed_facts, different node/relationship model."""

    def create_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        seed_ids, facts = self.seed_facts(question, doc_ids, limit=max_facts)
        ids = list(seed_ids)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            LIMIT 40
            """,
            ids=ids,
        )
        case_ids = [c["id"] for c in cases]
        for c in cases:
            facts.append(f"Vụ việc '{c['name']}': {c['summary']}")
        for row in self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
            OPTIONAL MATCH (cl)-[:MENTIONS]->(m:Substance)
            OPTIONAL MATCH (k)-[:INVOLVES]->(iv:Substance)
            WITH k, a, cl, collect(DISTINCT m.name) AS clause_subs, collect(DISTINCT iv.name) AS case_subs
            WHERE cl.number = 1 OR any(x IN clause_subs WHERE x IN case_subs)
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number,
                   cl.penalty AS penalty, cl.text AS text
            ORDER BY article, number
            """,
            case_ids=case_ids,
        ):
            facts.append(f"[{row['article']} - {row['title']}] khoản {row['number']}: {row['text']}")
        for row in self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.doc_id IN $doc_ids
               OR any(n IN $numbers WHERE a.id STARTS WITH ('Điều ' + n + ' '))
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            ORDER BY article, number
            """,
            doc_ids=doc_ids, numbers=list(set(re.findall(r"[Đđ]iều\s+(\d+)", question))),
        ):
            facts.append(f"[{row['article']} - {row['title']}] khoản {row['number']}: {row['text']}")
        seen, out = set(), []
        for fact in facts:
            if fact and fact not in seen:
                seen.add(fact)
                out.append(fact)
            if len(out) >= max_facts:
                break
        return out


def build_graph(graph: Neo4jGraph, law_docs: list, news_docs: list, llm_fn) -> None:
    graph.create_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article(article)
    known_crimes = [a["crime"] for a in articles if a["crime"]]
    for doc in news_docs:
        for case in extract_news_cases(doc, llm_fn, known_crimes):
            graph.add_news_case(case, doc)
