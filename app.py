import json, re, hashlib, secrets, datetime as dt
import fitz  # PyMuPDF
import requests
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="내 수업 복습기", page_icon="📚", layout="centered")
today = dt.date.today()
DAILY_LIMIT = 5  # 한 사람이 하루에 만들 수 있는 자료 수 (공용 API 키 보호)

# ---------- DB (배포: Postgres / 내 컴퓨터: SQLite 자동 선택) ----------
@st.cache_resource
def get_engine():
    try:
        url = st.secrets["DATABASE_URL"]
    except Exception:
        url = "sqlite:///study.db"
    url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return create_engine(url, pool_pre_ping=True)

ENG = get_engine()
PG = ENG.dialect.name == "postgresql"
PK = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

def rows(sql, **p):
    with ENG.begin() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), p)]

def run(sql, **p):
    with ENG.begin() as c:
        c.execute(text(sql), p)

def insert(sql, **p):
    with ENG.begin() as c:
        return c.execute(text(sql + " RETURNING id"), p).scalar()

@st.cache_resource
def init():
    with ENG.begin() as c:
        for s in [
            f"CREATE TABLE IF NOT EXISTS app_users(id {PK}, username TEXT UNIQUE, salt TEXT, pw TEXT)",
            f"CREATE TABLE IF NOT EXISTS courses(id {PK}, user_id INT, name TEXT, exam_date TEXT)",
            f"CREATE TABLE IF NOT EXISTS docs(id {PK}, user_id INT, course_id INT, filename TEXT, summary TEXT, created TEXT)",
            f"CREATE TABLE IF NOT EXISTS cards(id {PK}, user_id INT, course_id INT, doc_id INT, question TEXT, answer TEXT, page INT,"
            " ease REAL DEFAULT 2.5, ivl INT DEFAULT 0, reps INT DEFAULT 0, lapses INT DEFAULT 0, due TEXT)",
            f"CREATE TABLE IF NOT EXISTS logs(id {PK}, user_id INT, card_id INT, study_day TEXT, grade INT)",
        ]:
            c.execute(text(s))
    return True
init()

# ---------- 로그인 ----------
def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()

def login_screen():
    st.title("📚 내 수업 복습기")
    st.write("강의 PDF를 올리면 요약과 복습 카드를 만들고, 잊을 때쯤 다시 알려드려요.")
    tab1, tab2 = st.tabs(["로그인", "처음이에요 (가입)"])
    with tab1:
        u = st.text_input("아이디", key="lu"); p = st.text_input("비밀번호", type="password", key="lp")
        if st.button("로그인", type="primary", use_container_width=True):
            r = rows("SELECT * FROM app_users WHERE username=:u", u=u.strip())
            if r and hash_pw(p, r[0]["salt"]) == r[0]["pw"]:
                st.session_state["uid"], st.session_state["uname"] = r[0]["id"], r[0]["username"]; st.rerun()
            else:
                st.error("아이디 또는 비밀번호가 틀려요.")
    with tab2:
        u = st.text_input("사용할 아이디", key="su"); p = st.text_input("비밀번호 (4자 이상)", type="password", key="sp")
        if st.button("가입하고 시작", use_container_width=True):
            if len(u.strip()) < 2 or len(p) < 4:
                st.error("아이디 2자 이상, 비밀번호 4자 이상으로 해주세요.")
            elif rows("SELECT id FROM app_users WHERE username=:u", u=u.strip()):
                st.error("이미 있는 아이디예요.")
            else:
                salt = secrets.token_hex(8)
                uid = insert("INSERT INTO app_users(username,salt,pw) VALUES(:u,:s,:p)", u=u.strip(), s=salt, p=hash_pw(p, salt))
                st.session_state["uid"], st.session_state["uname"] = uid, u.strip(); st.rerun()
    st.caption("⚠️ 비밀번호는 암호화되어 저장되지만, 중요한 비밀번호는 쓰지 마세요.")

if "uid" not in st.session_state:
    login_screen(); st.stop()
UID = st.session_state["uid"]

# ---------- LLM ----------
def get_key():
    try: return st.secrets["GEMINI_API_KEY"]
    except Exception: return st.session_state.get("mykey", "")

def call_llm(prompt):
    model = "gemini-2.5-flash"
    try: model = st.secrets["GEMINI_MODEL"]
    except Exception: pass
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": get_key()}, timeout=120,
        json={"contents": [{"parts": [{"text": prompt}]}]})
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def extract_pages(file):
    doc = fitz.open(stream=file.read(), filetype="pdf")
    return [(i + 1, p.get_text().strip()) for i, p in enumerate(doc)]

def make_prompt(pages, n):
    body = "\n".join(f"[p.{i}]\n{t}" for i, t in pages if t)[:30000]
    return f"""너는 대학생 복습을 돕는 튜터다. 아래 강의자료에 '있는 내용만' 사용해라(지어내지 말 것).
JSON만 출력해라. 형식:
{{"summary": "핵심 요약 (5~8줄, 한국어)",
 "cards": [{{"q": "질문", "a": "간결한 정답/설명", "page": 페이지번호}}]}}
카드는 {n}개. 단순 암기보다 이해를 확인하는 질문을 섞어라.

강의자료:
{body}"""

def parse_json(t):
    return json.loads(re.search(r"\{.*\}", t, re.S).group(0))

# ---------- SM-2 ----------
GRADES = {"다시 (몰랐음)": 1, "어려웠음": 3, "알았음": 4, "쉬웠음": 5}

def schedule(card, q, exam_date):
    ease, ivl, reps, lapses = card["ease"], card["ivl"], card["reps"], card["lapses"]
    if q < 3:
        reps, ivl, lapses = 0, 1, lapses + 1
    else:
        ivl = 1 if reps == 0 else 3 if reps == 1 else round(ivl * ease)
        if q == 3: ivl = max(1, round(ivl * 0.8))
        if q == 5: ivl = round(ivl * 1.3)
        reps += 1
    ease = max(1.3, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    if exam_date:
        left = (dt.date.fromisoformat(exam_date) - today).days
        if left > 1: ivl = min(ivl, max(1, left - 1))
    ivl = max(1, ivl)
    return ease, ivl, reps, lapses, (today + dt.timedelta(days=ivl)).isoformat()

def streak():
    days = {r["study_day"] for r in rows("SELECT DISTINCT study_day FROM logs WHERE user_id=:u", u=UID)}
    n, d = 0, today
    if d.isoformat() not in days: d -= dt.timedelta(days=1)
    while d.isoformat() in days:
        n += 1; d -= dt.timedelta(days=1)
    return n

# ---------- 사이드바 ----------
st.sidebar.title("📚 내 수업 복습기")
st.sidebar.write(f"👤 {st.session_state['uname']}")
page = st.sidebar.radio("메뉴", ["오늘의 복습", "자료 올리기", "내 학습 현황"])
st.sidebar.metric("🔥 연속 학습", f"{streak()}일")
if not get_key():
    st.session_state["mykey"] = st.sidebar.text_input("Gemini API 키", type="password")
if st.sidebar.button("로그아웃"):
    st.session_state.clear(); st.rerun()

courses = rows("SELECT * FROM courses WHERE user_id=:u ORDER BY name", u=UID)

# ---------- 오늘의 복습 ----------
if page == "오늘의 복습":
    st.title("오늘의 복습")
    due = rows("SELECT cards.*, courses.name AS cname, courses.exam_date AS ed FROM cards JOIN courses ON courses.id=cards.course_id "
               "WHERE cards.user_id=:u AND cards.due<=:t ORDER BY cards.due LIMIT 50", u=UID, t=today.isoformat())
    done = rows("SELECT COUNT(*) AS n FROM logs WHERE user_id=:u AND study_day=:t", u=UID, t=today.isoformat())[0]["n"]
    goal = 8
    st.progress(min(done / goal, 1.0), text=f"오늘 목표 {done}/{goal}문제")
    if not due:
        st.success("🎉 오늘 복습할 카드가 없어요! 새 자료를 올려보세요.")
    else:
        st.caption(f"남은 복습 카드: {len(due)}장")
        card = due[0]
        st.subheader(f"[{card['cname']}] {card['question']}")
        st.caption(f"출처: p.{card['page']}")
        st.text_area("내 답 (선택 — 먼저 떠올려 보세요)", key=f"a{card['id']}")
        if st.session_state.get("show") != card["id"]:
            if st.button("정답 보기", type="primary", use_container_width=True):
                st.session_state["show"] = card["id"]; st.rerun()
        else:
            st.info(card["answer"])
            for col, (label, q) in zip(st.columns(4), GRADES.items()):
                if col.button(label, use_container_width=True, key=f"{label}{card['id']}"):
                    e, i, r, l, d = schedule(card, q, card["ed"])
                    run("UPDATE cards SET ease=:e,ivl=:i,reps=:r,lapses=:l,due=:d WHERE id=:id", e=e, i=i, r=r, l=l, d=d, id=card["id"])
                    run("INSERT INTO logs(user_id,card_id,study_day,grade) VALUES(:u,:c,:d,:g)", u=UID, c=card["id"], d=today.isoformat(), g=q)
                    st.session_state["show"] = None; st.rerun()

# ---------- 자료 올리기 ----------
elif page == "자료 올리기":
    st.title("강의자료 올리기")
    sel = st.selectbox("과목", [c["name"] for c in courses] + ["➕ 새 과목 만들기"])
    if sel == "➕ 새 과목 만들기":
        new = st.text_input("과목 이름")
        if st.button("과목 추가") and new.strip():
            run("INSERT INTO courses(user_id,name) VALUES(:u,:n)", u=UID, n=new.strip()); st.rerun()
    else:
        course = next(c for c in courses if c["name"] == sel)
        ed = st.date_input("시험일 (선택)", value=dt.date.fromisoformat(course["exam_date"]) if course["exam_date"] else None)
        if ed and str(ed) != course["exam_date"]:
            run("UPDATE courses SET exam_date=:e WHERE id=:i", e=str(ed), i=course["id"])
        f = st.file_uploader("PDF 파일", type="pdf")
        n = st.slider("만들 카드 수", 5, 30, 10)
        used = rows("SELECT COUNT(*) AS n FROM docs WHERE user_id=:u AND created=:t", u=UID, t=str(today))[0]["n"]
        st.caption(f"오늘 만든 자료 {used}/{DAILY_LIMIT}")
        if f and st.button("✨ 요약 + 복습카드 한 번에 만들기", type="primary", use_container_width=True):
            if not get_key(): st.error("API 키가 없어요. 왼쪽에 키를 입력하세요.")
            elif used >= DAILY_LIMIT: st.error("오늘 만들 수 있는 횟수를 모두 썼어요. 내일 다시 해주세요!")
            else:
                try:
                    with st.spinner("읽고 만드는 중... (30초 정도 걸려요)"):
                        pages = extract_pages(f)
                        if not any(t for _, t in pages):
                            st.error("글자를 읽을 수 없는 PDF예요 (스캔본일 수 있어요)."); st.stop()
                        data = parse_json(call_llm(make_prompt(pages, n)))
                    did = insert("INSERT INTO docs(user_id,course_id,filename,summary,created) VALUES(:u,:c,:f,:s,:t)",
                                 u=UID, c=course["id"], f=f.name, s=data["summary"], t=str(today))
                    for k in data["cards"]:
                        run("INSERT INTO cards(user_id,course_id,doc_id,question,answer,page,due) VALUES(:u,:c,:d,:q,:a,:p,:t)",
                            u=UID, c=course["id"], d=did, q=k["q"], a=k["a"], p=int(k.get("page") or 0), t=today.isoformat())
                    st.success(f"카드 {len(data['cards'])}장 생성 완료! '오늘의 복습'에서 시작하세요.")
                    st.markdown("### 요약"); st.write(data["summary"])
                except Exception as e:
                    st.error(f"실패: {e}")

# ---------- 내 학습 현황 ----------
else:
    st.title("내 학습 현황")
    if not courses: st.info("아직 과목이 없어요.")
    for co in courses:
        title = f"📘 {co['name']}" + (f" (시험 D-{(dt.date.fromisoformat(co['exam_date'])-today).days})" if co["exam_date"] else "")
        with st.expander(title, expanded=True):
            tot = rows("SELECT COUNT(*) AS n FROM cards WHERE course_id=:c", c=co["id"])[0]["n"]
            mastered = rows("SELECT COUNT(*) AS n FROM cards WHERE course_id=:c AND ivl>=7", c=co["id"])[0]["n"]
            weak = rows("SELECT question,lapses,page FROM cards WHERE course_id=:c AND lapses>0 ORDER BY lapses DESC LIMIT 5", c=co["id"])
            docs = rows("SELECT filename,summary FROM docs WHERE course_id=:c", c=co["id"])
            acc = rows("SELECT study_day, AVG(CASE WHEN grade>=3 THEN 1.0 ELSE 0 END)*100 AS acc FROM logs JOIN cards ON cards.id=logs.card_id "
                       "WHERE cards.course_id=:c GROUP BY study_day ORDER BY study_day", c=co["id"])
            st.write(f"카드 {tot}장 · 장기 기억 단계 {mastered}장")
            if tot: st.progress(mastered / tot)
            if weak:
                st.markdown("**⚠️ 자주 틀리는 개념**")
                for w in weak: st.write(f"- {w['question']} (p.{w['page']}, {w['lapses']}회 오답)")
            if acc: st.line_chart(pd.DataFrame(acc).set_index("study_day").astype(float))
            if docs and st.button("📑 학기 전체 통합 요약", key=f"sum{co['id']}"):
                if not get_key(): st.error("API 키가 없어요.")
                else:
                    joined = "\n\n".join(f"## {d['filename']}\n{d['summary']}" for d in docs)
                    try:
                        st.write(call_llm("아래는 한 과목의 자료별 요약이다. 자료 간 연결 관계를 살려 한국어로 통합 정리하고, "
                                          "마지막에 '시험 전 꼭 볼 것 3가지'를 써라.\n\n" + joined))
                    except Exception as e: st.error(e)
