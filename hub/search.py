from prometheus_client import Counter, Histogram

from hub.ai import Draft

TOKENS = Counter("hub_provider_tokens_total", "Provider tokens by operation", ["operation"])
ANSWERS = Counter("hub_answers_total", "Question outcomes", ["outcome"])
ANSWER_TIME = Histogram("hub_answer_seconds", "Question latency in seconds")

SEARCH = """
WITH eligible AS MATERIALIZED (
    SELECT v.id FROM accessible_sources(%s) s
    JOIN versions v ON v.source_id = s.id AND v.retired_at IS NULL
), semantic AS (
    SELECT c.id, c.embedding <=> %s::vector AS distance
    FROM chunks c JOIN eligible v ON v.id = c.version_id
    ORDER BY distance, c.id LIMIT 20
), keyword AS (
    SELECT c.id, ts_rank_cd(c.search, q.query) AS score
    FROM chunks c JOIN eligible v ON v.id = c.version_id
    CROSS JOIN websearch_to_tsquery('english', %s) q(query)
    WHERE c.search @@ q.query
    ORDER BY score DESC, c.id LIMIT 20
), ranks AS (
    SELECT id, 1.0 / (60 + row_number() OVER (ORDER BY distance, id)) AS score FROM semantic
    UNION ALL
    SELECT id, 1.0 / (60 + row_number() OVER (ORDER BY score DESC, id)) FROM keyword
), fused AS (
    SELECT id, sum(score) AS score FROM ranks GROUP BY id ORDER BY score DESC, id LIMIT 8
)
SELECT c.id, c.location, c.body, v.title
FROM fused f JOIN chunks c USING (id) JOIN versions v ON v.id = c.version_id
ORDER BY f.score DESC, c.id
"""


def vector_parameter(values: list[float]) -> str:
    return "[" + ",".join(map(str, values)) + "]"


def evidence_for_model(rows: list[dict]) -> list[dict]:
    return [
        {"passage": number, "title": row["title"], "location": row["location"], "text": row["body"]}
        for number, row in enumerate(rows, 1)
    ]


def verified_segments(draft: Draft, rows: list[dict]) -> list[dict]:
    if len(draft.segments) > 12:
        return []
    result = []
    for segment in draft.segments:
        if not segment.text.strip() or len(segment.text) > 2000 or not 1 <= len(segment.citations) <= 8:
            return []
        citations = []
        seen = set()
        for citation in segment.citations:
            if not 1 <= citation.passage <= len(rows) or citation.passage in seen:
                return []
            row = rows[citation.passage - 1]
            quote = citation.quote.strip()
            if not quote or len(quote) > 800 or quote not in row["body"]:
                return []
            seen.add(citation.passage)
            citations.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "location": row["location"],
                    "quote": quote,
                    "url": f"/citation/{row['id']}",
                }
            )
        result.append({"text": segment.text.strip(), "citations": citations})
    return result
