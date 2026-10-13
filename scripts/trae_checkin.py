#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TraeWork (work.trae.cn) daily auto check-in.

Why this script exists:
    The daily check-in entry on work.trae.cn is disabled by a front-end kill
    switch -- the feature gate function returns false unconditionally, so no
    check-in button is ever rendered and the page's own fetchStatus/claim calls
    short-circuit. The server-side "checkin_credits" activity is still live,
    so this script bypasses the UI entirely and calls the backend APIs.

Flow:
    GetUserToken -> checkin_credits/status -> claim (only if not checked in)
    -> re-check status to verify

Login state:
    The session credential is an httpOnly cookie that JS cannot read, so the
    FIRST run needs a one-time manual login in a popped-up browser. The login
    state is persisted in a Playwright user-data-dir and reused afterwards.

    Browser-free alternative (recommended for headless / container deployments):
    run --export-session on any machine that is logged in, ship the JSON to the
    target box, then run --session there. That path is plain HTTP only -- no
    Playwright, no Chromium, no system libraries.

Usage:
    python trae_checkin.py                  # auto (silent; pops browser only if login needed)
    python trae_checkin.py --login          # force guided login (when login state expired)
    python trae_checkin.py --show-device    # print the device fingerprint and exit
    python trae_checkin.py --device-id ID   # override device id for this run
    python trae_checkin.py --export-session sess.json  # export login cookies + device_id for headless server deploy
    python trae_checkin.py --session sess.json         # NO-BROWSER check-in straight from an exported session file
    python trae_checkin.py --import-session sess.json  # import a session into the browser profile, then check in

Exit codes:
    0 = checked in now, or already checked in today
    1 = failed (see checkin.log next to this script)

Environment overrides:
    TRAE_PROFILE_DIR   where the persistent browser profile is stored
    TRAE_CHECKIN_LOG   path of the log file

NOTE: all runtime output is deliberately English. Non-ASCII strings raise
UnicodeEncodeError under Windows legacy console encodings (GBK) when logging,
which silently aborts scheduled runs.
"""
import os
import re
import sys
import time
import json
import platform
import logging
import argparse
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.environ.get("TRAE_PROFILE_DIR") or os.path.join(BASE_DIR, "trae_browser_profile")
LOG_FILE = os.environ.get("TRAE_CHECKIN_LOG") or os.path.join(BASE_DIR, "checkin.log")
DEVICE_FILE = os.path.join(BASE_DIR, "device.json")

HOME_URL = "https://work.trae.cn"
API = "https://api.trae.cn"

# Log to a UTF-8 file only -- never to the console (GBK encoding hazard above).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("trae_checkin")


# ---------------------------------------------------------------------------
# Device fingerprint
#
# The desktop client calls these APIs from Electron's main process net.fetch,
# which is NOT subject to CORS, so it can send custom headers like x-device-id.
# A browser page fetch() cannot: those headers trigger a CORS preflight that the
# server rejects, which is exactly why a browser-only flow always gets code=9004
# regardless of body/query variants. Calling the API from Python lets us send the
# same headers the real client sends.
# ---------------------------------------------------------------------------

DEVICE_ID_PATTERN = re.compile(r"\[ICubeDeviceRegisterService\].*?resolve device_id:\s*(\d+)")


def _client_log_roots():
    """Candidate 'logs' directories of the local Trae / Trae CN desktop client."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return [os.path.join(base, n, "logs") for n in ("Trae CN", "Trae")]
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", n, "logs")
                for n in ("Trae CN", "Trae")]
    return [os.path.join(home, ".config", n, "logs") for n in ("Trae CN", "Trae")]


def detect_device_id():
    """Recover the device id the local Trae client registered (newest session first)."""
    hits = []
    for root in _client_log_roots():
        if not os.path.isdir(root):
            continue
        try:
            sessions = sorted(os.listdir(root), reverse=True)
        except Exception:
            continue
        for session in sessions:
            path = os.path.join(root, session, "sharedprocess.log")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        m = DEVICE_ID_PATTERN.search(line)
                        if m:
                            hits.append((os.path.getmtime(path), m.group(1)))
                            break
            except Exception:
                continue
    if not hits:
        return None
    hits.sort(key=lambda x: x[0], reverse=True)
    return hits[0][1]


def detect_device_brand():
    """Host machine model -- the client sends this as x-device-brand."""
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_ComputerSystem).Model"],
                capture_output=True, text=True, timeout=25,
            )
            val = (out.stdout or "").strip()
            if val:
                return val
        except Exception:
            pass
        return "System Product Name"
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.model"],
                                 capture_output=True, text=True, timeout=15)
            val = (out.stdout or "").strip()
            if val:
                return val
        except Exception:
            pass
        return "Mac"
    for p in ("/sys/class/dmi/id/product_name", "/sys/class/dmi/id/board_name"):
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                val = fh.read().strip()
                if val:
                    return val
        except Exception:
            continue
    return platform.node() or "unknown"


def detect_device_type():
    """The client's os_name value."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def load_device(override_id=None):
    """Load the device fingerprint, probing and caching it when missing."""
    cfg = {}
    if os.path.isfile(DEVICE_FILE):
        try:
            with open(DEVICE_FILE, "r", encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
        except Exception as e:
            log.warning("Could not read %s: %s", DEVICE_FILE, e)
            cfg = {}

    if override_id:
        cfg["device_id"] = override_id
    if not cfg.get("device_id"):
        cfg["device_id"] = detect_device_id()
    if not cfg.get("device_brand"):
        cfg["device_brand"] = detect_device_brand()
    if not cfg.get("device_type"):
        cfg["device_type"] = detect_device_type()

    if not cfg.get("device_id"):
        return None

    try:
        with open(DEVICE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("Could not write %s: %s", DEVICE_FILE, e)
    return cfg


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def api_post(path, token, device):
    """POST an empty JSON object to api.trae.cn using the client's headers."""
    headers = {
        "Content-Type": "application/json",
        "authorization": "Cloud-IDE-JWT " + token,
        "x-device-id": str(device["device_id"]),
        "x-device-brand": str(device["device_brand"]),
        "x-device-type": str(device["device_type"]),
        "x-rust-request-timeout": "30000",
    }
    req = urllib.request.Request(API + path, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            http = resp.status
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        http = e.code
    except Exception as e:
        return {"http": 0, "data": {"_error": str(e)}}
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {"_raw": raw[:500]}
    return {"http": http, "data": data}


def do_checkin(token, device):
    """status -> claim -> verify, entirely from Python (CORS-free + device headers)."""
    steps = []

    s1 = api_post("/trae/api/v2/ug/checkin_credits/status", token, device)
    steps.append({"step": "status_before", "http": s1["http"], "data": s1["data"]})
    d1 = s1.get("data") or {}
    if d1.get("code") != 0:
        return {"ok": False, "error": "STATUS_FAILED", "code": d1.get("code"), "steps": steps}
    if d1.get("checked_in") is True:
        return {"ok": True, "skipped": True, "reason": "ALREADY_CHECKED_IN",
                "credits": d1.get("credits"), "steps": steps}

    c = api_post("/trae/api/v2/ug/checkin_credits/claim", token, device)
    steps.append({"step": "claim", "http": c["http"], "data": c["data"]})
    dc = c.get("data") or {}
    if dc.get("code") != 0:
        return {"ok": False, "error": "CLAIM_FAILED", "code": dc.get("code"),
                "message": dc.get("message"), "steps": steps}

    time.sleep(2)
    s2 = api_post("/trae/api/v2/ug/checkin_credits/status", token, device)
    steps.append({"step": "status_after", "http": s2["http"], "data": s2["data"]})
    d2 = s2.get("data") or {}
    verified = d2.get("checked_in") is True
    if not verified:
        # Claim reported success (code 0) but the verify status still says
        # checked_in=false. Treat it as a hard failure -- the check-in was
        # NOT actually granted, so do not report success.
        return {"ok": False, "error": "VERIFY_FAILED", "code": d2.get("code"),
                "message": "claim reported success but status still shows checked_in=false",
                "steps": steps}
    return {"ok": True, "verified": True, "credits": d2.get("credits"), "steps": steps}


# Probe: fetch the JWT only, used to detect whether the login state is valid.
JS_PROBE = """
async () => {
  try {
    const res = await fetch('https://api.trae.cn/cloudide/api/v3/common/GetUserToken', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'include'
    });
    const data = await res.json();
    const token = data && data.Result && data.Result.Token;
    return {ok: !!token, token: token || null};
  } catch (e) {
    return {ok: false, error: String(e)};
  }
}
"""


def run_js(page, js):
    try:
        return page.evaluate(js)
    except Exception as e:
        log.warning("JS evaluation failed: %s", e)
        return None


def interpret(result):
    """Translate a check-in result into (ok, message)."""
    if result is None:
        return False, "Execution failed (page context unavailable)"
    if result.get("ok"):
        if result.get("skipped"):
            return True, "Already checked in today (no action needed), credits=%s" % result.get("credits")
        return True, "Check-in SUCCEEDED, verified, credits=%s" % result.get("credits")
    err = result.get("error")
    code = result.get("code")
    if err == "NOT_LOGGED_IN":
        return False, "Not logged in or login state expired (manual login required)"
    if err == "STATUS_FAILED":
        return False, "Status query failed, code=%s" % code
    if err == "CLAIM_FAILED":
        # IMPORTANT: claim is only reached when status_before.checked_in === false,
        # so ANY non-zero claim code here is a REAL failure. Codes 9004 / 9095 were
        # previously misread as "already checked in today", which made the script
        # report success while no check-in was actually granted.
        return False, ("Claim FAILED - check-in NOT granted (code=%s: %s). "
                       "Status still reports checked_in=false.") % (code, result.get("message", ""))
    if err == "VERIFY_FAILED":
        return False, ("Claim reported success but verification failed (code=%s: %s). "
                       "Check-in was NOT confirmed.") % (code, result.get("message", ""))
    return False, "Unknown result: %s" % result


def _launch(pw, headless):
    return pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1440, "height": 900},
    )


def silent_attempt(pw, device, force_login=False):
    """Silent mode: no window. Returns (need_login, result)."""
    if force_login:
        return True, None
    log.info("Silent mode start, reusing saved login state...")
    ctx = _launch(pw, headless=True)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        probe = run_js(page, JS_PROBE)
        if not probe or not probe.get("ok"):
            log.info("Login state invalid or missing; manual login required")
            return True, None
        log.info("Login state valid, running check-in flow")
        return False, do_checkin(probe["token"], device)
    finally:
        ctx.close()


def guided_login(pw, device, timeout=1800):
    """Pop up a browser, wait for manual login, then finish the check-in."""
    log.info("Browser opened, waiting for manual TraeWork login (max %s seconds)...", timeout)
    ctx = _launch(pw, headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        if sys.platform.startswith("win"):
            # Top-most reminder so the login window is not missed. MessageBoxW
            # takes UTF-16 directly, so non-ASCII text is safe here.
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "\u8bf7\u5728\u5df2\u6253\u5f00\u7684\u6d4f\u89c8\u5668\u7a97\u53e3\u4e2d\u5b8c\u6210 TraeWork "
                    "\u767b\u5f55\u3002\n\n\u767b\u5f55\u6210\u529f\u540e\uff0c\u811a\u672c\u4f1a\u81ea\u52a8"
                    "\u7ee7\u7eed\u5b8c\u6210\u7b7e\u5230\u3002",
                    "TraeWork Auto Check-in - Login Required",
                    0x00040000,  # MB_TOPMOST
                )
            except Exception as e:
                log.warning("Failed to show reminder dialog: %s", e)

        deadline = time.time() + timeout
        while time.time() < deadline:
            page.wait_for_timeout(4000)
            try:
                if not page.url.startswith(HOME_URL):
                    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                continue
            probe = run_js(page, JS_PROBE)
            if probe and probe.get("ok"):
                log.info("Login detected")
                return do_checkin(probe["token"], device)
        log.error("Timed out waiting for login (%s seconds)", timeout)
        return None
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# Headless-server first login (export / import session)
#
# On a display-less server (Docker panel, headless VPS, etc.) the guided login
# cannot run: it needs a visible browser AND a human to type credentials.
# Workaround: do the one-time login on any machine that HAS a display, export
# the authenticated cookies + device_id into a single JSON file, ship that file
# to the server, and `import-session` replays it. After import the persistent
# profile holds the session, so every later run is plain headless (no window).
# ---------------------------------------------------------------------------

def _trae_cookies(ctx):
    """Return only Trae-related cookies from a browser context."""
    try:
        all_cookies = ctx.cookies()
    except Exception as e:
        log.warning("Could not read cookies: %s", e)
        return []
    return [c for c in all_cookies if "trae.cn" in (c.get("domain") or "")]


def export_session(path, override_id=None):
    """Export the current login session (cookies + device_id) to a JSON file."""
    device = load_device(override_id=override_id)
    if device is None:
        log.error("Cannot determine device_id. Options:")
        log.error("  1) Start the Trae desktop client once (it writes the log "
                  "this script reads the device_id from), then re-run --export-session.")
        log.error("  2) Pass --device-id <NUMBER> to override the auto-detection.")
        print("ERROR: device_id not found. See checkin.log for details.", flush=True)
        return 1
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _launch(pw, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            probe = run_js(page, JS_PROBE)
            if not probe or not probe.get("ok"):
                log.error("Not logged in. Complete login first (run --login on a machine "
                          "with a display), then re-run --export-session.")
                print("ERROR: not logged in. Run --login first.", flush=True)
                return 1
            cookies = _trae_cookies(ctx)
            payload = {
                "cookies": cookies,
                "device_id": (device or {}).get("device_id"),
                "exported_at": datetime.now().isoformat(timespec="seconds"),
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            log.info("Exported %d trae cookies + device_id=%s -> %s",
                     len(cookies), payload["device_id"], path)
            print("OK: exported %d cookies, device_id=%s -> %s" %
                  (len(cookies), payload["device_id"], path), flush=True)
            return 0
        finally:
            ctx.close()


def import_session(path):
    """Import a session JSON, verify it, then run the check-in (headless server)."""
    if not os.path.isfile(path):
        log.error("Session file not found: %s", path)
        return 1
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as e:
        log.error("Cannot read session file: %s", e)
        return 1

    cookies = payload.get("cookies") or []
    device_id = payload.get("device_id")
    if device_id:
        cfg = {}
        if os.path.isfile(DEVICE_FILE):
            try:
                with open(DEVICE_FILE, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh) or {}
            except Exception:
                cfg = {}
        cfg["device_id"] = device_id
        cfg["device_brand"] = cfg.get("device_brand") or detect_device_brand()
        cfg["device_type"] = cfg.get("device_type") or detect_device_type()
        try:
            with open(DEVICE_FILE, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("Could not write %s: %s", DEVICE_FILE, e)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _launch(pw, headless=True)
        try:
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                except Exception as e:
                    log.warning("Failed to add some cookies: %s", e)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            probe = run_js(page, JS_PROBE)
            if not probe or not probe.get("ok"):
                log.error("Imported session invalid or expired (re-export from a "
                          "logged-in machine).")
                return 1
            device = load_device(override_id=device_id)
            if device is None:
                log.error("device_id missing; pass --device-id manually.")
                return 1
            result = do_checkin(probe["token"], device)
            ok, msg = interpret(result)
            if result and result.get("steps"):
                log.info("API details: %s", result["steps"])
            log.info("RESULT: %s", msg)
            return 0 if ok else 1
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# Browser-free mode (--session)
#
# The JWT is normally minted inside a browser page because the session cookie is
# httpOnly. Experiment shows this is not actually required: GetUserToken accepts
# the exported cookies over plain HTTP and returns a Token -- no JavaScript, no
# page context, no Chromium. So on a display-less box (Docker panel, Alpine
# container, minimal VPS) the whole Playwright + Chromium + system-library
# dependency chain can be dropped: plain Python stdlib is enough.
#
# Trade-off: cookie lifetime is finite (weeks, not forever). When the session
# finally expires the error is explicit, and the fix is to re-export on a
# machine that still has the login. Set-Cookie values returned by the server are
# merged back into the session file so rotation does not silently kill it.
# ---------------------------------------------------------------------------

def load_session_file(path):
    """Read a session JSON produced by --export-session."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh) or {}


def cookie_header_for(cookies, host):
    """Build a Cookie header with only the cookies that apply to `host`."""
    parts = []
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".")
        if not dom:
            continue
        if host == dom or host.endswith("." + dom):
            parts.append("%s=%s" % (c.get("name"), c.get("value")))
    return "; ".join(parts)


def http_get_token(cookies, host="api.trae.cn"):
    """Exchange session cookies for a JWT over plain HTTP. Returns (token, set_cookies)."""
    req = urllib.request.Request(
        API + "/cloudide/api/v3/common/GetUserToken",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie_header_for(cookies, host),
        },
    )
    raw, set_cookies = "", []
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                set_cookies = resp.headers.get_all("Set-Cookie") or []
            except Exception:
                set_cookies = []
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            set_cookies = e.headers.get_all("Set-Cookie") or []
        except Exception:
            set_cookies = []
    except Exception as e:
        log.warning("GetUserToken request failed: %s", e)
        return None, []

    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        log.warning("GetUserToken returned non-JSON response")
        return None, []
    token = (data.get("Result") or {}).get("Token")
    if not token:
        log.warning("GetUserToken returned no token: %s", data)
    return token, set_cookies


def merge_set_cookies(payload, set_cookies):
    """Merge server Set-Cookie pairs back into the session payload (in place).

    Returns the number of cookie values that actually changed.
    """
    cookies = payload.get("cookies") or []
    index = {c.get("name"): c for c in cookies if c.get("name")}
    changed = 0
    for sc in set_cookies or []:
        pair = sc.split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or value in ("", "deleted"):
            continue
        if name in index:
            if index[name].get("value") != value:
                index[name]["value"] = value
                changed += 1
        else:
            cookies.append({"name": name, "value": value, "domain": ".trae.cn", "path": "/"})
            changed += 1
            index[name] = cookies[-1]
    payload["cookies"] = cookies
    return changed


def session_checkin(path, override_id=None):
    """Browser-free check-in: read an exported session file and use plain HTTP."""
    if not os.path.isfile(path):
        log.error("Session file not found: %s", path)
        return 1
    try:
        payload = load_session_file(path)
    except Exception as e:
        log.error("Cannot read session file: %s", e)
        return 1

    cookies = payload.get("cookies") or []
    if not cookies:
        log.error("Session file has no cookies. Re-export with --export-session.")
        return 1

    device = load_device(override_id=override_id or payload.get("device_id"))
    if device is None:
        log.error("device_id missing; pass --device-id manually.")
        return 1

    log.info("=" * 50)
    log.info("TraeWork auto check-in START  %s  device_id=%s  mode=browser-free",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), device["device_id"])

    token, set_cookies = http_get_token(cookies)
    if not token:
        log.error("Session invalid or expired (no token returned). Re-export with "
                  "--export-session on a machine that is still logged in.")
        return 1

    changed = merge_set_cookies(payload, set_cookies)
    if changed:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            log.info("Refreshed %d cookie value(s) in the session file", changed)
        except Exception as e:
            log.warning("Could not update session file: %s", e)

    result = do_checkin(token, device)
    ok, msg = interpret(result)
    if result and result.get("steps"):
        log.info("API details: %s", result["steps"])
    log.info("RESULT: %s", msg)
    log.info("TraeWork auto check-in END  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="TraeWork daily auto check-in")
    ap.add_argument("--login", action="store_true",
                    help="force guided login in a visible browser")
    ap.add_argument("--show-device", action="store_true",
                    help="print the device fingerprint and exit")
    ap.add_argument("--device-id", default=None,
                    help="override the device id for this run")
    ap.add_argument("--export-session", metavar="PATH", default=None,
                    help="export login session (cookies + device_id) to a JSON file "
                         "for headless server deployment")
    ap.add_argument("--import-session", metavar="PATH", default=None,
                    help="import a session JSON (from --export-session) into the browser "
                         "profile, then check in (needs headless Chromium)")
    ap.add_argument("--session", metavar="PATH", default=None,
                    help="browser-free check-in straight from a session JSON; uses plain "
                         "HTTP only, no Playwright/Chromium required")
    args = ap.parse_args()

    if args.export_session:
        return export_session(args.export_session, override_id=args.device_id)
    if args.session:
        return session_checkin(args.session, override_id=args.device_id)
    if args.import_session:
        return import_session(args.import_session)

    device = load_device(override_id=args.device_id)
    if args.show_device or device is None:
        probe = {"device_id": (device or {}).get("device_id"),
                 "device_brand": detect_device_brand(),
                 "device_type": detect_device_type(),
                 "device_file": DEVICE_FILE}
        print(json.dumps(probe, indent=2, ensure_ascii=False))
        if device is None:
            print("ERROR: could not determine device_id automatically.\n"
                  "Log into the Trae desktop client once (it writes "
                  "sharedprocess.log), or pass --device-id manually.")
            return 1
        return 0

    log.info("=" * 50)
    log.info("TraeWork auto check-in START  %s  device_id=%s",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), device["device_id"])

    # Lazily imported so non-browser paths (e.g. --show-device) work without
    # playwright installed; only the actual browser flow needs it.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        need_login, result = silent_attempt(pw, device, force_login=args.login)
        if need_login:
            result = guided_login(pw, device)

    ok, msg = interpret(result)
    if result and result.get("steps"):
        log.info("API details: %s", result["steps"])
    log.info("RESULT: %s", msg)
    log.info("TraeWork auto check-in END  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
