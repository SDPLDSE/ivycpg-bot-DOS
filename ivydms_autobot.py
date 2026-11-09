"""
IvyCPG Daily Automation Bot — GITHUB ACTIONS READY VERSION
"""

import asyncio
import requests
import logging
import sys
import os
import re
import json
import time
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------
# CONFIG
# ---------------------------------------------

USERNAME           = os.environ.get("IVYDMS_USERNAME", "")
PASSWORD           = os.environ.get("IVYDMS_PASSWORD", "")
SITE_URL           = "https://cloud01-in.ivydms.com/web/DMS/Welcome"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "{}")

SHEET_ID           = "1Tpc1676oOssnI3j2lzlvwFfpUltkDJgkIg5xB2_SX9c"
SHEET_NAME         = "SUMMARY"
SHEET_GID          = "1428175641"
SHEET_SCREENSHOT   = "sheet_screenshot.png"
SHEET_PUB_ID       = "2PACX-1vQndLO6fIA0NzpgluI0YPvExs3X-H7bUpvnd6DpYcgnlD4uqWZORg6rF5mTgNlEtd5PG2JMgMk1N1H4"

DSE_ROW_MAP = {
    "13427": 3, "13388": 4, "11636": 5, "11199": 6,
    "7878" : 7, "11141": 8, "6334" : 9,
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
# HELPERS
# ---------------------------------------------

def get_ist_time():
    # Fixed deprecation warning for utcnow()
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

async def find_search_frame(page):
    for _ in range(40):
        for frame in page.frames:
            try:
                found = await frame.evaluate("""
                    () => typeof fnLoadSerach === 'function' ||
                          !!document.querySelector('a[onclick*="fnLoadSerach"]')
                """)
                if found:
                    return frame
            except Exception:
                pass
        await page.wait_for_timeout(500)
    return None

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

        portal_data = {}
        for row in table_rows:
            id_match = re.search(r'_\s*(\d+)', row.get("salesman", ""))
            if id_match:
                portal_data[id_match.group(1)] = {
                    "order_count": row.get("order_count", 0),
                    "order_value": round(row.get("order_value_raw", 0), 2)
                }

        batch_data = []
        for dse_id, row_num in DSE_ROW_MAP.items():
            if dse_id in portal_data:
                orders = portal_data[dse_id]["order_count"]
                value  = portal_data[dse_id]["order_value"]
            else:
                orders = 0
                value  = 0

            batch_data.append({"range" : f"{SHEET_NAME}!{COL_ORDER}{row_num}", "values": [[orders]]})
            batch_data.append({"range" : f"{SHEET_NAME}!{COL_VALUE}{row_num}", "values": [[value]]})

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

async def screenshot_sheet():
    log.info("Taking screenshot of Google Sheet...")
    pub_url = f"https://docs.google.com/spreadsheets/d/e/{SHEET_PUB_ID}/pubhtml?gid={SHEET_GID}&single=true"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,  
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"]
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

            if box and box["width"] > 200:
                pad = 12   
                clip = {
                    "x"     : max(0, box["x"] - pad),
                    "y"     : max(0, box["y"] - 5),
                    "width" : box["width"]  + pad * 2,
                    "height": box["height"] + 17,
                }
                await page.screenshot(path=SHEET_SCREENSHOT, clip=clip)
                return True

            await page.screenshot(path="full_sheet.png", full_page=True)
            img = Image.open("full_sheet.png")
            w, h = img.size
            cropped = img.crop((0, 25, w, min(h, 450)))
            cropped.save(SHEET_SCREENSHOT)
            return True
        except Exception as e:
            log.error(f"Sheet screenshot error: {e}")
            return False
        finally:
            await browser.close()

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
    log.info("IvyCPG Daily Bot — GITHUB ACTIONS RUN")
    log.info("=" * 50)

    ist_now   = get_ist_time()
    today_ist = ist_now.strftime("%d-%b-%Y")
    log.info(f"IST Date: {today_ist}")

    table_rows = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,  
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox", "--single-process"]
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page    = await context.new_page()

        try:
            log.info(f"Opening: {SITE_URL}")
            await page.goto(SITE_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(2000)

            await page.fill('input[name="UserName"]', USERNAME)
            await page.fill('input[name="Password"]', PASSWORD)
            await page.click('input[type="submit"], button[type="submit"], #btnLogin')
            
            # Massive wait to ensure GitHub runners clear the Login/IndexSubmit redirect
            log.info("Waiting 15 seconds for dashboard to load completely...")
            await page.wait_for_timeout(15000)
            
            # Additional check to wait for URL change
            try:
                await page.wait_for_url("**/Welcome*", timeout=15000)
            except Exception:
                log.warning("URL didn't match /Welcome, but proceeding anyway...")
                
            log.info(f"Logged in. Current URL: {page.url}")

            log.info("Searching for 'Daily Order Summary' in the sidebar...")
            search_box = page.locator("#MenuSearchBox")
            await search_box.wait_for(state="visible", timeout=30000)
            await search_box.click()

            await search_box.press_sequentially("Daily Order Summary", delay=50)
            await page.wait_for_timeout(2000) 

            dropdown_item = page.locator(".ui-autocomplete li").filter(has_text="Daily Order Summary").first
            await dropdown_item.wait_for(state="visible", timeout=15000)
            await dropdown_item.click()

            log.info("Successfully clicked Daily Order Summary from search results.")
            await page.wait_for_timeout(4000) 

            target_frame = await find_search_frame(page)
            if not target_frame:
                raise Exception("Search frame not found.")

            await target_frame.evaluate(f"""
                () => {{
                    document.querySelectorAll('input').forEach(inp => {{
                        if ((inp.name || inp.id || '').toLowerCase().includes('date') || inp.type === 'text') {{
                            inp.value = '{today_ist}';
                            inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                            inp.dispatchEvent(new Event('input',  {{bubbles:true}}));
                        }}
                    }});
                }}
            """)
            await page.wait_for_timeout(1000)

            await target_frame.evaluate("""
                () => {
                    const btn = document.querySelector('a[onclick*="fnLoadSerach"]');
                    if (btn) { btn.click(); return; }
                    if (typeof fnLoadSerach === 'function') { fnLoadSerach(); return; }
                    throw new Error('Search not found');
                }
            """)
            log.info("Search clicked. Waiting for grid data to load...")

            # Resilient Wait-For-Data Loop
            for _ in range(30): # Wait up to 30 seconds for rows to appear
                await page.wait_for_timeout(1000)
                row_count = await target_frame.evaluate("() => document.querySelectorAll('.grid-data-row').length")
                if row_count > 0:
                    log.info(f"Grid loaded! Found {row_count} rows.")
                    break

            await page.wait_for_timeout(2000) # One final buffer before extraction
            table_rows = await extract_table_data(target_frame)

        except Exception as e:
            log.error(f"Portal error: {e}")
        finally:
            await page.wait_for_timeout(2000)
            await browser.close()

    if not table_rows:
        log.warning("No data extracted from portal (or 0 orders for today). Skipping Sheet update.")
        return

    sheet_updated = update_google_sheet(table_rows)
    if sheet_updated:
        time.sleep(5)
        sheet_ok = await screenshot_sheet()
        if sheet_ok:
            send_photo_to_telegram(ist_now)
        else:
            log.error("Sheet screenshot failed!")


if __name__ == "__main__":
    asyncio.run(run_automation())
