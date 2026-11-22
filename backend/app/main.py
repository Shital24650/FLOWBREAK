from __future__ import annotations

import ipaddress
import logging
import json
import os
import re
import socket
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, field_validator
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from app.egress import public_egress_proxy

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
ARTIFACTS = DATA_DIR / "artifacts"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "flowbreak.db"
MAX_LINKS = int(os.getenv("MAX_LINKS", "80"))
MAX_RUNS = int(os.getenv("MAX_RUNS", "100"))

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con

def init_db() -> None:
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, completed_at TEXT, duration_ms INTEGER,
          result_json TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0
        )""")

def safe_public_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only public http:// and https:// URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing credentials are not allowed.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The target URL contains an invalid port.") from exc

    host = parsed.hostname.lower().rstrip(".")
    blocked_suffixes = (
        ".local", ".internal", ".test", ".localhost", ".home.arpa",
        ".lan", ".corp", ".invalid",
    )
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"} or host.endswith(blocked_suffixes):
        raise ValueError("Local and internal hosts are not allowed.")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                host, port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in resolved}
        except (OSError, ValueError) as exc:
            raise ValueError("The target hostname could not be resolved safely.") from exc

    if not addresses or any(not addr.is_global for addr in addresses):
        raise ValueError("Private, loopback, link-local and reserved network targets are blocked.")
    return value


def record_resource_http_failures(fail, http_seen: list[dict[str, Any]]) -> None:
    """Record failed subresources; the main document is handled by page.goto."""
    for item in http_seen:
        if item.get("resource_type") == "document":
            continue
        fail(
            "http",
            "high" if item["status"] >= 500 else "medium",
            f"HTTP {item['status']} resource error",
            item["url"],
            item["url"],
        )

class TestRequest(BaseModel):
    url: str
    mobile: bool = True
    desktop: bool = True
    check_links: bool = True
    capture_screenshot: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            return safe_public_url(value.strip())
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

async def analyze_failures(url: str, failures: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    if not failures:
        return {"status": "not_needed", "summary": "No failures were observed; AI analysis was not needed.", "recommendations": []}
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return {"status": "unavailable", "summary": "AI analysis is not configured. Add OPENAI_API_KEY to enable explanations.", "recommendations": []}
    try:
        client = AsyncOpenAI(api_key=key, timeout=20)
        response = await client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a software reliability analyst. Only reason from supplied observations. Never claim you reproduced a bug or invent facts. Return JSON with summary, recommendations array, and likely_causes array."},
                {"role": "user", "content": json.dumps({"target": url, "observed_metrics": summary, "observed_failures": failures})[:14000]},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        return {"status": "complete", **data}
    except Exception as exc:
        return {"status": "error", "summary": f"AI analysis failed: {type(exc).__name__}. Browser evidence is still available.", "recommendations": []}

def _prune_run_rows(con: sqlite3.Connection) -> list[str]:
    evicted = [
        row["id"] for row in con.execute(
            "SELECT id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (max(1, MAX_RUNS),),
        ).fetchall()
    ]
    if evicted:
        placeholders = ",".join("?" for _ in evicted)
        con.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", evicted)
    return evicted


def _remove_run_artifacts(run_ids: list[str]) -> None:
    for run_id in run_ids:
        # IDs are generated UUIDs; glob is constrained to that run's own PNG files.
        for artifact in ARTIFACTS.glob(f"{run_id}-*.png"):
            try:
                artifact.unlink(missing_ok=True)
            except OSError as exc:
                logging.warning("Could not remove artifact %s: %s", artifact.name, exc)


def persist(result: dict[str, Any]) -> None:
    with db() as con:
        con.execute("INSERT OR REPLACE INTO runs (id,url,status,created_at,completed_at,duration_ms,result_json,is_demo) VALUES (?,?,?,?,?,?,?,?)",
            (result["id"], result["url"], result["status"], result["started_at"], result.get("completed_at"), result.get("duration_ms"), json.dumps(result), int(result.get("is_demo", False))))
        evicted_ids = _prune_run_rows(con)
    # Delete files only after the SQLite transaction commits. A filesystem error
    # leaves an orphaned file, never a partially-pruned database transaction.
    _remove_run_artifacts(evicted_ids)

def read_run(run_id: str) -> dict[str, Any] | None:
    with db() as con:
        row = con.execute("SELECT result_json FROM runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None

async def run_browser_test(run_id: str, request: TestRequest) -> None:
    started = time.monotonic()
    result: dict[str, Any] = {
        "id": run_id, "url": request.url, "status": "running", "is_demo": False,
        "started_at": now(), "viewports": [], "failures": [], "links": [],
        "javascript_errors": [], "http_errors": [], "screenshots": [], "ai_analysis": None,
    }
    persist(result)
    def fail(category: str, severity: str, title: str, detail: str, link: str | None = None) -> None:
        result["failures"].append({"category": category, "severity": severity, "title": title, "detail": detail, "url": link})

    try:
        async with public_egress_proxy() as proxy_url:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    proxy={"server": proxy_url, "bypass": "<-loopback>"},
                    args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
                )
                link_client = await p.request.new_context(proxy={"server": proxy_url, "bypass": "<-loopback>"}) if request.check_links else None
                profiles = []
                if request.desktop:
                    profiles.append(("desktop", {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1}))
                if request.mobile:
                    profiles.append(("mobile", {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 1, "is_mobile": True, "has_touch": True}))
                if not profiles:
                    profiles.append(("desktop", {"viewport": {"width": 1440, "height": 900}}))
                for label, device in profiles:
                    context = await browser.new_context(**device)
                    page = await context.new_page()
                    page.set_default_timeout(15000)
                    async def guard_public_requests(route):
                        try:
                            safe_public_url(route.request.url)
                        except ValueError:
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", guard_public_requests)
                    js_seen: list[dict[str, str]] = []
                    http_seen: list[dict[str, Any]] = []
                    page.on("pageerror", lambda error: js_seen.append({"type": "pageerror", "message": str(error)[:1000]}))
                    page.on("console", lambda msg: js_seen.append({"type": "console", "message": msg.text[:1000]}) if msg.type == "error" else None)
                    page.on("response", lambda response: http_seen.append({"status": response.status, "url": response.url, "resource_type": response.request.resource_type}) if response.status >= 400 else None)
                    try:
                        response = await page.goto(request.url, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(1000)
                        title = await page.title()
                        final_url = page.url
                        if response and response.status >= 400:
                            fail("http", "critical", f"Main page returned HTTP {response.status}", final_url)
                        if not title.strip():
                            fail("content", "low", "Page has an empty document title", final_url)
                        screenshot_path = None
                        if request.capture_screenshot:
                            screenshot_path = ARTIFACTS / f"{run_id}-{label}.png"
                            await page.screenshot(path=str(screenshot_path), full_page=True, timeout=15000)
                            result["screenshots"].append({"viewport": label, "path": screenshot_path.name, "url": f"/api/runs/{run_id}/artifacts/{screenshot_path.name}"})
                        anchors = await page.locator("a[href]").evaluate_all("(els) => els.map(a => ({href:a.href, text:(a.innerText||a.getAttribute('aria-label')||'').trim().slice(0,160)}))")
                        result["viewports"].append({"name": label, "width": device["viewport"]["width"], "height": device["viewport"]["height"], "title": title, "final_url": final_url, "http_status": response.status if response else None, "screenshot": screenshot_path.name if screenshot_path else None})
                        result["javascript_errors"].extend([{"viewport": label, **item} for item in js_seen])
                        result["http_errors"].extend([{"viewport": label, **item} for item in http_seen])
                        record_resource_http_failures(fail, http_seen)
                        if label == "desktop" and request.check_links:
                            seen: set[str] = set()
                            for anchor in anchors:
                                href = anchor.get("href", "")
                                if not href.startswith(("http://", "https://")) or href in seen:
                                    continue
                                seen.add(href)
                                if len(seen) > MAX_LINKS:
                                    break
                                try:
                                    safe_public_url(href)
                                    link_response = await link_client.get(href, timeout=8000, max_redirects=10)
                                    item = {"url": href, "text": anchor.get("text", ""), "status": link_response.status, "ok": link_response.status < 400}
                                    result["links"].append(item)
                                    if not item["ok"]:
                                        fail("broken_link", "high" if link_response.status >= 500 else "medium", f"Broken link (HTTP {link_response.status})", href, href)
                                except Exception as exc:
                                    result["links"].append({"url": href, "text": anchor.get("text", ""), "status": None, "ok": False, "error": type(exc).__name__})
                                    fail("broken_link", "medium", "Link could not be verified", f"{href} ({type(exc).__name__})", href)
                    except PlaywrightTimeoutError:
                        fail("navigation", "critical", f"{label.title()} page load timed out", request.url)
                    except Exception as exc:
                        fail("navigation", "critical", f"{label.title()} browser test failed", f"{type(exc).__name__}: {str(exc)[:500]}")
                    finally:
                        await context.close()
                if link_client:
                    await link_client.dispose()
                await browser.close()
    except Exception as exc:
        fail("engine", "critical", "Browser engine failed", f"{type(exc).__name__}: {str(exc)[:500]}")

    for item in result["javascript_errors"]:
        fail("javascript", "high", "JavaScript error detected", item["message"])
    metrics = {
        "viewports_tested": len(result["viewports"]),
        "links_checked": len(result["links"]),
        "broken_links": sum(1 for item in result["links"] if not item["ok"]),
        "javascript_errors": len(result["javascript_errors"]),
        "http_errors": len(result["http_errors"]),
        "failures": len(result["failures"]),
    }
    result["metrics"] = metrics
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["status"] = "passed" if not result["failures"] and result["viewports"] else "failed"
    result["completed_at"] = now()
    result["ai_analysis"] = await analyze_failures(request.url, result["failures"], metrics)
    persist(result)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="FLOWBREAK API", version="1.0.0", description="Browser-based software reliability testing", lifespan=lifespan)
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["*"])

@app.get("/health")
async def health():
    return {"status": "ok", "service": "flowbreak-api", "timestamp": now()}

@app.post("/api/runs", status_code=202)
async def create_run(payload: TestRequest, background: BackgroundTasks):
    run_id = str(uuid.uuid4())
    initial = {"id": run_id, "url": payload.url, "status": "queued", "is_demo": False, "started_at": now(), "viewports": [], "failures": [], "links": [], "javascript_errors": [], "http_errors": [], "screenshots": [], "metrics": {}, "ai_analysis": None}
    with db() as con:
        con.execute("INSERT INTO runs (id,url,status,created_at,result_json,is_demo) VALUES (?,?,?,?,?,0)", (run_id,payload.url,"queued",initial["started_at"],json.dumps(initial)))
    background.add_task(run_browser_test, run_id, payload)
    return initial

@app.get("/api/runs")
async def list_runs():
    with db() as con:
        rows = con.execute("SELECT result_json FROM runs ORDER BY created_at DESC LIMIT 100").fetchall()
    return [json.loads(row["result_json"]) for row in rows]

@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    result = read_run(run_id)
    if not result:
        raise HTTPException(404, "Test run not found")
    return result

@app.get("/api/runs/{run_id}/artifacts/{filename}")
async def get_artifact(run_id: str, filename: str):
    if not re.fullmatch(r"[a-f0-9-]+-(desktop|mobile)\.png", filename) or not filename.startswith(run_id):
        raise HTTPException(400, "Invalid artifact name")
    path = (ARTIFACTS / filename).resolve()
    if path.parent != ARTIFACTS or not path.is_file():
        raise HTTPException(404, "Artifact not found")
    return FileResponse(path, media_type="image/png", filename=filename)

@app.post("/api/demo")
async def demo_run():
    run_id = str(uuid.uuid4())
    stamp = now()
    result = {
      "id":run_id,"url":"https://demo.flowbreak.dev","status":"failed","is_demo":True,"started_at":stamp,"completed_at":stamp,"duration_ms":4280,
      "viewports":[{"name":"desktop","width":1440,"height":900,"title":"Demo storefront","final_url":"https://demo.flowbreak.dev","http_status":200},{"name":"mobile","width":390,"height":844,"title":"Demo storefront","final_url":"https://demo.flowbreak.dev","http_status":200}],
      "failures":[{"category":"broken_link","severity":"medium","title":"Broken link (HTTP 404)","detail":"https://demo.flowbreak.dev/returns","url":"https://demo.flowbreak.dev/returns"},{"category":"javascript","severity":"high","title":"JavaScript error detected","detail":"TypeError: Cannot read properties of undefined (reading 'map')","url":None}],
      "links":[{"url":"https://demo.flowbreak.dev/","text":"Home","status":200,"ok":True},{"url":"https://demo.flowbreak.dev/returns","text":"Returns","status":404,"ok":False}],
      "javascript_errors":[{"viewport":"mobile","type":"pageerror","message":"TypeError: Cannot read properties of undefined (reading 'map')"}],
      "http_errors":[{"viewport":"desktop","status":404,"url":"https://demo.flowbreak.dev/returns"}],
      "screenshots":[],"metrics":{"viewports_tested":2,"links_checked":2,"broken_links":1,"javascript_errors":1,"http_errors":1,"failures":2},
      "ai_analysis":{"status":"demo","summary":"Demo explanation: the returns route responds with 404, and a mobile page script attempts to call map on an undefined value.","likely_causes":["The returns route may have been renamed or removed.","A component may render before its data is initialized."],"recommendations":["Update or remove the stale returns link.","Guard the collection before calling map and add a loading or empty state."]}
    }
    persist(result)
    return result

init_db()
