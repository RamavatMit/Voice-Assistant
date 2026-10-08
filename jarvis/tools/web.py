"""Chrome automation: one controlled Chrome window for opening pages, searching, and multi-step tasks on ANY website.

Perception: Playwright's AI accessibility snapshot (every element has a stable [ref=eN]), cut to the visible area.
Decision:   Gemini flash-lite via native tool calling (1M-token context handles real pages on the free tier).
Safety:     purchase/booking clicks need a spoken "yes"; passwords, OTPs and card numbers are never typed by the agent.
No site-specific code: the same loop works on every website.
"""
import logging
import re
import time
import urllib.parse
import webbrowser

from . import loop, tool
from .. import config

log = logging.getLogger("jarvis.web")

_playwright = None
_context = None
_page = None

COMMIT_WORDS = re.compile(r"\b(pay|payment|place order|buy now|confirm (booking|order|payment)|book now|proceed to pay|"
                          r"make payment|complete (purchase|booking|order)|checkout|submit order)\b", re.I)
SENSITIVE_FIELD = re.compile(r"password|passcode|otp|one.time|cvv|cvc|card.?number|credit.?card|debit.?card|expiry|"
                             r"\bpin\b|upi|security code|aadhaar|pan.?number", re.I)
SNAPSHOT_CHARS = 30000  # ~7.5k tokens

SYSTEM = """You are a web-browsing agent controlling a real browser for your user ("Sir") to complete a task.
Each turn you get the task, your previous actions, and the CURRENT page snapshot (YAML accessibility tree; [ref=eN] identifies elements).
Rules:
- Act with the tools; use refs from the CURRENT snapshot only. You may call up to 3 tools per turn when they don't change the page (e.g. fill several fields).
- If the task needs a website that isn't open, navigate to it (or use the site's own search box).
- Autocomplete fields: after a type result says AUTOCOMPLETE, your VERY NEXT action MUST be clicking the matching
  suggestion - typed text is NOT selected until its suggestion is clicked. Then check the field shows the right value.
- If a form keeps failing, try another route (the site's search, a menu, keyboard keys, or a search-results URL).
- Read the action results: "PAGE DID NOT CHANGE" or "BLOCKED" means that approach failed - try something different.
- Dates: open the date picker and click the day; navigate months if needed.
- Media ("play X"): search for it on the site and click the best matching result.
- Dismiss popups, cookie banners and sign-in nags that block progress (close / "not now"), unless sign-in is required.
- If information is missing (names, preferences, which option to choose), use ask_user - one short, friendly question.
- Never invent personal data. Never type passwords/OTP/card details (the user will type those).
- Stop with finish when the goal is reached, or when only payment/login remains for the user. The summary is SPOKEN:
  1-2 natural sentences with the useful facts (names, times, prices), no URLs.
- If stuck after several attempts, finish with success=false and explain briefly."""


_REF_PARAM = {"type": "string", "description": "element ref like e12"}
WEB_TOOLS = [
    loop.fn("click", "Click an element.", {"ref": _REF_PARAM}),
    loop.fn("type", "Clear a field and type text into it; submit=true presses Enter after.",
            {"ref": _REF_PARAM, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["ref", "text"]),
    loop.fn("select_option", "Choose an option in a <select> dropdown.", {"ref": _REF_PARAM, "option": {"type": "string"}}),
    loop.fn("press_key", "Press a keyboard key, e.g. Enter, Escape, ArrowDown, Tab.", {"key": {"type": "string"}}),
    loop.fn("scroll", "Scroll the page.", {"direction": {"type": "string", "enum": ["up", "down"]}}),
    loop.fn("navigate", "Go to a URL.", {"url": {"type": "string"}}),
    loop.fn("go_back", "Go back to the previous page.", {}),
]


def _browser():
    """Jarvis's Chrome window (own persistent profile in chrome_profile/, so logins stick). Falls back to Edge/Chromium."""
    global _playwright, _context, _page
    if _context is not None:
        return _context
    from playwright.sync_api import sync_playwright
    _playwright = _playwright or sync_playwright().start()
    for channel in ("chrome", "msedge", None):
        try:
            _context = _playwright.chromium.launch_persistent_context(
                config.BROWSER_PROFILE, headless=False, no_viewport=True, args=["--start-maximized"],
                **({"channel": channel} if channel else {}))
            break
        except Exception as e:
            log.warning("Browser channel %s unavailable: %s", channel, e)
    else:
        raise RuntimeError("no Chromium-based browser available (run: playwright install chromium)")

    def on_close(_):
        global _context, _page
        _context = _page = None

    def on_page(page):
        global _page
        _page = page  # follow new tabs/popups

    _context.on("close", on_close)
    _context.on("page", on_page)
    _page = _context.pages[0] if _context.pages else _context.new_page()
    return _context


def safe_url(url: str) -> str:
    """Only web URLs (blocks file:, javascript:, ms-settings: and credential-in-URL tricks)."""
    url = url.strip()
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)", url)
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        raise ValueError(f"refusing non-web URL scheme '{scheme.group(1)}'")
    if not scheme:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname or "@" in parsed.netloc or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname):
        raise ValueError(f"refusing unsafe URL '{url}'")
    return url


def _show(url: str) -> str:
    """Open a URL in Jarvis's Chrome (new tab if the current one is in use); default browser if Chrome can't start."""
    global _page
    try:
        _browser()
        page = _page if _page.url in ("about:blank", "chrome://newtab/") else _context.new_page()
        _page = page
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.bring_to_front()
        return f"Opened {page.title() or url} in Chrome"
    except Exception as e:
        log.warning("Controlled Chrome unavailable (%s); using the default browser", e)
        webbrowser.open(url)
        return f"Opened {url} in the default browser"


@tool("Open a website in Chrome.", {"url": {"type": "string"}})
def open_url(ctx, url: str) -> str:
    return _show(safe_url(url))


@tool("Search the web in Chrome (news, weather, prices, anything live).", {"query": {"type": "string"}})
def web_search(ctx, query: str) -> str:
    _show(config.SEARCH_URL.format(query=urllib.parse.quote_plus(query)))
    return SEARCH_RESULT.format(query=query)


SEARCH_RESULT = ("Search results for '{query}' are open in Chrome. You have NOT seen them: do not state any facts from "
                 "them. Either tell the user the results are open, or call look_at_screen / browser_task to read them.")

_BOX = re.compile(r" \[box=(-?\d+),(-?\d+),(\d+),(\d+)\]")
_REF = r"( \[ref=[a-z0-9]+\])?"


def compact_snapshot(snapshot: str, view_top: float | None = None, view_bottom: float | None = None) -> str:
    """Shrink an AI aria snapshot (with boxes) to what matters: elements on/near the screen, minus URLs,
    nameless wrappers, hidden elements and icon glyphs. Huge pages (~120k tokens) drop to a few thousand."""
    out, stack = [], []  # stack: (indent, visible) of ancestors
    more_below = False
    for line in snapshot.splitlines():
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        visible = stack[-1][1] if stack else True
        box = _BOX.search(line)
        if box:
            x, y, w, h = map(int, box.groups())
            on_page = w > 0 and h > 0 and x + w > 0
            in_view = view_top is None or (y + h >= view_top and y <= view_bottom)
            more_below |= on_page and view_bottom is not None and y > view_bottom
            visible = visible and on_page and in_view
            line = _BOX.sub("", line)
        stack.append((indent, visible))
        stripped = line.strip()
        if not visible or stripped.startswith(("- /url:", "- /placeholder:")):
            continue
        if re.fullmatch(r"- (generic|group|list|listitem|none|presentation|separator)" + _REF + r"( \[cursor=pointer\])?:?", stripped):
            continue
        text = re.fullmatch(r"- (generic|text|img|paragraph)" + _REF + r"( \[cursor=pointer\])?: ?(.*)", stripped)
        if text and not re.search(r"[A-Za-z0-9ऀ-ॿ]", text.group(4) or ""):
            continue
        if stripped.startswith("- img") and '"' not in stripped:
            continue
        line = line.replace(" [cursor=pointer]", "").replace(" [active]", "")
        out.append(line if len(line) <= 200 else line[:197] + "...")
    text = "\n".join(out)
    if len(text) > SNAPSHOT_CHARS:
        text = text[:SNAPSHOT_CHARS] + "\n..."
    if more_below:
        text += "\n(more content below - scroll down to see it)"
    return text


def read_page(page) -> str:
    """Compact snapshot of the visible area plus one more screen below (what a person would look at)."""
    top, height = page.evaluate("[window.scrollY, window.innerHeight]")
    return compact_snapshot(page.aria_snapshot(mode="ai", boxes=True), top - 100, top + 2 * height)


def _settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    time.sleep(0.6)  # let client-side frameworks render


def _element_info(locator) -> dict:
    return locator.evaluate("""el => ({
        type: (el.getAttribute('type') || '').toLowerCase(),
        autocomplete: el.getAttribute('autocomplete') || '',
        suggests: el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete') || !!el.getAttribute('list'),
        label: [el.getAttribute('aria-label'), el.getAttribute('name'), el.getAttribute('id'), el.getAttribute('placeholder'),
                el.labels && el.labels[0] ? el.labels[0].innerText : '', el.innerText].filter(Boolean).join(' ').slice(0, 200)
    })""")


def _click(locator) -> None:
    """Click; if an overlay (label, transparent div) intercepts the pointer, click through it."""
    try:
        locator.click(timeout=2500)
    except Exception:
        locator.click(timeout=2500, force=True)


def _act(ctx, name: str, args: dict) -> tuple[str, bool]:
    """Execute one browser action -> (result for the action log, whether more actions may follow without re-reading)."""
    page = _page
    url_before = page.url
    if name in ("click", "type", "select_option"):
        locator = page.locator(f"aria-ref={args['ref']}")
        info = _element_info(locator)
        if name == "click":
            if COMMIT_WORDS.search(info["label"]) and not ctx.confirm(
                    f"Sir, this will {info['label'].strip()[:60]}. Should I go ahead?"):
                return "CANCELLED by user", False
            _click(locator)
            result = f"clicked {info['label'][:60]!r}"
        elif name == "type":
            if info["type"] == "password" or info["autocomplete"].startswith("cc-") or SENSITIVE_FIELD.search(info["label"]):
                ctx.say("This one needs your private details, so please type it yourself in Chrome.")
                ctx.confirm("Just say yes when you're done.")
                return "user filled the sensitive field manually", False
            try:
                locator.fill(args["text"], timeout=2500)  # focuses the field itself; no pointer needed
            except Exception:  # custom widgets: focus, then type key by key (fires autocomplete events)
                _click(locator)
                page.keyboard.press("Control+A")
                page.keyboard.type(args["text"], delay=30)
            if args.get("submit"):
                page.keyboard.press("Enter")
            if info["suggests"] and not args.get("submit"):
                return f"typed {args['text']!r} into {info['label'][:40]!r} - AUTOCOMPLETE: pick the matching suggestion next", False
            result = f"typed {args['text']!r} into {info['label'][:40]!r}"
        else:
            locator.select_option(label=args["option"], timeout=6000)
            result = f"selected {args['option']!r}"
    elif name == "press_key":
        page.keyboard.press(args["key"])
        result = f"pressed {args['key']}"
    elif name == "scroll":
        page.mouse.wheel(0, -700 if args.get("direction") == "up" else 700)
        result = f"scrolled {args.get('direction', 'down')}"
    elif name == "navigate":
        page.goto(safe_url(args["url"]), wait_until="domcontentloaded", timeout=30000)
        return f"opened {page.url}", False
    elif name == "go_back":
        page.go_back(timeout=15000)
        return "went back", False
    else:
        return f"ERROR: unknown action {name}", False
    return result, page.url == url_before and _page is page  # page changed -> re-read before acting again


def _observe() -> tuple:
    page = _page
    _settle(page)
    snapshot = read_page(page)
    return (page.url, snapshot), f"CURRENT PAGE: {page.title()!r} {page.url}\n{snapshot}", None


@tool("Do anything on a website in Chrome that needs clicking, typing or reading: play media, book, shop, fill forms, "
      "compare prices, read results, etc. Continues in the current Chrome tab when no start_url is given.",
      {"goal": {"type": "string", "description": "Full task with every detail the user gave"},
       "start_url": {"type": "string", "description": "Website to start on, if one is known"}},
      required=["goal"], slow=True)
def browser_task(ctx, goal: str, start_url: str = "") -> str:
    if start_url:
        _show(safe_url(start_url))
    _browser()
    _page.bring_to_front()
    return loop.run(ctx, goal, SYSTEM, WEB_TOOLS, _observe, lambda name, args: _act(ctx, name, args), config.MAX_WEB_STEPS)
