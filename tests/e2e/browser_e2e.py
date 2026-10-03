"""
Browser end-to-end check for the IDAA dashboard.

Drives a headless Microsoft Edge / Google Chrome over the Chrome DevTools
Protocol (only the `websockets` package, which ships with uvicorn[standard])
against an already-running IDAA server:

    upload (auto-detect / manual) → validation → storage → analysis →
    dashboard KPIs & charts → insights → PDF report, plus error handling,
    stale-data reset between datasets, HTML-injection safety, page reload
    restore and responsive layout (390 px and 1280 px, no horizontal scroll).

Usage (server must already be running):
    python tests/e2e/browser_e2e.py --base-url http://127.0.0.1:8000 \
        --prefix zz_idaa_e2e_test_ --out <dir for CSVs + screenshots>

Prints a JSON summary including the file_ids / dataset_ids it created, so a
caller testing against a real database can remove exactly those rows.
Exits non-zero on the first failed check.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from websockets.sync.client import connect

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
from helpers import cafe_df, ecommerce_df, gym_df  # noqa: E402

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]
XSS_NAME = '<img src=x onerror="window.__xss=1">'


class CheckFailed(AssertionError):
    pass


def check(cond, message):
    if not cond:
        raise CheckFailed(message)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """Minimal synchronous CDP client for one page."""

    def __init__(self):
        exe = next((b for b in BROWSERS if os.path.exists(b)), None)
        check(exe, "No Edge/Chrome executable found")
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="idaa_cdp_")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", f"--remote-debugging-port={self.port}", "--remote-allow-origins=*",
             f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(80):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1))
                ws_url = next((t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page"), None)
                if ws_url:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        check(ws_url, "Browser did not expose a debuggable page")
        self.ws = connect(ws_url, max_size=None, open_timeout=10)
        self.msg_id = 0
        self.exceptions: list[str] = []
        for domain in ("Page", "Runtime", "DOM"):
            self.send(f"{domain}.enable")

    def send(self, method, **params):
        self.msg_id += 1
        my_id = self.msg_id
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=90))
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.exceptions.append((d.get("exception") or {}).get("description") or d.get("text", "?"))
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expression, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"JS error in {expression[:80]!r}: {r['exceptionDetails'].get('text')}")
        return r["result"].get("value")

    def wait(self, expression, timeout=90, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expression):
                return
            time.sleep(0.25)
        raise CheckFailed(f"Timed out waiting for {what}")

    def goto(self, url):
        self.send("Page.navigate", url=url)
        time.sleep(0.3)
        self.wait("document.readyState === 'complete'", what="page load")

    def set_file(self, selector, path):
        root = self.send("DOM.getDocument", depth=1)["root"]["nodeId"]
        node = self.send("DOM.querySelector", nodeId=root, selector=selector)["nodeId"]
        self.send("DOM.setFileInputFiles", nodeId=node, files=[str(path)])

    def viewport(self, width, height, mobile):
        self.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=mobile)

    def screenshot(self, path):
        data = self.send("Page.captureScreenshot", format="png")["data"]
        Path(path).write_bytes(base64.b64decode(data))

    def close(self):
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------

def write_inputs(out: Path, prefix: str) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    ecom = ecommerce_df()
    top_pid = ecom.groupby("product_id")["total_amount"].sum().idxmax()
    ecom.loc[ecom["product_id"] == top_pid, "product_name"] = XSS_NAME         # HTML-injection probe
    files = {
        "ecommerce": (ecom.rename(columns={"quantity": "Qty", "total_amount": "Order Total"}), "ecommerce"),
        "gym": (gym_df(), "gym"),
        "cafe": (cafe_df(), "cafe"),
        "unsupported": (pd.DataFrame({"city": list("abcdefgh"), "temp_c": np.arange(8)}), "weather"),
    }
    paths = {}
    for key, (df, name) in files.items():
        p = out / f"{prefix}{name}.csv"
        df.to_csv(p, index=False)
        paths[key] = p
    return paths


def upload(b: Browser, path: Path, schema: str):
    b.js(f"document.getElementById('schemaSelect').value = {json.dumps(schema)}")
    b.set_file("#fileInput", path)
    b.wait("!document.getElementById('startBtn').disabled", what="file selected")
    b.js("document.getElementById('startBtn').click()")


def wait_finished(b: Browser):
    b.wait("['done','error'].includes(document.querySelector('.step[data-step=\"persist\"]').dataset.state)"
           " || !document.getElementById('errorPanel').classList.contains('hidden')", what="pipeline to finish")
    check(b.js("document.getElementById('errorPanel').classList.contains('hidden')"),
          "Unexpected error panel: " + str(b.js("document.getElementById('errorPanel').innerText")))


def kpis(b: Browser) -> dict:
    return b.js("Object.fromEntries([...document.querySelectorAll('#kpiGrid .kpi')].map(k => "
                "[k.querySelector('.kpi-label').textContent, k.querySelector('.kpi-value').textContent]))")


def chart_labels(b: Browser, canvas_id: str):
    return b.js(f"(Chart.getChart(document.getElementById('{canvas_id}')) || {{data: {{labels: null}}}}).data.labels")


def no_horizontal_overflow(b: Browser) -> bool:
    return b.js("document.documentElement.scrollWidth <= window.innerWidth + 1")


# ---------------------------------------------------------------------------
# Main scenario
# ---------------------------------------------------------------------------

def run(base_url: str, out: Path, prefix: str) -> dict:
    paths = write_inputs(out, prefix)
    summary = {"base_url": base_url, "file_ids": [], "dataset_ids": [], "checks": 0, "screenshots": []}

    def ok(cond, msg):
        check(cond, msg)
        summary["checks"] += 1

    b = Browser()
    try:
        b.goto(base_url + "/")
        b.js("localStorage.clear()")          # start without a restored previous analysis
        b.goto(base_url + "/")
        b.wait("!document.getElementById('healthApi').textContent.toLowerCase().includes('checking')", what="health status")
        ok("online" in b.js("document.getElementById('healthApi').textContent").lower(), "API status not online")

        # 1. Ecommerce via auto-detect, with synonym columns and an HTML-injection product name
        upload(b, paths["ecommerce"], "auto")
        wait_finished(b)
        state = b.js("({file_id: state.upload.file_id, dataset_id: state.upload.dataset_id, schema: state.upload.schema_type,"
                     " mapping: state.upload.column_mapping, run: state.result.db_persistence})")
        summary["file_ids"].append(state["file_id"]); summary["dataset_ids"].append(state["dataset_id"])
        ok(state["schema"] == "ecommerce", f"auto-detect gave {state['schema']}")
        ok(state["mapping"].get("qty") == "quantity" and state["mapping"].get("order_total") == "total_amount",
           f"column mapping {state['mapping']}")
        k = kpis(b)
        ok(k.get("Total revenue") == "₹13,23,090.95", f"revenue KPI {k.get('Total revenue')}")
        ok(k.get("Orders") == "200" and k.get("Unique customers") == "55", f"KPIs {k}")
        ok(k.get("Cancellation rate") == "9%" and k.get("Return rate") == "7.5%", f"rate KPIs {k}")
        months = chart_labels(b, "chartA")
        ok(months and months == sorted(months) and months[0].startswith("2024-"), f"monthly chart labels {months}")
        ok(chart_labels(b, "chartB"), "category chart missing")
        ok(b.js("window.__xss === undefined"), "HTML injection executed!")
        ok(XSS_NAME.lower() in b.js("document.getElementById('topBody').textContent"), "injected name not shown as text")
        badge = b.js("document.getElementById('narrationBadge').textContent")
        ok(badge in ("Source: Google Gemini", "Source: deterministic fallback"), f"narration badge {badge!r}")
        ok(not b.js("document.getElementById('downloadPdf').disabled"), "PDF button disabled after analysis")
        pdf = b.js(f"fetch('/report/{state['file_id']}').then(async r => [r.status, r.headers.get('content-type'),"
                   " (await r.arrayBuffer()).byteLength])", await_promise=True)
        ok(pdf[0] == 200 and pdf[1] == "application/pdf" and pdf[2] > 2000, f"PDF fetch {pdf}")
        for page in ("dashboard", "insights", "report", "architecture", "upload"):
            b.js(f"document.querySelector('.nav-btn[data-page=\"{page}\"]').click()")
            ok(b.js(f"document.getElementById('page-{page}').classList.contains('active')"), f"nav {page}")

        # 2. Responsive layout
        for width, height, mobile, name in ((390, 844, True, "mobile"), (1280, 900, False, "desktop")):
            b.viewport(width, height, mobile)
            for page in ("upload", "dashboard"):
                b.js(f"document.querySelector('.nav-btn[data-page=\"{page}\"]').click()")
                time.sleep(0.3)
                ok(no_horizontal_overflow(b), f"horizontal overflow on {page} at {width}px")
                shot = out / f"{prefix}{name}_{page}.png"
                b.screenshot(shot)
                summary["screenshots"].append(str(shot))
        b.send("Emulation.clearDeviceMetricsOverride")

        # 3. Switch to gym — nothing from ecommerce may remain
        upload(b, paths["gym"], "auto")
        wait_finished(b)
        state = b.js("({file_id: state.upload.file_id, dataset_id: state.upload.dataset_id, schema: state.upload.schema_type})")
        summary["file_ids"].append(state["file_id"]); summary["dataset_ids"].append(state["dataset_id"])
        ok(state["schema"] == "gym", f"auto-detect gave {state['schema']}")
        k = kpis(b)
        ok(set(k) == {"Members", "Monthly fees", "Average age", "Avg attendance"}, f"gym KPI labels {list(k)}")
        ok(k["Members"] == "40", f"members {k['Members']}")
        ok(set(chart_labels(b, "chartA") or []) <= {"monthly", "yearly"}, "stale/incorrect gym chart A")
        top_text = b.js("document.getElementById('topBody').textContent")
        ok("img" not in top_text and "serum" not in top_text, "stale ecommerce rows in top table")
        ok(b.js("state.charts.length") == 2, "chart instances leaked")

        # 4. Cafe with manual selection
        upload(b, paths["cafe"], "cafe")
        wait_finished(b)
        state = b.js("({file_id: state.upload.file_id, dataset_id: state.upload.dataset_id})")
        summary["file_ids"].append(state["file_id"]); summary["dataset_ids"].append(state["dataset_id"])
        k = kpis(b)
        ok("Busiest day" in k and "Orders" not in k and k["Total revenue"] != "N/A", f"cafe KPIs {k}")

        # 5. Unsupported dataset with auto-detect → clear error, stale dashboard cleared
        upload(b, paths["unsupported"], "auto")
        b.wait("!document.getElementById('errorPanel').classList.contains('hidden')", what="unsupported error")
        ok("Unsupported" in b.js("document.getElementById('errorTitle').textContent"), "unsupported title")
        ok(b.js("document.querySelectorAll('#errorDetails tbody tr').length") == 3, "per-schema coverage table")
        ok(b.js("document.getElementById('dashContent').classList.contains('hidden')"), "stale dashboard after error")

        # 6. Wrong manual schema → suggestion
        upload(b, paths["ecommerce"], "gym")
        b.wait("!document.getElementById('errorPanel').classList.contains('hidden')", what="validation error")
        ok("ecommerce" in b.js("document.getElementById('errorSuggestion').textContent"), "missing schema suggestion")
        ok(b.js("document.querySelectorAll('#errorDetails tbody tr').length") > 0, "validation error table empty")

        # 7. Reload restores the last successful analysis (cafe) from /results
        b.goto(base_url + "/")
        b.wait("document.getElementById('datasetChip').textContent.includes('cafe')", what="restore after reload")
        ok(not b.js("document.getElementById('dashContent').classList.contains('hidden')"), "dashboard not restored")

        ok(not b.exceptions, f"JavaScript exceptions: {b.exceptions}")
    finally:
        b.close()
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--prefix", default="zz_idaa_e2e_test_")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = Path(args.out or tempfile.mkdtemp(prefix="idaa_e2e_"))
    try:
        summary = run(args.base_url.rstrip("/"), out, args.prefix)
    except CheckFailed as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        sys.exit(1)
    print(json.dumps({"status": "passed", **summary}))


if __name__ == "__main__":
    main()
