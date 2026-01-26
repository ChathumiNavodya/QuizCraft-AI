import streamlit as st
import sqlite3
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.output_parsers import JsonOutputParser

import PyPDF2
import os
from datetime import datetime
import hashlib
import json
import time
import re
from typing import Tuple

from docx import Document
from striprtf.striprtf import rtf_to_text
from odf.opendocument import load
from odf import teletype
from pptx import Presentation


# ============================================================
# App Branding
# ============================================================
APP_NAME = "QuizCraft AI"
APP_ICON = "🧠"
st.set_page_config(page_title=APP_NAME, page_icon=APP_ICON, layout="wide")
SCHEMA_VERSION = "2026-01-26-v9"


# ============================================================
# Paths
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quiz_performance.db")


# ============================================================
# CSS
# ============================================================
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
      [data-testid="stSidebar"] { min-width: 280px; max-width: 360px; }
      .qc-card { padding: 1rem; border-radius: 14px; border: 1px solid rgba(255,255,255,.08); background: rgba(255,255,255,.03); }
      .qc-muted { opacity: .85; }
    </style>
    """,
    unsafe_allow_html=True
)


# ============================================================
# Helpers
# ============================================================
def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return dk.hex()

def new_salt() -> str:
    return hashlib.sha256(str(time.time()).encode("utf-8")).hexdigest()[:16]


# ============================================================
# LLM Initialization (secrets.toml)
# ============================================================
def initialize_llm():
    try:
        api_key = st.secrets.get("GOOGLE_API_KEY", None) or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            st.error('Missing GOOGLE_API_KEY in .streamlit/secrets.toml')
            return None
        return ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key=api_key)
    except Exception as e:
        st.error(f"Failed to initialize LLM: {e}")
        return None


# ============================================================
# DB Setup
# ============================================================
@st.cache_resource
def init_db(schema_version: str):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            salt TEXT,
            password_hash TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS modules (
            module_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            module_name TEXT,
            UNIQUE(user_id, module_name),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS quizzes (
            quiz_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            module_id INTEGER,
            timestamp TEXT,
            notes_hash TEXT,
            difficulty TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (module_id) REFERENCES modules(module_id)
        );

        CREATE TABLE IF NOT EXISTS questions (
            question_id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER,
            question_text TEXT,
            options TEXT,
            correct_answer INTEGER,
            explanation TEXT,
            FOREIGN KEY (quiz_id) REFERENCES quizzes(quiz_id)
        );

        CREATE TABLE IF NOT EXISTS user_answers (
            answer_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            quiz_id INTEGER,
            question_id INTEGER,
            user_answer INTEGER,
            is_correct INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (quiz_id) REFERENCES quizzes(quiz_id),
            FOREIGN KEY (question_id) REFERENCES questions(question_id)
        );

        CREATE TABLE IF NOT EXISTS performance (
            performance_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            module_id INTEGER,
            timestamp TEXT,
            score INTEGER,
            total INTEGER,
            percentage REAL,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (module_id) REFERENCES modules(module_id)
        );

        CREATE TABLE IF NOT EXISTS notes_chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            notes_hash TEXT,
            chunk_index INTEGER,
            chunk_text TEXT
        );
    """)
    conn.commit()
    return conn


# ============================================================
# Auth
# ============================================================
def create_user(conn, username: str, password: str) -> Tuple[bool, str]:
    username = username.strip()
    if not username or not password:
        return False, "Username and password required."

    if len(password) < 6 or not re.search(r"\d", password):
        return False, "Password must be at least 6 characters and include a number."

    salt = new_salt()
    ph = hash_password(password, salt)
    try:
        conn.execute(
            "INSERT INTO users (username, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (username, salt, ph, datetime.now().isoformat(timespec="seconds"))
        )
        conn.commit()
        return True, "Account created. You can log in now."
    except sqlite3.IntegrityError:
        return False, "Username already exists."

def login_user(conn, username: str, password: str) -> Tuple[bool, str, int]:
    c = conn.cursor()
    c.execute("SELECT user_id, salt, password_hash FROM users WHERE username = ?", (username.strip(),))
    row = c.fetchone()
    if not row:
        return False, "User not found.", -1
    user_id, salt, ph = row
    if hash_password(password, salt) != ph:
        return False, "Wrong password.", -1
    return True, "Login successful.", user_id


# ============================================================
# Modules / DB helpers
# ============================================================
def get_or_create_module(conn, user_id: int, module_name: str):
    c = conn.cursor()
    c.execute("SELECT module_id FROM modules WHERE user_id = ? AND module_name = ?", (user_id, module_name))
    row = c.fetchone()
    if row:
        return row[0]
    c.execute("INSERT INTO modules (user_id, module_name) VALUES (?, ?)", (user_id, module_name))
    conn.commit()
    return c.lastrowid

def list_modules(conn, user_id: int):
    c = conn.cursor()
    c.execute("SELECT module_id, module_name FROM modules WHERE user_id = ? ORDER BY module_name ASC", (user_id,))
    return c.fetchall()

def save_quiz(conn, user_id: int, module_id: int, notes_hash: str, quiz_data: dict, difficulty: str):
    c = conn.cursor()
    timestamp = datetime.now().isoformat(timespec="seconds")
    c.execute(
        "INSERT INTO quizzes (user_id, module_id, timestamp, notes_hash, difficulty) VALUES (?, ?, ?, ?, ?)",
        (user_id, module_id, timestamp, notes_hash, difficulty),
    )
    quiz_id = c.lastrowid
    for q in quiz_data["questions"]:
        c.execute(
            "INSERT INTO questions (quiz_id, question_text, options, correct_answer, explanation) VALUES (?, ?, ?, ?, ?)",
            (quiz_id, q["question"], json.dumps(q["options"]), int(q["answer"]), q.get("explanation", "")),
        )
    conn.commit()
    return quiz_id

def list_quiz_ids_for_module(conn, user_id: int, module_id: int, limit: int = 50):
    return conn.execute(
        "SELECT quiz_id, timestamp, difficulty FROM quizzes WHERE user_id=? AND module_id=? ORDER BY quiz_id DESC LIMIT ?",
        (user_id, module_id, limit)
    ).fetchall()

def load_quiz(conn, quiz_id: int):
    qrow = conn.execute("SELECT quiz_id, difficulty, timestamp FROM quizzes WHERE quiz_id=?", (quiz_id,)).fetchone()
    if not qrow:
        return None
    qdf = pd.read_sql_query(
        "SELECT question_id, question_text, options, correct_answer, explanation FROM questions WHERE quiz_id=? ORDER BY question_id ASC",
        conn,
        params=(quiz_id,)
    )
    questions = []
    for _, r in qdf.iterrows():
        questions.append({
            "question_id": int(r["question_id"]),
            "question": r["question_text"],
            "options": json.loads(r["options"]),
            "answer": int(r["correct_answer"]),
            "explanation": r["explanation"] or ""
        })
    return {"quiz_id": qrow[0], "difficulty": qrow[1], "timestamp": qrow[2], "questions": questions}

def save_user_answers_and_performance(conn, user_id: int, module_id: int, quiz: dict, user_choices: dict):
    quiz_id = quiz["quiz_id"]
    total = len(quiz["questions"])
    score = 0

    conn.execute("DELETE FROM user_answers WHERE user_id=? AND quiz_id=?", (user_id, quiz_id))

    for q in quiz["questions"]:
        qid = q["question_id"]
        correct = q["answer"]
        chosen = int(user_choices.get(qid, -1))
        is_correct = 1 if chosen == correct else 0
        score += is_correct
        conn.execute(
            "INSERT INTO user_answers (user_id, quiz_id, question_id, user_answer, is_correct) VALUES (?, ?, ?, ?, ?)",
            (user_id, quiz_id, qid, chosen, is_correct)
        )

    percentage = (score / total) * 100 if total else 0
    conn.execute(
        "INSERT INTO performance (user_id, module_id, timestamp, score, total, percentage) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, module_id, datetime.now().isoformat(timespec="seconds"), score, total, percentage)
    )
    conn.commit()
    return score, total, percentage

def fetch_performance(conn, user_id: int, module_id: int):
    df = pd.read_sql_query(
        "SELECT timestamp, score, total, percentage FROM performance WHERE user_id=? AND module_id=? ORDER BY timestamp ASC",
        conn,
        params=(user_id, module_id),
    )
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


# ============================================================
# Plotting (nicer)
# ============================================================
def plot_progress(df: pd.DataFrame):
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(df["timestamp"], df["percentage"], marker="o", linestyle="-")
    ax.set_title("Performance Over Time")
    ax.set_xlabel("Date")
    ax.set_ylabel("Score (%)")
    ax.set_ylim(0, 100)
    ax.grid(True, alpha=0.3)

    locator = mdates.AutoDateLocator(minticks=3, maxticks=7)
    formatter = mdates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)

    fig.tight_layout()
    return fig


# ============================================================
# File extraction
# ============================================================
def extract_notes(file) -> str:
    name = file.name.lower()

    if name.endswith(".pdf"):
        reader = PyPDF2.PdfReader(file)
        return "\n".join([(p.extract_text() or "") for p in reader.pages]).strip()

    if name.endswith(".docx"):
        doc = Document(file)
        return "\n".join([p.text for p in doc.paragraphs]).strip()

    if name.endswith(".rtf"):
        raw = file.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        return rtf_to_text(raw).strip()

    if name.endswith(".odt"):
        doc = load(file)
        return teletype.extractText(doc.text).strip()

    if name.endswith(".pptx"):
        prs = Presentation(file)
        all_text = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    all_text.append(shape.text)
        return "\n".join(all_text).strip()

    raw = file.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return raw.strip()


# ============================================================
# JSON validation
# ============================================================
def validate_quiz_json(data):
    if not isinstance(data, dict) or "questions" not in data or not isinstance(data["questions"], list):
        return False
    for q in data["questions"]:
        if not {"question", "options", "answer"}.issubset(q.keys()):
            return False
        if not isinstance(q["options"], list) or len(q["options"]) != 4:
            return False
        if not isinstance(q["answer"], int) or not (0 <= q["answer"] <= 3):
            return False
    return True


# ============================================================
# Quiz generation (safe retry)
# ============================================================
def generate_quiz_once(llm, notes_text: str, num_questions: int, difficulty: str):
    parser = JsonOutputParser()
    prompt = PromptTemplate(
        template=(
            "You are a quiz generator.\n"
            "Difficulty level: {difficulty}\n"
            "Create {num_questions} multiple-choice questions based ONLY on the notes.\n"
            "Return ONLY valid JSON in this exact format:\n"
            "{{\"questions\": ["
            "{{\"question\": \"...\", \"options\": [\"A\",\"B\",\"C\",\"D\"], \"answer\": 0, \"explanation\": \"short explanation\"}}"
            "]}}\n\n"
            "NOTES:\n{notes}\n"
        ),
        input_variables=["notes", "num_questions", "difficulty"],
    )
    chain = prompt | llm | parser
    result = chain.invoke({"notes": notes_text, "num_questions": num_questions, "difficulty": difficulty})
    if not validate_quiz_json(result):
        raise ValueError("LLM returned invalid quiz JSON.")
    return result

def generate_quiz_with_retry(llm, notes_text: str, num_questions: int, difficulty: str, max_attempts: int = 3):
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return generate_quiz_once(llm, notes_text, num_questions, difficulty)
        except Exception as e:
            last_err = e
            if attempt < max_attempts:
                time.sleep(1.0)
            else:
                raise last_err


# ============================================================
# Main UI
# ============================================================
def main():
    # navigation state
    if "active_tab" not in st.session_state:
        st.session_state["active_tab"] = "Notes → Quiz"

    st.title(f"{APP_ICON} {APP_NAME}")
    st.caption("Generate quizzes from notes and let users answer them — with retakes & progress tracking.")

    # Welcome content BEFORE login (so page not empty)
    st.markdown('<div class="qc-card">', unsafe_allow_html=True)
    st.markdown("### Welcome 👋")
    st.markdown('<p class="qc-muted">Create quizzes from your notes, take quizzes, and track your progress.</p>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    c1.info("📝 Upload Notes → Quiz")
    c2.success("✅ Take Quiz + Retake")
    c3.warning("📊 Track Progress")
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("---")

    conn = init_db(SCHEMA_VERSION)
    llm = initialize_llm()
    if llm is None:
        st.stop()

    # ---------------- Sidebar Auth ----------------
    with st.sidebar:
        st.header("🔐 Account")
        st.caption("Login to access your modules and quizzes.")
        st.divider()

        if "user_id" not in st.session_state:
            auth_tab = st.radio("Choose", ["Login", "Register"], horizontal=True)

            if auth_tab == "Login":
                u = st.text_input("Username", key="login_user")
                show = st.checkbox("Show password", key="show_pw_login")
                p = st.text_input("Password", type="text" if show else "password", key="login_pass")

                if st.button("Login", use_container_width=True):
                    ok, msg, uid = login_user(conn, u, p)
                    if ok:
                        st.session_state["user_id"] = uid
                        st.session_state["username"] = u.strip()
                        st.rerun()
                    else:
                        st.error(msg)
            else:
                u = st.text_input("New username", key="reg_user")
                show2 = st.checkbox("Show password", key="show_pw_reg")
                p = st.text_input("New password", type="text" if show2 else "password", key="reg_pass")
                p2 = st.text_input("Confirm password", type="text" if show2 else "password", key="reg_pass2")

                if st.button("Create account", use_container_width=True):
                    if p != p2:
                        st.error("Passwords do not match.")
                    else:
                        ok, msg = create_user(conn, u, p)
                        st.success(msg) if ok else st.error(msg)

        else:
            st.success(f"Logged in as **{st.session_state.get('username','')}**")
            if st.button("Logout", use_container_width=True):
                st.session_state.clear()
                st.rerun()

    if "user_id" not in st.session_state:
        st.info("Please login to continue.")
        return

    user_id = st.session_state["user_id"]

    # ---------------- Sidebar Modules ----------------
    with st.sidebar:
        st.header("📚 Modules")
        module_name = st.text_input("Create module", placeholder="e.g., Accounting, Python, Biology")
        if st.button("➕ Create / Open", use_container_width=True):
            if module_name.strip():
                mid = get_or_create_module(conn, user_id, module_name.strip())
                st.session_state["module_id"] = mid
                st.session_state["module_name"] = module_name.strip()
                st.rerun()
            else:
                st.warning("Enter a module name.")

        st.markdown("---")
        modules = list_modules(conn, user_id)
        if modules:
            for mid, mname in modules:
                if st.button(f"📂 {mname}", key=f"open_{mid}", use_container_width=True):
                    st.session_state["module_id"] = mid
                    st.session_state["module_name"] = mname
                    st.rerun()

    if "module_id" not in st.session_state:
        st.info("Create or open a module from the sidebar to begin.")
        return

    module_id = st.session_state["module_id"]
    module_name = st.session_state.get("module_name", "")
    st.subheader(f"Module: {module_name}")

    # =========================
    # REAL NAVIGATION (no tabs)
    # =========================
    tab_labels = ["Notes → Quiz", "Take Quiz (1-50)", "Progress"]
    st.session_state["active_tab"] = st.radio(
        "Navigation",
        tab_labels,
        horizontal=True,
        index=tab_labels.index(st.session_state.get("active_tab", "Notes → Quiz")),
        label_visibility="collapsed",
    )
    st.markdown("---")

    # ---------------- Page 1: Generate quiz ----------------
    if st.session_state["active_tab"] == "Notes → Quiz":
        uploaded = st.file_uploader("Upload notes", type=["pdf", "docx", "rtf", "odt", "pptx", "txt"])

        num_questions = st.number_input(
            "Number of questions",
            min_value=1,
            max_value=50,
            value=10,
            step=1
        )

        difficulty = st.selectbox("Difficulty", ["Easy", "Medium", "Hard"], index=1)

        if uploaded:
            notes_text = extract_notes(uploaded)
            if not notes_text:
                st.error("Could not extract text from the file.")
                st.stop()

            notes_hash = compute_hash(notes_text)
            st.text_area("Notes preview", notes_text[:5000], height=260)

            if st.button("✨ Generate Quiz", use_container_width=True):
                with st.spinner("Generating quiz (with safe retries)..."):
                    try:
                        quiz = generate_quiz_with_retry(llm, notes_text, int(num_questions), difficulty, max_attempts=3)
                        quiz_id = save_quiz(conn, user_id, module_id, notes_hash, quiz, difficulty)
                        st.session_state["selected_quiz_id"] = quiz_id

                        st.success(f"Quiz created (ID: {quiz_id}).")

                        cA, cB = st.columns(2)
                        with cA:
                            if st.button("➡️ Go to Take Quiz", use_container_width=True):
                                st.session_state["active_tab"] = "Take Quiz (1-50)"
                                st.rerun()
                        with cB:
                            if st.button("📊 Go to Progress", use_container_width=True):
                                st.session_state["active_tab"] = "Progress"
                                st.rerun()

                        st.download_button(
                            "⬇️ Download Quiz JSON",
                            data=json.dumps(quiz, indent=2),
                            file_name=f"quiz_{quiz_id}.json",
                            mime="application/json",
                            use_container_width=True
                        )
                    except Exception as e:
                        st.error(f"Quiz generation failed after retries: {e}")
        else:
            st.info("Upload a file to generate a quiz.")

    # ---------------- Page 2: Take quiz ----------------
    elif st.session_state["active_tab"] == "Take Quiz (1-50)":
        rows = list_quiz_ids_for_module(conn, user_id, module_id, limit=50)

        if not rows:
            st.info("No quizzes found. Generate a quiz first.")
        else:
            options = [(qid, f"Quiz #{qid} • {ts} • {diff}") for (qid, ts, diff) in rows]
            default_qid = st.session_state.get("selected_quiz_id") or options[0][0]

            selected_label = st.selectbox(
                "Select a quiz (last 50)",
                options=[lbl for _, lbl in options],
                index=next((i for i, (qid, lbl) in enumerate(options) if qid == default_qid), 0),
            )

            selected_quiz_id = next(qid for qid, lbl in options if lbl == selected_label)
            st.session_state["selected_quiz_id"] = selected_quiz_id

            colA, _ = st.columns([1, 1])
            with colA:
                if st.button("🔁 Retake Quiz (clear answers)", use_container_width=True):
                    quiz_loaded = load_quiz(conn, selected_quiz_id)
                    for q in quiz_loaded["questions"]:
                        k = f"q_{selected_quiz_id}_{q['question_id']}"
                        if k in st.session_state:
                            del st.session_state[k]
                    st.success("Retake ready — answers cleared.")
                    st.rerun()

            quiz = load_quiz(conn, selected_quiz_id)
            st.markdown(f"**Quiz ID:** {selected_quiz_id} • **Difficulty:** {quiz['difficulty']} • **Created:** {quiz['timestamp']}")
            st.markdown("---")

            user_choices = {}

            with st.form(key=f"quiz_form_{selected_quiz_id}"):
                for i, q in enumerate(quiz["questions"], start=1):
                    st.markdown(f"### Q{i}. {q['question']}")
                    choice = st.radio(
                        "Choose one:",
                        options=[0, 1, 2, 3],
                        format_func=lambda x: q["options"][x],
                        key=f"q_{selected_quiz_id}_{q['question_id']}",
                        index=None
                    )
                    user_choices[q["question_id"]] = choice
                    st.markdown("")
                submitted = st.form_submit_button("✅ Submit Answers", use_container_width=True)

            if submitted:
                missing = [qid for qid, c in user_choices.items() if c is None]
                if missing:
                    st.error("Please answer all questions before submitting.")
                else:
                    score, total, pct = save_user_answers_and_performance(conn, user_id, module_id, quiz, user_choices)
                    st.success(f"Score: {score}/{total}  •  {pct:.1f}%")

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("📊 View Progress", use_container_width=True):
                            st.session_state["active_tab"] = "Progress"
                            st.rerun()
                    with c2:
                        if st.button("📝 Back to Notes → Quiz", use_container_width=True):
                            st.session_state["active_tab"] = "Notes → Quiz"
                            st.rerun()

                    st.markdown("---")
                    st.subheader("Review")
                    for i, q in enumerate(quiz["questions"], start=1):
                        chosen = int(user_choices[q["question_id"]])
                        correct = int(q["answer"])
                        if chosen == correct:
                            st.success(f"Q{i}: Correct ✅  ({q['options'][chosen]})")
                        else:
                            st.error(f"Q{i}: Wrong ❌  You chose: {q['options'][chosen]} | Correct: {q['options'][correct]}")
                        if q.get("explanation"):
                            st.caption(f"Explanation: {q['explanation']}")

    # ---------------- Page 3: Progress ----------------
    else:
        df = fetch_performance(conn, user_id, module_id)
        if df.empty:
            st.info("No performance recorded yet. Take a quiz and submit answers.")
        else:
            fig = plot_progress(df)
            st.pyplot(fig, use_container_width=True)
            st.dataframe(df, use_container_width=True)


if __name__ == "__main__":
    main()