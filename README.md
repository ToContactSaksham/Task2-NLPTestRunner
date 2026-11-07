# Task 2 — AI-Powered NLP Test Runner

A tool that takes plain-English test cases and runs them automatically in a browser. Uses **Google Gemini** to translate natural language into browser actions, and **Playwright** to execute them.

## How It Works

```
"Log in as a customer and verify the dashboard is shown"
                          ↓
         ┌─────────────────────────────┐
         │   Load page + get snapshot  │  (page title, links, buttons, inputs)
         └─────────────────────────────┘
                          ↓
         ┌─────────────────────────────┐
         │   Gemini AI plans actions   │  → JSON action list
         └─────────────────────────────┘
                          ↓
         ┌─────────────────────────────┐
         │   Playwright executes each  │  fill_by_label, click_text, assert_visible...
         └─────────────────────────────┘
                          ↓
                   PASS / FAIL report
```

## Setup

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Get a Gemini API key
- Get one free at: https://aistudio.google.com/app/apikey
- Set it as env var: `$env:GEMINI_API_KEY = "your_key_here"` (PowerShell)

### 3. Configure `.env` (optional)
```
GEMINI_API_KEY=your_gemini_api_key_here
```

## Running Tests

### Single test case (inline)
```bash
python runner.py \
  --url "https://phptravels.net" \
  --test "Log in as a customer and verify the dashboard is shown" \
  --email "user@travellermail.com" \
  --password "demouser"
```

### Multiple test cases from JSON file
```bash
python runner.py --test-file tests.json
```

### On a different website (the tool is not hardcoded to any site)
```bash
python runner.py \
  --url "https://your-site.com" \
  --test "Register a new account and verify confirmation email message" \
  --email "test@example.com" \
  --password "securepass"
```

### Headless mode (CI-friendly)
```bash
python runner.py --test-file tests.json --headless
```

### Full options
```
  --url          Starting URL for the test
  --test         Plain-English test description
  --test-file    JSON file with multiple test cases
  --email        Login email credential
  --password     Login password credential
  --username     Login username credential
  --gemini-key   Gemini API key (or set GEMINI_API_KEY env var)
  --headless     Run browser without a visible window
  --timeout      Element wait timeout in ms (default: 15000)
```

## Test File Format (`tests.json`)

```json
[
  {
    "url": "https://your-site.com",
    "test": "Log in as a customer and verify the dashboard is shown",
    "credentials": {
      "email": "user@example.com",
      "password": "password123"
    }
  },
  {
    "url": "https://your-site.com",
    "test": "Search for hotels in Paris and verify results appear",
    "credentials": {}
  }
]
```

## Example Output

```
╔══════════════════════════════════════════════════════════════════════════╗
║               NLP Test Runner — AI-Powered Browser Automation            ║
╚══════════════════════════════════════════════════════════════════════════╝
  Tests to run : 1
  Mode         : Headed (visible browser)
  AI Model     : gemini-2.0-flash

────────────────────────────────────────────────────────────────────────────
TEST 1/1: Log in as a customer and verify the dashboard is shown
URL: https://phptravels.net
────────────────────────────────────────────────────────────────────────────

  🌐 Loading https://phptravels.net...
  📄 Page: PHPTRAVELS | Travel Technology for Everyone
  🤖 Asking Gemini to plan actions for: 'Log in as a customer and verify the dashboard'...
  📋 Gemini planned 6 actions:
     1. {'action': 'navigate', 'url': 'https://phptravels.net/login'}
     2. {'action': 'fill_by_label', 'label': 'Email', 'value': 'user@travellermail.com'}
     3. {'action': 'fill_by_label', 'label': 'Password', 'value': 'demouser'}
     4. {'action': 'click_text', 'text': 'Login'}
     5. {'action': 'wait_for_url', 'pattern': 'dashboard'}
     6. {'action': 'assert_visible', 'text': 'Dashboard'}

  ▶  Executing...
     ✅ [PASS] Navigated to https://phptravels.net/login
     ✅ [PASS] Filled field 'Email' with 'u***@travellermail.com'
     ✅ [PASS] Filled field 'Password' with 'd******r'
     ✅ [PASS] Clicked 'Login'
     ✅ [PASS] URL contains 'dashboard'
     ✅ [PASS] Verified 'Dashboard' is visible
  📸 Final screenshot: screenshots/final_1699999999.png

╔══════════════════════════════════════════════════════════════════════════╗
║                     NLP TEST RUNNER — RESULTS                            ║
╠══════════════════════════════════════════════════════════════════════════╣
║  ✅ PASS  Log in as a customer and verify the dashboard is shown         ║
║       ⏱  12.3s                                                           ║
╠══════════════════════════════════════════════════════════════════════════╣
║  Total: 1   Passed: 1   Failed: 0                                        ║
╚══════════════════════════════════════════════════════════════════════════╝
```

## Approach & Design Decisions

### Why Gemini for NLP?
Gemini Flash is fast and understands web UI context well. The AI receives a **page snapshot** (title, links, buttons, visible input labels) alongside the test description, so it can make informed decisions about selectors and flow — not just blindly guess.

### Why Playwright?
- `get_by_label()`, `get_by_role()`, `get_by_text()` are semantic locators that mirror how users think about UI
- Built-in auto-waiting reduces flakiness
- Much faster than Selenium for Python

### Resilience Strategies
- **Label-based filling** (`fill_by_label`) uses Playwright's `get_by_label()` — works even if `id`/`name` attributes change
- **Text-based clicking** (`click_text`) tries button role → link role → text match
- **Assertions after every action** catch failures fast

### Limitations
- **Dynamic SPAs** may need extra wait time — increase `--timeout`
- **CAPTCHA** will block login automation on protected sites
- **Multi-step flows with state** (e.g., booking checkouts) are harder for the AI to plan in one shot
- **Ambiguous test descriptions** — be specific: "click the blue Login button" is better than "click login"
- **Gemini API rate limits** apply (free tier: ~15 req/min)
- Screenshots are saved in `./screenshots/` for debugging failures
