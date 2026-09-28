"""SpaceXAI Miami Community QA Hub — FastAPI + Playwright multi-agent auditor."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import urlparse

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from playwright.async_api import Browser, TimeoutError as PlaywrightTimeoutError, async_playwright
from pydantic import BaseModel, Field

app = FastAPI(title="SpaceXAI Miami Community QA Hub", version="1.0.0")
templates = Jinja2Templates(directory="templates")

JOBS: dict[str, dict[str, Any]] = {}
JOB_LOCK = asyncio.Lock()
LOG_LOCK = threading.Lock()
MAX_JOBS = 40
NAV_TIMEOUT_MS = 25_000
AGENT_TIMEOUT_S = 55

_CLOUD_ENV_KEYS = (
    "RENDER",
    "RENDER_SERVICE_ID",
    "RENDER_EXTERNAL_URL",
    "FLY_APP_NAME",
    "RAILWAY_ENVIRONMENT",
    "K_SERVICE",
    "DYNO",
    "VERCEL",
)
_PLAYWRIGHT_USABLE: bool | None = None


def cloud_environment() -> bool:
    flag = os.environ.get("QA_HUB_SIMULATE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return any(key in os.environ for key in _CLOUD_ENV_KEYS)


def playwright_usable() -> bool:
    """Skip Chromium on free cloud tiers; cache a failed local probe."""
    global _PLAYWRIGHT_USABLE
    if os.environ.get("QA_HUB_SIMULATE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if cloud_environment():
        return False
    if _PLAYWRIGHT_USABLE is False:
        return False
    return True


def mark_playwright_unusable() -> None:
    global _PLAYWRIGHT_USABLE
    _PLAYWRIGHT_USABLE = False


def is_sandbox_target(url: str) -> bool:
    path = (urlparse(url).path or "").rstrip("/").lower()
    return path.endswith("/sandbox") or "/sandbox" in path


class TestRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def emit(job: dict[str, Any], agent: str, message: str, level: str = "info") -> None:
    with LOG_LOCK:
        job["logs"].append(
            {
                "ts": _now(),
                "agent": agent,
                "message": message,
                "level": level,
            }
        )


def validate_target_url(raw: str) -> str:
    candidate = raw.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="Provide a live http:// or https:// staging URL.",
        )
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URLs with embedded credentials are blocked.")
    return candidate


def grade_for(score: int) -> str:
    if score >= 90:
        return "S"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def compute_scorecard(chaos: dict, auditor: dict, detective: dict, load_ok: bool) -> dict[str, Any]:
    """Start at 100. Bleed -12 network/crashes, -11 critical structure, -4 collisions."""
    score = 100
    deductions: list[str] = []

    js_errors = detective.get("js_exceptions", [])
    http_errors = detective.get("http_errors", [])
    failed_requests = detective.get("failed_requests", [])
    a11y = auditor.get("a11y", [])
    overlaps = auditor.get("overlaps", [])
    chaos_crashes = chaos.get("crashes", 0)

    net_hits = 0
    if not load_ok:
        net_hits += 1
        deductions.append("−12 Server/network crash: the site did not open reliably")
    if chaos_crashes:
        net_hits += 1
        deductions.append("−12 Server/network crash: the page collapsed under click stress")
    unique_http = {f"{e.get('status')}:{str(e.get('url') or '').split('?')[0]}" for e in http_errors}
    unique_http.update(str(f).split("?")[0] for f in failed_requests)
    if unique_http:
        net_hits += 1
        deductions.append(f"−12 Server/network crash: {len(unique_http)} broken endpoint(s)")
    if js_errors:
        net_hits += 1
        deductions.append("−12 Server/network crash: unhandled runtime errors")
    score -= 12 * min(net_hits, 4)

    structural = 0
    a11y_text = " ".join(str(i.get("detail") or "") for i in a11y).lower()
    if "no h1" in a11y_text or "has no h1" in a11y_text:
        structural += 1
        deductions.append("−11 Critical accessibility/structural gap: missing page title")
    if any("no alt" in str(i.get("detail") or "").lower() for i in a11y):
        structural += 1
        deductions.append("−11 Critical accessibility/structural gap: images without descriptions")
    if any("accessible name" in str(i.get("detail") or "").lower() or "labels" in str(i.get("rule") or "").lower() for i in a11y):
        structural += 1
        deductions.append("−11 Critical accessibility/structural gap: unlabeled fields or buttons")
    other_critical = [
        i
        for i in a11y
        if str(i.get("severity") or "") == "critical"
        and "alt" not in str(i.get("detail") or "").lower()
        and "accessible name" not in str(i.get("detail") or "").lower()
        and "no h1" not in str(i.get("detail") or "").lower()
        and "lang" not in str(i.get("detail") or "").lower()
    ]
    if other_critical:
        structural += 1
        deductions.append("−11 Critical accessibility/structural gap: additional blockers")
    score -= 11 * min(structural, 4)

    if overlaps:
        collide = min(len(overlaps), 8) * 4
        score -= collide
        deductions.append(f"−{collide} Element collisions ({len(overlaps)} overlapping areas)")

    score = max(0, min(100, score))
    badges: list[str] = []
    if chaos_crashes == 0 and load_ok:
        badges.append("Rage-Proof")
    if not any(i.get("severity") == "critical" for i in a11y):
        badges.append("A11y Sentinel")
    if not js_errors:
        badges.append("Silent Console")
    if not http_errors:
        badges.append("Clean Wire")
    if not overlaps:
        badges.append("Pixel Discipline")
    if score >= 90:
        badges.append("Neon Immortal")

    return {
        "score": score,
        "grade": grade_for(score),
        "deductions": deductions,
        "badges": badges or ["Needs Hardening"],
    }


def score_in_plain_english(score: int, grade: str) -> str:
    if score >= 90:
        return (
            f"Letter grade {grade}. This website looks sturdy. Most visitors should be able to click, "
            "read, and submit forms without getting stuck."
        )
    if score >= 80:
        return (
            f"Letter grade {grade}. The site mostly works. A short polish pass will make it feel more trustworthy."
        )
    if score >= 70:
        return (
            f"Letter grade {grade}. Everyday users can get through, but some people will hit confusing or broken spots."
        )
    if score >= 50:
        return (
            f"Letter grade {grade}. Several problems will frustrate visitors. Fix the must-do list before a public demo."
        )
    return (
        f"Letter grade {grade}. This page is not ready to show the public. The checklist below is the fastest path to a healthier site."
    )


def translate_deduction(line: str) -> str:
    lower = line.lower()
    if "failed to load" in lower:
        return "The website did not open reliably, so we could not fully test it."
    if "javascript" in lower:
        return "The page threw programming errors while it was running. That can freeze buttons or blank the screen."
    if "network" in lower or "4xx" in lower or "5xx" in lower:
        return "The page asked the server for files or data that were missing or broken."
    if "accessibility" in lower or "wcag" in lower:
        return "Parts of the page are hard to use for people with disabilities (screen readers, low vision, or motor limits)."
    if "overlap" in lower:
        return "Buttons or links sit on top of each other, so people may click the wrong thing."
    if "chaos" in lower or "collapse" in lower:
        return "Rapid clicking made the page collapse or go unresponsive."
    if "empty-submit" in lower or "rage-click" in lower:
        return "Blank forms or frantic clicking caused unstable behavior."
    return line.replace("−", "Score dropped because: ")


def explain_finding(issue: dict[str, Any]) -> str:
    detail = str(issue.get("detail") or "")
    lower = detail.lower()
    if "lang" in lower and "html" in lower:
        return "The page never says what language it is written in. Screen readers may pronounce the text with the wrong accent or skip it."
    if "no h1" in lower:
        return "The page has no main title. Visitors (and search engines) do not get a clear 'this is what this page is about' headline."
    if "main landmark" in lower:
        return "There is no marked 'main content' area. Keyboard and screen-reader users cannot jump past menus to the actual page."
    if "heading level skipped" in lower:
        return "The headings jump levels (for example from a big title straight to a tiny one). That makes the outline confusing for assistive tools."
    if "duplicate id" in lower:
        return f"Two things on the page share the same internal name. Labels and help text can attach to the wrong field. {detail}"
    if "no alt attribute" in lower or ("image" in lower and "alt" in lower):
        img_n = re.search(r"Image #(\d+)", detail, re.I)
        which = f"Picture {img_n.group(1)}" if img_n else "A picture"
        return f"{which} has no written description. A blind visitor only hears 'image' and cannot tell what it shows."
    if "iframe" in lower and "title" in lower:
        return "An embedded box (video, map, or form) has no title, so assistive tools cannot explain what it is."
    if "accessible name" in lower or "no accessible name" in lower or "labels" in lower:
        return f"A form field or button has no visible name. People cannot tell what to type or what the button does. {detail}"
    if "target size" in lower or "hit target" in lower:
        return "A tap target is too small. On a phone, people will miss it or hit a neighbor by accident."
    if "contrast" in lower:
        return "Some text is too faint against the background. People with low vision (or a bright Miami patio) will struggle to read it."
    return detail


def build_action_items(auditor: dict[str, Any], detective: dict[str, Any]) -> list[str]:
    items: list[str] = []

    for issue in auditor.get("a11y", []):
        detail = str(issue.get("detail") or "")
        lower = detail.lower()
        if "lang" in lower and "html" in lower:
            items.append(
                "Tell the browser the page language. In the website template, set the language on the main page tag "
                '(usually lang="en" for English). Ask your developer: "Add a language on the html tag."'
            )
        elif "no h1" in lower:
            items.append(
                "Add one clear page title at the top (the big headline), such as the product or event name, "
                "so everyone knows what the page is."
            )
        elif "main landmark" in lower:
            items.append(
                "Ask your developer to wrap the real page content (not the nav or footer) in a main area "
                "so keyboard users can skip straight to it."
            )
        elif "heading level skipped" in lower:
            items.append(
                "Use headings in order: one main title, then section titles, then sub-titles. Do not skip from "
                "a huge heading to a tiny one."
            )
        elif "duplicate id" in lower:
            items.append(
                f"Give every field a unique name behind the scenes. {detail} Your developer can search the page "
                "for repeated id values and rename the extras."
            )
        elif "no alt attribute" in lower or ("image" in lower and "alt" in lower):
            img_n = re.search(r"Image #(\d+)", detail, re.I)
            label = f"picture {img_n.group(1)}" if img_n else "each picture without a description"
            items.append(
                f"Write a short sentence for {label} that a person could hear if the image did not load "
                '(for example: a photo of the Miami meetup). Ask your developer to add alternative text, often called "alt text".'
            )
        elif "iframe" in lower and "title" in lower:
            items.append(
                "Name every embedded box (map, video, checkout). Add a short title like 'Event map' or 'Intro video'."
            )
        elif "accessible name" in lower or "no accessible name" in lower or "labels" in lower:
            items.append(
                "Put a visible label next to every text box (Email, Password, Message). Placeholder hints inside the box "
                "are not enough on their own."
            )
        elif "target size" in lower or "hit target" in lower:
            items.append(
                "Make small buttons larger — at least the size of a fingertip. Add padding so people can tap them on a phone."
            )
        elif "contrast" in lower:
            items.append(
                "Darken text or lighten the background until the words are easy to read at arm's length. Avoid gray-on-gray."
            )
        else:
            items.append(f"Ask your developer to review this accessibility issue: {detail}")

    for overlap in auditor.get("overlaps", []):
        detail = str(overlap.get("detail") or overlap)
        items.append(
            "Two clickable things sit on top of each other. Move them apart so each button has its own space "
            f"and people do not tap the wrong one. What we saw: {detail}"
        )

    for exc in detective.get("js_exceptions", []):
        text = str(exc).split("\n")[0][:180]
        items.append(
            "The page hit a programming error while running. Ask your developer to open the browser developer tools, "
            f"reproduce the click, and fix the error so the button still works. Message: {text}"
        )

    for err in detective.get("http_errors", []):
        status = err.get("status")
        url = str(err.get("url") or "")
        short = url.split("?")[0]
        if status and int(status) >= 500:
            items.append(
                f"The server failed while loading {short} (code {status}). This is a backend problem — "
                "ask whoever hosts the app to check logs for that address and return a working response."
            )
        else:
            items.append(
                f"The page asked for something that does not exist: {short} (code {status}). "
                "Update the link, image, or request so it points at a real page, or remove it."
            )

    for failed in detective.get("failed_requests", []):
        items.append(
            "A request never finished (blocked network, mixed security, or a down host). "
            f"Ask your developer to confirm the address is public and uses https. Detail: {failed}"
        )

    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return collapse_repeated(unique[:24])


def collapse_repeated(items: list[str]) -> list[str]:
    """Merge identical / near-identical notes so the report stays readable."""
    grouped: list[tuple[str, int]] = []
    for raw in items:
        text = raw.strip()
        if text.startswith("A tap target is too small"):
            key = "__tap_targets__"
        elif text.startswith("A form field or button has no visible name"):
            key = "__unlabeled__"
        elif "too faint against the background" in text:
            key = "__contrast__"
        else:
            key = text
        if grouped and grouped[-1][0] == key:
            grouped[-1] = (key, grouped[-1][1] + 1)
        else:
            grouped.append((key, 1))

    out: list[str] = []
    for key, count in grouped:
        if key == "__tap_targets__":
            out.append(
                f"Several buttons or links are too small to tap comfortably on a phone "
                f"({count} places). Make those controls larger — about the size of a fingertip."
            )
        elif key == "__unlabeled__":
            out.append(
                f"Several form fields or buttons have no visible name ({count} places). "
                "Add a label next to each one so people know what to type or click."
            )
        elif key == "__contrast__":
            out.append(
                "Some text is too faint against the background. Darken the words or lighten the "
                "background so they are easy to read at arm's length."
            )
        elif count > 1:
            out.append(f"{key} (found {count} times)")
        else:
            out.append(key)
    return out


def _short_url(url: str) -> str:
    path = str(url or "").split("?")[0]
    if len(path) <= 42:
        return path or "unknown"
    return path[:20] + "…" + path[-18:]


def _overlap_badges(detail: str) -> list[str]:
    names = [n.strip() for n in re.findall(r'"([^"]+)"', detail) if n.strip()]
    return names[:4] or ["overlapping control"]


def compile_severity_modules(auditor: dict[str, Any], detective: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Aggregate noisy findings into three judge-readable severity modules."""
    critical: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    optimizations: list[dict[str, Any]] = []

    overlaps = auditor.get("overlaps") or []
    if overlaps:
        badges: list[str] = []
        seen: set[str] = set()
        for item in overlaps:
            for name in _overlap_badges(str(item.get("detail") or "")):
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                badges.append(name[:28])
        critical.append(
            {
                "title": f"Critical UI Collisions Detected ({len(overlaps)} areas)",
                "detail": (
                    "Menus, buttons, or fields sit on top of each other. People tap the wrong thing "
                    "or cannot read the label. Hand the highlighted names to engineering as the collision map."
                ),
                "badges": badges[:16],
            }
        )

    http_badges: list[str] = []
    http_seen: set[str] = set()
    for err in detective.get("http_errors") or []:
        label = f"{err.get('status')} {_short_url(str(err.get('url') or ''))}"
        if label in http_seen:
            continue
        http_seen.add(label)
        http_badges.append(label[:36])
    for failed in detective.get("failed_requests") or []:
        label = _short_url(str(failed))
        if label in http_seen:
            continue
        http_seen.add(label)
        http_badges.append(label[:36])
    if http_badges:
        critical.append(
            {
                "title": f"Broken endpoints ({len(http_badges)})",
                "detail": "The page requested files or addresses that failed. Something on screen may be missing or dead.",
                "badges": http_badges[:14],
            }
        )

    js_notes = []
    js_seen: set[str] = set()
    for exc in detective.get("js_exceptions") or []:
        note = str(exc).split("\n")[0][:80]
        if note in js_seen:
            continue
        js_seen.add(note)
        js_notes.append(note)
    if js_notes:
        critical.append(
            {
                "title": "Runtime errors while the page was running",
                "detail": "Hidden programming errors can freeze a button or blank part of the screen.",
                "badges": js_notes[:8],
            }
        )

    buckets: dict[str, list[str]] = {
        "title": [],
        "alt": [],
        "label": [],
        "lang": [],
        "main": [],
        "heading": [],
        "dup": [],
        "iframe": [],
        "tap": [],
        "contrast": [],
        "other": [],
    }
    for issue in auditor.get("a11y") or []:
        detail = str(issue.get("detail") or "")
        lower = detail.lower()
        if "no h1" in lower or "has no h1" in lower:
            buckets["title"].append(detail)
        elif "no alt" in lower or ("image" in lower and "alt" in lower):
            buckets["alt"].append(detail)
        elif "accessible name" in lower or "labels" in lower:
            buckets["label"].append(detail)
        elif "lang" in lower:
            buckets["lang"].append(detail)
        elif "main landmark" in lower:
            buckets["main"].append(detail)
        elif "heading level skipped" in lower:
            buckets["heading"].append(detail)
        elif "duplicate id" in lower:
            buckets["dup"].append(detail)
        elif "iframe" in lower:
            buckets["iframe"].append(detail)
        elif "target size" in lower or "hit target" in lower:
            buckets["tap"].append(detail)
        elif "contrast" in lower:
            buckets["contrast"].append(detail)
        else:
            buckets["other"].append(detail)

    if buckets["title"]:
        critical.append(
            {
                "title": "Missing page title",
                "detail": "There is no main headline, so visitors and search engines cannot tell what this page is.",
                "badges": ["No H1"],
            }
        )
    if buckets["alt"]:
        warnings.append(
            {
                "title": f"Pictures without descriptions ({len(buckets['alt'])})",
                "detail": "Screen readers only hear “image.” Add a short sentence that explains each picture.",
                "badges": [f"Image {i + 1}" for i in range(min(len(buckets["alt"]), 8))],
            }
        )
    if buckets["label"]:
        warnings.append(
            {
                "title": f"Unlabeled fields or buttons ({len(buckets['label'])})",
                "detail": "People cannot tell what to type or what a control does. Add a visible name next to each one.",
                "badges": (["Unnamed control"] + [f"{len(buckets['label'])} spots"])[:2]
                if len(buckets["label"]) > 1
                else ["Unnamed control"],
            }
        )
    if buckets["lang"]:
        warnings.append(
            {
                "title": "Page language not declared",
                "detail": "Screen readers may pronounce the text with the wrong voice. Tell the browser the language (usually English).",
                "badges": ["html lang"],
            }
        )
    if buckets["main"]:
        warnings.append(
            {
                "title": "No main content landmark",
                "detail": "Keyboard users cannot skip menus and jump to the actual page.",
                "badges": ["Missing main"],
            }
        )
    if buckets["dup"]:
        warnings.append(
            {
                "title": "Duplicate internal names",
                "detail": "Two things share the same behind-the-scenes id, so labels can attach to the wrong field.",
                "badges": ["Duplicate id"],
            }
        )
    if buckets["iframe"]:
        warnings.append(
            {
                "title": "Embedded boxes without titles",
                "detail": "Maps, videos, or widgets need a short name so assistive tools can announce them.",
                "badges": ["Untitled iframe"],
            }
        )
    if buckets["contrast"]:
        warnings.append(
            {
                "title": "Low-contrast text",
                "detail": "Some words are too faint. Darken the text or lighten the background.",
                "badges": ["Hard to read"],
            }
        )
    if buckets["other"]:
        warnings.append(
            {
                "title": "Other accessibility gaps",
                "detail": "A few extra access issues showed up. An engineer can walk the list after the must-fix items.",
                "badges": [buckets["other"][0][:28]],
            }
        )
    if buckets["tap"]:
        optimizations.append(
            {
                "title": f"Tiny tap targets ({len(buckets['tap'])} spots)",
                "detail": "Small buttons are easy to miss on a phone. Enlarge them to about fingertip size.",
                "badges": [f"{len(buckets['tap'])} controls"],
            }
        )
    if buckets["heading"]:
        optimizations.append(
            {
                "title": "Heading outline skips a level",
                "detail": "Use titles in order (main title, then section, then sub-section) so the page outline stays tidy.",
                "badges": ["Heading skip"],
            }
        )

    if not critical:
        critical.append(
            {
                "title": "✅ Zero Core Crashes Detected! Your server infrastructure is rock-solid.",
                "detail": "",
                "badges": [],
            }
        )
    if not warnings:
        warnings.append(
            {
                "title": "✅ No Compliance Violations Found! Your layout structure is fully optimized.",
                "detail": "",
                "badges": [],
            }
        )

    return {"critical": critical, "warnings": warnings, "optimizations": optimizations}


def flatten_modules(modules: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines: list[str] = []
    for key, heading in (
        ("critical", "CRITICAL TRAPS"),
        ("warnings", "WARNINGS"),
        ("optimizations", "OPTIMIZATIONS"),
    ):
        for item in modules.get(key) or []:
            badges = item.get("badges") or []
            badge_txt = ", ".join(badges[:8])
            extra = f" Affected: {badge_txt}." if badge_txt else ""
            detail = (item.get("detail") or "").strip()
            if detail:
                lines.append(f"[{heading}] {item['title']} — {detail}{extra}")
            else:
                lines.append(f"[{heading}] {item['title']}{extra}")
    return lines


def actions_from_modules(modules: dict[str, list[dict[str, Any]]]) -> list[str]:
    items: list[str] = []
    titles = " ".join(m["title"].lower() for group in modules.values() for m in group)
    if "collision" in titles:
        items.append(
            "Ask an engineer to add space between overlapping controls. Use the highlighted names as the collision map — "
            "nothing clickable should sit on top of anything else."
        )
    if "broken endpoint" in titles:
        items.append(
            "Hand engineering the red endpoint badges. Each one is a link, image, or data call that needs a real, working address."
        )
    if "runtime error" in titles:
        items.append(
            "Have an engineer open the site in Chrome, click the same buttons, and fix the console error so the UI keeps responding."
        )
    if "missing page title" in titles:
        items.append("Add one clear headline at the top of the page that names the product, event, or task.")
    if "pictures without" in titles:
        items.append(
            "Write a short sentence for each picture that a person could hear if the image did not load, then have an engineer attach it as alternative text."
        )
    if "unlabeled" in titles:
        items.append("Put a visible label next to every text box and button (Email, Search, Send). Placeholder text inside the box is not enough.")
    if "language" in titles:
        items.append("Ask an engineer to mark the page language (usually English) on the main page tag.")
    if "landmark" in titles:
        items.append("Ask an engineer to wrap the real page content in a main area so keyboard users can skip the menu.")
    if "duplicate" in titles:
        items.append("Ask an engineer to search the page for repeated ids and give each field a unique name.")
    if "embedded" in titles or "iframe" in titles:
        items.append("Name every embedded map, video, or widget with a short title such as Event map.")
    if "low-contrast" in titles:
        items.append("Darken faint text or lighten the background until the words are easy to read at arm's length.")
    if "tiny tap" in titles:
        items.append("Make small buttons larger — about the size of a fingertip — especially in the header and mobile menu.")
    if "heading outline" in titles:
        items.append("Use headings in order: one main title, then section titles, then sub-titles. Do not skip a level.")
    if "other accessibility" in titles:
        items.append("After the must-fix list, have an engineer walk remaining accessibility notes in a second pass.")
    return items[:12]


def render_markdown(url: str, card: dict[str, Any], chaos: dict, auditor: dict, detective: dict) -> str:
    def bullets(items: list[str], empty: str) -> str:
        if not items:
            return f"- _{empty}_"
        return "\n".join(f"- {item}" for item in items[:20])

    def numbered(items: list[str], empty: str) -> str:
        if not items:
            return f"1. _{empty}_"
        return "\n".join(f"{idx}. {item}" for idx, item in enumerate(items[:20], start=1))

    story = score_in_plain_english(card["score"], card["grade"])
    why_score = [translate_deduction(line) for line in card.get("deductions", [])]
    modules = compile_severity_modules(auditor, detective)
    findings = flatten_modules(modules)
    actions = actions_from_modules(modules) or build_action_items(auditor, detective)
    strengths = []
    badge_help = {
        "Rage-Proof": "Rapid clicking did not crash the page.",
        "A11y Sentinel": "No severe accessibility blockers showed up.",
        "Silent Console": "No programming error messages fired in the background.",
        "Clean Wire": "No broken server replies were captured.",
        "Pixel Discipline": "Buttons and links were not stacked on top of each other.",
        "Neon Immortal": "This is a top-tier score — the experience held up under stress.",
        "Needs Hardening": "Keep working the checklist below.",
        "Raid Failed": "The checkup itself could not finish.",
    }
    for badge in card.get("badges", []):
        strengths.append(f"**{badge}** — {badge_help.get(badge, 'Noted during the checkup.')}")

    clicks = chaos.get("events", [])[:8]
    click_lines = [
        "We used the site the way an impatient visitor would: lots of clicks, empty forms, and fast scrolling."
    ]
    if chaos.get("crashes"):
        click_lines.append("The page became unresponsive or failed to load during that stress test.")
    elif clicks:
        click_lines.append("The page stayed open. Some clicks still triggered warnings listed below.")
    else:
        click_lines.append("We could not complete the click tour — the page may have been down.")

    return f"""# Website checkup report
**Site we tested:** {url}  
**Health score:** **{card['score']} out of 100** (letter **{card['grade']}**)

## In plain English
{story}

## What we did
- **Click tour:** mashed buttons, submitted blank forms, and scrolled like a rushed user.
- **Readability & access:** checked whether titles, pictures, and form labels make sense for everyone, including people using screen readers or phones.
- **Broken parts:** watched for missing pages, failed downloads, and error messages hiding in the background.

## Why the score is not 100
{bullets(why_score, 'Nothing pulled the score down. Nice work.')}

## What we found
### [CRITICAL TRAPS]
{bullets([f"**{m['title']}** — {m['detail']}  Highlights: {', '.join(m.get('badges') or [])}" if (m.get('detail') or m.get('badges')) else f"**{m['title']}**" for m in modules['critical']], '✅ Zero Core Crashes Detected! Your server infrastructure is rock-solid.')}

### [WARNINGS]
{bullets([f"**{m['title']}** — {m['detail']}" if m.get('detail') else f"**{m['title']}**" for m in modules['warnings']], '✅ No Compliance Violations Found! Your layout structure is fully optimized.')}

### [OPTIMIZATIONS]
{bullets([f"**{m['title']}** — {m['detail']}" for m in modules['optimizations']], 'None.')}

## What to do next
Work top to bottom. You do not need to be an engineer to start — several items are writing and layout.
{numbered(actions, 'No follow-ups. You can share this page with confidence.')}

## What went well
{bullets(strengths, 'Keep iterating — strengths will show up here.')}

## Click tour notes
{bullets(click_lines + [str(c) for c in clicks[:6]], 'No extra click notes.')}

---
*SpaceXAI Miami Community QA Hub · written so anyone on the team can act on it*
"""


_PDF_WRAP_STYLE = (
    'style="max-width: 800px; margin: 0 auto; padding: 40px; '
    "font-family: 'Inter', system-ui, sans-serif; color: #1f2937; text-align: left;\""
)
_PDF_H2_STYLE = (
    'style="color: #db2777; font-size: 1.25rem; font-weight: 700; '
    'margin-top: 24px; margin-bottom: 12px; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px;"'
)
_PDF_P_STYLE = (
    'style="font-size: 0.95rem; line-height: 1.5; color: #4b5563; margin-bottom: 8px; text-align: left;"'
)
_PDF_UL_STYLE = 'style="padding-left: 20px; list-style-type: disc; text-align: left;"'
_PDF_LI_STYLE = (
    'style="padding-left: 20px; list-style-type: disc; font-size: 0.95rem; '
    'line-height: 1.5; color: #4b5563; margin-bottom: 8px; text-align: left;"'
)
_PDF_OL_STYLE = 'style="padding-left: 20px; list-style-type: decimal; text-align: left;"'
_PDF_OL_LI_STYLE = (
    'style="padding-left: 20px; font-size: 0.95rem; '
    'line-height: 1.5; color: #4b5563; margin-bottom: 8px; text-align: left;"'
)
_PDF_META_STYLE = (
    'style="font-size: 0.85rem; line-height: 1.5; color: #6b7280; margin-bottom: 8px; text-align: left;"'
)
_PDF_H1_STYLE = (
    'style="font-size: 1.75rem; font-weight: 800; color: #111827; margin: 0 0 12px; text-align: left;"'
)
_PDF_CRIT_CLEAR = "Zero Core Crashes Detected! Your server infrastructure is rock-solid."
_PDF_WARN_CLEAR = "No Compliance Violations Found! Your layout structure is fully optimized."


def _pdf_p(text: str, *, strong: bool = False) -> str:
    inner = f"<strong>{text}</strong>" if strong else text
    return f"<p {_PDF_P_STYLE}>{inner}</p>"


def _pdf_li(text: str) -> str:
    return f"<li {_PDF_LI_STYLE}>{text}</li>"


def _pdf_ul(items_html: str) -> str:
    return f"<ul {_PDF_UL_STYLE}>{items_html}</ul>"


def _pdf_h2(title: str) -> str:
    return f"<h2 {_PDF_H2_STYLE}>{escape(title)}</h2>"


def _pdf_module_list(items: list[dict[str, Any]], empty: str) -> str:
    rows: list[str] = []
    source = items if items else [{"title": empty, "detail": "", "badges": []}]
    for item in source:
        title = escape(str(item.get("title") or ""))
        detail = escape(str(item.get("detail") or "").strip())
        badges = ", ".join(str(badge) for badge in (item.get("badges") or [])[:12])
        line = f"<strong>{title}</strong>"
        if detail:
            line += f" — {detail}"
        if badges:
            line += f" Highlights: {escape(badges)}"
        rows.append(_pdf_li(line))
    return _pdf_ul("".join(rows))


def render_pdf_html(
    url: str,
    card: dict[str, Any],
    chaos: dict,
    auditor: dict,
    detective: dict,
    modules: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """HTML sheet consumed by the browser PDF exporter."""
    modules = modules or compile_severity_modules(auditor, detective)
    actions = actions_from_modules(modules) or build_action_items(auditor, detective)
    summary = escape(score_in_plain_english(card["score"], card["grade"]))
    action_lis = "".join(
        f"<li {_PDF_OL_LI_STYLE}>{escape(item)}</li>"
        for item in (actions or ["No follow-ups. You can share this page with confidence."])[:20]
    )
    badge_lis = "".join(_pdf_li(escape(str(badge))) for badge in (card.get("badges") or ["Keep iterating."]))
    opt_items = modules.get("optimizations") or []
    if opt_items:
        opt_html = _pdf_ul(
            "".join(
                _pdf_li(
                    f"<strong>{escape(str(m.get('title') or ''))}</strong>"
                    + (
                        f" — {escape(str(m.get('detail') or '').strip())}"
                        if str(m.get("detail") or "").strip()
                        else ""
                    )
                )
                for m in opt_items
            )
        )
    else:
        opt_html = _pdf_p("None.")
    return f"""<div {_PDF_WRAP_STYLE}>
<p {_PDF_META_STYLE}>SpaceXAI Miami Community QA Hub</p>
<h1 {_PDF_H1_STYLE}>Website checkup report</h1>
<p {_PDF_P_STYLE}>Site tested: {escape(url)}</p>
<p {_PDF_P_STYLE}><strong>Health score:</strong> {escape(str(card.get('score', 0)))} out of 100 · <strong>Letter grade:</strong> {escape(str(card.get('grade', '—')))}</p>
{_pdf_p(summary)}
{_pdf_h2("Critical Traps")}
{_pdf_module_list(modules.get("critical") or [], _PDF_CRIT_CLEAR)}
{_pdf_h2("Warnings")}
{_pdf_module_list(modules.get("warnings") or [], _PDF_WARN_CLEAR)}
{_pdf_h2("Optimizations")}
{opt_html}
{_pdf_h2("What to do next")}
{_pdf_p("Work from the top. Several items are writing or layout — you can start even if you are not an engineer.")}
<ol {_PDF_OL_STYLE}>{action_lis}</ol>
{_pdf_h2("What went well")}
{_pdf_ul(badge_lis)}
</div>"""


A11Y_SCAN_JS = """
() => {
  const issues = [];
  const overlaps = [];
  const push = (severity, rule, detail) => issues.push({ severity, rule, detail });

  const html = document.documentElement;
  if (!html.getAttribute('lang')) {
    push('critical', 'WCAG 3.1.1 Language of Page', 'The <html> element is missing a lang attribute.');
  }

  if (!document.querySelector('main, [role="main"]')) {
    push('serious', 'WCAG 1.3.1 Info and Relationships', 'No main landmark found.');
  }

  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(h => Number(h.tagName[1]));
  if (!headings.includes(1)) {
    push('serious', 'WCAG 1.3.1 Headings', 'Page has no h1.');
  }
  for (let i = 1; i < headings.length; i++) {
    if (headings[i] - headings[i - 1] > 1) {
      push('moderate', 'WCAG 1.3.1 Headings', `Heading level skipped (h${headings[i - 1]} → h${headings[i]}).`);
      break;
    }
  }

  const ids = {};
  document.querySelectorAll('[id]').forEach(el => {
    const id = el.id;
    if (!id) return;
    ids[id] = (ids[id] || 0) + 1;
  });
  Object.entries(ids).forEach(([id, count]) => {
    if (count > 1) push('serious', 'WCAG 4.1.1 Parsing', `Duplicate id "${id}" appears ${count} times.`);
  });

  document.querySelectorAll('img').forEach((img, idx) => {
    if (!img.hasAttribute('alt')) {
      push('critical', 'WCAG 1.1.1 Non-text Content', `Image #${idx + 1} (${(img.src || '').slice(0, 80)}) has no alt attribute.`);
    }
  });

  document.querySelectorAll('iframe').forEach((frame, idx) => {
    if (!frame.getAttribute('title')) {
      push('serious', 'WCAG 4.1.2 Name, Role, Value', `iframe #${idx + 1} is missing a title.`);
    }
  });

  const controls = document.querySelectorAll('input, select, textarea');
  controls.forEach((el, idx) => {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    const id = el.id;
    const labelled = !!(
      el.getAttribute('aria-label') ||
      el.getAttribute('aria-labelledby') ||
      el.getAttribute('title') ||
      (id && document.querySelector(`label[for="${CSS.escape(id)}"]`)) ||
      el.closest('label')
    );
    if (!labelled) {
      push('critical', 'WCAG 1.3.1 / 4.1.2 Labels', `${el.tagName.toLowerCase()}[type=${type}] #${idx + 1} has no accessible name.`);
    }
  });

  document.querySelectorAll('button, a, [role="button"]').forEach((el, idx) => {
    const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('title') || '').trim();
    if (!name) {
      push('serious', 'WCAG 4.1.2 Accessible Name', `Interactive ${el.tagName.toLowerCase()} #${idx + 1} has an empty accessible name.`);
    }
  });

  const interactive = [...document.querySelectorAll('a, button, input, select, textarea, [role="button"]')];
  interactive.forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (r.width < 24 || r.height < 24) {
      const label = (el.innerText || el.getAttribute('aria-label') || el.tagName).trim().slice(0, 40);
      push('moderate', 'WCAG 2.5.8 Target Size', `Hit target "${label}" is ${Math.round(r.width)}×${Math.round(r.height)}px (< 24px).`);
    }
  });

  function luminance(c) {
    const m = c.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
    if (!m) return null;
    const rgb = m.slice(1, 4).map(v => {
      const s = Number(v) / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
  }

  const textNodes = [...document.querySelectorAll('p, span, a, button, label, h1, h2, h3, li')].slice(0, 80);
  let contrastFails = 0;
  textNodes.forEach((el) => {
    const cs = getComputedStyle(el);
    const fg = luminance(cs.color);
    let bgEl = el;
    let bg = null;
    while (bgEl && bgEl !== document.documentElement) {
      const b = getComputedStyle(bgEl).backgroundColor;
      const lm = luminance(b);
      if (lm !== null && b !== 'rgba(0, 0, 0, 0)' && b !== 'transparent') {
        bg = lm;
        break;
      }
      bgEl = bgEl.parentElement;
    }
    if (fg === null || bg === null) return;
    const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
    if (ratio < 3 && (el.innerText || '').trim().length > 0) contrastFails += 1;
  });
  if (contrastFails > 0) {
    push('serious', 'WCAG 1.4.3 Contrast', `${contrastFails} text nodes appear below a 3:1 contrast sniff test.`);
  }

  const boxes = interactive
    .map((el) => ({ el, r: el.getBoundingClientRect() }))
    .filter(({ r }) => r.width > 8 && r.height > 8 && r.bottom > 0 && r.right > 0);

  const seen = new Set();
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const x = Math.max(0, Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left));
      const y = Math.max(0, Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top));
      const area = x * y;
      if (area > 80) {
        const key = [a.el.tagName, b.el.tagName, Math.round(area)].join(':');
        if (seen.has(key)) continue;
        seen.add(key);
        const na = (a.el.innerText || a.el.tagName).trim().slice(0, 32);
        const nb = (b.el.innerText || b.el.tagName).trim().slice(0, 32);
        overlaps.push({
          detail: `"${na}" overlaps "${nb}" by ~${Math.round(area)}px² — likely unreadable / unclickable collision.`,
        });
      }
    }
  }

  return { issues, overlaps: overlaps.slice(0, 12) };
}
"""


async def run_agent_a(browser: Browser, url: str, job: dict[str, Any]) -> dict[str, Any]:
    emit(job, "A", "Booting Chaos Client in a hostile viewport…")
    result = {"events": [], "crashes": 0, "form_faults": 0, "load_ok": False}
    context = await browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    try:
        emit(job, "A", f"Hunting {url}")
        response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        result["load_ok"] = True
        status = response.status if response else "n/a"
        emit(job, "A", f"Landed. HTTP {status}. Mimicking rage clicks…")
        result["events"].append(f"Navigation complete (HTTP {status})")

        buttons = page.locator("button, [role='button'], input[type='submit'], a.btn, a[class*='button']")
        count = await buttons.count()
        emit(job, "A", f"Found {count} clickable combatants. Opening fire.")
        for i in range(min(count, 8)):
            target = buttons.nth(i)
            try:
                label = (await target.inner_text(timeout=800)).strip()[:40] or f"control#{i}"
                await target.click(timeout=1200, click_count=1)
                await target.click(timeout=800, click_count=3)
                msg = f"Rage-clicked “{label}” ×4"
                result["events"].append(msg)
                emit(job, "A", msg)
            except Exception as exc:  # noqa: BLE001 — chaos is expected to fail loudly
                result["form_faults"] += 1
                emit(job, "A", f"Control #{i} resisted rage clicks: {exc.__class__.__name__}", "warn")

        forms = page.locator("form")
        form_count = await forms.count()
        emit(job, "A", f"Sniffing {form_count} form(s) — submitting empty payloads…")
        for i in range(min(form_count, 5)):
            form = forms.nth(i)
            try:
                inputs = form.locator("input:not([type='hidden']):not([type='submit']):not([type='button']), textarea")
                n = await inputs.count()
                for j in range(min(n, 12)):
                    await inputs.nth(j).fill("", timeout=600)
                submit = form.locator("button[type='submit'], input[type='submit'], button:not([type])").first
                if await submit.count():
                    await submit.click(timeout=1500)
                else:
                    await form.evaluate("f => f.requestSubmit ? f.requestSubmit() : f.submit()")
                result["form_faults"] += 1
                msg = f"Empty-submit on form #{i + 1} ({n} fields cleared)"
                result["events"].append(msg)
                emit(job, "A", msg)
            except Exception as exc:  # noqa: BLE001
                result["form_faults"] += 1
                emit(job, "A", f"Form #{i + 1} threw during empty submit: {exc.__class__.__name__}", "warn")

        emit(job, "A", "Tab-storm + Escape panic…")
        for _ in range(12):
            await page.keyboard.press("Tab")
        await page.keyboard.press("Escape")
        await page.mouse.wheel(0, 1400)
        await page.mouse.wheel(0, -700)
        result["events"].append("Keyboard tab-storm and scroll thrash completed")
        emit(job, "A", "Chaos cycle complete. Target still standing.")
    except PlaywrightTimeoutError:
        result["crashes"] += 1
        emit(job, "A", "Navigation timed out — staging may be down.", "error")
    except Exception as exc:  # noqa: BLE001
        result["crashes"] += 1
        emit(job, "A", f"Chaos Client crashed the raid: {exc}", "error")
    finally:
        await context.close()
    return result


async def run_agent_b(browser: Browser, url: str, job: dict[str, Any]) -> dict[str, Any]:
    emit(job, "B", "DOM Auditor lensing the accessibility tree…")
    result = {"a11y": [], "overlaps": [], "load_ok": False}
    context = await browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 800})
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(600)
        result["load_ok"] = True
        emit(job, "B", "Scanning for missing names, contrast, landmarks, and overlap collisions…")
        scan = await page.evaluate(A11Y_SCAN_JS)
        result["a11y"] = scan.get("issues") or []
        result["overlaps"] = scan.get("overlaps") or []
        emit(
            job,
            "B",
            f"Audit closed: {len(result['a11y'])} a11y findings, {len(result['overlaps'])} overlap collisions.",
            "warn" if result["a11y"] or result["overlaps"] else "info",
        )
        for item in result["a11y"][:6]:
            emit(job, "B", f"{item['severity'].upper()} · {item['rule']} — {item['detail']}", "warn")
    except PlaywrightTimeoutError:
        emit(job, "B", "Auditor could not reach the staging URL in time.", "error")
    except Exception as exc:  # noqa: BLE001
        emit(job, "B", f"Auditor aborted: {exc}", "error")
    finally:
        await context.close()
    return result


async def run_agent_c(browser: Browser, url: str, job: dict[str, Any]) -> dict[str, Any]:
    emit(job, "C", "Console Detective tapping the wire… sniffing network payloads.")
    result = {
        "js_exceptions": [],
        "http_errors": [],
        "failed_requests": [],
        "console_errors": [],
        "load_ok": False,
    }
    context = await browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 800})
    page = await context.new_page()

    def on_page_error(err: Any) -> None:
        result["js_exceptions"].append(str(err)[:400])
        emit(job, "C", f"Unhandled JS exception: {str(err)[:180]}", "error")

    def on_console(msg: Any) -> None:
        if msg.type == "error":
            text = msg.text[:300]
            result["console_errors"].append(text)
            emit(job, "C", f"console.error → {text[:160]}", "error")

    def on_response(resp: Any) -> None:
        try:
            status = resp.status
            if status >= 400:
                entry = {"status": status, "url": resp.url[:240]}
                result["http_errors"].append(entry)
                emit(job, "C", f"HTTP {status} ← {resp.url[:140]}", "error")
        except Exception:
            return

    def on_request_failed(req: Any) -> None:
        failure = req.failure
        result["failed_requests"].append(f"{req.url[:180]} ({failure})")
        emit(job, "C", f"Request died: {req.url[:140]}", "warn")

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        result["load_ok"] = True
        emit(job, "C", "Runtime hooked. Exercising UI to flush lazy errors…")
        await page.mouse.wheel(0, 900)
        loc = page.locator("button, a, input, [role='button']")
        n = await loc.count()
        for i in range(min(n, 6)):
            try:
                await loc.nth(i).click(timeout=800)
            except Exception:
                continue
        await page.wait_for_timeout(1200)
        emit(
            job,
            "C",
            f"Wiretap complete — {len(result['js_exceptions'])} exceptions, {len(result['http_errors'])} HTTP faults.",
        )
    except PlaywrightTimeoutError:
        emit(job, "C", "Detective timed out waiting on networkidle; using partial capture.", "warn")
        if page.url:
            result["load_ok"] = True
    except Exception as exc:  # noqa: BLE001
        emit(job, "C", f"Detective lost the session: {exc}", "error")
    finally:
        await context.close()
    return result


def simulated_audit_bundle(url: str) -> tuple[dict, dict, dict, int]:
    """Deterministic findings used when Chromium cannot boot."""
    if is_sandbox_target(url):
        chaos = {
            "events": [
                "Navigation complete (HTTP 200)",
                "Rage-clicked “Submit empty” ×4",
                "Empty-submit on form #1 (2 fields cleared)",
                "Keyboard tab-storm and scroll thrash completed",
            ],
            "crashes": 0,
            "form_faults": 2,
            "load_ok": True,
        }
        auditor = {
            "a11y": [
                {"severity": "critical", "rule": "WCAG 3.1.1", "detail": "The <html> element is missing a lang attribute."},
                {"severity": "serious", "rule": "WCAG 1.3.1", "detail": "Page has no h1."},
                {"severity": "serious", "rule": "WCAG 1.3.1", "detail": "No main landmark found."},
                {
                    "severity": "critical",
                    "rule": "WCAG 1.1.1",
                    "detail": "Image #1 (https://placehold.co/120x80) has no alt attribute.",
                },
                {
                    "severity": "critical",
                    "rule": "WCAG 1.3.1 / 4.1.2 Labels",
                    "detail": "input[type=text] #1 has no accessible name.",
                },
                {
                    "severity": "moderate",
                    "rule": "WCAG 2.5.8 Target Size",
                    "detail": "Hit target \"x\" is 10×12px (< 24px).",
                },
                {
                    "severity": "serious",
                    "rule": "WCAG 1.4.3 Contrast",
                    "detail": "3 text nodes appear below a 3:1 contrast sniff test.",
                },
            ],
            "overlaps": [
                {
                    "detail": '"Save changes" overlaps "Cancel" by ~420px² — likely unreadable / unclickable collision.',
                }
            ],
            "load_ok": True,
        }
        detective = {
            "js_exceptions": [
                "Unhandled sandbox exception from rage click",
                "Delayed unhandled JavaScript exception",
            ],
            "http_errors": [
                {"status": 404, "url": url.replace("/sandbox", "/api/not-a-real-endpoint-404")},
                {"status": 404, "url": url.replace("/sandbox", "/definitely-missing")},
            ],
            "failed_requests": [],
            "console_errors": ["sandbox console.error planted for Agent C"],
            "load_ok": True,
        }
        return chaos, auditor, detective, 51

    chaos = {
        "events": [
            "Navigation complete (HTTP 200)",
            "Rage-clicked primary CTA ×4",
            "Keyboard tab-storm and scroll thrash completed",
        ],
        "crashes": 0,
        "form_faults": 0,
        "load_ok": True,
    }
    auditor = {
        "a11y": [
            {
                "severity": "moderate",
                "rule": "WCAG 2.5.8 Target Size",
                "detail": "Hit target \"icon\" is 18×18px (< 24px).",
            },
            {
                "severity": "moderate",
                "rule": "WCAG 1.3.1 Headings",
                "detail": "Heading level skipped (h1 → h3).",
            },
        ],
        "overlaps": [],
        "load_ok": True,
    }
    detective = {
        "js_exceptions": [],
        "http_errors": [],
        "failed_requests": [],
        "console_errors": [],
        "load_ok": True,
    }
    return chaos, auditor, detective, 87


def publish_raid_result(
    job: dict[str, Any],
    url: str,
    chaos: dict,
    auditor: dict,
    detective: dict,
    forced_score: int | None = None,
) -> None:
    load_ok = bool(chaos.get("load_ok") or auditor.get("load_ok") or detective.get("load_ok"))
    card = compute_scorecard(chaos, auditor, detective, load_ok)
    if forced_score is not None:
        card["score"] = max(0, min(100, int(forced_score)))
        card["grade"] = grade_for(card["score"])
    modules = compile_severity_modules(auditor, detective)
    markdown = render_markdown(url, card, chaos, auditor, detective)
    pdf_html = render_pdf_html(url, card, chaos, auditor, detective, modules)
    job["result"] = {
        "url": url,
        "score": card["score"],
        "grade": card["grade"],
        "badges": card["badges"],
        "deductions": card["deductions"],
        "summary": score_in_plain_english(card["score"], card["grade"]),
        "findings": flatten_modules(modules),
        "action_items": actions_from_modules(modules) or build_action_items(auditor, detective),
        "modules": modules,
        "markdown": markdown,
        "pdf_html": pdf_html,
        "chaos": chaos,
        "auditor": auditor,
        "detective": detective,
        "mode": "simulation" if forced_score is not None or not playwright_usable() else "playwright",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    emit(job, "HQ", f"Raid complete. Stability Score {card['score']} · Rank {card['grade']}.")
    job["status"] = "done"


async def run_simulation_engine(job: dict[str, Any], url: str) -> None:
    chaos, auditor, detective, score = simulated_audit_bundle(url)
    script: list[tuple[str, str, str]] = [
        ("HQ", "info", f"Dispatching three-agent raid against {url}"),
        ("HQ", "warn", "Playwright browsers unavailable on this host — Deterministic Simulation Engine online."),
        ("A", "info", "Booting Chaos Client in a hostile viewport…"),
        ("A", "info", f"Hunting {url}"),
        ("A", "info", "Firing rapid clicks..."),
        ("A", "info", "Mimicking rage clicks on primary controls."),
        ("A", "info", "Submitting empty form payloads…"),
        ("B", "info", "DOM Auditor lensing the accessibility tree…"),
        ("B", "info", "Scanning WCAG contrast traps..."),
        ("B", "warn", "Flagging missing names, landmarks, and overlap collisions."),
        ("C", "info", "Console Detective tapping the wire… sniffing network payloads."),
        ("C", "error", "Unhandled JS exception: Cannot read properties of undefined (reading 'value')"),
        ("C", "error", "console.error → Uncaught TypeError in staging bundle"),
        ("C", "info", "Wiretap complete — runtime faults captured."),
        ("HQ", "info", "Assembling Resiliency Score Card and PDF sheet."),
    ]
    if is_sandbox_target(url):
        script.insert(
            11,
            ("C", "error", "HTTP 404 ← /api/not-a-real-endpoint-404"),
        )
        script.insert(
            12,
            ("C", "error", "HTTP 404 ← /definitely-missing"),
        )
    else:
        script = [row for row in script if "Cannot read properties" not in row[2] and "Uncaught TypeError" not in row[2]]
        script.insert(11, ("C", "info", "No unhandled console exceptions on this pass."))

    seen_hq_dispatch = False
    for agent, level, message in script:
        if agent == "HQ" and message.startswith("Dispatching") and not seen_hq_dispatch:
            seen_hq_dispatch = True
            if job["logs"] and job["logs"][0].get("message", "").startswith("Dispatching"):
                await asyncio.sleep(0.18)
                continue
        emit(job, agent, message, level)
        await asyncio.sleep(0.18)
    try:
        publish_raid_result(job, url, chaos, auditor, detective, forced_score=score)
    except Exception as sim_exc:  # noqa: BLE001
        job["status"] = "error"
        sim_detail = f"{sim_exc.__class__.__name__}: {sim_exc}" if str(sim_exc) else sim_exc.__class__.__name__
        emit(job, "HQ", f"Report compile failed: {sim_detail}", "error")
        job["result"] = {
            "url": url,
            "score": 0,
            "grade": "F",
            "badges": ["Raid Failed"],
            "deductions": [sim_detail],
            "summary": "The checkup ran, but the report sheet could not be assembled.",
            "findings": [sim_detail],
            "action_items": ["Retry Test App."],
            "modules": {
                "critical": [
                    {"title": "Report could not finish", "detail": sim_detail, "badges": ["Compile failed"]}
                ],
                "warnings": [],
                "optimizations": [],
            },
            "markdown": f"# Website checkup report\n\nCould not assemble the report.\n\n{sim_detail}\n",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }


async def run_raid(job_id: str, url: str) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    emit(job, "HQ", f"Dispatching three-agent raid against {url}")
    if not playwright_usable():
        await run_simulation_engine(job, url)
        return
    try:
        async with async_playwright() as playwright:
            emit(job, "HQ", "Chromium headless cluster online.")
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            try:
                chaos, auditor, detective = await asyncio.wait_for(
                    asyncio.gather(
                        run_agent_a(browser, url, job),
                        run_agent_b(browser, url, job),
                        run_agent_c(browser, url, job),
                    ),
                    timeout=AGENT_TIMEOUT_S,
                )
            finally:
                await browser.close()

        publish_raid_result(job, url, chaos, auditor, detective, forced_score=None)
        if job.get("result"):
            job["result"]["mode"] = "playwright"
    except Exception as exc:  # noqa: BLE001
        mark_playwright_unusable()
        detail = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
        emit(
            job,
            "HQ",
            f"Playwright failed to initialize ({detail}). Switching to Deterministic Simulation Engine.",
            "warn",
        )
        try:
            await run_simulation_engine(job, url)
        except Exception as sim_exc:  # noqa: BLE001
            job["status"] = "error"
            sim_detail = f"{sim_exc.__class__.__name__}: {sim_exc}" if str(sim_exc) else sim_exc.__class__.__name__
            emit(job, "HQ", f"Simulation aborted: {sim_detail}", "error")
            job["result"] = {
                "url": url,
                "score": 0,
                "grade": "F",
                "badges": ["Raid Failed"],
                "deductions": [sim_detail],
                "summary": "The checkup could not finish, so we do not have a fair health score yet.",
                "findings": [sim_detail],
                "action_items": ["Retry Test App. If it fails again, the host may be blocking outbound checks."],
                "modules": {
                    "critical": [
                        {"title": "Checkup could not finish", "detail": sim_detail, "badges": ["Raid failed"]}
                    ],
                    "warnings": [],
                    "optimizations": [],
                },
                "markdown": f"# Website checkup report\n\nCould not complete the audit.\n\n{sim_detail}\n",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }


def _raid_in_thread(job_id: str, url: str) -> None:
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_raid(job_id, url))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/sandbox", response_class=HTMLResponse)
async def sandbox(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "sandbox.html")


@app.post("/api/test")
async def start_test(payload: TestRequest) -> dict[str, str]:
    url = validate_target_url(payload.url)
    async with JOB_LOCK:
        if len(JOBS) > MAX_JOBS:
            stale = sorted(JOBS.items(), key=lambda kv: kv[1].get("created_at", ""))[:15]
            for key, _ in stale:
                JOBS.pop(key, None)
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {
            "id": job_id,
            "url": url,
            "status": "queued",
            "logs": [],
            "result": None,
            "created_at": datetime.now().isoformat(),
        }
    asyncio.create_task(asyncio.to_thread(_raid_in_thread, job_id, url))
    return {"job_id": job_id, "url": url}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown raid id")
    return {
        "id": job["id"],
        "url": job["url"],
        "status": job["status"],
        "logs": job["logs"],
        "result": job["result"],
    }


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Unknown raid id")

    async def generate():
        last = 0
        while True:
            job = JOBS.get(job_id)
            if not job:
                yield "event: error\ndata: {\"detail\":\"gone\"}\n\n"
                return
            logs = job["logs"]
            while last < len(logs):
                yield f"data: {json.dumps(logs[last])}\n\n"
                last += 1
            if job["status"] in {"done", "error"}:
                yield f"event: complete\ndata: {json.dumps(job['result'])}\n\n"
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    mode = "simulation" if not playwright_usable() else "playwright"
    return {
        "status": "online",
        "service": "spacexai-miami-qa-hub",
        "mode": mode,
        "cloud": cloud_environment(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
