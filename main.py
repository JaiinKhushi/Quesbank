import json, os, re, secrets, sqlite3
from typing import List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB = os.getenv("DB_PATH", "questions.db")
ADMIN_ID = os.getenv("ADMIN_ID", "admin")
ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me")
API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

app = FastAPI(title="Interview Question Bank")


DATABASE_URL = os.getenv("DATABASE_URL", "")
PG = DATABASE_URL.startswith("postgres")
if PG:
    import psycopg
    from psycopg.rows import dict_row


def conn():
    if PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def sql(text: str) -> str:
    return text.replace("?", "%s") if PG else text


with conn() as c:
    if PG:
        c.execute("""CREATE TABLE IF NOT EXISTS questions(
            id SERIAL PRIMARY KEY,
            student_name TEXT DEFAULT '', roll_no TEXT DEFAULT '',
            company TEXT NOT NULL, role TEXT, round TEXT,
            topic TEXT, question TEXT NOT NULL,
            created TIMESTAMP DEFAULT NOW())""")
    else:
        c.execute("""CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL, role TEXT, round TEXT,
            topic TEXT, question TEXT NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP)""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(questions)")]
        for col in ("student_name", "roll_no"):
            if col not in cols:
                c.execute(f"ALTER TABLE questions ADD COLUMN {col} TEXT DEFAULT ''")


class CleanIn(BaseModel):
    company: str
    text: str


class Item(BaseModel):
    q: str
    topic: str = "General"


class SaveIn(BaseModel):
    student_name: str
    roll_no: str
    company: str
    role: str = ""
    round: str = "Other"
    items: List[Item]


def basic_clean(text: str):
    parts = [p.strip() for p in re.split(r"\n+|(?<=\?)\s+", text) if len(p.strip()) > 3]
    out = []
    for p in parts:
        p = re.sub(r"^[\-\*\d\.\)\s]+", "", p)
        p = p[:1].upper() + p[1:]
        if not p.endswith(("?", ".")):
            p += "?"
        out.append({"q": p, "topic": "General"})
    return out


def ai_clean(company: str, text: str):
    prompt = (
        "You clean up interview questions written by students in messy language. "
        "Split the text into individual questions. Rewrite each in clear, correct English, "
        "expand abbreviations, fix typos, and keep the meaning. Do not add new content. "
        "Then give each question ONE topic label that describes what it is really about. "
        "Choose from: OOPs, DBMS, SQL, DSA, Algorithms, OS, Computer Networks, System Design, "
        "API Design, Backend, Frontend, Cloud and DevOps, Machine Learning, Projects, Resume, "
        "Behavioral, HR, Aptitude, Puzzle, Coding, Other. "
        "Rules: Use Computer Networks only for protocols, HTTP, TCP, DNS and similar network topics. "
        "Use Projects or Resume only when the question is about the candidate's own project or resume. "
        "Use DSA or Algorithms only for real data structure or algorithm questions. "
        "Designing or scaling a whole system, traffic, load or improvements to a design are System Design. "
        "Designing endpoints or REST APIs is API Design. Tables, schemas, constraints and indexes are DBMS. "
        "If a question is a follow up, use the topic of the scenario it belongs to. "
        "If nothing fits well, use Other. Never force a label that does not match. "
        "Return ONLY a JSON array of objects with keys q and topic.\n\n"
        f"Company: {company}\n\nText:\n{text}"
    )
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": API_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
        timeout=40,
    )
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.sub(r"```json|```", "", raw).strip()
    data = json.loads(raw)
    return [{"q": d["q"], "topic": d.get("topic", "General")} for d in data if d.get("q")]
    

@app.post("/api/clean")
def clean(body: CleanIn):
    if not body.company.strip() or not body.text.strip():
        raise HTTPException(400, "Company and questions are required")
    if API_KEY:
        try:
            return {"items": ai_clean(body.company, body.text), "ai": True}
        except Exception:
            pass
    return {"items": basic_clean(body.text), "ai": False}


@app.post("/api/questions")
def save(body: SaveIn):
    name = " ".join(body.student_name.split())
    roll = body.roll_no.strip()
    if not name or not roll:
        raise HTTPException(400, "Name and roll number are required")
    company = " ".join(body.company.split()).title()
    if not company:
        raise HTTPException(400, "Company is required")
    with conn() as c:
        for it in body.items:
            if it.q.strip():
                c.execute(
                    sql("INSERT INTO questions(student_name,roll_no,company,role,round,topic,question) "
                        "VALUES(?,?,?,?,?,?,?)"),
                    (name, roll, company, body.role.strip(), body.round, it.topic, it.q.strip()),
                )
    return {"saved": len(body.items)}


def fetch_groups(q: Optional[str] = None):
    query, args = "SELECT * FROM questions", []
    if q:
        cols = ["company", "question", "topic", "student_name", "roll_no"]
        query += " WHERE " + " OR ".join(f"LOWER(COALESCE({c},'')) LIKE ?" for c in cols)
        args = [f"%{q.lower()}%"] * len(cols)
    with conn() as c:
        rows = c.execute(sql(query + " ORDER BY LOWER(company), id"), args).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["company"], []).append(dict(r))
    return [{"company": k, "questions": v} for k, v in groups.items()]


def require_admin(admin_id: str, key: str):
    ok_id = secrets.compare_digest(admin_id.encode(), ADMIN_ID.encode())
    ok_key = secrets.compare_digest(key.encode(), ADMIN_KEY.encode())
    if not (ok_id and ok_key):
        raise HTTPException(401, "Invalid admin ID or password")


@app.post("/api/login")
def login(x_admin_id: str = Header(default=""), x_admin_key: str = Header(default="")):
    require_admin(x_admin_id, x_admin_key)
    return {"ok": True}


@app.get("/api/questions")
def list_questions(q: Optional[str] = None, x_admin_id: str = Header(default=""), x_admin_key: str = Header(default="")):
    require_admin(x_admin_id, x_admin_key)
    return fetch_groups(q)


@app.delete("/api/questions/{qid}")
def delete(qid: int, x_admin_id: str = Header(default=""), x_admin_key: str = Header(default="")):
    require_admin(x_admin_id, x_admin_key)
    with conn() as c:
        c.execute(sql("DELETE FROM questions WHERE id=?"), (qid,))
    return {"deleted": qid}


@app.get("/api/export", response_class=PlainTextResponse)
def export(x_admin_id: str = Header(default=""), x_admin_key: str = Header(default="")):
    require_admin(x_admin_id, x_admin_key)
    lines = []
    for g in fetch_groups():
        lines.append(g["company"])
        for i, x in enumerate(g["questions"], 1):
            lines.append(f"{i}. {x['question']} [{x['topic']}, {x['round']}] by {x['student_name']} ({x['roll_no']})")
        lines.append("")
    return "\n".join(lines)


@app.get("/")
def home():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
