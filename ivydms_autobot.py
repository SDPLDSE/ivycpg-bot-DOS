"""
IvyCPG Daily Automation Bot — GITHUB ACTIONS VERSION
"""

import asyncio
import requests
import logging
import sys
import os
import re
import json
import time
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------
# CONFIG — from GitHub Secrets
# ---------------------------------------------

USERNAME           = os.environ.get("IVYDMS_USERNAME", "")
PASSWORD           = os.environ.get("IVYDMS_PASSWORD", "")
SITE_URL           = "https://cloud01-in.ivydms.com/web/DMS/Welcome"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")

SHEET_ID           = "1Tpc1676oOssnI3j2lzlvwFfpUltkDJgkIg5xB2_SX9c"
SHEET_NAME         = "SUMMARY"
SHEET_GID          = "1428175641"
SHEET_SCREENSHOT   = "sheet_screenshot.png"
SHEET_PUB_ID       = "2PACX-1vQndLO6fIA0NzpgluI0YPvExs3X-H7bUpvnd6DpYcgnlD4uqWZORg6rF5mTgNlEtd5PG2JMgMk1N1H4"

# DSE ID → Sheet row number
DSE_ROW_MAP = {
    "13427": 3,   # AKHIL K A
    "13388": 4,   # BILHAN S S
    "11636": 5,   # AJEESH G
    "11199": 6,   # SUNILKUMAR V I
    "7878" : 7,   # MAHESH CHANDRAN M R
    "11141": 8,   # MOHANKUMAR K
    "6334" : 9,   # UNNI S K
}

COL_ORDER = "C"
COL_VALUE = "E"

# ---------------------------------------------
# LOGGING
# ---------------------------------------------

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

# ---------------------------------------------
# IST TIME
# ---------------------------------------------

def get_ist_time():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

# ---------------------------------------------
# HELPERS
# ---------------------------------------------

async def js_click_title(page, title):
    log.info(f"Trying to click menu: {title}")
    # Use specific locator to find ONLY the visible element, ignoring hidden duplicates
    selector = f'a[title="{title}"] >> visible=true'

    try:
        await page.wait_for_selector(selector, state="attached", timeout=30000)
        # force=True bypasses some strict visibility/interception checks
        await page.locator(selector).first.click(force=True)
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning(f"Playwright native click failed for {title}: {e}. Trying JS fallback...")
        await page.evaluate(f"""
            () => {{
                const els = Array.from(document.querySelectorAll('a[title="{title}"]'));
                const visibleEl = els.find(el => el.offsetWidth > 0 && el.offsetHeight > 0);
                if (visibleEl) visibleEl.click();
                else throw new Error('Menu {title} not found');
            }}
        """)
        await page.wait_for_timeout(2000)

async def find_search_frame(page):
    for _ in range(40):
        for frame in page.frames:
            try:
                found = await frame.evaluate("""
                    () => typeof fnLoadSerach === 'function' ||
                          !!document.querySelector('a[onclick*="fnLoadSerach"]')
                """)
                if found:
                    log.info(f"Found search frame: {frame.url}")
                    return frame
            except Exception:
                pass
        await page.wait_for_timeout(500)
    return None

# ---------------------------------------------
# EXTRACT FROM IVYDMS
# ---------------------------------------------

async def extract_table_data(target_frame):
    try:
        rows = await target_frame.evaluate(r"""
            () => {
                const result = [];
                const dataRows = document.querySelectorAll('.grid-data-row');
                dataRows.forEach(row => {
                    const cells = row.querySelectorAll('.grid-data-cell');
                    if (cells.length >= 3) {
                        const salesman = cells[0].getAttribute('title') || cells[0].innerText.trim();
                        const orderTxt = cells[1].getAttribute('title') || cells[1].innerText.trim();
                        const valueTxt = cells[2].getAttribute('title') || cells[2].innerText.trim();
                        const salesmanClean = salesman.replace('Salesman Name : ', '').trim();
                        const orderCount    = parseInt(orderTxt.replace(/\D/g, '')) || 0;
                        const valueClean    = parseFloat(
                            valueTxt.replace('Order Value : ', '').replace(/[₹,\s]/g, '')
                        ) || 0;
                        if (salesmanClean && salesmanClean.includes('_')) {
                            result.push({
                                salesman       : salesmanClean,
                                order_count    : orderCount,
                                order_value    : valueTxt,
                                order_value_raw: valueClean
                            });
                        }
                    }
                });
                return result;
            }
        """)
        log.info(f"Extracted {len(rows)} rows from portal.")
        for r in rows:
            log.info(f"  {r['salesman']} → Orders:{r['order_count']} Value:{r['order_value_raw']}")
        return rows
    except Exception as e:
        log.error(f"Extraction error: {e}")
        return []

# ---------------------------------------------
# UPDATE GOOGLE SHEET VIA SERVICE ACCOUNT
# ---------------------------------------------

def update_google_sheet(table_rows):
    log.info("Updating Google Sheet via Service Account...")
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=creds)
        sheet   = service.spreadsheets()

        # Build portal data lookup by ID
        portal_data = {}
        for row in table_rows:
            id_match = re.search(r'_\s*(\d+)', row.get("salesman", ""))
            if id_match:
                portal_data[id_match.group(1)] = {
                    "order_count": row.get("order_count", 0),
                    "order_value": round(row.get("order_value_raw", 0), 2)
                }
        log.info(f"Portal data: {portal_data}")

        # Build batch update
        batch_data = []
        for dse_id, row_num in DSE_ROW_MAP.items():
            if dse_id in portal_data:
                orders = portal_data[dse_id]["order_count"]
                value  = portal_data[dse_id]["order_value"]
            else:
                orders = 0
                value  = 0
                log.warning(f"  ID {dse_id} not in portal — writing 0")

            batch_data.append({
                "range" : f"{SHEET_NAME}!{COL_ORDER}{row_num}",
                "values": [[orders]]
            })
            batch_data.append({
                "range" : f"{SHEET_NAME}!{COL_VALUE}{row_num}",
                "values": [[value]]
            })
            log.info(f"  Row {row_num} ID={dse_id}: Orders={orders} Value={value}")

        result = sheet.values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": batch_data}
        ).execute()

        updated = result.get("totalUpdatedCells", 0)
        log.info(f"Sheet updated! Total cells updated: {updated}")
        return True

    except Exception as e:
        log.error(f"Sheet update error: {e}")
        return False

# ---------------------------------------------
# SCREENSHOT SHEET — PUBLISHED HTML
# ---------------------------------------------

async def screenshot_sheet():
    log.info("Taking screenshot of Google Sheet...")

    pub_url = (
        f"https://docs.google.com/spreadsheets/d/e/{SHEET_PUB_ID}"
        f"/pubhtml?gid={SHEET_GID}&single=true"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ]
        )
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        page    = await context.new_page()

        try:
            await page.goto(pub_url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(4000)

            box = None
            iframe_el = await page.query_selector("iframe#pageswitcher-content")
            if iframe_el:
                frame     = await iframe_el.content_frame()
                table_el  = await frame.query_selector("table.waffle")
                if not table_el:
                    table_el = await frame.query_selector("table")
                if table_el:
                    box = await table_el.bounding_box()
                    log.info(f"Table bounding box (page coords): {box}")

            if box and box["width"] > 200:
                pad_x   = 12
                pad_top = 5
                pad_bot = 12
                clip = {
                    "x"     : max(0, box["x"] - pad_x),
                    "y"     : max(0, box["y"] - pad_top),
                    "width" : box["width"]  + pad_x * 2,
                    "height": box["height"] + pad_top + pad_bot,
                }
                await page.screenshot(path=SHEET_SCREENSHOT, clip=clip)
                log.info(f"Table screenshot saved (iframe clip): {SHEET_SCREENSHOT}")
                return True

            log.warning("Table element not found in iframe — falling back to full page crop.")
            await page.screenshot(path="full_sheet.png", full_page=True)
            img = Image.open("full_sheet.png")
            w, h = img.size
            cropped = img.crop((0, 25, w, min(h, 450)))
            cropped.save(SHEET_SCREENSHOT)
            log.info(f"Fallback screenshot saved: {SHEET_SCREENSHOT}")
            return True

        except Exception as e:
            log.error(f"Sheet screenshot error: {e}")
            return False
        finally:
            await browser.close()

# ---------------------------------------------
# SEND TO TELEGRAM
# ---------------------------------------------

def send_photo_to_telegram(ist_now):
    now     = ist_now.strftime("%d %b %Y %I:%M %p IST")
    caption = f"Daily Sales Report NKA\n{now}"
    try:
        with open(SHEET_SCREENSHOT, "rb") as img:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": img},
                timeout=30
            )
        if resp.ok:
            log.info("Photo sent to Telegram! ✅")
        else:
            log.error(f"Telegram Error: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ---------------------------------------------
# MAIN
# ---------------------------------------------

async def run_automation():
    log.info("=" * 50)
    log.info("IvyCPG Daily Bot — GITHUB ACTIONS VERSION")
    log.info("=" * 50)

    ist_now   = get_ist_time()
    today_ist = ist_now.strftime("%d-%b-%Y")
    log.info(f"IST Date: {today_ist} | Time: {ist_now.strftime('%I:%M %p')}")

    table_rows = []

    async with async_playwright() as p:
        # Headless mode for production ("--single-process" removed: unstable on GitHub runners)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ]
        )

        # Mimic the local (working) setup: same viewport, Indian locale/timezone, normal Chrome user agent
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = await context.new_page()

        try:
            log.info(f"Opening: {SITE_URL}")
            await page.goto(SITE_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            await page.fill('input[name="UserName"]', USERNAME)
            await page.fill('input[name="Password"]', PASSWORD)
            await page.click('input[type="submit"], button[type="submit"], #btnLogin')

            # Wait for dashboard to fully load
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.wait_for_timeout(4000)
            log.info(f"Logged in. URL: {page.url}")

            # Debug: shows what the portal actually displays after login
            try:
                body_text = await page.evaluate("() => document.body.innerText.slice(0, 400)")
                log.info(f"Page text after login: {body_text!r}")
                await page.screenshot(path="debug_after_login.png")
            except Exception as dbg_e:
                log.warning(f"Debug capture failed: {dbg_e}")

            # Execute clicks
            await js_click_title(page, "Transactions")
            await js_click_title(page, "Receivables")
            await js_click_title(page, "Orders")

            # Click Daily Order Summary
            await page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const target = links.find(a => a.textContent.trim() === 'Daily Order Summary');
                    if (target) target.click();
                    else throw new Error('Daily Order Summary not found');
                }
            """)
            await page.wait_for_timeout(3000)

            target_frame = await find_search_frame(page)
            if not target_frame:
                raise Exception("Search frame not found.")

            # Set today's date
            await target_frame.evaluate(f"""
                () => {{
                    document.querySelectorAll('input').forEach(inp => {{
                        if ((inp.name || inp.id || '').toLowerCase().includes('date')
                                || inp.type === 'text') {{
                            inp.value = '{today_ist}';
                            inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                            inp.dispatchEvent(new Event('input',  {{bubbles:true}}));
                        }}
                    }});
                }}
            """)
            await page.wait_for_timeout(1000)

            # Click Search
            await target_frame.evaluate("""
                () => {
                    const btn = document.querySelector('a[onclick*="fnLoadSerach"]');
                    if (btn) { btn.click(); return; }
                    if (typeof fnLoadSerach === 'function') { fnLoadSerach(); return; }
                    throw new Error('Search not found');
                }
            """)
            log.info("Search clicked.")

            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(3000)

            for _ in range(30):
                rows = await target_frame.evaluate(
                    "() => document.querySelectorAll('.grid-data-row').length"
                )
                if rows > 0:
                    log.info(f"Grid rows found: {rows}")
                    break
                await page.wait_for_timeout(500)

            await page.wait_for_timeout(2000)
            table_rows = await extract_table_data(target_frame)

        except Exception as e:
            log.error(f"Portal error: {e}")
            # Capture the page at the moment of failure for debugging
            try:
                await page.screenshot(path="debug_error.png")
            except Exception:
                pass
        finally:
            await browser.close()

    if not table_rows:
        log.error("No data extracted from portal!")
        return

    # Update Google Sheet
    sheet_updated = update_google_sheet(table_rows)
    if sheet_updated:
        log.info("Waiting 5 seconds for sheet to sync...")
        time.sleep(5)

    # Screenshot Sheet
    sheet_ok = await screenshot_sheet()

    # Send to Telegram
    if sheet_ok:
        send_photo_to_telegram(ist_now)
    else:
        log.error("Sheet screenshot failed!")


if __name__ == "__main__":
    asyncio.run(run_automation())
