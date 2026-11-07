"""
NLP Test Runner — Task 2
========================
Takes plain-English test cases and executes them in a browser.
Uses Google Gemini to translate natural language into browser actions.

Architecture:
  User writes: "Log in as a customer and verify the dashboard is shown"
       ↓
  Gemini AI:   Understands intent, generates a sequence of JSON actions
       ↓
  Playwright:  Executes each action in a real browser
       ↓
  Reporter:    PASS / FAIL with detailed log

Usage:
  python runner.py --url "https://phptravels.net" \\
                   --test "Log in as a customer and verify the dashboard" \\
                   --email "user@travellermail.com" --password "demouser" \\
                   --gemini-key "YOUR_API_KEY"

  # Or use a test file:
  python runner.py --test-file tests.json

  # Or set GEMINI_API_KEY env var and skip --gemini-key
"""

import argparse
import json
import os
import sys
import time
import re
import traceback
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import sync_playwright, Page, Browser, Playwright, TimeoutError as PWTimeout
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

load_dotenv()

# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class TestConfig:
    url: str
    test_description: str
    credentials: dict[str, str] = field(default_factory=dict)
    headless: bool = False
    timeout_ms: int = 15000

@dataclass
class StepResult:
    action: str
    status: str          # "pass" | "fail" | "skip"
    detail: str = ""
    screenshot: str = ""

@dataclass
class TestResult:
    test_name: str
    passed: bool
    steps: list[StepResult] = field(default_factory=list)
    error: str = ""
    duration_s: float = 0.0


# ─── Gemini prompt builder ────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a web automation expert. Your job is to translate a plain-English
test case into a JSON list of browser actions that Playwright can execute.

You will receive:
- url: the starting URL
- test: the plain-English test description
- credentials: optional dict with 'email', 'password', 'username', etc.
- page_snapshot: a simplified text snapshot of the current page (labels, links, inputs)

You must reply with ONLY valid JSON — an array of action objects. No explanation text.

Each action object has this shape:
{
  "action": "<action_type>",
  ...action-specific fields...
}

Supported action types and their fields:

  navigate:        { "url": "https://..." }
  click:           { "selector": "...", "text": "..." }   // use 'text' for link/button text, 'selector' for CSS
  fill:            { "selector": "...", "value": "..." }  // fill a text input; use label text as selector hint
  fill_by_label:   { "label": "...", "value": "..." }     // find input by associated label text
  select:          { "label": "...", "value": "..." }     // select dropdown option by visible text
  click_text:      { "text": "..." }                      // click any element with this visible text
  wait_for_text:   { "text": "...", "timeout_ms": 10000 } // assert text appears on page
  wait_for_url:    { "pattern": "..." }                   // assert URL contains this substring
  assert_visible:  { "text": "..." }                      // assert element with text is visible
  assert_title:    { "contains": "..." }                  // assert page title contains string
  scroll_down:     {}
  screenshot:      { "name": "..." }

IMPORTANT RULES:
- Use fill_by_label when filling form fields (more resilient than CSS selectors)
- Use click_text for buttons and links when you know the text
- Always add assert/wait steps after actions to verify results
- For login flows: navigate → fill credentials → click login button → assert dashboard/success
- Keep selectors simple. Prefer text-based over attribute-based selectors.
- If the test says "verify X", add an assert_visible or wait_for_text for X
- For credential fields: use the credentials dict values if provided
"""

def build_action_prompt(config: TestConfig, page_snapshot: str) -> str:
    return json.dumps({
        "url": config.url,
        "test": config.test_description,
        "credentials": config.credentials,
        "page_snapshot": page_snapshot[:3000],  # limit snapshot size
    }, indent=2)


# ─── Playwright page snapshot ─────────────────────────────────────────────────

def get_page_snapshot(page: Page) -> str:
    """Extract a text summary of the page for the AI to understand context."""
    try:
        snapshot = page.evaluate("""() => {
            const parts = [];
            parts.push('TITLE: ' + document.title);
            parts.push('URL: ' + window.location.href);
            
            // Links
            const links = Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.innerText.trim())
                .filter(t => t.length > 0 && t.length < 60)
                .slice(0, 20);
            if (links.length) parts.push('LINKS: ' + links.join(' | '));
            
            // Buttons
            const buttons = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button]'))
                .map(b => (b.innerText || b.value || '').trim())
                .filter(t => t.length > 0)
                .slice(0, 15);
            if (buttons.length) parts.push('BUTTONS: ' + buttons.join(' | '));
            
            // Form inputs with labels
            const inputs = Array.from(document.querySelectorAll('input:not([type=hidden]), select, textarea'))
                .map(inp => {
                    let label = inp.placeholder || inp.getAttribute('aria-label') || '';
                    const id = inp.id;
                    if (id) {
                        const lbl = document.querySelector('label[for="' + id + '"]');
                        if (lbl) label = lbl.innerText.trim() || label;
                    }
                    return label || inp.name || inp.type;
                })
                .filter(t => t)
                .slice(0, 15);
            if (inputs.length) parts.push('INPUTS: ' + inputs.join(' | '));
            
            // Headings
            const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
                .map(h => h.innerText.trim())
                .filter(t => t.length > 0)
                .slice(0, 8);
            if (headings.length) parts.push('HEADINGS: ' + headings.join(' | '));
            
            return parts.join('\\n');
        }""")
        return snapshot or "Page loaded (no snapshot available)"
    except Exception as e:
        return f"Snapshot error: {e}"


# ─── Gemini AI planner ────────────────────────────────────────────────────────

def plan_actions(client: genai.Client, config: TestConfig, page_snapshot: str) -> list[dict]:
    """Call Gemini to get a list of browser actions for the test."""
    prompt = build_action_prompt(config, page_snapshot)
    
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
        ),
        contents=prompt,
    )
    
    raw = response.text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    raw = raw.strip()
    
    return json.loads(raw)


# ─── Playwright action executor ───────────────────────────────────────────────

class ActionExecutor:
    def __init__(self, page: Page, timeout_ms: int = 15000, screenshot_dir: str = "screenshots"):
        self.page = page
        self.timeout = timeout_ms
        self.screenshot_dir = screenshot_dir
        os.makedirs(screenshot_dir, exist_ok=True)

    def execute(self, action: dict) -> StepResult:
        act = action.get("action", "")
        try:
            detail = self._dispatch(act, action)
            return StepResult(action=str(action), status="pass", detail=detail)
        except Exception as e:
            # Take failure screenshot
            ts = int(time.time())
            path = os.path.join(self.screenshot_dir, f"fail_{act}_{ts}.png")
            try:
                self.page.screenshot(path=path)
            except Exception:
                pass
            return StepResult(action=str(action), status="fail",
                              detail=str(e), screenshot=path)

    def _dispatch(self, act: str, params: dict) -> str:
        if act == "navigate":
            self.page.goto(params["url"], wait_until="domcontentloaded", timeout=30000)
            return f"Navigated to {params['url']}"

        elif act == "click":
            if "text" in params:
                self.page.get_by_text(params["text"], exact=False).first.click(timeout=self.timeout)
                return f"Clicked element with text '{params['text']}'"
            else:
                self.page.locator(params["selector"]).first.click(timeout=self.timeout)
                return f"Clicked '{params['selector']}'"

        elif act == "click_text":
            text = params["text"]
            # Try multiple strategies
            try:
                self.page.get_by_role("button", name=text, exact=False).first.click(timeout=5000)
            except Exception:
                try:
                    self.page.get_by_role("link", name=text, exact=False).first.click(timeout=5000)
                except Exception:
                    self.page.get_by_text(text, exact=False).first.click(timeout=self.timeout)
            return f"Clicked '{text}'"

        elif act == "fill":
            selector = params["selector"]
            value = params["value"]
            self.page.locator(selector).first.fill(value, timeout=self.timeout)
            return f"Filled '{selector}' with '{value}'"

        elif act == "fill_by_label":
            label = params["label"]
            value = params["value"]
            self.page.get_by_label(label, exact=False).first.fill(value, timeout=self.timeout)
            return f"Filled field '{label}' with '{_mask(value)}'"

        elif act == "select":
            label = params["label"]
            value = params["value"]
            self.page.get_by_label(label, exact=False).select_option(label=value, timeout=self.timeout)
            return f"Selected '{value}' in '{label}'"

        elif act == "wait_for_text":
            text = params["text"]
            t = params.get("timeout_ms", self.timeout)
            self.page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=t)
            return f"Found text '{text}' on page"

        elif act == "wait_for_url":
            pattern = params["pattern"]
            self.page.wait_for_url(f"**{pattern}**", timeout=self.timeout)
            return f"URL contains '{pattern}'"

        elif act == "assert_visible":
            text = params["text"]
            locator = self.page.get_by_text(text, exact=False).first
            locator.wait_for(state="visible", timeout=self.timeout)
            assert locator.is_visible(), f"Element with text '{text}' is not visible"
            return f"Verified '{text}' is visible"

        elif act == "assert_title":
            contains = params["contains"]
            title = self.page.title()
            assert contains.lower() in title.lower(), \
                f"Title '{title}' does not contain '{contains}'"
            return f"Title contains '{contains}'"

        elif act == "scroll_down":
            self.page.evaluate("window.scrollBy(0, 500)")
            time.sleep(0.5)
            return "Scrolled down"

        elif act == "screenshot":
            name = params.get("name", f"step_{int(time.time())}")
            path = os.path.join(self.screenshot_dir, f"{name}.png")
            self.page.screenshot(path=path)
            return f"Screenshot saved: {path}"

        else:
            return f"Unknown action '{act}' — skipped"


def _mask(value: str) -> str:
    """Mask sensitive values like passwords."""
    if len(value) <= 2:
        return "***"
    return value[0] + "*" * (len(value) - 2) + value[-1]


# ─── Main test runner ─────────────────────────────────────────────────────────

class NLPTestRunner:
    def __init__(self, gemini_key: str, headless: bool = False):
        self.client = genai.Client(api_key=gemini_key)
        self.headless = headless

    def run(self, config: TestConfig) -> TestResult:
        start = time.time()
        result = TestResult(test_name=config.test_description)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context(viewport={"width": 1366, "height": 768})
            page = context.new_page()
            executor = ActionExecutor(page, timeout_ms=config.timeout_ms)

            try:
                # Step 1: Load the page and snapshot it
                print(f"\n  🌐 Loading {config.url}...")
                page.goto(config.url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
                snapshot = get_page_snapshot(page)
                print(f"  📄 Page: {page.title()}")

                # Step 2: Ask Gemini for the action plan
                print(f"  🤖 Asking Gemini to plan actions for: '{config.test_description}'...")
                actions = plan_actions(self.client, config, snapshot)
                print(f"  📋 Gemini planned {len(actions)} actions:")
                for i, a in enumerate(actions, 1):
                    print(f"     {i}. {a}")

                # Step 3: Execute each action
                print(f"\n  ▶  Executing...")
                for action in actions:
                    step = executor.execute(action)
                    result.steps.append(step)
                    icon = "✅" if step.status == "pass" else "❌"
                    print(f"     {icon} [{step.status.upper()}] {step.detail}")
                    if step.status == "fail":
                        result.passed = False
                        result.error = step.detail
                        break

                if not result.error:
                    result.passed = True

            except Exception as e:
                result.passed = False
                result.error = traceback.format_exc()
                print(f"  ❌ Unexpected error: {e}")
            finally:
                # Final screenshot
                try:
                    ts = int(time.time())
                    path = f"screenshots/final_{ts}.png"
                    os.makedirs("screenshots", exist_ok=True)
                    page.screenshot(path=path)
                    print(f"  📸 Final screenshot: {path}")
                except Exception:
                    pass
                browser.close()

        result.duration_s = time.time() - start
        return result


def print_report(results: list[TestResult]) -> int:
    """Print a formatted report and return exit code."""
    print("\n")
    print("╔" + "═" * 70 + "╗")
    print("║" + "  NLP TEST RUNNER — RESULTS".center(70) + "║")
    print("╠" + "═" * 70 + "╣")

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    for r in results:
        icon = "✅ PASS" if r.passed else "❌ FAIL"
        line = f"  {icon}  {r.test_name}"
        print("║" + line[:70].ljust(70) + "║")
        timing = f"       ⏱  {r.duration_s:.1f}s"
        print("║" + timing.ljust(70) + "║")
        if not r.passed and r.error:
            err = r.error.strip().splitlines()[-1][:60]
            print("║" + f"       ↳  {err}".ljust(70) + "║")
        print("║" + " " * 70 + "║")

    print("╠" + "═" * 70 + "╣")
    summary = f"  Total: {len(results)}   Passed: {passed}   Failed: {failed}"
    print("║" + summary.ljust(70) + "║")
    print("╚" + "═" * 70 + "╝")

    return 0 if failed == 0 else 1


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="NLP Test Runner — turns plain-English test cases into browser automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single test case inline
  python runner.py \\
    --url "https://phptravels.net" \\
    --test "Log in as a customer and verify the dashboard is shown" \\
    --email "user@travellermail.com" --password "demouser"

  # Multiple tests from JSON file
  python runner.py --test-file tests.json

  # Different site, no credentials
  python runner.py --url "https://example.com" \\
    --test "Verify the homepage loads and shows a welcome message"
"""
    )
    parser.add_argument("--url", help="Starting URL for the test")
    parser.add_argument("--test", help="Plain-English test description")
    parser.add_argument("--test-file", help="JSON file with multiple test cases")
    parser.add_argument("--email", help="Login email (optional credential)")
    parser.add_argument("--password", help="Login password (optional credential)")
    parser.add_argument("--username", help="Login username (optional credential)")
    parser.add_argument("--gemini-key", default=os.getenv("GEMINI_API_KEY"),
                        help="Gemini API key (or set GEMINI_API_KEY env var)")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser in headless mode")
    parser.add_argument("--timeout", type=int, default=15000,
                        help="Element wait timeout in ms (default: 15000)")
    args = parser.parse_args()

    if not args.gemini_key:
        print("❌ Error: Gemini API key required. Use --gemini-key or set GEMINI_API_KEY env var.")
        sys.exit(1)

    runner = NLPTestRunner(gemini_key=args.gemini_key, headless=args.headless)
    test_configs: list[TestConfig] = []

    if args.test_file:
        with open(args.test_file) as f:
            data = json.load(f)
        for item in data:
            creds = item.get("credentials", {})
            test_configs.append(TestConfig(
                url=item["url"],
                test_description=item["test"],
                credentials=creds,
                headless=args.headless,
                timeout_ms=args.timeout,
            ))
    elif args.url and args.test:
        creds = {}
        if args.email:    creds["email"] = args.email
        if args.password: creds["password"] = args.password
        if args.username: creds["username"] = args.username
        test_configs.append(TestConfig(
            url=args.url,
            test_description=args.test,
            credentials=creds,
            headless=args.headless,
            timeout_ms=args.timeout,
        ))
    else:
        parser.print_help()
        sys.exit(1)

    print("╔" + "═" * 70 + "╗")
    print("║" + "  NLP Test Runner — AI-Powered Browser Automation".center(70) + "║")
    print("╚" + "═" * 70 + "╝")
    print(f"  Tests to run : {len(test_configs)}")
    print(f"  Mode         : {'Headless' if args.headless else 'Headed (visible browser)'}")
    print(f"  AI Model     : gemini-2.0-flash")

    results: list[TestResult] = []
    for i, config in enumerate(test_configs, 1):
        print(f"\n{'─'*72}")
        print(f"TEST {i}/{len(test_configs)}: {config.test_description}")
        print(f"URL: {config.url}")
        print(f"{'─'*72}")
        result = runner.run(config)
        results.append(result)

    sys.exit(print_report(results))


if __name__ == "__main__":
    main()
