#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Explore — semantic research pipeline for MediaCrawler.
Web UI on port 18998. Phases: topic → keywords_review → crawling → analysis → next round.
LLM calls go to DeepSeek with SSE streaming.
"""
import json
import os
import re
import sqlite3 as _sqlite
import subprocess
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context
import logging
logger = logging.getLogger("MediaCrawler")

# Load .env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

app = Flask(__name__, static_folder="static", static_url_path="/static")

DATA_FILE = os.path.join(os.path.dirname(__file__), "explore_state.json")
TASKS_FILE = os.path.join(os.path.dirname(__file__), "tasks.json")

LLM_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
LLM_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def _load_state():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return _default_state()


def _save_state(s):
    with open(DATA_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _default_state():
    return {
        "topic": "",
        "research_goal": "",
        "rounds": [],
        "current_round": 0,
        "phase": "topic",
        "created_at": datetime.now().isoformat(),
    }


state = _load_state()


# ── helpers ──────────────────────────────────────────────────────────

def _get_crawler_db_path():
    """返回主库路径。数据已合并，不再按账号分库。"""
    return os.path.join(ROOT, "database", "sqlite_tables.db")


def _set_config_keywords(keywords):
    config_path = os.path.join(ROOT, "config", "base_config.py")
    with open(config_path, "r") as f:
        content = f.read()
    kw_str = ",".join(keywords)
    content = re.sub(r'(KEYWORDS\s*=\s*)".*"', f'\\1"{kw_str}"', content)
    with open(config_path, "w") as f:
        f.write(content)


def _set_config_max_notes(count):
    config_path = os.path.join(ROOT, "config", "base_config.py")
    with open(config_path, "r") as f:
        content = f.read()
    content = re.sub(r'(CRAWLER_MAX_NOTES_COUNT\s*=\s*)\d+', f'\\g<1>{count}', content)
    with open(config_path, "w") as f:
        f.write(content)


def _is_crawler_running():
    try:
        r = subprocess.run(
            ["pgrep", "-f", "main.py.*platform.*xhs"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


# ── config parser ──

_CONFIG_EDITABLE = {
    "PLATFORM", "KEYWORDS", "XHS_INTERNATIONAL", "LOGIN_TYPE", "COOKIES",
    "CRAWLER_TYPE", "CRAWLER_MAX_NOTES_COUNT", "ENABLE_GET_COMMENTS",
    "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES", "ENABLE_GET_SUB_COMMENTS",
    "ENABLE_RANDOM_SLEEP", "CRAWLER_MIN_SLEEP_SEC", "CRAWLER_MAX_SLEEP_SEC",
    "ENABLE_IP_PROXY", "IP_PROXY_POOL_COUNT", "IP_PROXY_PROVIDER_NAME",
    "HEADLESS", "SAVE_LOGIN_STATE", "ENABLE_CDP_MODE", "CDP_DEBUG_PORT",
    "CUSTOM_BROWSER_PATH", "CDP_HEADLESS", "BROWSER_LAUNCH_TIMEOUT",
    "CDP_CONNECT_EXISTING", "AUTO_CLOSE_BROWSER", "SAVE_DATA_OPTION",
    "SAVE_DATA_PATH", "USER_DATA_DIR", "START_PAGE",
    "ENABLE_SMART_CRAWLER", "MAX_CONCURRENCY_NUM", "ENABLE_GET_MEDIAS",
    "ENABLE_TEST_MODE", "TEST_REPORT_OUTPUT_PATH", "TEST_REPORT_ITEM_COUNT",
    "ENABLE_GET_WORDCLOUD", "STOP_WORDS_FILE", "FONT_PATH",
    "CRAWLER_MAX_FAILURE_RATE", "CRAWLER_MAX_CONSECUTIVE_FAILURES",
    "CRAWLER_MAX_EMPTY_PAGES", "DISABLE_SSL_VERIFY",
}


def _parse_config():
    """Parse base_config.py and return all simple key-value pairs."""
    config_path = os.path.join(ROOT, "config", "base_config.py")
    with open(config_path, "r") as f:
        lines = f.readlines()

    result = []
    for line in lines:
        # Skip imports, comments, empty lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("from ") or stripped.startswith("import "):
            continue

        # Match simple assignments: KEY = value
        m = re.match(r'^([A-Z_][A-Z_0-9]*)\s*=\s*(.+)$', stripped)
        if not m:
            continue

        key = m.group(1)
        raw_value = m.group(2).strip()

        # Strip inline comment (but preserve # inside quotes)
        if "#" in raw_value:
            # Only strip # that's not inside quotes
            in_quote = False
            quote_char = None
            for i, ch in enumerate(raw_value):
                if ch in ('"', "'") and not in_quote:
                    in_quote = True
                    quote_char = ch
                elif ch == quote_char and in_quote:
                    in_quote = False
                elif ch == "#" and not in_quote:
                    raw_value = raw_value[:i].strip()
                    break

        # Skip complex/multiline values
        if raw_value.startswith("{") or raw_value.startswith("(") or raw_value.startswith("["):
            continue

        # Parse value
        value, vtype = _parse_config_value(raw_value)
        result.append({"key": key, "value": value, "type": vtype})

    return result


def _parse_config_value(raw):
    """Parse a single config value and return (value, type_string)."""
    # Boolean
    if raw == "True":
        return True, "bool"
    if raw == "False":
        return False, "bool"
    if raw == "None":
        return None, "none"

    # Integer
    try:
        return int(raw), "int"
    except (ValueError, TypeError):
        pass

    # Float
    try:
        return float(raw), "float"
    except (ValueError, TypeError):
        pass

    # String (quoted)
    m = re.match(r'^["\'](.+)["\']$', raw)
    if m:
        return m.group(1), "str"

    # Unquoted string (like kuaidaili, qrcode etc.)
    return raw.strip('"').strip("'"), "str"


def _set_config_value(key, value):
    """Update a single config value in base_config.py."""
    if key not in _CONFIG_EDITABLE:
        return False

    config_path = os.path.join(ROOT, "config", "base_config.py")
    with open(config_path, "r") as f:
        content = f.read()

    # Build the replacement based on value type
    if isinstance(value, bool):
        new_str = str(value)
    elif isinstance(value, (int, float)):
        new_str = str(value)
    elif value is None:
        new_str = "None"
    else:
        new_str = f'"{value}"'

    # Replace: match KEY = <anything> on its own line
    pattern = rf'^({key}\s*=\s*).+$'
    replacement = rf'\1{new_str}'
    new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)

    if new_content == content:
        return False  # no change

    with open(config_path, "w") as f:
        f.write(new_content)
    return True


# ── task history ──


def _load_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_tasks(tasks):
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def _next_task_id(tasks):
    """Generate next task ID: YYYYMMDD-NNN"""
    today = datetime.now().strftime("%Y%m%d")
    max_n = 0
    for t in tasks:
        if t.get("id", "").startswith(today):
            try:
                n = int(t["id"].split("-")[1])
                if n > max_n:
                    max_n = n
            except (IndexError, ValueError):
                pass
    return f"{today}-{max_n + 1:03d}"


def _create_task_record():
    """Create a new task record from current state."""
    tasks = _load_tasks()
    task = {
        "id": _next_task_id(tasks),
        "topic": state.get("topic", ""),
        "research_goal": state.get("research_goal", ""),
        "round": state.get("current_round", 1),
        "keywords": [],
        "keywords_count": 0,
        "max_notes_per_kw": 30,
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "status": "running",
        "post_count": 0,
        "comment_count": 0,
        "analysis": "",
    }
    tasks.insert(0, task)
    _save_tasks(tasks)
    return task


def _update_task_for_round():
    """Update the latest running task with round results."""
    tasks = _load_tasks()
    if not tasks:
        return

    task = tasks[0]
    if task.get("status") != "running":
        return

    # Update from current state
    if state.get("rounds"):
        last_round = state["rounds"][-1]
        task["keywords"] = last_round.get("keywords", [])
        task["keywords_count"] = len(task["keywords"])
        task["max_notes_per_kw"] = last_round.get("max_notes_per_kw", 30)
        task["post_count"] = last_round.get("post_count", 0)
        task["comment_count"] = last_round.get("comment_count", 0)
        task["analysis"] = last_round.get("analysis", "")
        task["finished_at"] = datetime.now().isoformat()
        task["status"] = "completed"

    _save_tasks(tasks)


def _call_llm(messages, stream=False):
    """Call DeepSeek API. Returns response content string or streaming Response."""
    if not LLM_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    payload = {"model": LLM_MODEL, "messages": messages, "stream": stream}
    if not stream:
        payload["max_tokens"] = 4000

    headers = {
        "Authorization": f"Bearer {LLM_KEY}",
        "Content-Type": "application/json",
    }

    if stream:
        resp = requests.post(
            f"{LLM_BASE}/v1/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()

        def generate():
            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                if line_str.startswith("data: "):
                    data = line_str[6:]
                    if data == "[DONE]":
                        yield "data: [DONE]\n\n"
                        break
                    yield f"data: {data}\n\n"

        return Response(stream_with_context(generate()), mimetype="text/event-stream")

    resp = requests.post(
        f"{LLM_BASE}/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ── prompt builders ──

def _build_keywords_prompt(topic, goal, round_num, rounds):
    prev_info = _build_prev_context(rounds)
    if round_num == 1:
        rules = f"""请为调研主题生成一组广泛且中立的搜索关键词。

核心规则：
1. 只生成广泛词 — 主题+通用描述词，如"{topic}体验"、"{topic}分享"、"{topic}真实感受"、"聊一聊{topic}"、"关于{topic}的讨论"
2. 不要生成维度细分词或对比词
3. 禁止使用负面偏倚词（如"卡顿""吐槽""差评""不清晰""问题"），这会扭曲比例
4. 从不同角度描述同一主题，覆盖不同表达方式

输出格式（每行一个，共15-20个）：
关键词 | 广泛"""
    else:
        rules = """请为调研主题生成补充搜索关键词。

核心规则：
1. 60% 广泛词 — 主题+通用描述词
2. 30% 维度词 — 按产品线/功能/场景拆分
3. 10% 中性对比词 — 优缺点讨论、横向对比
4. 禁止使用负面偏倚词（如"卡顿""吐槽""差评""不清晰"），这会扭曲比例
5. 注意补充之前未覆盖的维度，避免已充分抓取的词

输出格式（每行一个）：
关键词 | 分类

分类可选：广泛 / 维度 / 对比"""
    return f"""你是一位市场调研专家。我需要搜索小红书上的内容，完成以下调研。

【调研主题】{topic}
【调研目标】{goal or topic}
【当前轮次】第{round_num}轮 {"（首次广度摸底）" if round_num == 1 else "（维度补全）"}

【之前的轮次】
{prev_info}

{rules}"""


def _build_analysis_prompt(state):
    last = (state.get("rounds") or [{}])[-1]
    return f"""你是一位数据分析师。我刚刚在小红书上完成了第{state['current_round']}轮数据抓取。

【调研主题】{state['topic']}
【调研目标】{state['research_goal'] or state['topic']}
【本轮关键词({last.get('keywords', [])[:20]})】{', '.join(last.get('keywords', [])[:20])}
【本轮数据量】帖子{last.get('post_count', 0)}条

请分析：

1. 本轮抓取了哪些话题维度，哪些维度数据偏少？注意区分"爬到的数据里本来就少"和"根本没去搜"两种情况。
2. 数据是否足够回答调研目标？为什么？
3. 如果不够，下一轮应该补充搜索哪些方向？给出5-10个具体关键词。如果够了，说"建议结束调研"。

输出格式：
【维度覆盖评估】
【数据饱和度判断】
【下一轮建议】关键词 | 分类"""


def _build_prev_context(rounds):
    if not rounds:
        return "（首次调研，无历史数据）"
    return "\n".join(
        f"第{r['round']}轮: 关键词({len(r['keywords'])}个)={', '.join(r['keywords'][:15])}{'...' if len(r['keywords']) > 15 else ''}  帖子={r['post_count']}  分析摘要:{r.get('analysis', '')[:100]}"
        for r in rounds
    )


# ── API ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "static", "explore.html")
    if not os.path.exists(html_path):
        return "explore.html not found", 404
    with open(html_path) as f:
        return f.read()


@app.route("/api/prompt/keywords")
def api_prompt_keywords():
    """Return the keyword generation prompt text."""
    return jsonify({
        "prompt": _build_keywords_prompt(
            state["topic"], state["research_goal"],
            state["current_round"], state.get("rounds", []),
        ),
    })


@app.route("/api/prompt/analysis")
def api_prompt_analysis():
    """Return the analysis prompt text."""
    return jsonify({"prompt": _build_analysis_prompt(state)})


@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/api/topic", methods=["POST"])
def api_set_topic():
    global state
    data = request.get_json(force=True)
    topic = (data.get("topic") or "").strip()
    research_goal = (data.get("research_goal") or "").strip()
    if not topic:
        return jsonify({"error": "topic required"}), 400

    state["topic"] = topic
    state["research_goal"] = research_goal or topic
    state["phase"] = "keywords_review"
    state["current_round"] = 1
    state["rounds"] = []
    state["pending_suggestions"] = []
    state["approved_keywords"] = None
    _save_state(state)
    return jsonify({"ok": True, "round": 1})


# ── LLM streaming endpoints ──

@app.route("/api/llm/keywords", methods=["GET"])
def api_llm_keywords():
    """Stream keyword suggestions from LLM."""
    try:
        prompt = _build_keywords_prompt(
            state["topic"], state["research_goal"],
            state["current_round"], state.get("rounds", []),
        )
        return _call_llm([{"role": "user", "content": prompt}], stream=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/llm/analyze", methods=["GET"])
def api_llm_analysis():
    """Stream analysis from LLM."""
    try:
        prompt = _build_analysis_prompt(state)
        return _call_llm([{"role": "user", "content": prompt}], stream=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── keyword management ──

@app.route("/api/keywords/suggest", methods=["POST"])
def api_keywords_suggest():
    global state
    data = request.get_json(force=True)
    suggestions = data.get("suggestions", [])
    normalized = []
    for item in suggestions:
        if isinstance(item, str):
            normalized.append({"keyword": item.strip(), "category": "建议"})
        elif isinstance(item, dict):
            kw = (item.get("keyword") or "").strip()
            if kw:
                normalized.append({"keyword": kw, "category": item.get("category", "建议")})
    state["pending_suggestions"] = normalized
    state["phase"] = "keywords_review"
    _save_state(state)
    return jsonify({"ok": True, "count": len(normalized)})


@app.route("/api/keywords/approve", methods=["POST"])
def api_approve_keywords():
    global state
    data = request.get_json(force=True)
    keywords = data.get("keywords", [])
    notes = data.get("notes", "")
    max_notes_per_kw = data.get("max_notes_per_kw", 30)

    if not keywords:
        return jsonify({"error": "keywords required"}), 400

    state["approved_keywords"] = {
        "keywords": keywords,
        "notes": notes,
        "max_notes_per_kw": max_notes_per_kw,
    }
    _save_state(state)
    return jsonify({"ok": True, "count": len(keywords)})


@app.route("/api/crawl/start", methods=["POST"])
def api_start_crawl():
    global state
    approved = state.get("approved_keywords")
    if not approved or not approved.get("keywords"):
        return jsonify({"error": "请先批准关键词"}), 400

    if _is_crawler_running():
        return jsonify({"error": "爬虫已在运行中"}), 409

    keywords = approved["keywords"]
    max_notes = approved.get("max_notes_per_kw", 30)

    _set_config_keywords(keywords)
    _set_config_max_notes(max_notes)

    current_round = state["current_round"]
    state["rounds"].append({
        "round": current_round,
        "keywords": keywords,
        "max_notes_per_kw": max_notes,
        "notes": approved.get("notes", ""),
        "status": "crawling",
        "started_at": datetime.now().isoformat(),
        "post_count": 0,
        "comment_count": 0,
    })
    state["phase"] = "crawling"
    state["approved_keywords"] = None
    _save_state(state)

    # Create task record
    task = _create_task_record()
    task["keywords"] = keywords
    task["keywords_count"] = len(keywords)
    task["max_notes_per_kw"] = max_notes
    tasks = _load_tasks()
    if tasks and tasks[0]["id"] == task["id"]:
        tasks[0] = task
        _save_tasks(tasks)

    cmd = [sys.executable, os.path.join(ROOT, "main.py"), "--platform", "xhs"]
    subprocess.Popen(cmd, cwd=ROOT)
    return jsonify({"ok": True, "round": current_round, "keywords": keywords, "max_notes_per_kw": max_notes, "task_id": task["id"]})


@app.route("/api/crawl/status")
def api_crawl_status():
    running = _is_crawler_running()
    try:
        db_path = _get_crawler_db_path()
        conn = _sqlite.connect(db_path, timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM xhs_note")
        posts = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM xhs_note_comment")
        comments = cur.fetchone()[0]
        conn.close()
    except Exception:
        posts, comments = 0, 0
    return jsonify({"running": running, "total_posts": posts, "total_comments": comments})


@app.route("/api/round/finish", methods=["POST"])
def api_finish_round():
    global state
    data = request.get_json(force=True)
    analysis = data.get("analysis", "")

    if state["rounds"]:
        current = state["rounds"][-1]
        current["status"] = "done"
        current["analysis"] = analysis
        current["finished_at"] = datetime.now().isoformat()

        try:
            keywords = current["keywords"]
            db_path = _get_crawler_db_path()
            if os.path.exists(db_path) and keywords:
                conn = _sqlite.connect(db_path, timeout=5)
                cur = conn.cursor()
                ph = ",".join(["?"] * len(keywords))
                cur.execute(f"SELECT COUNT(*) FROM xhs_note WHERE source_keyword IN ({ph})", keywords)
                current["post_count"] = cur.fetchone()[0]
                cur.execute(
                    f"SELECT COUNT(*) FROM xhs_note_comment WHERE note_id IN "
                    f"(SELECT note_id FROM xhs_note WHERE source_keyword IN ({ph}))", keywords,
                )
                current["comment_count"] = cur.fetchone()[0]
                conn.close()
        except Exception:
            current["post_count"] = -1
            current["comment_count"] = -1

    state["current_round"] += 1
    state["phase"] = "keywords_review"
    state["pending_suggestions"] = []
    state["approved_keywords"] = None
    _save_state(state)
    _update_task_for_round()
    return jsonify({"ok": True, "round": state["current_round"]})


@app.route("/api/rounds/history")
def api_rounds_history():
    return jsonify(state.get("rounds", []))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    global state
    state = _default_state()
    _save_state(state)
    return jsonify({"ok": True})


# ── common topics ──

_TOPICS_FILE = os.path.join(os.path.dirname(__file__), "common_topics.json")


def _load_topics():
    if os.path.exists(_TOPICS_FILE):
        with open(_TOPICS_FILE) as f:
            return json.load(f)
    return []


def _save_topics(topics):
    with open(_TOPICS_FILE, "w") as f:
        json.dump(topics, f, ensure_ascii=False)


@app.route("/api/common-topics")
def api_common_topics():
    return jsonify(_load_topics())


@app.route("/api/common-topics", methods=["POST"])
def api_add_common_topic():
    data = request.get_json(force=True)
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "topic required"}), 400
    topics = _load_topics()
    if topic not in topics:
        topics.insert(0, topic)
    # Keep max 20
    _save_topics(topics[:20])
    return jsonify({"ok": True})


@app.route("/api/common-topics/<path:topic>", methods=["DELETE"])
def api_delete_common_topic(topic):
    topics = _load_topics()
    topics = [t for t in topics if t != topic]
    _save_topics(topics)
    return jsonify({"ok": True})


# ── config API ──

@app.route("/api/config")
def api_get_config():
    """Return all config key-value pairs parsed from base_config.py."""
    try:
        config = _parse_config()
        return jsonify({"config": config})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["PUT"])
def api_set_config():
    """Update a single config value in base_config.py."""
    data = request.get_json(force=True)
    key = (data.get("key") or "").strip()
    value = data.get("value")

    if not key:
        return jsonify({"error": "key required"}), 400

    if key not in _CONFIG_EDITABLE:
        return jsonify({"error": f"'{key}' is not editable"}), 400

    try:
        ok = _set_config_value(key, value)
        return jsonify({"ok": ok, "key": key})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── task history API ──

@app.route("/api/tasks")
def api_tasks():
    """Return all task records, newest first."""
    tasks = _load_tasks()
    return jsonify(tasks)


@app.route("/api/tasks/<task_id>")
def api_task_detail(task_id):
    """Return a single task record."""
    tasks = _load_tasks()
    for t in tasks:
        if t.get("id") == task_id:
            return jsonify(t)
    return jsonify({"error": "task not found"}), 404


@app.route("/api/tasks/<task_id>/resume", methods=["POST"])
def api_task_resume(task_id):
    """Resume a task: restore its context into current state."""
    global state
    tasks = _load_tasks()
    task = None
    for t in tasks:
        if t.get("id") == task_id:
            task = t
            break

    if not task:
        return jsonify({"error": "task not found"}), 404

    # Restore state from task
    state["topic"] = task.get("topic", "")
    state["research_goal"] = task.get("research_goal", "")
    state["current_round"] = task.get("round", 1)
    state["rounds"] = []  # Start fresh rounds in the resumed context
    state["phase"] = "keywords_review"
    state["pending_suggestions"] = []
    state["approved_keywords"] = None

    # Pre-populate with task's keywords as suggestions for review
    if task.get("keywords"):
        state["pending_suggestions"] = [
            {"keyword": kw, "category": "原任务关键词"}
            for kw in task["keywords"]
        ]

    _save_state(state)
    return jsonify({"ok": True, "task_id": task_id, "topic": state["topic"]})


if __name__ == "__main__":
    # Clean up stale running tasks
    tasks = _load_tasks()
    changed = False
    for t in tasks:
        if t.get("status") == "running":
            t["status"] = "abandoned"
            changed = True
    if changed:
        _save_tasks(tasks)

    PORT = 18998
    logger.info(f'[explore] LLM model: {LLM_MODEL}')
    logger.info(f'[explore] LLM base: {LLM_BASE}')
    logger.info(f'[explore] Starting on http://0.0.0.0:{PORT}')
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
