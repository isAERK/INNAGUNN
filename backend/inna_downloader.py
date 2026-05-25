
"""
INNA Archive Downloader

A careful local archiver for a student's own INNA account.
It uses Chrome + Playwright with a persistent browser profile. No Google password is
stored or typed by this script. If login is needed, the user completes it in Chrome.

Default test target:
    python inna_downloader.py download --group-id 638000

Useful full run:
    python inna_downloader.py download --all --out "E:\\INNA_Archive"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime as dt
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import textwrap
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

SIDE_LOG_FILE = None
EVENTS_FILE = None

def configure_events_file(path):
    global EVENTS_FILE
    EVENTS_FILE = Path(path) if path else None
    if EVENTS_FILE:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        EVENTS_FILE.write_text('', encoding='utf-8')

def emit_event(event_type, **data):
    if not EVENTS_FILE:
        return
    try:
        timestamp = now_iso()
    except Exception:
        timestamp = dt.datetime.now().isoformat(timespec="seconds")
    payload = {"time": timestamp, "type": event_type}
    payload.update(data)
    try:
        with EVENTS_FILE.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + '\n')
            fh.flush()
    except Exception:
        pass

def configure_side_log(path):
    global SIDE_LOG_FILE
    SIDE_LOG_FILE = Path(path) if path else None
    if SIDE_LOG_FILE:
        SIDE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIDE_LOG_FILE.write_text('', encoding='utf-8')

def side_log(message):
    text = str(message)
    if SIDE_LOG_FILE:
        try:
            with SIDE_LOG_FILE.open('a', encoding='utf-8') as fh:
                fh.write(text + '\n')
                fh.flush()
        except Exception:
            pass
        return
    try:
        print(text, flush=True)
    except Exception:
        pass

from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover
    print("Missing dependency: playwright")
    print("Install with: py -m pip install -r requirements.txt")
    raise

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


BASE_URL_CANDIDATES = ["https://nam.inna.is"]
BASE_URL = "https://nam.inna.is"
STUDENT_HOME = f"{BASE_URL}/Components/Students/Students.html#/"
ACCESS_URL = "https://r.inna.is/adgangur"
DEFAULT_SCHOOL = "Menntaskólinn við Hamrahlíð"

DATE_SAFE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
FORBIDDEN_WINDOWS_CHARS = r'<>:"/\|?*'
MAX_FILENAME_LEN = 140

MEDIA_EXTENSIONS = {".m4v", ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
MEDIA_MIME_PREFIXES = ("video/", "audio/")


class LoginNeeded(RuntimeError):
    """Raised when an API call looks like a login/session problem."""


class ApiError(RuntimeError):
    """Raised for unexpected API failures."""


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def beep() -> None:
    """Play a warning sound. Works best on Windows."""
    try:
        if sys.platform.startswith("win"):
            import winsound

            for frequency in (880, 660, 880):
                winsound.Beep(frequency, 250)
        else:
            print("\a", end="", flush=True)
    except Exception:
        print("\a", end="", flush=True)


def log(msg: str) -> None:
    # Use side_log so Tauri sidecar runs do not crash on Windows console encodings
    # like cp1252 when Icelandic/combining characters appear.
    side_log(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}")


def sanitize_filename(value: Any, default: str = "untitled", max_len: int = MAX_FILENAME_LEN) -> str:
    s = html.unescape(str(value or default)).strip()
    s = re.sub(r"\s+", " ", s)
    s = "".join("_" if ch in FORBIDDEN_WINDOWS_CHARS else ch for ch in s)
    s = s.replace("\u00a0", " ").strip(" .")
    if not s:
        s = default
    if len(s) > max_len:
        stem, ext = os.path.splitext(s)
        if ext and len(ext) < 12:
            s = stem[: max_len - len(ext) - 1].rstrip(" .") + ext
        else:
            s = s[:max_len].rstrip(" .")
    return s or default


def dedupe_path(path: Path, overwrite: bool = False) -> Path:
    if overwrite or not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for i in range(2, 10_000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not deduplicate path: {path}")




def material_identity(file_item: Dict[str, Any]) -> str:
    """Stable identity for Efni rows so the same INNA item is only processed once.

    INNA can surface the same material through overlapping collections/groups.
    The old downloader would dutifully fetch each repeated row, which is how
    you get the cursed "same thing four times" archive confetti.
    """
    file_id = file_item.get("fileId") or file_item.get("id")
    group_id = file_item.get("groupId")
    if file_id:
        return f"file:{group_id or ''}:{file_id}"
    link = (file_item.get("link") or file_item.get("url") or file_item.get("nameLinkCombined") or "").strip()
    if link:
        return "link:" + link.lower()
    title = file_item.get("name") or file_item.get("description") or file_item.get("fileName") or file_item.get("nameLinkCombined") or ""
    date = file_item.get("displayDate") or file_item.get("display") or ""
    return "row:" + str(date).strip().lower() + ":" + str(title).strip().lower()

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def rel_to(root: Path, path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def guess_extension(content_type: Optional[str], filename: Optional[str] = None) -> str:
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/pdf":
        return ".pdf"
    if ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return ".docx"
    if ct == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        return ".pptx"
    if ct == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return ".xlsx"
    if ct == "application/msword":
        return ".doc"
    if ct == "application/vnd.ms-powerpoint":
        return ".ppt"
    if ct == "application/vnd.ms-excel":
        return ".xls"
    ext = mimetypes.guess_extension(ct or "")
    return ext or ".bin"


def parse_content_disposition_filename(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # Handles simple filename= and RFC5987 filename*= forms.
    m = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.I)
    if m:
        from urllib.parse import unquote

        return unquote(m.group(1)).strip('"')
    m = re.search(r'filename="([^"]+)"', value, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename=([^;]+)", value, flags=re.I)
    if m:
        return m.group(1).strip().strip('"')
    return None


def dt_from_epoch_ms(value: Any) -> Optional[dt.datetime]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return dt.datetime.fromtimestamp(int(value) / 1000)
    except Exception:
        return None


def parse_decimal_number(value: Any) -> Optional[float]:
    """Parse INNA numbers such as 5, 5.0, '5,5', '5 ein.' into float."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Keep the first number-looking token.
    match = re.search(r"-?\d+(?:[,.]\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except Exception:
        return None

def format_decimal_number(value: Optional[float]) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")

def is_metid_course_record(course: Dict[str, Any]) -> bool:
    status = str(course.get("status") or course.get("state") or "").lower()
    code = str(course.get("course_code") or course.get("moduleName") or course.get("code") or "").lower()
    name = str(course.get("course_name") or course.get("moduleNameLong") or course.get("moduleName2") or "").lower()
    return (
        "metið" in status
        or "metid" in status
        or "evaluated" in status
        or "(m)" in code
        or "(m)" in name
        or bool(course.get("evaluated_elsewhere"))
    )


def normalize_date_string(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit() and len(value) >= 12):
        parsed = dt_from_epoch_ms(value)
        return parsed.strftime("%Y-%m-%d %H:%M") if parsed else str(value)
    s = str(value).strip()
    # INNA often uses dd.mm.yyyy HH:MM
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    if len(s) >= 10 and re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:16].replace("T", " ")
    return s


def date_prefix(value: Any) -> str:
    s = normalize_date_string(value)
    if not s:
        return "undated"
    # yyyy-mm-dd
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    # dd.mm.yyyy
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return sanitize_filename(s[:10], "undated")


def html_to_text(value: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def absolute_inna_url(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return urljoin(BASE_URL, path_or_url)


def inna_origin_from_url(url: str) -> Optional[str]:
    m = re.match(r"^(https://nam\.inna\.is)(?:/|$)", url or "", flags=re.I)
    return m.group(1).lower() if m else None


def set_base_url(base_url: str, reason: str = "") -> None:
    global BASE_URL, STUDENT_HOME
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return
    if BASE_URL != base_url:
        BASE_URL = base_url
        STUDENT_HOME = f"{BASE_URL}/Components/Students/Students.html#/"
        log(f"Using INNA host: {BASE_URL}" + (f" ({reason})" if reason else ""))


def set_base_url_from_page(page, reason: str = "current page") -> None:
    origin = inna_origin_from_url(getattr(page, "url", ""))
    if origin:
        set_base_url(origin, reason)


async def find_working_inna_base(api: "InnaApi") -> bool:
    """Confirm the session against the MH INNA student host."""
    bases = []
    for b in [BASE_URL] + BASE_URL_CANDIDATES:
        if b not in bases:
            bases.append(b)
    for base in bases:
        old_base = BASE_URL
        set_base_url(base, "probing session")
        try:
            user = await api.get_json("/api/UserData/GetLoggedInUser", default=None)
            if user:
                set_base_url(base, "session confirmed")
                return True
        except Exception:
            pass
        finally:
            if old_base not in bases:
                set_base_url(old_base)
    return False


def html_doc(title: str, body: str, extra_head: str = "") -> str:
    return f"""<!doctype html>
<html lang="is">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{
    font-family: Arial, Helvetica, sans-serif;
    margin: 2rem;
    color: #252833;
    line-height: 1.45;
  }}
  h1, h2, h3 {{ color: #333846; }}
  .bar {{
    background: #a8bf2a;
    color: white;
    padding: 1rem 1.2rem;
    border-radius: 12px;
    margin-bottom: 1rem;
  }}
  .panel {{
    border: 1px solid #d9dde8;
    border-radius: 12px;
    padding: 1rem;
    margin: 1rem 0;
    break-inside: avoid;
  }}
  .muted {{ color: #687080; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: .6rem 0 1rem;
  }}
  th, td {{
    border-bottom: 1px solid #e4e7ef;
    text-align: left;
    vertical-align: top;
    padding: .45rem .35rem;
  }}
  th {{ color: #687080; font-size: .8rem; text-transform: uppercase; }}
  code, pre {{
    background: #f6f7f9;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
  }}
  code {{ padding: .1rem .25rem; }}
  pre {{ padding: .8rem; overflow: auto; white-space: pre-wrap; }}
  img {{ max-width: 100%; height: auto; }}
  .answer-selected {{ font-weight: bold; }}
  .answer-correct {{ color: #0b6b32; }}
  .answer-wrong {{ color: #8a1f11; }}
  .quiz-wrap {{ display: grid; gap: .75rem; }}
  .quiz-task {{ padding: .85rem 1rem; margin: .75rem 0; }}
  .task-header {{ display: flex; align-items: center; justify-content: space-between; gap: .75rem; margin-bottom: .45rem; }}
  .task-title {{ margin: 0; font-size: 1.05rem; }}
  .task-question {{ margin: .45rem 0 .65rem; font-weight: 650; }}
  .task-question p {{ margin: .25rem 0; }}
  .score-pill, .chip {{ display: inline-flex; align-items: center; border-radius: 999px; padding: .16rem .55rem; font-size: .78rem; font-weight: 800; white-space: nowrap; }}
  .score-pill.good {{ background: #dcfce7; color: #166534; }}
  .score-pill.bad {{ background: #fee2e2; color: #991b1b; }}
  .answer-list {{ display: grid; gap: .45rem; margin-top: .65rem; }}
  .answer-card {{ border: 1px solid #e4e7ef; border-radius: 12px; padding: .65rem .8rem; background: #fff; }}
  .answer-card.correct {{ border-color: #86efac; background: #f0fdf4; }}
  .answer-card.selected {{ box-shadow: inset 5px 0 0 #2563eb; }}
  .answer-card.wrong-selected {{ border-color: #fca5a5; background: #fff7f7; box-shadow: inset 5px 0 0 #dc2626; }}
  .answer-line {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .75rem; align-items: start; }}
  .answer-text {{ font-weight: 750; line-height: 1.35; }}
  .answer-extra {{ color: #475569; font-size: .92rem; margin-top: .18rem; }}
  .answer-pair {{ color: #475569; font-weight: 500; }}
  .answer-given {{ font-weight: 900; }}
  .chip-row {{ display: flex; flex-wrap: wrap; gap: .3rem; justify-content: flex-end; min-width: 9rem; }}
  .chip.selected {{ background: #dbeafe; color: #1d4ed8; }}
  .chip.correct {{ background: #dcfce7; color: #166534; }}
  .chip.wrong {{ background: #fee2e2; color: #991b1b; }}
  .answer-card.correct .answer-text::before {{ content: "✓ "; color: #16a34a; font-weight: 900; }}
  .answer-card.wrong-selected .answer-text::before {{ content: "✕ "; color: #dc2626; font-weight: 900; }}
  @media (max-width: 720px) {{
    .answer-line {{ grid-template-columns: 1fr; }}
    .chip-row {{ justify-content: flex-start; min-width: 0; }}
  }}
  .written-answer {{ border-left: 4px solid #2563eb; background: #f8fafc; padding: .8rem; border-radius: 10px; margin-top: .6rem; }}
  details.quiz-details {{ margin-top: .4rem; }}
  details.quiz-details summary {{ cursor: pointer; color: #687080; }}
  @media print {{
    body {{ margin: 1cm; }}
    a {{ color: inherit; text-decoration: none; }}
  }}
</style>
{extra_head}
</head>
<body>
{body}
</body>
</html>
"""


def metadata_table(rows: Iterable[Tuple[str, Any]]) -> str:
    cells = []
    for key, value in rows:
        if value in (None, "", [], {}):
            continue
        cells.append(f"<tr><th>{html.escape(str(key))}</th><td>{value if key.endswith('_html') else html.escape(str(value))}</td></tr>")
    if not cells:
        return ""
    return "<table><tbody>" + "\n".join(cells) + "</tbody></table>"


def render_json_pre(data: Any) -> str:
    return f"<pre>{html.escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"


def truthy_flag(value: Any) -> bool:
    return value in (True, 1, "1", "true", "True", "TRUE", "yes", "Yes", "já", "Já")


def compact_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def htmlish(value: Any) -> str:
    """Render small trusted INNA HTML snippets, otherwise escape plain text."""
    if value in (None, "", [], {}):
        return ""
    s = str(value)
    if "<" in s and ">" in s:
        return s
    return html.escape(s)


def selected_payloads(answer: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = answer.get("selected")
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def answer_display_parts(answer: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Return a compact visible answer and any extra chips/pair text.

    INNA uses different shapes for multiple choice, matching, fill-in, and text
    answers. This keeps the generated quiz HTML glanceable instead of showing
    a raw table-creature with labels floating away from the answer.
    """
    base = answer.get("taskAnswerText") or answer.get("answerText") or answer.get("text") or ""
    payloads = selected_payloads(answer)
    extras: List[str] = []
    if payloads:
        opts = []
        for payload in payloads:
            opt = payload.get("options") or payload.get("text") or payload.get("answer") or payload.get("value")
            if opt not in (None, ""):
                opts.append(str(opt))
        if opts:
            if base:
                extras.append(" → ".join(html.escape(x) for x in opts))
            else:
                base = ", ".join(opts)
    fill = answer.get("fillInStudentAnswers")
    if fill not in (None, "", [], {}):
        if isinstance(fill, list):
            fill_text = ", ".join(str(x.get("answer") or x.get("text") or x) for x in fill)
        else:
            fill_text = str(fill)
        if base:
            extras.append(fill_text)
        else:
            base = fill_text
    return htmlish(base) or "<span class='muted'>(empty answer)</span>", extras


def render_task_html(task: Dict[str, Any]) -> str:
    order = task.get("finalOrder") or task.get("originalOrder") or ""
    points = task.get("taskPoints")
    earned = task.get("taskEarnedPoints")
    try:
        good_score = float(str(earned).replace(",", ".")) >= float(str(points).replace(",", "."))
    except Exception:
        good_score = False
    score_class = "good" if good_score else "bad"
    task_html = htmlish(task.get("taskTextHtml") or task.get("taskText") or "")
    parts: List[str] = []
    parts.append('<section class="panel quiz-task">')
    parts.append('<div class="task-header">')
    parts.append(f'<h3 class="task-title">Task {html.escape(str(order))}</h3>')
    if points not in (None, "") or earned not in (None, ""):
        parts.append(f'<span class="score-pill {score_class}">{html.escape(compact_value(earned))} / {html.escape(compact_value(points))} points</span>')
    parts.append('</div>')
    if task_html:
        parts.append(f'<div class="task-question">{task_html}</div>')

    if task.get("textAnswer"):
        parts.append('<div class="written-answer"><strong>Written answer</strong>')
        parts.append(f'<div>{htmlish(task.get("textAnswer"))}</div></div>')

    answers = task.get("answers") or []
    if answers:
        parts.append('<div class="answer-list">')
        for answer in answers:
            selected = truthy_flag(answer.get("selectedAnswer")) or bool(selected_payloads(answer)) or bool(answer.get("fillInStudentAnswers"))
            correct = truthy_flag(answer.get("correctAnswer")) or any(truthy_flag(p.get("correctAnswer")) for p in selected_payloads(answer))
            card_classes = ["answer-card"]
            if correct:
                card_classes.append("correct")
            if selected:
                card_classes.append("selected")
            if selected and not correct:
                card_classes.append("wrong-selected")
            answer_html, extras = answer_display_parts(answer)
            chips: List[str] = []
            if selected:
                chips.append('<span class="chip selected">Your answer</span>')
            if correct:
                chips.append('<span class="chip correct">Correct answer</span>')
            if selected and not correct:
                chips.append('<span class="chip wrong">Wrong choice</span>')
            extra_html = "".join(f'<div class="answer-extra"><span class="answer-pair">→</span> <span class="answer-given">{extra}</span></div>' for extra in extras)
            chip_html = "".join(chips)
            class_html = " ".join(card_classes)
            parts.append(f'<div class="{class_html}"><div class="answer-line">')
            parts.append(f'<div><div class="answer-text">{answer_html}</div>{extra_html}</div>')
            parts.append(f'<div class="chip-row">{chip_html}</div>')
            parts.append('</div></div>')
        parts.append('</div>')

    meta = metadata_table([
        ("Weight", task.get("taskWeight")),
        ("Task type", task.get("taskTypeId")),
        ("Task ID", task.get("taskId")),
    ])
    if meta:
        parts.append(f'<details class="quiz-details"><summary>Details</summary>{meta}</details>')
    parts.append('</section>')
    return "\n".join(parts)


def update_download_stats(stats: Dict[str, Any], item: Dict[str, Any]) -> None:
    stats["seen"] = stats.get("seen", 0) + 1
    stats["bytes"] = stats.get("bytes", 0) + int(item.get("download_bytes_received") or 0)
    kind = str(item.get("kind") or "")
    err = str(item.get("download_error_kind") or "")
    if kind == "external_link":
        stats["links"] = stats.get("links", 0) + 1
    elif err == "skipped_media":
        stats["skipped_media"] = stats.get("skipped_media", 0) + 1
    elif item.get("download_timed_out") or err == "download_timeout":
        stats["timeouts"] = stats.get("timeouts", 0) + 1
        stats["failed"] = stats.get("failed", 0) + 1
    elif err:
        stats["failed"] = stats.get("failed", 0) + 1
    elif item.get("path"):
        if item.get("download_content_type") == "already-exists":
            stats["existing"] = stats.get("existing", 0) + 1
        else:
            stats["saved"] = stats.get("saved", 0) + 1
    else:
        stats["unknown"] = stats.get("unknown", 0) + 1


def stats_line(stats: Dict[str, Any]) -> str:
    mb = int(stats.get("bytes", 0)) / (1024 * 1024)
    extra = []
    if stats.get("duplicates"):
        extra.append(f"duplicates {stats.get('duplicates', 0)}")
    if stats.get("skipped_media"):
        extra.append(f"skipped media {stats.get('skipped_media', 0)}")
    extra_text = (" · " + " · ".join(extra)) if extra else ""
    return (
        f"saved {stats.get('saved', 0)} · already {stats.get('existing', 0)} · "
        f"links {stats.get('links', 0)} · failed {stats.get('failed', 0)} · "
        f"timeouts {stats.get('timeouts', 0)}{extra_text} · {mb:.1f} MB"
    )


def make_external_link_file(path: Path, url: str, title: str) -> None:
    safe_url = html.escape(url, quote=True)
    title_esc = html.escape(title or url)
    write_text(
        path,
        f"""<!doctype html>
<meta charset="utf-8">
<title>{title_esc}</title>
<meta http-equiv="refresh" content="0; url={safe_url}">
<p><a href="{safe_url}">{title_esc}</a></p>
""",
    )


def looks_like_media(name: Any, content_type: Any = "") -> bool:
    suffix = Path(str(name or "")).suffix.lower()
    ct = str(content_type or "").lower()
    return suffix in MEDIA_EXTENSIONS or any(ct.startswith(prefix) for prefix in MEDIA_MIME_PREFIXES)


def record_skipped_download(root: Path, category: str, title: str, url: str, reason: str, metadata: Dict[str, Any]) -> str:
    folder = root / "_download_failures" / sanitize_filename(category or "skipped_downloads")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_title = sanitize_filename(title or "download", max_len=90)
    record_path = dedupe_path(folder / f"{stamp} - {safe_title}.json", overwrite=False)
    write_json(record_path, {
        "created_at": now_iso(),
        "category": category,
        "title": title,
        "url": url,
        "reason": reason,
        "metadata": metadata,
    })
    if url:
        make_external_link_file(record_path.with_suffix(".html"), url, title or url)
    return rel_to(root, record_path) or record_path.as_posix()


def record_download_failure(root: Path, category: str, title: str, result: "DownloadResult", metadata: Dict[str, Any]) -> str:
    """Write a small rescue note for failed/time-out downloads and return its relative path."""
    if result.timed_out or "timeout" in str(category).lower():
        category = "timeouts"
    elif "http" in str(category).lower():
        category = "http_errors"
    elif not category:
        category = "download_errors"
    folder = root / "_download_failures" / sanitize_filename(category, "download_failures")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_title = sanitize_filename(title or Path(str(result.url)).name or "download", max_len=90)
    record_path = dedupe_path(folder / f"{stamp} - {safe_title}.json", overwrite=False)
    write_json(record_path, {
        "created_at": now_iso(),
        "category": category,
        "title": title,
        "url": result.url,
        "status": result.status,
        "content_type": result.content_type,
        "error_kind": result.error_kind,
        "error": result.error,
        "bytes_received": result.bytes_received,
        "timed_out": result.timed_out,
        "metadata": metadata,
    })
    # Also save a clickable original-link file beside the JSON.
    make_external_link_file(record_path.with_suffix(".html"), result.url, title or result.url)
    return rel_to(root, record_path) or record_path.as_posix()


@dataclass
class DownloadResult:
    path: Optional[Path]
    url: str
    status: int
    content_type: str = ""
    error: str = ""
    error_kind: str = ""
    bytes_received: int = 0
    timed_out: bool = False


class InnaApi:
    def __init__(self, context, delay_seconds: float = 0.05, request_timeout_ms: int = 20000, download_timeout_ms: int = 1800000, download_stall_timeout_ms: int = 5000, verbose_api: bool = False, verbose_downloads: bool = False):
        self.context = context
        self.delay_seconds = delay_seconds
        self.request_timeout_ms = request_timeout_ms
        self.download_timeout_ms = download_timeout_ms
        self.download_stall_timeout_ms = download_stall_timeout_ms
        self.verbose_api = verbose_api
        self.verbose_downloads = verbose_downloads

    async def get_json(self, path: str, default: Any = None, allow_204: bool = True) -> Any:
        url = absolute_inna_url(path)
        await asyncio.sleep(self.delay_seconds)
        response = await self.context.request.get(url, headers={"Accept": "application/json;charset=UTF-8"}, timeout=self.request_timeout_ms)
        status = response.status
        ct = response.headers.get("content-type", "")
        if status in (401, 403):
            raise LoginNeeded(f"INNA returned {status} for {path}")
        if status == 204 and allow_204:
            return default
        text = await response.text()
        if status >= 400:
            # LTI 404s and a few optional endpoints can be ignored by caller when desired.
            raise ApiError(f"HTTP {status} for {path}: {text[:200]}")
        if "text/html" in ct.lower() and ("login" in text.lower() or "accounts.google" in text.lower()):
            raise LoginNeeded(f"API returned a login page for {path}")
        if not text.strip():
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError(f"Expected JSON from {path}, got {ct}: {text[:200]}") from exc

    async def _cookie_header(self, url: str) -> str:
        try:
            cookies = await self.context.cookies([url])
        except Exception:
            cookies = await self.context.cookies()
        return "; ".join(f"{c.get('name')}={c.get('value')}" for c in cookies if c.get("name") and c.get("value") is not None)

    async def download(self, path: str, dest: Path, overwrite: bool = False) -> DownloadResult:
        """Download one INNA file with real streaming timeouts.

        Important behavior:
        - If the server does not produce the first byte within
          --download-stall-timeout-ms, the file is recorded as a timeout.
        - If a download starts but then produces no further bytes for that same
          stall window, it is also recorded as a timeout.
        - If bytes keep arriving, media files are allowed to keep downloading
          until --download-timeout-ms.
        """
        url = absolute_inna_url(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0 and not overwrite:
            return DownloadResult(path=dest, url=url, status=200, content_type="already-exists", bytes_received=dest.stat().st_size)

        await asyncio.sleep(self.delay_seconds)

        if httpx is None:
            return DownloadResult(
                path=None,
                url=url,
                status=0,
                error="Missing dependency: httpx. Run py -m pip install -r requirements.txt",
                error_kind="missing_dependency",
            )

        tmp = dest.with_suffix(dest.suffix + ".part")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

        cookie_header = await self._cookie_header(url)
        headers = {
            "Accept": "*/*",
            "Referer": STUDENT_HOME,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header

        stall_seconds = max(0.5, self.download_stall_timeout_ms / 1000)
        total_seconds = max(stall_seconds, self.download_timeout_ms / 1000)
        # Keep httpx's own read timeout slightly above our manual timeout so our
        # clearer error message wins most of the time.
        timeout = httpx.Timeout(connect=10.0, read=stall_seconds + 1.0, write=10.0, pool=10.0)
        bytes_received = 0
        status = 0
        ct = ""
        started_at = time.monotonic()
        next_progress_at = 5 * 1024 * 1024

        if self.verbose_downloads:
            log(f"    download start: {dest.name}")
            log(f"      URL: {url}")
            log(f"      first-byte/stall timeout: {stall_seconds:.1f}s; total timeout: {total_seconds:.1f}s")

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                cm = client.stream("GET", url, headers=headers)
                try:
                    response = await asyncio.wait_for(cm.__aenter__(), timeout=stall_seconds)
                except asyncio.TimeoutError:
                    return DownloadResult(
                        path=None,
                        url=url,
                        status=0,
                        content_type="",
                        error=f"No response headers within {stall_seconds:.1f} seconds",
                        error_kind="download_timeout",
                        bytes_received=0,
                        timed_out=True,
                    )

                try:
                    status = response.status_code
                    ct = response.headers.get("content-type", "")
                    if self.verbose_downloads:
                        log(f"      response: HTTP {status}; content-type={ct or 'unknown'}")

                    if status in (401, 403):
                        raise LoginNeeded(f"INNA returned {status} for download {path}")
                    if status >= 400:
                        try:
                            body = await asyncio.wait_for(response.aread(), timeout=stall_seconds)
                            text = body.decode("utf-8", errors="replace")[:1000]
                        except Exception as exc:
                            text = f"HTTP {status}; could not read error body: {exc}"
                        return DownloadResult(path=None, url=url, status=status, content_type=ct, error=text, error_kind="http_error")

                    iterator = response.aiter_bytes()
                    with tmp.open("wb") as fh:
                        while True:
                            remaining_total = total_seconds - (time.monotonic() - started_at)
                            if remaining_total <= 0:
                                return DownloadResult(
                                    path=None,
                                    url=url,
                                    status=status,
                                    content_type=ct,
                                    error=f"Download exceeded total timeout of {total_seconds:.1f} seconds",
                                    error_kind="download_timeout",
                                    bytes_received=bytes_received,
                                    timed_out=True,
                                )
                            try:
                                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=min(stall_seconds, remaining_total))
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError:
                                if bytes_received <= 0:
                                    error = f"No bytes received within {stall_seconds:.1f} seconds"
                                else:
                                    error = f"Download stalled after {bytes_received} bytes: no new bytes for {stall_seconds:.1f} seconds"
                                return DownloadResult(
                                    path=None,
                                    url=url,
                                    status=status,
                                    content_type=ct,
                                    error=error,
                                    error_kind="download_timeout",
                                    bytes_received=bytes_received,
                                    timed_out=True,
                                )
                            if not chunk:
                                continue
                            if bytes_received == 0 and self.verbose_downloads:
                                log(f"      first bytes received: {len(chunk)} bytes")
                            fh.write(chunk)
                            bytes_received += len(chunk)
                            if self.verbose_downloads and bytes_received >= next_progress_at:
                                log(f"      progress: {bytes_received / (1024 * 1024):.1f} MB")
                                next_progress_at += 5 * 1024 * 1024
                finally:
                    await cm.__aexit__(None, None, None)

            if bytes_received <= 0:
                return DownloadResult(
                    path=None,
                    url=url,
                    status=status,
                    content_type=ct,
                    error=f"Download ended without any bytes",
                    error_kind="download_timeout",
                    bytes_received=0,
                    timed_out=True,
                )

            tmp.replace(dest)
            if self.verbose_downloads:
                log(f"      download done: {bytes_received / (1024 * 1024):.1f} MB")
            return DownloadResult(path=dest, url=url, status=status, content_type=ct, bytes_received=bytes_received)

        except httpx.ReadTimeout:
            return DownloadResult(
                path=None,
                url=url,
                status=status,
                content_type=ct,
                error=f"Download stalled: no bytes received for {stall_seconds:.1f} seconds",
                error_kind="download_timeout",
                bytes_received=bytes_received,
                timed_out=True,
            )
        except httpx.RequestError as exc:
            return DownloadResult(path=None, url=url, status=status, content_type=ct, error=str(exc), error_kind="request_error", bytes_received=bytes_received)
        finally:
            # A failed or timed-out file should not leave a half-video fossil in
            # the real archive folders. The JSON/HTML rescue record is written by caller.
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


class PdfRenderer:
    def __init__(self, playwright, browser_channel: Optional[str], enabled: bool = True, timeout_ms: int = 45000):
        self.playwright = playwright
        self.browser_channel = browser_channel
        self.enabled = enabled
        self.timeout_ms = timeout_ms
        self.browser = None

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            kwargs = {"headless": True}
            if self.browser_channel:
                kwargs["channel"] = self.browser_channel
            self.browser = await self.playwright.chromium.launch(**kwargs)
        except Exception as exc:
            log(f"PDF renderer disabled: {exc}")
            self.enabled = False
            self.browser = None

    async def _render_file_once(self, html_path: Path, pdf_path: Path) -> bool:
        page = await self.browser.new_page()
        try:
            await page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=self.timeout_ms)
            await page.emulate_media(media="print")
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
            )
            return True
        finally:
            await page.close()

    async def render_file(self, html_path: Path, pdf_path: Path) -> bool:
        if not self.enabled or self.browser is None:
            return False
        try:
            return await asyncio.wait_for(
                self._render_file_once(html_path, pdf_path),
                timeout=max(5, self.timeout_ms / 1000),
            )
        except Exception as exc:
            log(f"Could not render PDF for {html_path.name}: {type(exc).__name__}: {exc}")
            return False

    async def close(self) -> None:
        if self.browser is not None:
            await self.browser.close()


async def api_is_logged_in(api: InnaApi) -> bool:
    try:
        user = await api.get_json("/api/UserData/GetLoggedInUser", default=None)
        if user:
            return True
    except Exception:
        pass
    return await find_working_inna_base(api)


async def try_click_school(page, school_name: str) -> bool:
    try:
        row = page.locator("tr", has_text=school_name).first
        if await row.count():
            link = row.locator("a").first
            if await link.count():
                await link.click(timeout=3000)
                return True
        link = page.get_by_text(school_name, exact=False).first
        if await link.count():
            await link.click(timeout=3000)
            return True
    except Exception:
        pass
    return False


async def try_click_google_login(page) -> bool:
    """Click a visible Google login button/link when INNA shows one."""
    candidates = [
        "Google",
        "Innskrá með Google",
        "Skrá inn með Google",
        "Sign in with Google",
        "Continue with Google",
    ]
    for label in candidates:
        try:
            loc = page.get_by_text(label, exact=False).first
            if await loc.count():
                await loc.click(timeout=3000)
                return True
        except Exception:
            pass
    return False


async def ensure_logged_in(page, api: InnaApi, school_name: str) -> None:
    set_base_url_from_page(page, "already-open Chrome page")
    if await api_is_logged_in(api):
        log("Already logged in.")
        return

    log("Opening INNA access page. Complete Google login if Chrome asks.")
    await page.goto(ACCESS_URL, wait_until="domcontentloaded")

    for attempt in range(180):
        set_base_url_from_page(page, "browser URL")
        if await api_is_logged_in(api):
            log("Login confirmed.")
            await page.goto(STUDENT_HOME, wait_until="domcontentloaded")
            return

        clicked = await try_click_school(page, school_name)
        if clicked:
            log(f"Clicked school selector: {school_name}")
            await page.wait_for_timeout(2500)
        else:
            google_clicked = await try_click_google_login(page)
            if google_clicked:
                log("Clicked a visible Google login option.")
                await page.wait_for_timeout(2500)

        url = page.url.lower()
        if attempt % 10 == 0:
            if "accounts.google" in url:
                beep()
                log("Google login page is open. Please log in in the Chrome window.")
            else:
                log("Waiting for INNA login/session...")

        await page.wait_for_timeout(1000)

    beep()
    raise LoginNeeded("Timed out waiting for INNA login.")


async def extract_school_profiles_once(page) -> List[Dict[str, Any]]:
    """Read profile/school rows from the current r.inna.is/adgangur page without waiting or clicking."""
    try:
        profiles = await page.evaluate(
            """() => {
                const rows = Array.from(document.querySelectorAll('tr'));
                const out = [];
                for (const row of rows) {
                    const cells = Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim()).filter(Boolean);
                    if (cells.length >= 4 && cells[0] !== 'Nafn' && !cells.join(' ').toLowerCase().includes('kennit')) {
                        const link = row.querySelector('a');
                        out.push({
                            name: cells[0] || '',
                            kennitala: cells[1] || '',
                            status: cells[2] || '',
                            school: cells[3] || '',
                            href: link ? link.href : '',
                            row_text: row.innerText.trim()
                        });
                    }
                }
                if (!out.length) {
                    const links = Array.from(document.querySelectorAll('a'));
                    for (const link of links) {
                        const text = link.innerText.trim();
                        if (text && /skóli|mennta|verzl|hamra|háskóli/i.test(text)) {
                            out.push({name: '', kennitala: '', status: '', school: text, href: link.href, row_text: text});
                        }
                    }
                }
                return out;
            }"""
        )
        seen = set()
        cleaned = []
        for prof in profiles or []:
            key = (prof.get('school') or '', prof.get('name') or '', prof.get('status') or '')
            if prof.get('school') and key not in seen:
                seen.add(key)
                cleaned.append(prof)
        return cleaned
    except Exception:
        return []


async def quick_login_status(page, api: InnaApi) -> Dict[str, Any]:
    """Fast, non-invasive login check for the GUI greenlight."""
    set_base_url_from_page(page, "status check")
    result = {
        "created_at": now_iso(),
        "logged_in": False,
        "profile_selector_ready": False,
        "profiles_count": 0,
        "profiles": [],
        "page_url": page.url,
        "base_url": BASE_URL,
        "message": "Not checked",
    }
    try:
        if await api_is_logged_in(api):
            result["logged_in"] = True
            result["message"] = "INNA session active"
            result["page_url"] = page.url
            result["base_url"] = BASE_URL
            try:
                user = await api.get_json("/api/UserData/GetLoggedInUser", default={})
                if isinstance(user, dict):
                    result["user_name"] = user.get("studentName") or user.get("name") or ""
                    result["school_short"] = user.get("schoolShort") or ""
                    result["initials"] = user.get("initials") or ""
            except Exception:
                pass
            return result
    except Exception as exc:
        result["message"] = f"API check failed: {exc}"

    try:
        await page.goto(ACCESS_URL, wait_until="domcontentloaded", timeout=8000)
        await page.wait_for_timeout(600)
        profiles = await extract_school_profiles_once(page)
        result["profiles"] = profiles
        result["profiles_count"] = len(profiles)
        result["profile_selector_ready"] = bool(profiles)
        result["page_url"] = page.url
        if profiles:
            result["message"] = "Google login/profile selector ready"
        elif "accounts.google" in page.url.lower():
            result["message"] = "Google login needed"
        else:
            result["message"] = "Not logged in or profile selector not loaded"
    except Exception as exc:
        result["message"] = f"Status check failed: {exc}"
    return result


async def scrape_school_profiles(page, wait_seconds: int = 180) -> List[Dict[str, Any]]:
    """Return the school/profile choices shown at r.inna.is/adgangur.

    The access page is the safest place to discover which INNA profiles the
    signed-in user can enter. The function waits for Google login if needed, but
    it does not store credentials and it does not pick a profile.
    """
    await page.goto(ACCESS_URL, wait_until="domcontentloaded")
    for attempt in range(wait_seconds):
        try:
            profiles = await page.evaluate(
                """() => {
                    const rows = Array.from(document.querySelectorAll('tr'));
                    const out = [];
                    for (const row of rows) {
                        const cells = Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim()).filter(Boolean);
                        if (cells.length >= 4 && cells[0] !== 'Nafn' && !cells.join(' ').toLowerCase().includes('kennit')) {
                            const link = row.querySelector('a');
                            out.push({
                                name: cells[0] || '',
                                kennitala: cells[1] || '',
                                status: cells[2] || '',
                                school: cells[3] || '',
                                href: link ? link.href : '',
                                row_text: row.innerText.trim()
                            });
                        }
                    }
                    // Fallback for pages where the rows are not literal <tr> elements.
                    if (!out.length) {
                        const links = Array.from(document.querySelectorAll('a'));
                        for (const link of links) {
                            const text = link.innerText.trim();
                            if (text && /skóli|mennta|verzl|hamra|háskóli/i.test(text)) {
                                out.push({name: '', kennitala: '', status: '', school: text, href: link.href, row_text: text});
                            }
                        }
                    }
                    return out;
                }"""
            )
            # Dedupe by school + name + status.
            seen = set()
            cleaned = []
            for prof in profiles or []:
                key = (prof.get('school') or '', prof.get('name') or '', prof.get('status') or '')
                if prof.get('school') and key not in seen:
                    seen.add(key)
                    cleaned.append(prof)
            if cleaned:
                return cleaned
        except Exception:
            pass

        url = page.url.lower()
        if 'accounts.google' in url and attempt % 10 == 0:
            beep()
            log('Google login page is open. Please log in in the Chrome window.')
        elif attempt % 10 == 0:
            log('Waiting for INNA profile list...')

        try:
            if await try_click_google_login(page):
                log('Clicked a visible Google login option.')
                await page.wait_for_timeout(2500)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

    raise LoginNeeded('Timed out waiting for INNA profile list.')


async def select_school_profile(page, api: InnaApi, school_name: str) -> None:
    """Open r.inna.is/adgangur and click the requested school/profile."""
    if not school_name:
        await ensure_logged_in(page, api, DEFAULT_SCHOOL)
        return
    log(f"Selecting INNA profile/school: {school_name}")
    await page.goto(ACCESS_URL, wait_until="domcontentloaded")
    for attempt in range(180):
        clicked = await try_click_school(page, school_name)
        if clicked:
            log(f"Clicked school selector: {school_name}")
            await page.wait_for_timeout(2500)
        set_base_url_from_page(page, "selected school profile")
        if await api_is_logged_in(api):
            log("School/profile session confirmed.")
            return
        url = page.url.lower()
        if attempt % 10 == 0:
            if "accounts.google" in url:
                beep()
                log("Google login page is open. Please log in in the Chrome window.")
            else:
                log("Waiting for school/profile session...")
        if not clicked:
            try:
                if await try_click_google_login(page):
                    log("Clicked a visible Google login option.")
                    await page.wait_for_timeout(2500)
            except Exception:
                pass
        await page.wait_for_timeout(1000)
    beep()
    raise LoginNeeded(f"Timed out selecting school/profile: {school_name}")



def course_identity(course: Dict[str, Any]) -> str:
    """Stable identity for replacing a course in archive_index.json."""
    term = str(course.get("term") or course.get("termCode") or course.get("_termCode") or "")
    group = str(course.get("group_id") or course.get("groupId") or "")
    code = str(course.get("course_code") or course.get("moduleName") or course.get("code") or "")
    if group:
        return f"{term}|{group}|{code}"
    return str(course.get("id") or f"{term}|{code}|{course.get('folder', '')}")

def load_existing_archive_index(root: Path) -> Dict[str, Any]:
    path = root / "archive_index.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            log(f"Existing archive index found: {path}")
            log(f"  existing courses: {len(data.get('courses') or [])}")
            return data
    except Exception as exc:
        log(f"WARNING: Could not read existing archive index for merge: {exc}")
    return {}


async def fetch_student_transcript(api: InnaApi) -> List[Dict[str, Any]]:
    """Fetch the student's visible study record/semester transcript.

    Older versions accidentally used a variable named `transcript` without
    fetching it first in the Tauri catalog path. This helper centralizes the
    transcript fetch and normalizes a few possible response shapes.
    """
    endpoints = [
        "/api/StudentStudyRecords/GetStudentStudyRecords",
        "/api/StudentStudyRecords/GetStudentStudyRecord",
        "/api/StudentStudyRecords/GetStudyRecords",
    ]

    last_error = None
    for endpoint in endpoints:
        try:
            data = await api.get_json(endpoint, default=None)
            if isinstance(data, list):
                log(f"Transcript: {len(data)} term rows from {endpoint}")
                return data
            if isinstance(data, dict):
                for key in ("terms", "studyRecords", "studentStudyRecords", "items", "data", "records"):
                    value = data.get(key)
                    if isinstance(value, list):
                        log(f"Transcript: {len(value)} term rows from {endpoint} key {key}")
                        return value
                # Some APIs wrap the list in data.items.
                nested = data.get("data")
                if isinstance(nested, dict):
                    for key in ("terms", "studyRecords", "studentStudyRecords", "items", "records"):
                        value = nested.get(key)
                        if isinstance(value, list):
                            log(f"Transcript: {len(value)} term rows from {endpoint} data.{key}")
                            return value
        except Exception as exc:
            last_error = exc
            log(f"Transcript endpoint failed: {endpoint}: {exc}")

    raise RuntimeError(f"Could not fetch student transcript/study records. Last error: {last_error}")


def build_catalog(transcript: List[Dict[str, Any]], include_unviewable: bool = True) -> Dict[str, Any]:
    """Small JSON summary for the GUI: terms, courses, and viewability."""
    terms: List[Dict[str, Any]] = []
    courses_flat: List[Dict[str, Any]] = []
    for term in transcript or []:
        term_code = term.get("termCode") or ""
        term_item = {
            "term_code": term_code,
            "term_name": term.get("termName") or term_code,
            "term_id": term.get("termId"),
            "total_hours": term.get("totalHours"),
            "total_finished_units": term.get("totalFinishedUnits"),
            "courses": [],
        }
        for rec in term.get("studyRecords", []) or []:
            course = {
                "term_code": term_code,
                "term_name": term_item["term_name"],
                "course_code": rec.get("moduleName") or "",
                "course_name": rec.get("moduleNameLong") or rec.get("moduleName2") or "",
                "group_id": rec.get("groupId"),
                "grade": rec.get("grade"),
                "units": rec.get("units") or rec.get("moduleUnits"),
                "status": rec.get("status"),
                "viewable": bool(rec.get("groupId")),
            }
            if include_unviewable or course["viewable"]:
                term_item["courses"].append(course)
                courses_flat.append(course)
        terms.append(term_item)
    return {"created_at": now_iso(), "terms": terms, "courses": courses_flat}


def _norm_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _collapse_repeated_letters(value: str) -> str:
    # INNA sometimes has user-facing labels like DANS2B05 while the internal
    # course code is DANS2BB05. Collapsing repeated letters makes that match,
    # without accidentally treating DANS2BK05 as the same course.
    return re.sub(r"([A-Z])\1+", r"\1", value)


def course_matches(course: Dict[str, Any], query: str) -> bool:
    if not query:
        return True
    q = _norm_code(query)
    if not q:
        return True

    code = _norm_code(course.get("moduleName", ""))
    code_collapsed = _collapse_repeated_letters(code)
    group_id = _norm_code(course.get("groupId", ""))

    # Prefer strict course-code matching. This keeps DANS2B05 from matching
    # DANS2BK05, while still letting it match DANS2BB05.
    if q in {code, code_collapsed, group_id}:
        return True
    if code.startswith(q) or code_collapsed.startswith(q):
        return True

    # Longer text search for course names. Useful for queries like "Danska 2".
    text_candidates = [
        course.get("moduleNameLong", ""),
        course.get("moduleName2", ""),
        course.get("subjectName", ""),
    ]
    text_joined = _norm_code(" ".join(str(x) for x in text_candidates))
    if len(q) >= 4 and q in text_joined:
        return True

    return False

def flatten_study_records(study_records_response: List[Dict[str, Any]], include_unviewable: bool = False) -> List[Dict[str, Any]]:
    courses: List[Dict[str, Any]] = []
    for term in study_records_response or []:
        for rec in term.get("studyRecords", []) or []:
            item = dict(rec)
            item["_termName"] = term.get("termName")
            item["_termCode"] = term.get("termCode") or rec.get("termCode")
            item["_termId"] = term.get("termId") or rec.get("termId")
            item["_termSummary"] = {
                "termName": term.get("termName"),
                "termCode": term.get("termCode"),
                "termId": term.get("termId"),
                "totalHours": term.get("totalHours"),
                "totalFinishedUnits": term.get("totalFinishedUnits"),
                "sumStatus": term.get("sumStatus"),
                "sumChoice": term.get("sumChoice"),
            }
            has_group = bool(item.get("groupId"))
            if not include_unviewable and not has_group:
                continue
            courses.append(item)
    return courses


def select_courses(courses: List[Dict[str, Any]], args) -> List[Dict[str, Any]]:
    selected = []
    for course in courses:
        if args.group_id and str(course.get("groupId")) != str(args.group_id):
            continue
        if args.term and str(course.get("_termCode") or course.get("termCode")) != str(args.term):
            continue
        if getattr(args, "terms", None):
            wanted_terms = {x.strip() for x in str(args.terms).split(",") if x.strip()}
            if wanted_terms and str(course.get("_termCode") or course.get("termCode")) not in wanted_terms:
                continue
        if args.course and not course_matches(course, args.course):
            continue
        selected.append(course)

    if args.all:
        # With --all, term filter still applies, but course/group filter can narrow if supplied.
        return selected

    if args.group_id or args.course or args.term or getattr(args, "terms", None):
        return selected

    raise SystemExit(
        "No course scope provided. Use --course DANS2B05 for the test run, "
        "--term 2023H, --group-id 638000, or --all."
    )


def course_folder_name(course: Dict[str, Any], module_info: Optional[Dict[str, Any]] = None) -> str:
    code = (module_info or {}).get("moduleName") or course.get("moduleName") or "COURSE"
    name = (module_info or {}).get("moduleName2") or course.get("moduleNameLong") or course.get("moduleName2") or ""
    return sanitize_filename(f"{code} - {name}".strip(" -"), "course")


def make_transcript_only_course_index(root: Path, course_rec: Dict[str, Any], ordinal: int = 0) -> Dict[str, Any]:
    """Create an archive_index course entry for courses with no groupId, such as Metið/(M) courses.

    These cannot be opened in INNA as a course room, but the transcript still has
    useful information: name, grade/einkunn, units/einingar, status, and term.
    """
    term = course_rec.get("_termCode") or course_rec.get("termCode") or ""
    term_name = course_rec.get("_termName") or course_rec.get("termName") or term
    code = course_rec.get("moduleName") or course_rec.get("course_code") or course_rec.get("code") or ""
    name = course_rec.get("moduleNameLong") or course_rec.get("moduleName2") or course_rec.get("course_name") or ""
    status = course_rec.get("status") or ""
    grade = course_rec.get("grade")
    units = course_rec.get("units") or course_rec.get("moduleUnits")
    evaluated = is_metid_course_record({
        "status": status,
        "course_code": code,
        "course_name": name,
        "evaluated_elsewhere": True if str(status).lower() in ("metið", "metid") else False,
    })

    folder = root / "_transcript_only" / sanitize_filename(str(term or "unknown_term"), "unknown_term")
    folder.mkdir(parents=True, exist_ok=True)
    stem = sanitize_filename(f"{code} - {name}".strip(" -"), f"transcript_course_{ordinal}")
    metadata_path = folder / f"{stem}.json"
    html_path = folder / f"{stem}.html"

    item = {
        "id": f"{term}-transcript-{code or ordinal}",
        "term": term,
        "term_name": term_name,
        "group_id": None,
        "course_code": code,
        "course_name": name,
        "teacher": "",
        "final_grade": grade,
        "status": status,
        "units": units,
        "viewable": False,
        "transcript_only": True,
        "evaluated_elsewhere": evaluated,
        "not_downloadable_reason": "This course has no INNA groupId/course room. It is kept from the transcript only, commonly because it is marked Metið/(M).",
        "folder": rel_to(root, folder),
        "overview_html": rel_to(root, html_path),
        "overview_pdf": None,
        "metadata_json": rel_to(root, metadata_path),
        "materials": [],
        "assignments": [],
        "assignment_archive_errors": 0,
        "grades": [],
        "booklist": [],
        "classmates": [],
        "raw_json_dir": None,
        "transcript_record": course_rec,
    }

    write_json(metadata_path, item)
    rows = [
        ("Course code", code),
        ("Course name", name),
        ("Term", term),
        ("Status", status),
        ("Einkunn / grade", grade),
        ("Einingar / units", units),
        ("Why no files were downloaded", item["not_downloadable_reason"]),
    ]
    write_text(
        html_path,
        html_doc(
            f"{code} - {name} (transcript only)",
            "<div class='bar'><h1>Transcript-only course</h1><p>This course is visible in the study record, but INNA does not expose a course room/groupId for it.</p></div>"
            + metadata_table(rows),
        ),
    )
    return item


def assignment_type_label(data: Dict[str, Any]) -> str:
    exam = str(data.get("exam", data.get("type", "")))
    if exam == "1" or str(data.get("type")) == "1":
        return "Próf"
    return "Verkefni"



def safe_assignment_error_folder(root: Path, n: int, aid: str, title: Any) -> Path:
    """Create a stable folder name for an assignment that failed mid-processing."""
    raw_title = str(title or aid or "unknown")
    safe_title = safe_filename(raw_title)[:70] if "safe_filename" in globals() else re.sub(r"[^A-Za-z0-9_. -]+", "_", raw_title)[:70]
    return root / f"{n:03d}_ERROR_{aid or 'no-id'}_{safe_title}"

def make_assignment_folder_name(row: Dict[str, Any], info: Dict[str, Any], student: Dict[str, Any]) -> str:
    title = info.get("name") or row.get("name") or f"assignment_{row.get('assignmentId') or row.get('id')}"
    due = info.get("returnDate") or student.get("fullReturnDate") or row.get("returnDate") or row.get("handInFullDate")
    grade = student.get("grade") or row.get("grade") or ""
    typ = assignment_type_label(info or row)
    prefix = date_prefix(due)
    suffix = f" [{typ}]"
    if grade not in ("", None):
        suffix += f" [grade {grade}]"
    return sanitize_filename(f"{prefix} - {title}{suffix}", f"assignment_{row.get('assignmentId') or row.get('id')}")


def render_assignment_html(
    title: str,
    course_label: str,
    info: Dict[str, Any],
    student: Dict[str, Any],
    duration: Any,
    project_attachments: List[Dict[str, Any]],
    submitted_attachments: List[Dict[str, Any]],
    criteria: Any,
    details: Any,
    downloaded_files: List[Dict[str, Any]],
    feedback_html: str,
) -> str:
    rows = [
        ("Course", course_label),
        ("Assignment ID", info.get("assignmentId")),
        ("Type", assignment_type_label(info)),
        ("Name", info.get("name")),
        ("Assigned", student.get("assignedFullDate") or info.get("assignedFullDate")),
        ("Due", student.get("fullReturnDate") or info.get("returnDate")),
        ("Submitted", student.get("studentHandedInFullDate") or student.get("studentHandedInDate")),
        ("Opened", student.get("studentOpenedDate")),
        ("Handed in", student.get("handedIn")),
        ("Late", student.get("lateReturn")),
        ("Grade", student.get("grade")),
        ("Weight", info.get("weight") or student.get("weight")),
        ("Grade published", student.get("gradePublished")),
    ]

    body = [f'<div class="bar"><h1>{html.escape(title)}</h1><p>{html.escape(course_label)}</p></div>']
    body.append('<div class="panel"><h2>Metadata</h2>')
    body.append(metadata_table(rows))
    body.append("</div>")

    if feedback_html:
        body.append('<div class="panel"><h2>Umsögn / skil frá kennara</h2>')
        body.append(feedback_html)
        body.append("</div>")

    if downloaded_files:
        body.append('<div class="panel"><h2>Files</h2><table><thead><tr><th>Name</th><th>Kind</th><th>Offline path</th></tr></thead><tbody>')
        for f in downloaded_files:
            label = f.get("name") or f.get("fileName") or f.get("title") or "file"
            path = f.get("path") or ""
            kind = f.get("kind") or f.get("contentType") or f.get("content_type") or ""
            href = quote(path, safe="/:#?&=%") if path else "#"
            body.append(f'<tr><td>{html.escape(label)}</td><td>{html.escape(str(kind))}</td><td><a href="{href}">{html.escape(path)}</a></td></tr>')
        body.append("</tbody></table></div>")

    tasks = student.get("tasks") or []
    if tasks:
        body.append(f'<div class="panel"><h2>Online quiz/test tasks ({len(tasks)})</h2><div class="quiz-wrap">')
        for task in tasks:
            body.append(render_task_html(task))
        body.append("</div></div>")

    # Keep all API data available in the HTML too, so the PDF is self-contained enough.
    body.append('<details class="panel"><summary><strong>Raw JSON snapshot</strong></summary>')
    body.append(render_json_pre({
        "assignment_info": info,
        "student": student,
        "duration": duration,
        "project_attachments": project_attachments,
        "submitted_attachments": submitted_attachments,
        "criteria": criteria,
        "details": details,
    }))
    body.append("</details>")

    return html_doc(title, "\n".join(body))


def render_course_overview_html(
    course_label: str,
    module_info: Dict[str, Any],
    course_rec: Dict[str, Any],
    materials: List[Dict[str, Any]],
    assignments: List[Dict[str, Any]],
    grade_rules: List[Dict[str, Any]],
    outside_grades: List[Dict[str, Any]],
    classmates: List[Dict[str, Any]],
    teaching_plan: Any,
    announcements: Any,
) -> str:
    body = [f'<div class="bar"><h1>{html.escape(course_label)}</h1><p>Offline course overview generated {html.escape(now_iso())}</p></div>']
    body.append("<div class='panel'><h2>Course info</h2>")
    body.append(metadata_table([
        ("Term", module_info.get("termCode") or course_rec.get("_termCode")),
        ("Group ID", module_info.get("groupId") or course_rec.get("groupId")),
        ("Teacher(s)", module_info.get("teachers")),
        ("Subject", module_info.get("subjectName")),
        ("Final grade from transcript", course_rec.get("grade")),
        ("Status", course_rec.get("status")),
        ("Units", course_rec.get("units")),
    ]))
    body.append("</div>")

    body.append("<div class='panel'><h2>Counts</h2>")
    body.append(metadata_table([
        ("Material groups", len(materials or [])),
        ("Material files/links", sum(len(g.get("files", []) or []) for g in materials or [])),
        ("Assignments/tests", len(assignments or [])),
        ("Grade rule entries", len(grade_rules or [])),
        ("Outside-grade entries", len(outside_grades or [])),
        ("Classmates", len(classmates or [])),
    ]))
    body.append("</div>")

    if module_info.get("booklist"):
        body.append("<div class='panel'><h2>Námsgagnalisti</h2><table><thead><tr><th>Title</th><th>Author</th><th>Publisher</th><th>Year</th></tr></thead><tbody>")
        for b in module_info.get("booklist", []) or []:
            body.append(
                "<tr>"
                f"<td>{html.escape(str(b.get('bookname') or b.get('title') or ''))}</td>"
                f"<td>{html.escape(str(b.get('author') or ''))}</td>"
                f"<td>{html.escape(str(b.get('publisher') or ''))}</td>"
                f"<td>{html.escape(str(b.get('year') or ''))}</td>"
                "</tr>"
            )
        body.append("</tbody></table></div>")

    if assignments:
        body.append("<div class='panel'><h2>Assignments and tests</h2><table><thead><tr><th>Due</th><th>Name</th><th>Type</th><th>Grade</th><th>Weight</th></tr></thead><tbody>")
        for a in assignments:
            body.append(
                "<tr>"
                f"<td>{html.escape(normalize_date_string(a.get('handInFullDate') or a.get('returnDate')))}</td>"
                f"<td>{html.escape(str(a.get('name') or ''))}</td>"
                f"<td>{html.escape('Próf' if str(a.get('exam') or a.get('type')) == '1' else 'Verkefni')}</td>"
                f"<td>{html.escape(str(a.get('grade') or ''))}</td>"
                f"<td>{html.escape(str(a.get('weight') or ''))}</td>"
                "</tr>"
            )
        body.append("</tbody></table></div>")

    if classmates:
        body.append("<div class='panel'><h2>Hópalisti</h2><table><thead><tr><th>Name</th><th>Email</th></tr></thead><tbody>")
        for p in classmates:
            email = p.get("personalEmail") or p.get("email") or ""
            body.append(
                "<tr>"
                f"<td>{html.escape(str(p.get('name') or ''))}</td>"
                f"<td>{html.escape(str(email))}</td>"
                "</tr>"
            )
        body.append("</tbody></table></div>")

    body.append('<details class="panel"><summary><strong>Raw JSON snapshot</strong></summary>')
    body.append(render_json_pre({
        "course_record": course_rec,
        "module_info": module_info,
        "teaching_plan": teaching_plan,
        "announcements": announcements,
        "grade_rules": grade_rules,
        "outside_grades": outside_grades,
    }))
    body.append("</details>")

    return html_doc(course_label, "\n".join(body))


async def download_inline_comment_images(api: InnaApi, feedback_html: str, folder: Path, root: Path, overwrite: bool) -> Tuple[str, List[Dict[str, Any]]]:
    if not feedback_html:
        return "", []

    images_dir = folder / "inline_feedback_images"
    downloaded: List[Dict[str, Any]] = []

    def repl(match):
        return match.group(0)

    # Find src attributes that point to INNA's inline comment attachment endpoint.
    pattern = re.compile(r'(?P<prefix>src=["\'])(?P<src>/api/Attachment/DownloadAttachment/(?P<id>\d+)/6\?student=0[^"\']*)(?P<suffix>["\'])')
    new_html = feedback_html

    for match in list(pattern.finditer(feedback_html)):
        src = match.group("src")
        attachment_id = match.group("id")
        dest = images_dir / f"inline_feedback_{attachment_id}.png"
        try:
            result = await api.download(src, dest, overwrite=overwrite)
            if result.path:
                rel = rel_to(root, result.path)
                downloaded.append({"kind": "inline_feedback_image", "attachmentId": attachment_id, "path": rel, "url": absolute_inna_url(src)})
                local_src = os.path.relpath(result.path, folder).replace(os.sep, "/")
                new_html = new_html.replace(src, local_src)
        except Exception as exc:
            log(f"Could not download inline feedback image {attachment_id}: {exc}")

    return new_html, downloaded


async def download_attachment(
    api: InnaApi,
    attachment: Dict[str, Any],
    attachment_type: int,
    dest_dir: Path,
    root: Path,
    kind: str,
    overwrite: bool,
    student: int = 1,
) -> Dict[str, Any]:
    name = attachment.get("fileName") or attachment.get("commentAttachmentName") or attachment.get("name") or f"attachment_{attachment.get('attachmentId') or attachment.get('commentAttachmentId')}"
    content_type = attachment.get("contentType") or attachment.get("content_type") or ""
    attachment_id = attachment.get("attachmentId") or attachment.get("commentAttachmentId")
    ext = guess_extension(content_type, name)
    safe = sanitize_filename(name)
    if not Path(safe).suffix and ext:
        safe += ext
    dest = dest_dir / safe
    path = f"/api/Attachment/DownloadAttachment/{attachment_id}/{attachment_type}?student={student}"
    result = await api.download(path, dest, overwrite=overwrite)
    item = dict(attachment)
    item.update({
        "kind": kind,
        "download_url": absolute_inna_url(path),
        "path": rel_to(root, result.path) if result.path else None,
        "download_status": result.status,
        "download_error": result.error,
        "download_error_kind": result.error_kind,
        "download_content_type": result.content_type,
        "download_bytes_received": result.bytes_received,
        "download_timed_out": result.timed_out,
    })
    if result.timed_out or result.error_kind:
        item["download_failure_record"] = record_download_failure(root, result.error_kind or "download_errors", name, result, item)
    return item


async def download_module_file(
    api: InnaApi,
    file_item: Dict[str, Any],
    dest_dir: Path,
    root: Path,
    overwrite: bool,
    skip_media: bool = False,
) -> Dict[str, Any]:
    item = dict(file_item)
    link = file_item.get("link")
    content_type = str(file_item.get("contentType") or "")
    if link or content_type == "0" or file_item.get("fileName") == "linkur":
        title = file_item.get("name") or file_item.get("description") or link or "external link"
        filename = sanitize_filename(f"{date_prefix(file_item.get('displayDate') or file_item.get('display'))} - {title}", "external_link")
        dest = dest_dir / f"{filename}.html"
        make_external_link_file(dest, link or file_item.get("nameLinkCombined") or "", title)
        item.update({"kind": "external_link", "path": rel_to(root, dest), "original_url": link or file_item.get("nameLinkCombined")})
        return item

    file_id = file_item.get("fileId")
    group_id = file_item.get("groupId")
    name = file_item.get("name") or file_item.get("nameLinkCombined") or file_item.get("description") or f"file_{file_id}"
    if not Path(str(name)).suffix:
        name += guess_extension(content_type, name)
    path = f"/api/Attachment/DownloadFile/{file_id}/{group_id}?student=1"
    download_url = absolute_inna_url(path)

    # Videos/audio can be huge or stream forever from old INNA links. By default,
    # keep a clickable rescue record instead of letting one media file block the whole archive.
    if looks_like_media(name, content_type) and skip_media:
        item.update({
            "kind": "module_file_skipped_media",
            "download_url": download_url,
            "path": None,
            "download_status": 0,
            "download_error": "Skipped media file because --skip-media was used.",
            "download_error_kind": "skipped_media",
            "download_bytes_received": 0,
            "download_timed_out": False,
        })
        item["download_failure_record"] = record_skipped_download(root, "skipped_media", name, download_url, item["download_error"], item)
        return item

    filename = sanitize_filename(f"{date_prefix(file_item.get('displayDate') or file_item.get('display'))} - {name}")
    dest = dest_dir / filename
    result = await api.download(path, dest, overwrite=overwrite)
    item.update({
        "kind": "module_file",
        "download_url": download_url,
        "path": rel_to(root, result.path) if result.path else None,
        "download_status": result.status,
        "download_error": result.error,
        "download_error_kind": result.error_kind,
        "download_content_type": result.content_type,
        "download_bytes_received": result.bytes_received,
        "download_timed_out": result.timed_out,
    })
    if result.timed_out or result.error_kind:
        item["download_failure_record"] = record_download_failure(root, result.error_kind or "download_errors", name, result, item)
    return item


def merge_assignments(group_rows: List[Dict[str, Any]], student_projects: Any) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    for row in group_rows or []:
        aid = str(row.get("assignmentId") or row.get("id") or "")
        if aid:
            merged[aid] = dict(row)

    project_list = []
    if isinstance(student_projects, dict):
        project_list = student_projects.get("assignments") or []
    elif isinstance(student_projects, list):
        project_list = student_projects

    for row in project_list or []:
        aid = str(row.get("assignmentId") or row.get("id") or "")
        if not aid:
            continue
        existing = merged.get(aid, {})
        # Prefer group-row fields where present, but add StudentProjects fields.
        combined = dict(row)
        combined.update(existing)
        if "assignmentId" not in combined and "id" in combined:
            combined["assignmentId"] = str(combined["id"])
        merged[aid] = combined

    def sort_key(item: Dict[str, Any]):
        return (
            normalize_date_string(item.get("handInFullDate") or item.get("returnDate") or item.get("assignedFullDate") or item.get("assignDate")),
            str(item.get("name") or ""),
        )

    return sorted(merged.values(), key=sort_key)


async def fetch_optional(api: InnaApi, path: str, default: Any = None) -> Any:
    if getattr(api, "verbose_api", False):
        log(f"  API GET {path}")
    try:
        result = await api.get_json(path, default=default)
        if getattr(api, "verbose_api", False):
            if isinstance(result, list):
                log(f"  API OK  {path} ({len(result)} rows)")
            elif isinstance(result, dict):
                log(f"  API OK  {path} ({len(result)} keys)")
            else:
                log(f"  API OK  {path}")
        return result
    except ApiError as exc:
        log(f"Optional endpoint failed: {path} ({exc})")
        return default
    except LoginNeeded:
        raise
    except Exception as exc:
        log(f"Optional endpoint failed: {path} ({type(exc).__name__}: {exc})")
        return default


def payload_brief(value: Any) -> str:
    """Small API payload description for debug logs."""
    if isinstance(value, list):
        return f"{len(value)} rows"
    if isinstance(value, dict):
        return f"{len(value)} keys"
    if value is None:
        return "None"
    return type(value).__name__



def compact_student_profile(user: Any, photo_rel: Optional[str] = None) -> Dict[str, Any]:
    """Keep only the useful identity fields for the archive header.

    GetLoggedInUser contains many settings. For the public-friendly archive we
    save the name/school/profile fields, not the entire raw response.
    """
    user = user if isinstance(user, dict) else {}
    return {
        "name": user.get("studentName") or user.get("name") or user.get("fullName") or "",
        "initials": user.get("initials") or "",
        "student_id_number": user.get("studentIdNumber") or "",
        "school_short": user.get("schoolShort") or "",
        "default_term_id": user.get("defaultTermId") or "",
        "log_in_type": user.get("logInType") or "",
        "photo": photo_rel,
        "captured_at": now_iso(),
    }


async def save_student_profile(api: InnaApi, root: Path, download_photo: bool = True) -> Dict[str, Any]:
    """Save the logged-in student's display name and optional profile photo."""
    profile_dir = root / "00_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    user = await fetch_optional(api, "/api/UserData/GetLoggedInUser", {})
    photo_rel = None
    if download_photo:
        try:
            photo_json = await fetch_optional(api, "/api/Photos/GetUserPhoto", {})
            if isinstance(photo_json, dict) and photo_json.get("image"):
                raw = str(photo_json.get("image") or "")
                if "," in raw and raw.lower().startswith("data:"):
                    raw = raw.split(",", 1)[1]
                image_bytes = base64.b64decode(raw, validate=False)
                ext = ".jpg"
                if image_bytes.startswith(b"\\x89PNG"):
                    ext = ".png"
                elif image_bytes.startswith(b"GIF"):
                    ext = ".gif"
                photo_path = profile_dir / ("profile_photo" + ext)
                photo_path.write_bytes(image_bytes)
                photo_rel = rel_to(root, photo_path)
        except Exception as exc:
            log(f"  profile photo: skipped ({exc})")

    profile = compact_student_profile(user, photo_rel)
    write_json(profile_dir / "student_profile.json", profile)
    log(f"Student profile: {profile.get('name') or 'unknown'} {('· ' + profile.get('school_short')) if profile.get('school_short') else ''}")
    if photo_rel:
        log(f"Student photo: {photo_rel}")
    return profile


async def process_course(
    api: InnaApi,
    playwright,
    pdf: PdfRenderer,
    root: Path,
    course_rec: Dict[str, Any],
    args,
    download_files: bool,
) -> Dict[str, Any]:
    group_id = str(course_rec.get("groupId"))
    log(f"Course {course_rec.get('moduleName')} / group {group_id}: fetching metadata")
    emit_event("course_metadata_start", course_code=course_rec.get('moduleName'), course_name=course_rec.get('moduleNameLong') or course_rec.get('moduleName2'), term=course_rec.get('_termCode') or course_rec.get('termCode'), group_id=group_id)

    log(f"  metadata: module info")
    module_info = await fetch_optional(api, f"/api/ModulesAndBooklist/GetModuleInfo?groupId={group_id}", {})
    log(f"  metadata: core course endpoints")
    course_label = f"{module_info.get('moduleName') or course_rec.get('moduleName')} - {module_info.get('moduleName2') or course_rec.get('moduleNameLong') or ''}".strip(" -")
    term = module_info.get("termCode") or course_rec.get("_termCode") or course_rec.get("termCode") or "unknown_term"
    course_dir = root / sanitize_filename(term) / course_folder_name(course_rec, module_info)
    course_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = course_dir / "_raw_json"
    keep_raw_json = not getattr(args, "skip_raw_json", False)
    include_raw_json = keep_raw_json
    materials_raw = await fetch_optional(api, f"/api/Attachment/GetModuleFiles?groupId={group_id}&isStudent=1", [])
    common_files = await fetch_optional(api, f"/api/Attachment/GetStudentGroupCommonFiles?groupId={group_id}", [])
    teaching_plan = await fetch_optional(api, f"/api/TeachingPlan/GetTeachingPlan?groupId={group_id}", None)
    external_links = await fetch_optional(api, f"/api/ExternalLinks/GetExternalLinksByGroupId/{group_id}", [])
    announcements = await fetch_optional(api, f"/api/Announcements/GetAnnouncementByGroupId?groupId={group_id}", [])
    grade_rules = await fetch_optional(api, f"/api/StudentGrades/GetStudentGradeRuleGrades?groupId={group_id}", [])
    outside_grades = await fetch_optional(api, f"/api/StudentGrades/GetStudentGradesOutsideGraderule?groupId={group_id}", [])
    if getattr(args, "skip_classmates", False):
        classmates = []
        log("  hópalisti: skipped by setting")
    else:
        classmates = await fetch_optional(api, f"/api/Groups/{group_id}/coStudents", [])
    summary = await fetch_optional(api, f"/api/Groups/{group_id}/Summary", {})
    log(f"  metadata: assignments")
    group_rows = await fetch_optional(api, f"/api/GetAssignments/GetStudentAssignmentsGroup?control=2&groupId={group_id}&order=0&type=", [])
    open_rows = await fetch_optional(api, f"/api/GetAssignments/GetStudentAssignmentsGroup?control=0&groupId={group_id}&order=0&type=", [])
    student_projects = await fetch_optional(api, f"/api/GetAssignments/Groups/{group_id}/StudentProjects", {})

    assignment_rows = merge_assignments((group_rows or []) + (open_rows or []), student_projects)
    if getattr(args, 'assignment_debug', False):
        log(f"  assignment list endpoints: closed={payload_brief(group_rows)}; open={payload_brief(open_rows)}; projects={payload_brief(student_projects)}")
    log(f"  metadata: found {len(assignment_rows)} assignments/tests")
    emit_event("course_metadata_done", course=course_label, course_code=module_info.get('moduleName') or course_rec.get('moduleName'), course_name=module_info.get('moduleName2') or course_rec.get('moduleNameLong'), term=term, group_id=group_id, assignments=len(assignment_rows), material_rows=sum(len(g.get('files', []) or []) for g in (materials_raw or [])))

    if keep_raw_json:
        write_json(raw_dir / "course_record.json", course_rec)
        write_json(raw_dir / "module_info.json", module_info)
        write_json(raw_dir / "materials_raw.json", materials_raw)
        write_json(raw_dir / "common_files.json", common_files)
        write_json(raw_dir / "teaching_plan.json", teaching_plan)
        write_json(raw_dir / "external_links.json", external_links)
        write_json(raw_dir / "announcements.json", announcements)
        write_json(raw_dir / "grade_rules.json", grade_rules)
        write_json(raw_dir / "outside_grades.json", outside_grades)
        write_json(raw_dir / "classmates.json", classmates)
        write_json(raw_dir / "summary.json", summary)
        write_json(raw_dir / "assignment_rows.json", assignment_rows)
    else:
        log("  raw JSON snapshots: skipped by privacy setting")

    # Course overview copy.
    log("  course overview: writing HTML")
    overview_html = course_dir / "course_overview.html"
    overview_pdf = course_dir / "course_overview.pdf"
    write_text(
        overview_html,
        render_course_overview_html(course_label, module_info, course_rec, materials_raw, assignment_rows, grade_rules, outside_grades, classmates, teaching_plan, announcements),
    )
    if download_files and not args.skip_pdf:
        log("  course overview: rendering PDF")
        ok = await pdf.render_file(overview_html, overview_pdf)
        log("  course overview: PDF " + ("done" if ok else "skipped"))

    # Materials / Efni
    material_index: List[Dict[str, Any]] = []
    material_root = course_dir / "01_efni"
    total_material_files = sum(len(group.get("files", []) or []) for group in (materials_raw or []))
    log(f"{course_label}: processing {total_material_files} course material rows")
    emit_event("materials_start", course=course_label, total=total_material_files)
    material_counter = 0
    seen_materials: Dict[str, Dict[str, Any]] = {}
    material_stats: Dict[str, Any] = {"seen": 0, "saved": 0, "existing": 0, "links": 0, "failed": 0, "timeouts": 0, "skipped_media": 0, "duplicates": 0, "bytes": 0}
    for group in materials_raw or []:
        group_name = group.get("fileGroup") or "Other"
        group_dir = material_root / sanitize_filename(group_name)
        for f in group.get("files", []) or []:
            material_counter += 1
            title_for_log = f.get('name') or f.get('description') or f.get('nameLinkCombined') or f.get('fileName') or 'untitled'
            log(f"  material [{material_counter}/{total_material_files}]: {title_for_log}")
            emit_event("material_current", course=course_label, current=material_counter, total=total_material_files, name=title_for_log)
            identity = material_identity(f)
            if identity in seen_materials and not args.overwrite:
                first = seen_materials[identity]
                item = dict(f)
                item.update({
                    "kind": "duplicate_reference",
                    "duplicate_of_key": identity,
                    "path": first.get("path"),
                    "original_url": first.get("original_url"),
                    "download_url": first.get("download_url"),
                    "download_content_type": "duplicate-reference",
                    "download_bytes_received": 0,
                })
                material_stats["seen"] = material_stats.get("seen", 0) + 1
                material_stats["duplicates"] = material_stats.get("duplicates", 0) + 1
                log(f"    duplicate row, reusing: {item.get('path') or item.get('original_url') or identity}")
            elif download_files:
                try:
                    item = await download_module_file(api, f, group_dir, root, overwrite=args.overwrite, skip_media=args.skip_media)
                    seen_materials[identity] = item
                    update_download_stats(material_stats, item)
                    if item.get("download_error_kind") == "skipped_media":
                        log(f"    skipped media, recorded link: {item.get('download_failure_record')}")
                    elif item.get("download_timed_out"):
                        log(f"    timeout, recorded and continuing: {item.get('download_failure_record')}")
                    elif item.get("path"):
                        action = "already had" if item.get("download_content_type") == "already-exists" else "saved"
                        log(f"    {action}: {item.get('path')}")
                except Exception as exc:
                    item = dict(f)
                    item.update({"download_error": str(exc), "download_error_kind": "exception", "path": None})
                    seen_materials[identity] = item
                    update_download_stats(material_stats, item)
                    log(f"File failed in {course_label}: {f.get('name') or f.get('description')} ({exc})")
            else:
                item = dict(f)
                item["kind"] = "external_link" if f.get("link") or str(f.get("contentType")) == "0" else "module_file"
                seen_materials[identity] = item
                update_download_stats(material_stats, item)
            item["group"] = group_name
            material_index.append(item)
            emit_event("material_progress", course=course_label, current=material_counter, total=total_material_files, stats=dict(material_stats), name=title_for_log)
            if material_counter == total_material_files or material_counter % max(1, args.progress_every) == 0:
                log(f"  efni progress [{material_counter}/{total_material_files}]: {stats_line(material_stats)}")
    emit_event("materials_done", course=course_label, current=total_material_files, total=total_material_files, stats=dict(material_stats))
    log(f"{course_label}: Efni complete: {stats_line(material_stats)}")

    log(f"{course_label}: course materials done; moving on to external links, teaching plan, then assignments")

    # ExternalLinks endpoint, separate from file link rows.
    ext_dir = material_root / "external_links"
    for i, link in enumerate(external_links or [], start=1):
        url = link.get("url") or link.get("link") or link.get("href") or link.get("URL")
        title = link.get("name") or link.get("title") or f"external_link_{i}"
        item = dict(link)
        if url and download_files:
            dest = ext_dir / f"{sanitize_filename(title)}.html"
            make_external_link_file(dest, url, title)
            item.update({"kind": "external_link", "path": rel_to(root, dest), "original_url": url})
        material_index.append(item)

    # Teaching plan / Námsáætlun attachment if visible.
    if isinstance(teaching_plan, dict) and teaching_plan.get("attachmentId"):
        # INNA uses attachment type 9 for assignment/project attachments. Teaching plan may differ,
        # so we first preserve metadata and try a few known types gently.
        plan_dir = course_dir / "03_namsaaetlun"
        plan_meta = dict(teaching_plan)
        if download_files:
            for attachment_type in (9, 3, 4):
                try:
                    plan_item = await download_attachment(
                        api,
                        {
                            "attachmentId": teaching_plan.get("attachmentId"),
                            "fileName": teaching_plan.get("name") or "namsaaetlun",
                            "contentType": teaching_plan.get("contentType"),
                        },
                        attachment_type,
                        plan_dir,
                        root,
                        "teaching_plan",
                        overwrite=args.overwrite,
                        student=1,
                    )
                    if plan_item.get("path"):
                        plan_meta.update(plan_item)
                        break
                except Exception:
                    continue
        write_json(plan_dir / "teaching_plan.json", plan_meta)
        material_index.append(plan_meta)

    log("  course extras: teaching plan / external links")

    # Booklist
    booklist = module_info.get("booklist") or []
    booklist_dir = course_dir / "04_namsgagnalisti"
    write_json(booklist_dir / "booklist.json", booklist)
    if booklist:
        book_html = html_doc(
            f"{course_label} - Námsgagnalisti",
            "<div class='bar'><h1>Námsgagnalisti</h1></div>"
            + "<table><thead><tr><th>Title</th><th>Author</th><th>Publisher</th><th>Year</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{html.escape(str(b.get('bookname') or b.get('title') or ''))}</td>"
                f"<td>{html.escape(str(b.get('author') or ''))}</td>"
                f"<td>{html.escape(str(b.get('publisher') or ''))}</td>"
                f"<td>{html.escape(str(b.get('year') or ''))}</td></tr>"
                for b in booklist
            )
            + "</tbody></table>",
        )
        write_text(booklist_dir / "booklist.html", book_html)

    log(f"  booklist: {len(booklist)} rows")

    # Grades
    log(f"  grades: {len(grade_rules or []) + len(outside_grades or [])} rows")
    emit_event("grades_start", course=course_label, total=len(grade_rules or []) + len(outside_grades or []))
    grades_dir = course_dir / "05_einkunnir"
    write_json(grades_dir / "grade_rules.json", grade_rules)
    write_json(grades_dir / "outside_grades.json", outside_grades)
    with (grades_dir / "course_grades.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "name", "grade", "weight", "recalculatedWeight", "assignmentId", "finalGrade", "gradeRule", "raw_json"])
        writer.writeheader()
        for g in grade_rules or []:
            writer.writerow({
                "source": "grade_rule",
                "name": g.get("name") or g.get("gradeRule"),
                "grade": g.get("grade"),
                "weight": g.get("weight"),
                "recalculatedWeight": g.get("recalculatedWeight"),
                "assignmentId": g.get("assignmentId"),
                "finalGrade": g.get("finalGrade"),
                "gradeRule": g.get("gradeRule"),
                "raw_json": json.dumps(g, ensure_ascii=False) if include_raw_json else "",
            })
        for g in outside_grades or []:
            writer.writerow({
                "source": "outside_grade",
                "name": g.get("name"),
                "grade": g.get("grade"),
                "weight": g.get("weight"),
                "recalculatedWeight": "",
                "assignmentId": g.get("assignmentId"),
                "finalGrade": "",
                "gradeRule": "",
                "raw_json": json.dumps(g, ensure_ascii=False) if include_raw_json else "",
            })

    # Hópalisti
    log(f"  hópalisti: {len(classmates or [])} rows")
    emit_event("classmates_start", course=course_label, total=len(classmates or []))
    classmates_dir = course_dir / "06_hopalisti"
    write_json(classmates_dir / "hopalisti.json", classmates or [])
    if classmates:
        classmates_html = html_doc(
            f"{course_label} - Hópalisti",
            "<div class='bar'><h1>Hópalisti</h1></div>"
            + "<table><thead><tr><th>Name</th><th>Email</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{html.escape(str(p.get('name') or ''))}</td>"
                f"<td>{html.escape(str(p.get('personalEmail') or p.get('email') or ''))}</td></tr>"
                for p in classmates
            )
            + "</tbody></table>",
        )
        write_text(classmates_dir / "hopalisti.html", classmates_html)

    # Assignments and tests
    assignments_index: List[Dict[str, Any]] = []
    assignment_root = course_dir / "02_verkefni_og_prof"
    max_assignments = args.max_assignments if args.max_assignments and args.max_assignments > 0 else None
    rows_to_process = assignment_rows[:max_assignments] if max_assignments else assignment_rows

    log(f"{course_label}: processing {len(rows_to_process)} assignments/tests")
    if getattr(args, 'assignment_debug', False):
        log("  assignment debug enabled: will fetch duration, project attachments, info, student response, criteria, submitted attachments, and details for each assignment")
    emit_event("assignments_start", course=course_label, total=len(rows_to_process))
    assignment_stats: Dict[str, Any] = {"seen": 0, "saved": 0, "existing": 0, "links": 0, "failed": 0, "timeouts": 0, "skipped_media": 0, "bytes": 0}
    for n, row in enumerate(rows_to_process, start=1):
        try:
            aid = str(row.get("assignmentId") or row.get("id") or "")
            if not aid:
                continue
            log(f"  [{n}/{len(rows_to_process)}] assignment {aid}: {row.get('name')}")
            emit_event("assignment_current", course=course_label, current=n, total=len(rows_to_process), assignment_id=aid, title=row.get('name'))
            duration = await fetch_optional(api, f"/api/GetAssignments/GetAssignmentsDurationById?assignmentId={aid}", {})
            project_attachments = await fetch_optional(api, f"/api/Attachment/GetProjectAttachmentsByAssignmentId?assignmentId={aid}", [])
            info = await fetch_optional(api, f"/api/GetAssignments/GetAssignmentInfo?assignmentId={aid}", {})
            student = await fetch_optional(api, f"/api/GetAssignments/GetAssignmentStudentOne?assignmentId={aid}", {})
            criteria = await fetch_optional(api, f"/api/Competence/GetAssignmentCriteria/{aid}", [])
            submitted_attachments = await fetch_optional(api, f"/api/Attachment/GetAssignmentAttachments?assignmentId={aid}", [])
            details = await fetch_optional(api, f"/api/GetAssignments/GetAssignmentDetails?assignmentId={aid}&groupId={group_id}", {})
            if getattr(args, 'assignment_debug', False):
                log(
                    "    assignment endpoints OK: "
                    f"duration={payload_brief(duration)}; "
                    f"project_attachments={payload_brief(project_attachments)}; "
                    f"info={payload_brief(info)}; "
                    f"student={payload_brief(student)}; "
                    f"criteria={payload_brief(criteria)}; "
                    f"submitted_attachments={payload_brief(submitted_attachments)}; "
                    f"details={payload_brief(details)}"
                )
                emit_event(
                    "assignment_endpoints_done",
                    course=course_label,
                    current=n,
                    total=len(rows_to_process),
                    assignment_id=aid,
                    duration=payload_brief(duration),
                    project_attachments=payload_brief(project_attachments),
                    info=payload_brief(info),
                    student=payload_brief(student),
                    criteria=payload_brief(criteria),
                    submitted_attachments=payload_brief(submitted_attachments),
                    details=payload_brief(details),
                )

            assignment_dir = assignment_root / make_assignment_folder_name(row, info, student)
            assignment_dir.mkdir(parents=True, exist_ok=True)

            downloaded_assignment_files: List[Dict[str, Any]] = []

            # Project/instruction attachments: type 9.
            project_dir = assignment_dir / "assignment_attachments"
            for attachment in project_attachments or []:
                if download_files:
                    try:
                        downloaded_assignment_files.append(
                            await download_attachment(api, attachment, 9, project_dir, root, "assignment_attachment", args.overwrite, student=1)
                        )
                    except Exception as exc:
                        item = dict(attachment)
                        item.update({"kind": "assignment_attachment", "download_error": str(exc)})
                        downloaded_assignment_files.append(item)
                        log(f"    project attachment failed: {attachment.get('fileName')} ({exc})")
                else:
                    downloaded_assignment_files.append(dict(attachment, kind="assignment_attachment"))

            # Student submitted files: type 3.
            submitted_dir = assignment_dir / "submitted_files"
            for attachment in submitted_attachments or []:
                if download_files:
                    try:
                        downloaded_assignment_files.append(
                            await download_attachment(api, attachment, 3, submitted_dir, root, "submitted_file", args.overwrite, student=1)
                        )
                    except Exception as exc:
                        item = dict(attachment)
                        item.update({"kind": "submitted_file", "download_error": str(exc)})
                        downloaded_assignment_files.append(item)
                        log(f"    submitted attachment failed: {attachment.get('fileName')} ({exc})")
                else:
                    downloaded_assignment_files.append(dict(attachment, kind="submitted_file"))

            # Teacher comment attachments: type 4.
            feedback_dir = assignment_dir / "teacher_feedback_attachments"
            for attachment in student.get("commentAttachment") or []:
                if download_files:
                    try:
                        downloaded_assignment_files.append(
                            await download_attachment(api, attachment, 4, feedback_dir, root, "teacher_feedback_attachment", args.overwrite, student=1)
                        )
                    except Exception as exc:
                        item = dict(attachment)
                        item.update({"kind": "teacher_feedback_attachment", "download_error": str(exc)})
                        downloaded_assignment_files.append(item)
                        log(f"    feedback attachment failed: {attachment.get('commentAttachmentName')} ({exc})")
                else:
                    downloaded_assignment_files.append(dict(attachment, kind="teacher_feedback_attachment"))

            feedback_html = student.get("commentByTeacher") or row.get("commentByTeacher") or ""
            if download_files and feedback_html:
                feedback_html, inline_files = await download_inline_comment_images(api, feedback_html, assignment_dir, root, args.overwrite)
                downloaded_assignment_files.extend(inline_files)

            if feedback_html:
                write_text(assignment_dir / "teacher_feedback.html", html_doc(f"{row.get('name')} - feedback", f"<div class='bar'><h1>Teacher feedback</h1></div>{feedback_html}"))
                write_text(assignment_dir / "teacher_feedback.txt", html_to_text(feedback_html))

            metadata = {
                "assignment_row": row,
                "duration": duration,
                "assignment_info": info,
                "student": student,
                "criteria": criteria,
                "grade_distribution_or_details": details,
                "project_attachments": project_attachments,
                "submitted_attachments": submitted_attachments,
                "downloaded_files": downloaded_assignment_files,
                "generated_at": now_iso(),
            }
            metadata_path = assignment_dir / "metadata.json"
            write_json(metadata_path, metadata)

            assignment_title = info.get("name") or row.get("name") or f"Assignment {aid}"
            assignment_html = assignment_dir / "assignment_page.html"
            assignment_pdf = assignment_dir / "assignment_page.pdf"
            write_text(
                assignment_html,
                render_assignment_html(
                    assignment_title,
                    course_label,
                    info,
                    student,
                    duration,
                    project_attachments,
                    submitted_attachments,
                    criteria,
                    details,
                    downloaded_assignment_files,
                    feedback_html,
                ),
            )
            if download_files and not args.skip_pdf:
                log(f"    rendering assignment PDF: {aid}")
                ok = await pdf.render_file(assignment_html, assignment_pdf)
                log(f"    assignment PDF {aid}: " + ("done" if ok else "skipped"))

            for _downloaded in downloaded_assignment_files:
                update_download_stats(assignment_stats, _downloaded)
            emit_event("assignment_progress", course=course_label, current=n, total=len(rows_to_process), assignment_id=aid, title=assignment_title, stats=dict(assignment_stats))
            if n == len(rows_to_process) or n % max(1, args.progress_every) == 0:
                log(f"  assignment progress [{n}/{len(rows_to_process)}]: {stats_line(assignment_stats)}")

            assignments_index.append({
                "assignment_id": aid,
                "title": assignment_title,
                "name": assignment_title,
                "type_label": assignment_type_label(info or row),
                "grade": student.get("grade") or row.get("grade"),
                "weight": info.get("weight") or row.get("weight"),
                "due_date": normalize_date_string(student.get("fullReturnDate") or info.get("returnDate") or row.get("returnDate")),
                "assigned_date": normalize_date_string(student.get("assignedFullDate") or row.get("assignedFullDate") or row.get("assignDate")),
                "submitted_date": normalize_date_string(student.get("studentHandedInFullDate") or student.get("studentHandedInDate")),
                "status": "Submitted" if student.get("handedIn") or row.get("handedIn") else "",
                "pdf": rel_to(root, assignment_pdf) if assignment_pdf.exists() else None,
                "html": rel_to(root, assignment_html),
                "metadata": rel_to(root, metadata_path),
                "teacher_feedback": html_to_text(feedback_html) if feedback_html else "",
                "feedback_html": feedback_html,
                "files": downloaded_assignment_files,
            })
        except LoginNeeded:
            raise
        except Exception as exc:
            traceback_text = traceback.format_exc()
            assignment_stats["failed"] = int(assignment_stats.get("failed", 0) or 0) + 1
            try:
                aid_for_error = str(row.get("assignmentId") or row.get("id") or "")
                title_for_error = row.get("name") or row.get("title") or aid_for_error or f"Assignment {n}"
            except Exception:
                aid_for_error = ""
                title_for_error = f"Assignment {n}"

            log(f"  assignment failed [{n}/{len(rows_to_process)}] {aid_for_error}: {exc}")
            emit_event(
                "assignment_error",
                course=course_label,
                current=n,
                total=len(rows_to_process),
                assignment_id=aid_for_error,
                title=title_for_error,
                error=str(exc),
            )

            try:
                error_dir = safe_assignment_error_folder(assignment_root, n, aid_for_error, title_for_error)
                error_dir.mkdir(parents=True, exist_ok=True)
                write_text(error_dir / "assignment_error.txt", f"{type(exc).__name__}: {exc}\n\n{traceback_text}")
                write_json(error_dir / "assignment_error.json", {
                    "assignment_row": row,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "traceback": traceback_text,
                    "generated_at": now_iso(),
                })
                error_html = error_dir / "assignment_error.html"
                write_text(
                    error_html,
                    html_doc(
                        f"Assignment failed - {title_for_error}",
                        "<div class='bar'><h1>Assignment could not be fully archived</h1></div>"
                        f"<p><strong>Assignment:</strong> {html.escape(str(title_for_error))}</p>"
                        f"<p><strong>Error:</strong> {html.escape(str(exc))}</p>"
                        "<p>The downloader kept the rest of the course instead of dropping it from the viewer.</p>"
                    ),
                )
                assignments_index.append({
                    "assignment_id": aid_for_error,
                    "title": title_for_error,
                    "name": title_for_error,
                    "type_label": "Archive error",
                    "grade": row.get("grade") if isinstance(row, dict) else "",
                    "weight": row.get("weight") if isinstance(row, dict) else "",
                    "due_date": normalize_date_string(row.get("returnDate")) if isinstance(row, dict) else "",
                    "assigned_date": normalize_date_string(row.get("assignedFullDate") or row.get("assignDate")) if isinstance(row, dict) else "",
                    "submitted_date": "",
                    "status": "Archive error",
                    "pdf": None,
                    "html": rel_to(root, error_html),
                    "metadata": rel_to(root, error_dir / "assignment_error.json"),
                    "teacher_feedback": "",
                    "feedback_html": "",
                    "files": [],
                    "archive_error": str(exc),
                })
            except Exception as inner_exc:
                log(f"    could not write assignment error record: {inner_exc}")
            continue

    emit_event("assignments_done", course=course_label, current=len(rows_to_process), total=len(rows_to_process), stats=dict(assignment_stats))
    log(f"{course_label}: assignments complete: {stats_line(assignment_stats)}")

    course_json_path = course_dir / "course.json"
    course_index = {
        "id": f"{term}-{group_id}-{module_info.get('moduleName') or course_rec.get('moduleName')}",
        "term": term,
        "term_name": course_rec.get("_termName"),
        "group_id": group_id,
        "course_code": module_info.get("moduleName") or course_rec.get("moduleName"),
        "course_name": module_info.get("moduleName2") or course_rec.get("moduleNameLong"),
        "teacher": module_info.get("teachers"),
        "final_grade": course_rec.get("grade"),
        "status": course_rec.get("status"),
        "units": course_rec.get("units"),
        "folder": rel_to(root, course_dir),
        "overview_html": rel_to(root, overview_html),
        "overview_pdf": rel_to(root, overview_pdf) if overview_pdf.exists() else None,
        "metadata_json": rel_to(root, course_json_path),
        "materials": material_index,
        "assignments": assignments_index,
        "assignment_archive_errors": sum(1 for a in assignments_index if a.get("archive_error")),
        "grades": (grade_rules or []) + (outside_grades or []),
        "booklist": booklist,
        "classmates": classmates or [],
        "raw_json_dir": rel_to(root, raw_dir) if keep_raw_json else None,
    }
    write_json(course_json_path, course_index)
    log(f"Wrote course index: {course_json_path}")
    return course_index




def compute_academic_summary(courses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Credits/einingar and grade averages for the whole archive."""
    total_units = 0.0
    metid_units = 0.0
    non_metid_units_with_grade = 0.0
    numeric_grades: List[float] = []
    weighted_sum = 0.0
    transcript_only_count = 0
    metid_count = 0

    for course in courses or []:
        units = parse_decimal_number(course.get("units"))
        grade = parse_decimal_number(course.get("final_grade") or course.get("grade"))
        metid = is_metid_course_record(course)

        if units is not None:
            total_units += units
            if metid:
                metid_units += units

        if course.get("transcript_only"):
            transcript_only_count += 1
        if metid:
            metid_count += 1

        # The requested averages exclude Metið/(M) courses.
        if not metid and grade is not None:
            numeric_grades.append(grade)
            if units is not None and units > 0:
                weighted_sum += grade * units
                non_metid_units_with_grade += units

    average_grade = (sum(numeric_grades) / len(numeric_grades)) if numeric_grades else None
    weighted_average_grade = (weighted_sum / non_metid_units_with_grade) if non_metid_units_with_grade else None

    return {
        "total_units": round(total_units, 2),
        "metid_units": round(metid_units, 2),
        "non_metid_units_with_numeric_grade": round(non_metid_units_with_grade, 2),
        "numeric_grade_count_non_metid": len(numeric_grades),
        "average_grade_non_metid": round(average_grade, 3) if average_grade is not None else None,
        "weighted_average_grade_non_metid": round(weighted_average_grade, 3) if weighted_average_grade is not None else None,
        "transcript_only_courses": transcript_only_count,
        "metid_courses": metid_count,
        "graduation_progress_200": round(min(total_units / 200 * 100, 100), 1) if total_units else 0,
        "graduation_progress_240": round(min(total_units / 240 * 100, 100), 1) if total_units else 0,
    }


def collect_archive_summary(root: Path, archive_index: Dict[str, Any]) -> Dict[str, Any]:
    """Human-friendly totals for the GUI/viewer and publication-minded users."""
    courses = archive_index.get("courses", []) or []
    summary: Dict[str, Any] = {
        "created_at": now_iso(),
        "courses": len(courses),
        "materials": 0,
        "assignments": 0,
        "downloaded_or_linked_files": 0,
        "grades": 0,
        "classmates": 0,
        "course_errors": len(archive_index.get("errors", []) or []),
        "download_failures": 0,
        "failure_categories": {},
        "academic": compute_academic_summary(courses),
    }
    for course in courses:
        materials = course.get("materials", []) or []
        assignments = course.get("assignments", []) or []
        summary["materials"] += len(materials)
        summary["assignments"] += len(assignments)
        summary["grades"] += len(course.get("grades", []) or [])
        summary["classmates"] += len(course.get("classmates", []) or [])
        summary["downloaded_or_linked_files"] += sum(1 for m in materials if m.get("path") or m.get("original_url") or m.get("link_file"))
        for assignment in assignments:
            summary["downloaded_or_linked_files"] += len(assignment.get("files", []) or [])
    failures_root = root / "_download_failures"
    if failures_root.exists():
        for json_file in failures_root.rglob("*.json"):
            category = json_file.parent.name
            summary["download_failures"] += 1
            summary["failure_categories"][category] = summary["failure_categories"].get(category, 0) + 1
    return summary


def write_archive_summary(root: Path, archive_index: Dict[str, Any]) -> None:
    summary = collect_archive_summary(root, archive_index)
    archive_index["summary"] = summary
    write_json(root / "archive_summary.json", summary)
    failure_rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{v}</td></tr>" for k, v in sorted(summary.get("failure_categories", {}).items())
    ) or '<tr><td colspan="2">No failed/dead-link records found.</td></tr>'
    severity = "good" if summary.get("download_failures", 0) == 0 and summary.get("course_errors", 0) == 0 else "warn"
    body = f"""
<div class="summary-hero {severity}">
  <h1>INNA Archive Summary</h1>
  <p>Created {html.escape(str(summary.get('created_at') or ''))}</p>
</div>
<div class="summary-grid">
  <div><strong>{summary.get('courses')}</strong><span>Courses</span></div>
  <div><strong>{summary.get('materials')}</strong><span>Efni rows</span></div>
  <div><strong>{summary.get('assignments')}</strong><span>Assignments / tests</span></div>
  <div><strong>{summary.get('grades')}</strong><span>Grade entries</span></div>
  <div><strong>{summary.get('downloaded_or_linked_files')}</strong><span>Files / links</span></div>
  <div><strong>{summary.get('download_failures')}</strong><span>Download issues</span></div>
  <div><strong>{summary.get('academic', {}).get('total_units', '')}</strong><span>Total einingar</span></div>
  <div><strong>{summary.get('academic', {}).get('average_grade_non_metid', '') or '—'}</strong><span>Average grade, non-M</span></div>
  <div><strong>{summary.get('academic', {}).get('weighted_average_grade_non_metid', '') or '—'}</strong><span>Weighted avg., non-M</span></div>
</div>
<section class="panel">
  <h2>Warnings and failed/dead links</h2>
  <p>Failure records, if any, are stored under <code>_download_failures</code>. These are usually dead links, timed-out videos, or files INNA no longer serves.</p>
  <table><tbody>{failure_rows}</tbody></table>
</section>
<section class="panel">
  <h2>Open the archive</h2>
  <p><a href="viewer.html">Open the interactive archive viewer</a></p>
</section>
"""
    extra = """
<style>
.summary-hero{padding:22px;border-radius:18px;color:white;background:#7f971d;margin-bottom:18px}.summary-hero.warn{background:#b45309}.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}.summary-grid div{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:16px}.summary-grid strong{display:block;font-size:28px}.summary-grid span{color:#667085}.panel{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:16px;margin-bottom:14px}table{border-collapse:collapse;width:100%}td{border-bottom:1px solid #e5e7eb;padding:8px}
</style>
"""
    write_text(root / "archive_summary.html", html_doc("INNA Archive Summary", body, extra))

def write_global_reports(root: Path, archive_index: Dict[str, Any], transcript: Any, include_raw_json: bool = True) -> None:
    write_archive_summary(root, archive_index)
    write_json(root / "archive_index.json", archive_index)
    if include_raw_json:
        write_json(root / "transcript_raw.json", transcript)
    else:
        try:
            (root / "transcript_raw.json").unlink()
        except FileNotFoundError:
            pass

    # Transcript CSV
    with (root / "transcript_courses.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["term", "term_name", "course_code", "course_name", "group_id", "status", "grade", "units", "raw_json"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for term in transcript or []:
            for rec in term.get("studyRecords", []) or []:
                writer.writerow({
                    "term": term.get("termCode") or rec.get("termCode"),
                    "term_name": term.get("termName"),
                    "course_code": rec.get("moduleName"),
                    "course_name": rec.get("moduleNameLong"),
                    "group_id": rec.get("groupId"),
                    "status": rec.get("status"),
                    "grade": rec.get("grade"),
                    "units": rec.get("units"),
                    "raw_json": json.dumps(rec, ensure_ascii=False) if include_raw_json else "",
                })

    # Global grade CSV
    with (root / "grades_all_courses.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["term", "course_code", "course_name", "group_id", "source", "name", "grade", "weight", "assignment_id", "final_grade", "raw_json"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for course in archive_index.get("courses", []):
            # final transcript row
            writer.writerow({
                "term": course.get("term"),
                "course_code": course.get("course_code"),
                "course_name": course.get("course_name"),
                "group_id": course.get("group_id"),
                "source": "transcript_final",
                "name": "Final course grade",
                "grade": course.get("final_grade"),
                "weight": "",
                "assignment_id": "",
                "final_grade": True,
                "raw_json": "",
            })
            for g in course.get("grades", []) or []:
                writer.writerow({
                    "term": course.get("term"),
                    "course_code": course.get("course_code"),
                    "course_name": course.get("course_name"),
                    "group_id": course.get("group_id"),
                    "source": "course_grade_endpoint",
                    "name": g.get("name") or g.get("gradeRule"),
                    "grade": g.get("grade"),
                    "weight": g.get("weight") or g.get("recalculatedWeight"),
                    "assignment_id": g.get("assignmentId"),
                    "final_grade": g.get("finalGrade"),
                    "raw_json": json.dumps(g, ensure_ascii=False) if include_raw_json else "",
                })
            for a in course.get("assignments", []) or []:
                if a.get("grade") not in (None, ""):
                    writer.writerow({
                        "term": course.get("term"),
                        "course_code": course.get("course_code"),
                        "course_name": course.get("course_name"),
                        "group_id": course.get("group_id"),
                        "source": "assignment",
                        "name": a.get("title") or a.get("name"),
                        "grade": a.get("grade"),
                        "weight": a.get("weight"),
                        "assignment_id": a.get("assignment_id"),
                        "final_grade": "",
                        "raw_json": "",
                    })

    # A tiny launch page for humans.
    write_text(
        root / "index.html",
        """<!doctype html>
<meta charset="utf-8">
<title>INNA Archive</title>
<meta http-equiv="refresh" content="0; url=viewer.html">
<p><a href="viewer.html">Open INNA archive viewer</a></p>
""",
    )


def copy_viewer_to_archive(root: Path) -> None:
    candidates = []

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir / "viewer.html",
            exe_dir / "resources" / "viewer.html",
            exe_dir / "resources" / "backend" / "viewer.html",
            exe_dir.parent / "resources" / "viewer.html",
            exe_dir.parent / "resources" / "backend" / "viewer.html",
        ])

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates.extend([
        base / "viewer.html",
        base / "backend" / "viewer.html",
        Path(__file__).resolve().parent / "viewer.html",
        Path(__file__).resolve().parent.parent / "viewer.html",
        Path.cwd() / "viewer.html",
        Path.cwd() / "backend" / "viewer.html",
    ])

    seen = set()
    for viewer in candidates:
        try:
            key = viewer.resolve()
        except Exception:
            key = viewer
        if key in seen:
            continue
        seen.add(key)
        if viewer.exists():
            shutil.copy2(viewer, root / "viewer.html")
            log(f"Copied viewer.html from: {viewer}")
            return

    write_text(root / "viewer.html", """<!doctype html>
<meta charset="utf-8">
<title>INNA Archive</title>
<h1>INNA Archive</h1>
<p>The full viewer.html resource was not found during export.</p>
<p>Open archive_index.json, archive_summary.html, or transcript_courses.csv directly.</p>
""")
    log("WARNING: viewer.html resource was not found; wrote minimal fallback viewer.")


async def run(args) -> None:
    set_base_url(args.base_url, "command-line default")

    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    user_data_dir = Path(args.profile_dir).expanduser()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        connected_over_cdp = bool(args.connect_cdp)

        if connected_over_cdp:
            # Google can reject sign-in in browsers launched by automation.
            # In CDP mode, the user starts real Chrome manually and signs in there;
            # this script only attaches to that already-running Chrome window.
            log(f"Connecting to already-open Chrome via CDP: {args.connect_cdp}")
            browser = await p.chromium.connect_over_cdp(args.connect_cdp)
            if not browser.contexts:
                raise RuntimeError("Connected to Chrome, but found no browser contexts.")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            launch_kwargs = dict(
                user_data_dir=str(user_data_dir),
                headless=False,
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
            )
            if args.browser_channel:
                launch_kwargs["channel"] = args.browser_channel

            log(f"Launching browser profile: {user_data_dir}")
            context = await p.chromium.launch_persistent_context(**launch_kwargs)
            page = context.pages[0] if context.pages else await context.new_page()

        api = InnaApi(context, delay_seconds=args.delay, request_timeout_ms=args.api_timeout_ms, download_timeout_ms=args.download_timeout_ms, download_stall_timeout_ms=args.download_stall_timeout_ms, verbose_api=args.verbose_api, verbose_downloads=args.verbose_downloads)

        try:
            if args.command == "status":
                status = await quick_login_status(page, api)
                if status.get("logged_in"):
                    log("Login status: OK, INNA session active.")
                elif status.get("profile_selector_ready"):
                    log(f"Login status: profile selector ready ({status.get('profiles_count', 0)} profile(s)).")
                else:
                    log("Login status: not logged in yet.")
                if args.status_json:
                    write_json(Path(args.status_json).expanduser(), status)
                    log(f"Wrote status JSON: {args.status_json}")
                return

            if args.command == "profiles":
                # Prefer reading the normal r.inna.is/adgangur profile selector, but
                # handle the one-school case where INNA skips the selector and opens
                # the active school directly. This command must be quick for the GUI,
                # so it never waits for 180 seconds.
                profiles: List[Dict[str, Any]] = []
                debug: Dict[str, Any] = {"created_at": now_iso(), "steps": []}

                try:
                    log("Opening INNA profile selector: https://r.inna.is/adgangur")
                    await page.goto(ACCESS_URL, wait_until="domcontentloaded", timeout=10000)
                    await page.wait_for_timeout(1500)
                    debug["after_access_url"] = page.url
                    try:
                        debug["title"] = await page.title()
                    except Exception:
                        debug["title"] = ""

                    profiles = await extract_school_profiles_once(page)
                    debug["steps"].append({"step": "extract_school_profiles_once", "count": len(profiles)})
                except Exception as exc:
                    msg = f"Profile selector quick read failed: {exc}"
                    log(msg)
                    debug["steps"].append({"step": "goto_or_extract_failed", "error": str(exc)})

                try:
                    debug["page_url"] = page.url
                    debug["page_title"] = await page.title()
                    debug["body_text_sample"] = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''")
                    debug["links"] = await page.evaluate("""() => Array.from(document.querySelectorAll('a')).slice(0, 80).map(a => ({
                        text: (a.innerText || a.textContent || '').trim().slice(0, 200),
                        href: a.href || ''
                    })).filter(x => x.text || x.href)""")
                    debug["table_rows"] = await page.evaluate("""() => Array.from(document.querySelectorAll('tr')).slice(0, 40).map(tr => tr.innerText.trim().replace(/\\s+/g, ' ').slice(0, 300)).filter(Boolean)""")
                except Exception as exc:
                    debug["page_debug_error"] = str(exc)

                if not profiles:
                    set_base_url_from_page(page, "profile command after access page")
                    try:
                        if await api_is_logged_in(api):
                            user = {}
                            try:
                                user = await api.get_json("/api/UserData/GetLoggedInUser", default={})
                            except Exception:
                                user = {}
                            profiles = [{
                                "school": "Current INNA session",
                                "name": user.get("studentName") or user.get("name") or "Already inside INNA",
                                "status": "Active one-school/direct session",
                                "row_text": "Current INNA session",
                                "direct": True,
                                "base_url": BASE_URL,
                            }]
                            log("INNA opened directly into one school; using Current INNA session.")
                            debug["steps"].append({"step": "direct_session_fallback", "count": 1})
                    except Exception as exc:
                        debug["steps"].append({"step": "direct_session_check_failed", "error": str(exc)})

                log("Available INNA profiles/schools:")
                for i, prof in enumerate(profiles, start=1):
                    label = prof.get("school") or prof.get("row_text") or "Unknown"
                    name = prof.get("name") or ""
                    status = prof.get("status") or ""
                    log(f"  {i}. {label} · {name} · {status}")

                if args.profiles_json:
                    write_json(Path(args.profiles_json).expanduser(), {"created_at": now_iso(), "profiles": profiles, "debug": debug})
                    log(f"Wrote profiles JSON: {args.profiles_json}")
                    try:
                        dbg_path = Path(args.profiles_json).expanduser().with_name("inna-profile-debug-latest.json")
                        write_json(dbg_path, {"created_at": now_iso(), "profiles": profiles, "debug": debug})
                        log(f"Wrote profile debug JSON: {dbg_path}")
                    except Exception:
                        pass
                return

            if args.command in ("catalog", "scan", "download", "login"):
                if args.force_school_select and args.school and not str(args.school).startswith("Current INNA session"):
                    await select_school_profile(page, api, args.school)
                else:
                    await ensure_logged_in(page, api, args.school)

                student_profile = await save_student_profile(api, out_root, download_photo=(args.command == "download"))
                transcript = await fetch_student_transcript(api)

                if args.command == "login":
                    log("Login/profile session confirmed.")
                    return

            if args.command == "catalog":
                catalog = build_catalog(transcript, include_unviewable=True)
                log("Available semesters:")
                for term in catalog.get("terms", []):
                    viewable = sum(1 for c in term.get("courses", []) if c.get("viewable"))
                    total = len(term.get("courses", []))
                    log(f"  {term.get('term_code')} · {term.get('term_name')} · {viewable}/{total} viewable courses")
                if args.catalog_json:
                    write_json(Path(args.catalog_json).expanduser(), catalog)
                    log(f"Wrote catalog JSON: {args.catalog_json}")
                return

            # Always include transcript-only/unviewable courses in the archive index.
            # They are not downloaded, but they carry useful final grade/einingar data.
            all_courses = flatten_study_records(transcript, include_unviewable=True)
            selected = select_courses(all_courses, args)

            if not selected:
                log("No courses matched the requested scope.")
                log("Try --course DANS2BB05 or --group-id 638000. The user-facing DANS2B05 label may be DANS2BB05 in the API.")
                return

            log("Selected courses:")
            for c in selected:
                no_room = "" if c.get("groupId") else " · transcript-only/no groupId"
                log(f"  {c.get('_termCode') or c.get('termCode')} · {c.get('moduleName')} · {c.get('moduleNameLong')} · group {c.get('groupId')} · grade {c.get('grade')}{no_room}")
            emit_event("run_start", total_courses=len(selected), out=str(out_root), terms=getattr(args, 'terms', ''))

            download_files = args.command == "download"
            pdf_renderer = PdfRenderer(p, args.browser_channel, enabled=(download_files and not args.skip_pdf), timeout_ms=args.pdf_timeout_ms)
            await pdf_renderer.start()

            existing_archive_index = load_existing_archive_index(out_root)
            archive_index = {
                "created_at": existing_archive_index.get("created_at") or now_iso(),
                "source": "INNA via local Playwright browser session",
                "school": args.school or existing_archive_index.get("school", ""),
                "student_profile": student_profile or existing_archive_index.get("student_profile", {}),
                "courses": list(existing_archive_index.get("courses") or []),
                "notes": [
                    "This archive was created from the student's logged-in INNA session.",
                    "It may contain classmates' names/emails if Hópalisti was visible.",
                    "Keep this folder/private USB key safe.",
                ],
                "errors": list(existing_archive_index.get("errors") or []),
            }

            errors: List[Dict[str, Any]] = []
            for course_no, course in enumerate(selected, start=1):
                emit_event("course_start", current=course_no, total=len(selected), course_code=course.get('moduleName'), course_name=course.get('moduleNameLong') or course.get('moduleName2'), term=course.get('_termCode') or course.get('termCode'), group_id=course.get('groupId'))
                try:
                    if not course.get("groupId"):
                        log(f"Course {course.get('moduleName')} has no groupId; keeping transcript-only entry.")
                        course_index = make_transcript_only_course_index(out_root, course, course_no)
                        cid = course_identity(course_index)
                        archive_index["courses"] = [c for c in (archive_index.get("courses") or []) if course_identity(c) != cid]
                        archive_index["courses"].append(course_index)
                        emit_event("transcript_only_course", current=course_no, total=len(selected), course_code=course_index.get('course_code'), course_name=course_index.get('course_name'), term=course_index.get('term'), grade=course_index.get('final_grade'), units=course_index.get('units'), status=course_index.get('status'))
                        emit_event("course_done", current=course_no, total=len(selected), course_code=course_index.get('course_code'), course_name=course_index.get('course_name'), term=course_index.get('term'), group_id=None)
                        write_global_reports(out_root, archive_index, transcript, include_raw_json=not getattr(args, "skip_raw_json", False))
                        continue

                    course_index = await process_course(api, p, pdf_renderer, out_root, course, args, download_files=download_files)
                    cid = course_identity(course_index)
                    archive_index["courses"] = [c for c in (archive_index.get("courses") or []) if course_identity(c) != cid]
                    archive_index["courses"].append(course_index)
                    emit_event("course_done", current=course_no, total=len(selected), course_code=course_index.get('course_code'), course_name=course_index.get('course_name'), term=course_index.get('term'), group_id=course_index.get('group_id'))
                    write_global_reports(out_root, archive_index, transcript, include_raw_json=not getattr(args, "skip_raw_json", False))
                except LoginNeeded:
                    raise
                except Exception as exc:
                    traceback_text = traceback.format_exc()
                    emit_event("course_error", current=course_no, total=len(selected), course_code=course.get('moduleName'), course_name=course.get('moduleNameLong') or course.get('moduleName2'), term=course.get('_termCode') or course.get('termCode'), group_id=course.get('groupId'), error=str(exc))
                    emit_event("course_failed_before_index", current=course_no, total=len(selected), course_code=course.get('moduleName'), group_id=course.get('groupId'), error=str(exc), traceback=traceback_text[-2000:])
                    log(f"Course failed: {course.get('moduleName')} group {course.get('groupId')}: {exc}")
                    error_record = {"course": course, "error": str(exc), "traceback": traceback_text, "generated_at": now_iso()}
                    errors.append(error_record)
                    archive_index.setdefault("errors", []).append(error_record)
                    write_json(out_root / "archive_errors.json", errors)

            await pdf_renderer.close()
            if errors:
                existing_errors = list(archive_index.get("errors") or [])
                for err in errors:
                    if err not in existing_errors:
                        existing_errors.append(err)
                archive_index["errors"] = existing_errors
            log(f"non-destructive archive courses total: {len(archive_index.get('courses', []) or [])}")
            write_global_reports(out_root, archive_index, transcript, include_raw_json=not getattr(args, "skip_raw_json", False))
            log(f"Wrote archive index: {out_root / 'archive_index.json'}")
            log(f"Wrote archive summary: {out_root / 'archive_summary.html'}")
            copy_viewer_to_archive(out_root)
            log(f"Wrote viewer: {out_root / 'viewer.html'}")

            log("")
            emit_event("done", archive_root=str(out_root), courses=len(archive_index.get('courses', []) or []), errors=len(errors))
            log(f"Done. Archive root: {out_root}")
            log(f"Open: {out_root / 'viewer.html'}")
            log("For best local browsing, run this in the archive folder:")
            log("  py -m http.server 8000")
            log("Then open http://localhost:8000/viewer.html")

        finally:
            # Leave Chrome open briefly so the user can see the final state.
            if args.keep_browser_open:
                log("Keeping browser open. Press Ctrl+C in this terminal when done.")
                try:
                    while True:
                        await asyncio.sleep(3600)
                except KeyboardInterrupt:
                    pass
            if not connected_over_cdp:
                await context.close()
            else:
                log("Leaving your manually opened Chrome window open.")


def default_out_dir() -> str:
    docs = Path.home() / "Documents"
    return str(docs / "INNA_Archive")


def default_profile_dir() -> str:
    return str(Path.home() / ".inna_archive_chrome_profile")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive INNA courses, files, assignments, grades, submissions, feedback, and Hópalisti.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--log-file", default="", help="Append human-readable log output to this file.")
    parser.add_argument("--events-file", default="", help="Write machine-readable JSONL progress events to this file.")
    parser.add_argument("command", choices=["status", "profiles", "catalog", "login", "scan", "download"], help="profiles lists INNA school/profile choices; catalog lists semesters/courses; scan writes metadata only; download also downloads files.")
    parser.add_argument("--out", default=default_out_dir(), help="Archive output folder. USB key paths like E:\\INNA_Archive also work.")
    parser.add_argument("--profile-dir", default=default_profile_dir(), help="Persistent Chrome profile used only for this tool.")
    parser.add_argument("--browser-channel", default="chrome", help="Playwright browser channel. Use chrome for installed Google Chrome, or empty string for bundled Chromium.")
    parser.add_argument("--connect-cdp", default="http://127.0.0.1:9222", help="Connect to an already-open real Chrome launched with --remote-debugging-port. This clean build uses real Chrome by default to avoid Google blocking automated-browser login.")
    parser.add_argument("--base-url", default="https://nam.inna.is", help="INNA student host. For this MH archive tool this should stay as https://nam.inna.is.")
    parser.add_argument("--school", default=DEFAULT_SCHOOL, help="School/profile name to click on r.inna.is/adgangur.")
    parser.add_argument("--force-school-select", action="store_true", help="Always visit r.inna.is/adgangur and click --school before fetching the transcript. Useful when the Google account has multiple INNA profiles.")
    parser.add_argument("--profiles-json", default=None, help="For the profiles command: write discovered INNA profiles/schools to this JSON file.")
    parser.add_argument("--status-json", default=None, help="For the status command: write quick login/profile status to this JSON file.")
    parser.add_argument("--catalog-json", default=None, help="For the catalog command: write terms/courses to this JSON file.")
    parser.add_argument("--course", default=None, help="Course code/name filter, e.g. DANS2B05. Fuzzy enough to match DANS2BB05.")
    parser.add_argument("--term", default=None, help="Term filter, e.g. 2023H.")
    parser.add_argument("--terms", default=None, help="Comma-separated term filters, e.g. 2023H,2024V, for the GUI semester picker.")
    parser.add_argument("--group-id", default=None, help="Exact INNA groupId, e.g. 638000.")
    parser.add_argument("--all", action="store_true", help="Archive all courses in scope. Courses marked Metið/(M) with no groupId are kept as transcript-only entries.")
    parser.add_argument("--include-unviewable", action="store_true", help="Compatibility flag. Transcript-only/no-group courses are included by default in archive indexes.")
    parser.add_argument("--max-assignments", type=int, default=0, help="Limit assignments per course for test runs. 0 means no limit.")
    parser.add_argument("--render-pdf", action="store_true", help="Also render PDF snapshots of generated HTML pages. HTML is the default archive view.")
    parser.add_argument("--skip-pdf", action="store_true", help="Deprecated compatibility flag. PDFs are skipped unless --render-pdf is used.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing downloaded files instead of skipping/deduplicating.")
    parser.add_argument("--delay", type=float, default=0.05, help="Small delay between API requests.")
    parser.add_argument("--api-timeout-ms", type=int, default=20000, help="Timeout for JSON/API calls in milliseconds. Prevents hanging forever on one sleepy endpoint.")
    parser.add_argument("--download-timeout-ms", type=int, default=1800000, help="Total timeout for one file download in milliseconds. Bytes may keep arriving for large media until this limit; dead/stalled files are caught by --download-stall-timeout-ms.")
    parser.add_argument("--download-stall-timeout-ms", type=int, default=5000, help="If a download sends no bytes for this many milliseconds, skip it and write a record under _download_failures/timeouts.")
    parser.add_argument("--skip-media", action="store_true", help="Skip video/audio files and save clickable rescue records instead of downloading them. Default is to attempt media and rely on the stall timeout.")
    parser.add_argument("--include-media", action="store_true", help="Deprecated no-op. Media is attempted by default now; use --skip-media to skip it.")
    parser.add_argument("--skip-classmates", action="store_true", help="Do not archive Hópalisti/classmate names and emails. Recommended for public/default privacy, optional for private personal archives.")
    parser.add_argument("--privacy-mode", action="store_true", help="Privacy-friendly preset: skip Hópalisti and raw JSON snapshots/CSV raw_json fields. Recommended as the public default.")
    parser.add_argument("--skip-raw-json", action="store_true", help="Do not save endpoint-level _raw_json folders or transcript_raw.json. Structured archive metadata is still written.")
    parser.add_argument("--pdf-timeout-ms", type=int, default=45000, help="Timeout for rendering one generated HTML page to PDF. If a PDF gets stuck, the script skips it and keeps archiving.")
    parser.add_argument("--progress-every", type=int, default=10, help="Print an Efni/assignment progress summary every N rows.")
    parser.add_argument("--verbose-api", action="store_true", help="Print every API endpoint as it is fetched. Useful for debugging stalls.")
    parser.add_argument("--verbose-downloads", action="store_true", help="Print file download starts, first-byte status, and large-file progress.")
    parser.add_argument("--assignment-debug", action="store_true", help="Print extra assignment endpoint summaries and emit assignment endpoint events.")
    parser.add_argument("--keep-browser-open", action="store_true", help="Keep Chrome open at the end for debugging.")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_side_log(getattr(args, 'log_file', ''))
    configure_events_file(getattr(args, 'events_file', ''))
    if getattr(args, 'log_file', ''):
        log(f"Sidecar log file: {args.log_file}")
    if getattr(args, 'events_file', ''):
        log(f"Sidecar events file: {args.events_file}")
        emit_event('sidecar_started', command=getattr(args, 'command', ''), out=getattr(args, 'out', ''))
    if args.browser_channel == "":
        args.browser_channel = None
    if getattr(args, "privacy_mode", False):
        args.skip_classmates = True
        args.skip_raw_json = True
    if not args.render_pdf:
        args.skip_pdf = True
    try:
        asyncio.run(run(args))
    except LoginNeeded as exc:
        beep()
        print(f"\nLogin/session problem: {exc}")
        print("Run the command again. Chrome should open; finish Google/INNA login there.")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
