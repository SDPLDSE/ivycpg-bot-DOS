"""
IvyCPG Daily Automation Bot — GitHub Actions Version (Headless)
"""

import asyncio
import requests
import logging
import sys
import os
import subprocess
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# ---------------------------------------------
# CONFIG — values come from GitHub Secrets
# ---------------------------------------------

USERNAME           = os.environ.get("IVYDMS_USERNAME", "SABARI_2194")
PASSWORD           = os.environ.get("IVYDMS_PASSWORD", "Sabari@71")
SITE_URL           = "https://cloud01-in.ivydms.com/web/DMS/Welcome"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

SCREENSHOT_PATH    = "daily_screenshot.png"

# ---------------------------------------------
# LOGGING
# ---------------------------------------------

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

# Crop: skip sidebar and top header
CROP_X      = 215
CROP_Y      = 155
CROP_WIDTH  = 1065
CROP_HEIGHT = 620

# ---------------------------------------------
# TELEGRAM
# ---------------------------------------------

def get_ist_time():
    # Method 1: Use TZ environment variable approach
    os.environ["TZ"] = "Asia/Kolkata"
    try:
        import time
        time.tzset()
    except Exception:
        pass

    # Method 2: Use date shell command as backup
    try:
        result = subprocess.run(
            ["date", "+%d %b %Y %I:%M %p IST"],
            env={**os.environ, "TZ": "Asia/Kolkata"},
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        log.error(f"Shell date failed: {e}")

    # Method 3: Manual UTC+5:30 offset calculation
    utc_now = datetime.utcnow()
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    now = ist_now.strftime("%d %b %Y %I:%M %p IST")
    from datetime import timedelta
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    return ist_now.strftime("%d %b %Y %I:%M %p IST")

def send_to_telegram():
    ist_time = get_ist_time()
    caption  = f"Daily Order Summary\n{ist_time}"
    log.info(f"=== CAPTION: {caption} ===")
    try:
        with open(SCREENSHOT_PATH, "rb") as img:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": img},
                timeout=30
            )
        if resp.ok:
            log.info("Screenshot sent to Telegram.")
        else:
            log.error(f"Telegram Error: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

# ---------------------------------------------
# HELPERS
# ---------------------------------------------

async def js_click_title(page, title):
    log.info(f"Clicking menu: {title}")
    await page.evaluate(f"""
        () => {{
            const el = document.querySelector('a[title="{title}"]');
            if (el) el.click();
            else throw new Error('Not found: title={title}');
        }}
    """)
    await page.wait_for_timeout(2500)

async def find_search_frame(page):
    for attempt in range(40):
        for frame in page.frames:
            try:
                found = await frame.evaluate("""
                    () => typeof fnLoadSerach === 'function' ||
                          !!document.querySelector('a[onclick*="fnLoadSerach"]')
                """)
                if found:
                    log.info(f"Found fnLoadSerach in frame: {frame.url}")
                    return frame
            except Exception:
                pass
        await page.wait_for_timeout(500)
    return None

# ---------------------------------------------
# AUTOMATION
# ---------------------------------------------

async def run_automation():
    log.info("Starting automation run...")

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--single-process",
            ]
        )

        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page    = await context.new_page()

        try:
            log.info(f"Opening: {SITE_URL}")
            await page.goto(SITE_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            log.info("Entering credentials...")
            await page.fill('input[name="UserName"]', USERNAME)
            await page.fill('input[name="Password"]', PASSWORD)

            log.info("Clicking Login...")
            await page.click('input[type="submit"], button[type="submit"], #btnLogin')
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(4000)
            log.info(f"Logged in. URL: {page.url}")

            await js_click_title(page, "Transactions")
            await js_click_title(page, "Receivables")
            await js_click_title(page, "Orders")

            log.info("Clicking: Daily Order Summary")
            await page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const target = links.find(a => a.textContent.trim() === 'Daily Order Summary');
                    if (target) target.click();
                    else throw new Error('Daily Order Summary link not found');
                }
            """)

            log.info("Searching all frames for Search button...")
            target_frame = await find_search_frame(page)
            if target_frame is None:
                raise Exception("Could not find fnLoadSerach in any frame.")

            log.info("Clicking Search button...")
            await target_frame.evaluate("""
                () => {
                    const btn = document.querySelector('a[onclick*="fnLoadSerach"]');
                    if (btn) { btn.click(); return; }
                    if (typeof fnLoadSerach === 'function') { fnLoadSerach(); return; }
                    throw new Error('Search button not clickable');
                }
            """)
            log.info("Search clicked.")

            log.info("Waiting for results...")
            await page.wait_for_load_state("networkidle", timeout=15000)

            for _ in range(30):
                rows = await target_frame.evaluate(
                    "() => document.querySelectorAll('table tbody tr').length"
                )
                if rows > 0:
                    log.info(f"Table loaded: {rows} rows.")
                    break
                await page.wait_for_timeout(500)
            else:
                log.warning("No rows found, screenshotting anyway.")

            await page.wait_for_timeout(2000)

            log.info("Taking screenshot...")
            await page.screenshot(
                path=SCREENSHOT_PATH,
                clip={"x": CROP_X, "y": CROP_Y, "width": CROP_WIDTH, "height": CROP_HEIGHT}
            )
            log.info("Screenshot saved.")

        except Exception as e:
            log.error(f"Automation error: {e}")
            try:
                await page.screenshot(path=SCREENSHOT_PATH)
            except Exception:
                pass
        finally:
            await browser.close()

    send_to_telegram()


if __name__ == "__main__":
    asyncio.run(run_automation())
