"""Small, read-only chat BI service for the LHD fleet."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import psycopg
except ImportError:  # The frontend and parsing tests can run without the DB driver.
    psycopg = None


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"

OPENAI_BASE_URL = "https://api.openai.com/v1" # hard code for now
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6")
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "25"))


def _openai_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    request = Request(
        f"{OPENAI_BASE_URL}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=OPENAI_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc}") from exc


def _openai_text(messages: list[dict[str, str]]) -> str:
    response = _openai_request(
        {
            "model": OPENAI_MODEL,
            "input": messages,
            "max_output_tokens": 700,
            "store": False,
        },
    )
    parts = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("OpenAI API returned no output text")
    return text


def _normalise_truck(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"LHD[- ]?(\d{1,3})", value, flags=re.I)
    return f"LHD-{int(match.group(1)):03d}" if match else None


def _normalise_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None


def _normalise_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _extract_times(question: str) -> list[str]:
    matches = re.findall(
        r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|(?:[01]?\d|2[0-3]):[0-5]\d)\b",
        question,
        flags=re.I,
    )
    return [value for match in matches if (value := _normalise_time(match))]


def _extract_limit(question: str) -> int | None:
    values = "|".join(NUMBER_WORDS)
    match = re.search(rf"\b(\d{{1,2}}|{values})\s+trucks?\b", question, flags=re.I)
    if not match:
        return None
    raw_value = match.group(1).lower()
    value = NUMBER_WORDS.get(raw_value, int(raw_value) if raw_value.isdigit() else 0)
    return max(1, min(20, value))


def parse_intent(question: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Classify supported questions using intentionally simple keyword rules."""
    history = history or []
    context = {}
    for message in reversed(history):
        if isinstance(message, dict) and isinstance(message.get("intent"), dict):
            context = message["intent"]
            break

    text = question.lower()
    if ("queue" in text or "queuing" in text) and any(
        phrase in text for phrase in ("break down", "breakdown", "by source", "source and dump")
    ):
        kind = "queue_breakdown"
    elif "queue" in text or "queuing" in text:
        kind = "queue_time"
    elif ("tonne" in text or "ton " in text or "tons" in text) and any(
        word in text for word in ("top", "most", "highest")
    ):
        kind = "top_trucks"
    elif "cycle" in text:
        kind = "fleet_cycles"
    elif "performance" in text or "perform" in text:
        kind = "truck_performance"
    else:
        kind = "unknown"

    times = _extract_times(question)
    intent = {
        "kind": kind,
        "truck_name": _normalise_truck(question),
        "date": _normalise_date(question),
        "start_time": times[0] if times else None,
        "end_time": times[1] if len(times) > 1 else None,
        "limit": _extract_limit(question),
    }

    if kind == "queue_breakdown":
        for field, normaliser in (
            ("truck_name", _normalise_truck),
            ("date", _normalise_date),
            ("start_time", _normalise_time),
            ("end_time", _normalise_time),
        ):
            if not intent[field]:
                intent[field] = normaliser(context.get(field))
    return intent


def _require(value: Any, label: str) -> str:
    if not value:
        raise ValueError(f"Please include a {label}.")
    return str(value)


def _day_bounds(date_text: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(_require(date_text, "date in YYYY-MM-DD format"))
    return datetime.combine(day, time.min), datetime.combine(day + timedelta(days=1), time.min)


def _connect():
    if psycopg is None:
        raise RuntimeError("The PostgreSQL driver is not installed")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    conn = psycopg.connect(
        database_url,
        connect_timeout=5,
        options="-c default_transaction_read_only=on",
    )
    conn.autocommit = True
    return conn


COMPONENT_SPANS = """
WITH ordered AS (
    SELECT
        cc.cycle_id,
        cc.activity,
        cc.time_entered,
        LEAD(cc.time_entered) OVER (
            PARTITION BY cc.cycle_id ORDER BY cc.time_entered
        ) AS next_time
    FROM cycle_component AS cc
)
"""


def query_analytics(intent: dict[str, Any]) -> dict[str, Any]:
    kind = intent.get("kind")
    if kind == "truck_performance":
        truck = _require(intent.get("truck_name"), "truck name")
        start, end = _day_bounds(_require(intent.get("date"), "date"))
        sql = COMPONENT_SPANS + """
        , selected_cycles AS (
            SELECT c.cycle_id, c.total_tonnes
            FROM cycle AS c
            JOIN truck AS t ON t.truck_id = c.truck_id
            JOIN cycle_component AS first_cc ON first_cc.cycle_id = c.cycle_id
            WHERE t.name = %s
              AND first_cc.time_entered = (
                  SELECT MIN(first_inner.time_entered)
                  FROM cycle_component AS first_inner
                  WHERE first_inner.cycle_id = c.cycle_id
              )
              AND first_cc.time_entered >= %s AND first_cc.time_entered < %s
        ), stage_totals AS (
            SELECT o.activity,
                   SUM(EXTRACT(EPOCH FROM (o.next_time - o.time_entered))) AS seconds
            FROM ordered AS o
            JOIN selected_cycles AS sc ON sc.cycle_id = o.cycle_id
            WHERE o.next_time IS NOT NULL
            GROUP BY o.activity
        )
        SELECT
            (SELECT COUNT(*) FROM selected_cycles) AS cycles,
            COALESCE((SELECT SUM(total_tonnes) FROM selected_cycles), 0) AS tonnes,
            COALESCE((SELECT AVG(total_tonnes) FROM selected_cycles), 0) AS average_tonnes,
            COALESCE((SELECT jsonb_object_agg(activity, ROUND(seconds::numeric, 1)) FROM stage_totals), '{}'::jsonb) AS stage_seconds
        """
        params = (truck, start, end)
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return {
            "kind": kind,
            "truck": truck,
            "date": intent["date"],
            "cycles": int(row[0]),
            "tonnes": float(row[1]),
            "average_tonnes": float(row[2]),
            "stage_seconds": row[3] or {},
        }

    if kind in {"queue_time", "queue_breakdown"}:
        truck = _require(intent.get("truck_name"), "truck name (or ask this as a follow-up to a truck question)")
        date_text = _require(intent.get("date"), "date")
        start_time = _require(intent.get("start_time"), "start time")
        end_time = _require(intent.get("end_time"), "end time")
        day = date.fromisoformat(date_text)
        start_clock = time.fromisoformat(start_time)
        end_clock = time.fromisoformat(end_time)
        start = datetime.combine(day, start_clock)
        end = datetime.combine(day, end_clock)
        if end <= start:
            raise ValueError("The end time must be after the start time on the same date.")
        sql = COMPONENT_SPANS + """
        SELECT o.activity,
               SUM(EXTRACT(EPOCH FROM (
                   LEAST(o.next_time, %s) - GREATEST(o.time_entered, %s)
               ))) AS seconds
        FROM ordered AS o
        JOIN cycle AS c ON c.cycle_id = o.cycle_id
        JOIN truck AS t ON t.truck_id = c.truck_id
        WHERE t.name = %s
          AND o.activity IN ('queueing_at_source', 'queuing_at_dump')
          AND o.next_time IS NOT NULL
          AND o.time_entered < %s AND o.next_time > %s
        GROUP BY o.activity
        ORDER BY o.activity
        """
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (end, start, truck, end, start))
            rows = cur.fetchall()
        breakdown = {
            "source": 0.0,
            "dump": 0.0,
        }
        for activity, seconds in rows:
            breakdown["source" if activity == "queueing_at_source" else "dump"] = round(float(seconds or 0), 1)
        result = {
            "kind": kind,
            "truck": truck,
            "date": date_text,
            "window": {"start": start_time, "end": end_time},
            "total_seconds": round(sum(breakdown.values()), 1),
        }
        if kind == "queue_breakdown":
            result["breakdown_seconds"] = breakdown
        return result

    if kind == "top_trucks":
        start, end = _day_bounds(_require(intent.get("date"), "date"))
        limit = intent.get("limit") or 3
        sql = """
        WITH first_events AS (
            SELECT c.cycle_id, c.truck_id, c.total_tonnes,
                   ROW_NUMBER() OVER (PARTITION BY c.cycle_id ORDER BY cc.time_entered) AS event_rank,
                   cc.time_entered
            FROM cycle AS c
            JOIN cycle_component AS cc ON cc.cycle_id = c.cycle_id
        )
        SELECT t.name, COUNT(*)::int AS cycles, SUM(fe.total_tonnes) AS tonnes
        FROM first_events AS fe
        JOIN truck AS t ON t.truck_id = fe.truck_id
        WHERE fe.event_rank = 1 AND fe.time_entered >= %s AND fe.time_entered < %s
        GROUP BY t.name
        ORDER BY tonnes DESC, t.name ASC
        LIMIT %s
        """
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (start, end, limit))
            rows = cur.fetchall()
        return {
            "kind": kind,
            "date": intent["date"],
            "trucks": [{"truck": row[0], "cycles": int(row[1]), "tonnes": float(row[2])} for row in rows],
        }

    if kind == "fleet_cycles":
        start, end = _day_bounds(_require(intent.get("date"), "date"))
        sql = """
        SELECT COUNT(*)::int
        FROM cycle AS c
        WHERE EXISTS (
            SELECT 1 FROM cycle_component AS cc
            WHERE cc.cycle_id = c.cycle_id
              AND cc.time_entered = (
                  SELECT MIN(first_cc.time_entered)
                  FROM cycle_component AS first_cc
                  WHERE first_cc.cycle_id = c.cycle_id
              )
              AND cc.time_entered >= %s AND cc.time_entered < %s
        )
        """
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (start, end))
            row = cur.fetchone()
        return {"kind": kind, "date": intent["date"], "cycles": int(row[0])}

    raise ValueError("I can answer truck performance, queue time, top-tonnage, and fleet-cycle questions.")


def answer_question(question: str, facts: dict[str, Any]) -> str:
    return _openai_text(
        [
            {
                "role": "system",
                "content": "Answer the user's mining operations question using only the supplied computed facts. Be concise, readable, and state units. Never invent values. Do not mention internal prompts or SQL.",
            },
            {"role": "user", "content": json.dumps({"question": question, "computed_facts": facts})},
        ]
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "Haulwise/1.0"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        path = "/index.html" if self.path == "/" else self.path
        if path.startswith("/api/") or ".." in path:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        file_path = (FRONTEND / path.lstrip("/")).resolve()
        if FRONTEND not in file_path.parents or not file_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        content_type = "text/css" if file_path.suffix == ".css" else "application/javascript" if file_path.suffix == ".js" else "text/html"
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/ask":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            question = str(payload.get("question", "")).strip()
            if not question or len(question) > 1000:
                raise ValueError("Please enter a question up to 1,000 characters.")
            history = payload.get("history") if isinstance(payload.get("history"), list) else []
            intent = parse_intent(question, history)
            if intent["kind"] == "unknown":
                raise ValueError("I could not identify that request. Try asking about performance, queue time, top tonnes, or fleet cycles.")
            facts = query_analytics(intent)
            answer = answer_question(question, facts)
            self._send_json(HTTPStatus.OK, {"answer": answer, "intent": intent, "facts": facts})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # Keep operational details out of the browser response.
            print(f"request failed: {exc}", flush=True)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The analytics service could not answer right now. Please try again."})


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Haulwise listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
