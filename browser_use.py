"""jorge browser-use: headless Chrome automation with persistent profile and credential cache.

Model-agnostic: the decide/progress/ask callbacks come from assistant.py.
"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(os.path.dirname(os.path.realpath(__file__)))
PROFILE_DIR = Path.home() / ".config" / "jorge-browser"
SECRETS_FILE = BASE / "browser_secrets.json"
DOWNLOADS_DIR = Path.home() / "Downloads"
SHOTS_DIR = Path.home() / "Pictures"
START_URL = "https://duckduckgo.com"
MAX_STEPS = 12

_TEXT_RE = re.compile(r"\s+")
_2FA_RE = re.compile(r"\b(code|2fa|otp|token|verification|authentication|mfa)\b", re.I)


def _load_secrets() -> dict:
    try:
        return json.loads(SECRETS_FILE.read_text())
    except Exception:
        return {}


def _save_secrets(data: dict) -> None:
    SECRETS_FILE.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().replace("www.", "")


def save_credentials(site: str, email: str, password: str) -> str:
    d = _domain(site) or site.strip()
    data = _load_secrets()
    data[d] = {"email": email.strip(), "password": password.strip()}
    _save_secrets(data)
    return f"✓ Credentials for {d} saved (never shared, stored locally)."


def instagram_upload(file: str, caption: str = "", progress=None, headless: bool = True) -> str:
    """Upload a video to Instagram with a hardcoded mechanical flow (no AI decide loop)."""
    progress = progress or (lambda m: None)
    file = os.path.expanduser(file)
    if not os.path.exists(file):
        return f"⚠ file not found: {file}"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Playwright is not installed — run: python3 -m pip install playwright"
    progress(f"🌐 opening instagram.com (logged-in profile)")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel="chrome", headless=headless,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.instagram.com/", timeout=90000)
            page.wait_for_timeout(5000)
            if page.query_selector("input[type=password]"):
                ctx.close()
                return "⚠ Not logged into instagram.com — run: jorge browser-login instagram.com first."
            username = "yashusingh774"
            try:
                m = re.search(r'href="/([a-z0-9_\.]{2,30})/"', page.content())
                if m:
                    username = m.group(1)
            except Exception:
                pass
            existing_reels: list[str] = []
            try:
                page.goto(f"https://www.instagram.com/{username}/reels/", timeout=60000)
                page.wait_for_timeout(5000)
                existing_reels = [l.get_attribute("href") for l in page.query_selector_all(f'a[href*="/{username}/reel/"]')]
                page.goto("https://www.instagram.com/", timeout=60000)
                page.wait_for_timeout(4000)
            except Exception:
                pass
            progress("➕ opening the create menu")
            created = False
            for sel in (
                'svg[aria-label="New post"]',
                'svg[aria-label="Create"]',
                'div[role="menuitem"]:has-text("New post")',
                'span:has-text("New post")',
            ):
                try:
                    el = page.query_selector(sel)
                    if el:
                        el.click()
                        page.wait_for_timeout(2000)
                        created = True
                        break
                except Exception:
                    continue
            if not created:
                page.screenshot(path=SHOTS_DIR / "jorge_ig_create_fail.png")
                ctx.close()
                return "⚠ Couldn't find Instagram's Create button. Screenshot: ~/Pictures/jorge_ig_create_fail.png"
            for sel in ('[aria-label="Post"]', 'span:has-text("New post")', 'div[role="menuitem"]:has-text("New post")'):
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        break
                except Exception:
                    continue
            page.wait_for_timeout(2500)
            file_input = page.query_selector("input[type=file]")
            if not file_input:
                page.screenshot(path=SHOTS_DIR / "jorge_ig_nofile.png")
                ctx.close()
                return "⚠ Upload dialog didn't open. Screenshot: ~/Pictures/jorge_ig_nofile.png"
            progress(f"📤 attaching {os.path.basename(file)}")
            file_input.set_input_files(file)
            page.wait_for_timeout(4000)
            for i in range(2):
                next_btn = None
                for _ in range(10):
                    for bsel in ('div[role="button"]:has-text("Next")', 'button:has-text("Next")'):
                        try:
                            b = page.query_selector(bsel)
                            if b and b.is_visible():
                                next_btn = b
                                break
                        except Exception:
                            continue
                    if next_btn:
                        break
                    page.wait_for_timeout(1500)
                if not next_btn:
                    page.screenshot(path=SHOTS_DIR / "jorge_ig_next_fail.png")
                    ctx.close()
                    return f"⚠ 'Next' button not found (step {i + 1}). Screenshot: ~/Pictures/jorge_ig_next_fail.png"
                next_btn.click(force=True)
                page.wait_for_timeout(3000)
            if caption:
                cap = page.query_selector('div[aria-label="Write a caption..."]')
                if cap:
                    cap.click()
                    page.type(caption)
                    page.wait_for_timeout(800)
            share = None
            for _ in range(10):
                for ssel in ('div[role="button"]:has-text("Share")', 'button:has-text("Share")'):
                    try:
                        s = page.query_selector(ssel)
                        if s and s.is_visible():
                            share = s
                            break
                    except Exception:
                        continue
                if share:
                    break
                page.wait_for_timeout(1500)
            if not share:
                page.screenshot(path=SHOTS_DIR / "jorge_ig_share_fail.png")
                ctx.close()
                return "⚠ Share button not found. Screenshot: ~/Pictures/jorge_ig_share_fail.png"
            progress("🚀 sharing…")
            try:
                dlg = page.query_selector('[role="dialog"]')
                if dlg:
                    ai_sw = dlg.query_selector('[role="switch"]')
                    if ai_sw and ai_sw.get_attribute("aria-checked") != "true":
                        try:
                            ai_sw.click(timeout=5000)
                        except Exception:
                            ai_sw.evaluate("el => el.click()")
                        page.wait_for_timeout(800)
                share.evaluate("""el => {
                    const r = el.getBoundingClientRect();
                    let top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
                    let n = 0;
                    while (top && top !== el && n < 10) { top.style.pointerEvents = 'none'; top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2); n++; }
                    return n;
                }""")
                page.wait_for_timeout(300)
                b2 = share.bounding_box()
                page.mouse.move(b2["x"] + b2["width"]/2, b2["y"] + b2["height"]/2, steps=3)
                page.wait_for_timeout(300)
                page.mouse.down()
                page.wait_for_timeout(200)
                page.mouse.up()
            except Exception as e:
                page.screenshot(path=SHOTS_DIR / "jorge_ig_share_fail.png")
                ctx.close()
                return f"⚠ Couldn't click Share ({e}). Screenshot: ~/Pictures/jorge_ig_share_fail.png"
            configured = {"ok": False}
            def _on_resp(r):
                if "configure" in r.url and r.status == 200:
                    configured["ok"] = True
            page.on("response", _on_resp)
            ok = False
            for _ in range(30):
                page.wait_for_timeout(2000)
                if configured["ok"]:
                    ok = True
                    break
                try:
                    body = page.inner_text("body")
                except Exception:
                    body = ""
                if re.search(r"(your (?:post|reel|video) (?:has been |is )?(?:shared|live)|post shared|shared your)", body, re.I):
                    ok = True
                    break
                try:
                    dlg2 = page.query_selector('[role="dialog"]')
                    dlg_open = dlg2 is not None and dlg2.is_visible()
                except Exception:
                    dlg_open = True
                if not dlg_open:
                    ok = True
                    break
                for err in ("couldn't share", "couldn't publish", "something went wrong", "try again later",
                        "action blocked", "temporarily blocked", "can't post", "cant post",
                        "blocked from", "restricted"):
                    if err in body.lower():
                        page.screenshot(path=SHOTS_DIR / "jorge_ig_error.png")
                        ctx.close()
                        return f"⚠ Instagram rejected the post: '{err}' — screenshot: ~/Pictures/jorge_ig_error.png"
            if ok:
                for _ in range(10):
                    try:
                        dlg3 = page.query_selector('[role="dialog"]')
                        if dlg3 is None or not dlg3.is_visible():
                            break
                        close_btn = (page.query_selector('[aria-label="Close"]')
                                     or page.query_selector('[aria-label*="close" i]'))
                        if close_btn and close_btn.is_visible():
                            close_btn.evaluate("el => el.click()")
                        else:
                            page.keyboard.press("Escape")
                        page.wait_for_timeout(1500)
                    except Exception:
                        break
                        username = "yashusingh774"
            dlg_text = ""
            try:
                dlg_text = page.inner_text("[role=dialog]") or ""
                m = re.search(r"Tag People\s*\|\s*([a-z0-9_\.]{2,30})", dlg_text, re.I)
                if m:
                    username = m.group(1)
            except Exception:
                pass
            try:
                page.goto(f"https://www.instagram.com/{username}/reels/", timeout=60000)
                page.wait_for_timeout(6000)
                now_reels = [l.get_attribute("href") for l in page.query_selector_all(f'a[href*="/{username}/reel/"]')]
                if any(r not in existing_reels for r in now_reels):
                    ok = True
            except Exception:
                pass
            page.screenshot(path=SHOTS_DIR / "jorge_ig_result.png")
            ctx.close()
            if ok:
                return f"✅ Video uploaded to Instagram! Check https://www.instagram.com/{username}/reels/ — proof: ~/Pictures/jorge_ig_result.png"
            try:
                what = re.sub(r"\s+", " ", (dlg_text or "")[:160]).strip() or "composer stayed open"
            except Exception:
                what = "composer stayed open"
            return f"⚠ Share didn't finalize — {what}. Screenshot: ~/Pictures/jorge_ig_result.png"
    except Exception as e:
        return f"⚠ Upload failed: {e}"


def browser_login(site: str, progress=None) -> str:
    """Open the persistent profile VISIBLY so the user can log in manually (captcha/2FA walls included),
    then wait for them to close the window and verify the session saved."""
    progress = progress or (lambda m: None)
    d = _domain(site) or site.strip()
    url = "https://" + (site if "://" in site else site)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Playwright is not installed — run: python3 -m pip install playwright"
    progress(f"🌐 opening {url} in a VISIBLE window — log in yourself, then close the window")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                viewport=None,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, timeout=90000)
            page.wait_for_timeout(1500)
            progress("⌛ waiting for you to log in — close the window when done (the profile saves your session)")
            while len(ctx.pages) > 0:
                time.sleep(2)
            ctx.close()
    except Exception as e:
        return f"⚠ browser window failed: {e}"
    time.sleep(2)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel="chrome", headless=True,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, timeout=90000)
            page.wait_for_timeout(5000)
            pw = page.query_selector("input[type=password]")
            logged_in = pw is None
            ctx.close()
    except Exception:
        logged_in = False
    if logged_in:
        return f"✅ {d} session verified — jorge is logged in now."
    return (
        f"⚠ The {d} window closed but jorge's profile still shows the login page — you may not have "
        f"finished logging in (need to see the logged-in home page). Try again: jorge browser-login {d}"
    )


def save_2fa(site: str, code: str) -> str:
    d = _domain(site) or site.strip()
    data = _load_secrets()
    data["_2fa_" + d] = code.strip()
    _save_secrets(data)
    return f"✓ 2FA code for {d} saved — retry the browser task now."


def _snapshot(page) -> str:
    try:
        url = page.url
    except Exception:
        url = "?"
    try:
        title = page.title()
    except Exception:
        title = "?"
    body = ""
    try:
        raw = page.inner_text("body")
        body = _TEXT_RE.sub(" ", raw).strip()[:2500]
    except Exception:
        pass
    els = []
    seen = set()
    try:
        for b in page.query_selector_all("button, a, input, textarea, select, [role=button], [role=link]"):
            try:
                tag = b.evaluate("el => el.tagName.toLowerCase()")
                txt = (b.inner_text() if tag in ("button", "a") else "")
                txt = _TEXT_RE.sub(" ", txt).strip()[:40]
                aria = ""
                try:
                    aria = (b.get_attribute("aria-label") or "").strip()[:40]
                except Exception:
                    pass
                ph = ""
                try:
                    ph = (b.get_attribute("placeholder") or "").strip()[:40]
                except Exception:
                    pass
                name = ""
                try:
                    name = (b.get_attribute("name") or "").strip()[:30]
                except Exception:
                    pass
                if tag == "input":
                    it = (b.get_attribute("type") or "text").lower()
                    label = ph or aria or name or it
                    key = ("in", label, it)
                    if key in seen:
                        continue
                    seen.add(key)
                    els.append(f"input {len(els) + 1}: type={it} label={label!r}")
                elif tag in ("button", "a", "select", "textarea"):
                    label = aria or txt or name or tag
                    key = ("el", label)
                    if key in seen:
                        continue
                    seen.add(key)
                    if txt or aria:
                        els.append(f"clickable {len(els) + 1}: {tag} {label!r}")
            except Exception:
                continue
            if len(els) >= 40:
                break
    except Exception:
        pass
    snap = f"URL: {url}\nTITLE: {title}\n\nVISIBLE TEXT:\n{body}\n\nELEMENTS:\n" + "\n".join(els) if els else (
        f"URL: {url}\nTITLE: {title}\n\nVISIBLE TEXT:\n{body}\n\nELEMENTS: (none)")
    return snap[:4200]


def _is_2fa_input(el, page_text: str = "") -> bool:
    try:
        ph = (el.get_attribute("placeholder") or "") + " " + (el.get_attribute("name") or "") + " " + (el.get_attribute("aria-label") or "") + " " + (el.get_attribute("id") or "")
        it = (el.get_attribute("type") or "text").lower()
        if it == "text" and bool(_2FA_RE.search(ph)):
            return True
        if it == "text" and re.search(r"\b(6.?digit|code sent|verification|verify it.?s you|authenticator|enter the code|check your)\b", page_text, re.I):
            return True
        if it in ("text", "tel", "number") and re.search(r"\b(one.?time.?code|otp|2fa|mfa)\b", ph, re.I):
            return True
        if it in ("text", "tel", "number") and (el.get_attribute("autocomplete") or "") == "one-time-code":
            return True
        if it in ("text", "tel", "number") and (el.get_attribute("inputmode") or "") == "numeric":
            return True
    except Exception:
        pass
    return False


def _settle(page) -> None:
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass


def _wait_ready(page, timeout_ms: int = 15000) -> None:
    try:
        page.wait_for_selector("input, button, a", timeout=timeout_ms)
    except Exception:
        pass
    _settle(page)


def _has_content(snap: str) -> bool:
    if "ELEMENTS: (none)" not in snap:
        return True
    try:
        text = snap.split("VISIBLE TEXT:")[1].split("ELEMENTS:")[0].strip()
    except IndexError:
        return False
    return len(text) > 30


_LOGIN_TEXT_RE = re.compile(r"\b(log ?in|sign ?in|log ?into)\b", re.I)


def _click_login_link(page) -> bool:
    for el in page.query_selector_all("a, button, [role=button]"):
        try:
            label = (el.inner_text() or "") + " " + (el.get_attribute("aria-label") or "")
        except Exception:
            continue
        if _LOGIN_TEXT_RE.search(label):
            try:
                el.click()
                return True
            except Exception:
                continue
    return False


_BOTCHECK_RE = re.compile(
    r"(cloudflare|verify you are human|checking your browser|cf-chl|attention required|enable javascript and cookies|"
    r"(?:i'?m|i am|im) not a robot|recaptcha|captcha|are you a robot|confirm you are (?:not |a )?robot|"
    r"blocked|unusual activity|suspicious activity|login required to continue)",
    re.I,
)


def _click_submit(page) -> bool:
    login_words = re.compile(r"\b(log in|sign in|continue|next|submit|confirm|get started|create account|save)\b", re.I)
    for sel in ("button[type=submit]", "input[type=submit]", "button", "[role=button]", "[type=button]"):
        try:
            els = page.query_selector_all(sel)
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            try:
                txt = (el.inner_text() or "").strip()
            except Exception:
                txt = ""
            if not txt:
                try:
                    txt = (el.get_attribute("aria-label") or "") + " " + (el.get_attribute("value") or "")
                except Exception:
                    txt = ""
            if (el.get_attribute("type") == "submit" and not txt) or login_words.search(txt):
                try:
                    el.click()
                    return True
                except Exception:
                    continue
    return False


def _fill_and_submit(page, email, password) -> None:
    inputs = page.query_selector_all("input")
    filled = 0
    for el in inputs:
        try:
            it = (el.get_attribute("type") or "text").lower()
            if it == "email" or "email" in ((el.get_attribute("name") or "") + (el.get_attribute("placeholder") or "")).lower():
                el.fill(email)
                filled += 1
        except Exception:
            pass
    for el in inputs:
        try:
            it = (el.get_attribute("type") or "text").lower()
            if it == "password":
                el.fill(password)
                filled += 1
        except Exception:
            pass
    if filled:
        if not _click_submit(page):
            el = page.query_selector("input[type=password], input[type=email]")
            if el:
                try:
                    el.press("Enter")
                except Exception:
                    pass


def browser_task(task: str, start_url: str = "", decide=None, ask=None, progress=None, headless: bool = True) -> str:
    """Run a browser task. decide(step, snapshot, history) -> dict command. Returns the answer string."""
    if decide is None:
        return "Browser engine not wired (no decide callback)."
    progress = progress or (lambda m: None)
    files = []
    history: list[str] = []
    last_cmd: dict | None = None
    repeats = 0
    last_login_url = ""
    login_flow_done = False
    login_attempts = 0
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return "Playwright is not installed — run: python3 -m pip install playwright"
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=headless,
                channel="chrome",
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(15000)
            url = start_url.strip() or START_URL
            if not url.startswith("http"):
                url = "https://" + url
            progress(f"🌐 opening {url}")
            page.goto(url, timeout=40000, wait_until="domcontentloaded")
            _wait_ready(page)
            for step in range(MAX_STEPS):
                try:
                    snap = _snapshot(page)
                except Exception:
                    _wait_ready(page, timeout_ms=6000)
                    snap = _snapshot(page)
                for _ in range(3):
                    if _has_content(snap):
                        break
                    _wait_ready(page, timeout_ms=6000)
                    snap = _snapshot(page)
                low = (snap + " " + page.url).lower()
                if _BOTCHECK_RE.search(low):
                    return (
                        f"⚠ {_domain(page.url) or page.url} is showing a bot-check / "
                        "Cloudflare wall — headless automation can't get past it right now. "
                        "Run: jorge browser-login <site> to open the profile in a visible window, "
                        "log in yourself, and close it — the session will stick afterward."
                    )
                inputs = page.query_selector_all("input")
                pwd = [el for el in inputs if (el.get_attribute("type") or "").lower() == "password"]
                dom = _domain(page.url)
                two = [el for el in inputs if _is_2fa_input(el, snap)]
                if login_flow_done and not pwd and not two:
                    progress(f"✅ logged in on {dom}")
                    login_flow_done = False
                    login_attempts = 0
                if pwd:
                    login_attempts += 1
                    if login_attempts > 3:
                        data = _load_secrets()
                        data.pop(dom, None)
                        _save_secrets(data)
                        return (
                            f"⚠ Login on {dom} keeps failing after {login_attempts - 1} tries — the site is "
                            "rejecting the credentials or showing a verification wall. I've forgotten the "
                            "saved password for it — send the right one and I'll retry. If it still fails, "
                            "log in once manually in the browser profile (google-chrome "
                            "--user-data-dir=~/.config/jorge-browser) and the session will stick."
                        )
                    creds = _load_secrets().get(dom)
                    if not creds:
                        if ask is None:
                            return f"🔐 I need {dom} credentials (email + password). Send them like: email: you@x.com, pass: hunter2"
                        email = ask(f"your email for {dom}")
                        password = ask(f"your password for {dom}")
                        if not (email and password):
                            return "Login cancelled — no credentials given."
                        save_credentials(dom, email, password)
                        creds = {"email": email, "password": password}
                    if page.url == last_login_url:
                        if last_login_url:
                            progress(f"🔑 re-entering password on {dom}...")
                        _wait_ready(page, timeout_ms=6000)
                    _fill_and_submit(page, creds["email"], creds["password"])
                    last_login_url = page.url
                    progress(f"🔑 logging into {dom}...")
                    login_flow_done = True
                    _wait_ready(page)
                    continue
                if _LOGIN_TEXT_RE.search(task) and "login" not in page.url.lower() and not pwd and not two:
                    if _click_login_link(page):
                        progress(f"🔓 clicked the Log In link on {dom}")
                        _settle(page)
                        continue
                if two:
                    code = _load_secrets().get("_2fa_" + dom)
                    if not code:
                        if ask is None:
                            return f"🔐 2FA code needed for {dom}. Send it like: code: 123456"
                        code = ask(f"2FA code for {dom}")
                        if not code:
                            return "Login cancelled — no 2FA code."
                    try:
                        two[0].fill(code)
                        btn = page.query_selector("button[type=submit], [type=submit]")
                        if btn:
                            btn.click()
                        else:
                            two[0].press("Enter")
                    except Exception:
                        pass
                    data = _load_secrets()
                    data.pop("_2fa_" + dom, None)
                    _save_secrets(data)
                    login_flow_done = True
                    login_attempts = 0
                    _settle(page)
                    continue
                cmd = decide(task, snap, history)
                if not isinstance(cmd, dict) or "cmd" not in cmd:
                    return "The browser agent got confused — nothing further was done."
                if cmd == last_cmd:
                    repeats += 1
                else:
                    repeats = 0
                last_cmd = cmd
                if repeats >= 3:
                    try:
                        where = f"{page.url} ({page.title()})"
                    except Exception:
                        where = "?"
                    return (
                        f"I got stuck repeating the same action on {where} — the page is probably "
                        "a modal or dynamic wall that needs a human touch. Give me a hint or do "
                        "that one step manually, then tell me to continue."
                    )
                c = cmd.get("cmd")
                if c == "answer":
                    ans = str(cmd.get("text", "")).strip() or "Done."
                    if files:
                        ans += "\n\nSaved files:\n- " + "\n- ".join(str(f) for f in files)
                    return ans
                if c == "goto":
                    u = str(cmd.get("url", "")).strip()
                    if not u.startswith("http"):
                        u = "https://" + u
                    progress(f"🧭 navigating to {u}")
                    page.goto(u, timeout=40000, wait_until="domcontentloaded")
                    _wait_ready(page)
                    history.append(f"goto {u}")
                    continue
                if c == "screenshot":
                    SHOTS_DIR.mkdir(exist_ok=True)
                    path = SHOTS_DIR / f"jorge_{time.strftime('%Y%m%d_%H%M%S')}.png"
                    page.screenshot(path=str(path))
                    files.append(path)
                    progress(f"📸 screenshot saved: {path.name}")
                    history.append("screenshot saved")
                    continue
                if c == "click":
                    i = int(cmd.get("i", -1))
                    try:
                        with page.expect_download(timeout=8000) as dl_info:
                            page.query_selector_all("button, a, [role=button], [role=link]")[i].click()
                        dl = dl_info.value
                        dest = DOWNLOADS_DIR / (Path(dl.suggested_filename or "download").name)
                        dl.save_as(str(dest))
                        files.append(dest)
                        history.append(f"click {i} (downloaded {dest.name})")
                        progress(f"⬇ downloaded: {dest.name}")
                    except PWTimeout:
                        try:
                            page.query_selector_all("button, a, [role=button], [role=link]")[i].click()
                            history.append(f"click {i}")
                            progress(f"🖱 clicked element {i}")
                        except Exception:
                            history.append(f"click {i} FAILED")
                            progress(f"✗ click {i} failed")
                    except Exception:
                        history.append(f"click {i} FAILED")
                        progress(f"✗ click {i} failed")
                    _settle(page)
                    continue
                if c == "type":
                    i = int(cmd.get("i", -1))
                    val = str(cmd.get("value", ""))
                    try:
                        page.query_selector_all("input, textarea")[i].fill(val)
                        history.append(f"typed into {i}")
                        progress(f"⌨ typed into input {i}")
                    except Exception:
                        history.append(f"type {i} FAILED")
                        progress(f"✗ type {i} failed")
                    continue
                history.append(f"unknown cmd {c!r}")
            try:
                last = f"Last page: {page.url} ({page.title()})"
            except Exception:
                last = "Last page: ?"
            tried = "; ".join(history[-6:]) or "nothing"
            return (
                f"I couldn't finish that in the browser within {MAX_STEPS} steps. {last}. "
                f"Steps tried: {tried}. Tell me what's left and I'll continue."
            )
    except Exception as e:
        if type(e).__name__ == "MissingInfo":
            raise
        return f"⚠ Browser error: {e}"


def _safe_domain(site: str) -> str:
    return _domain(site) or site.strip()