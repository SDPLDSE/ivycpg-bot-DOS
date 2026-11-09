"""
IvyCPG Daily Automation Bot — GITHUB ACTIONS FINAL VERSION
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

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

def get_ist_time():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

# ---------------------------------------------
# TELEGRAM HELPERS
# ---------------------------------------------
def send_photo_to_telegram(ist_now, photo_path, custom_caption=None):
    now = ist_now.strftime("%d %b %Y %I:%M %p IST")
    caption = custom_caption if custom_caption else f"Daily Sales Report NKA\n{now}"
    try:
        with open(photo_path, "rb") as img:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": img},
                timeout=30
            )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ---------------------------------------------
# EXTRACTION & SHEETS
# ---------------------------------------------
async def find_search_frame(page):
    for _ in range(40):
        for frame in page.frames:
            try:
                found = await frame.evaluate("() => typeof fnLoadSerach === 'function' || !!document.querySelector('a[onclick*=\"fnLoadSerach\"]')")
                if found: return frame
            except: pass
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
                        const valueClean    = parseFloat(valueTxt.replace('Order Value : ', '').replace(/[₹,\s]/g, '')) || 0;
                        if (salesmanClean && salesmanClean.includes('_')) {
                            result.push({ salesman: salesmanClean, order_count: orderCount, order_value_raw: valueClean });
                        }
                    }
                });
                return result;
            }
        """)
        log.info(f"Extracted {len(rows)} rows from portal.")
        return rows
    except Exception as e:
        log.error(f"Extraction error: {e}")
        return []

def update_google_sheet(table_rows):
    log.info("Updating Google Sheet...")
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()

        portal_data = {}
        for row in table_rows:
            id_match = re.search(r'_\s*(\d+)', row.get("salesman", ""))
            if id_match:
                portal_data[id_match.group(1)] = {"order_count": row.get("order_count", 0), "order_value": round(row.get("order_value_raw", 0), 2)}

        batch_data = []
        for dse_id, row_num in DSE_ROW_MAP.items():
            orders = portal_data[dse_id]["order_count"] if dse_id in portal_data else 0
            value  = portal_data[dse_id]["order_value"] if dse_id in portal_data else 0
            batch_data.append({"range": f"{SHEET_NAME}!{COL_ORDER}{row_num}", "values": [[orders]]})
            batch_data.append({"range": f"{SHEET_NAME}!{COL_VALUE}{row_num}", "values": [[value]]})

        sheet.values().batchUpdate(spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": batch_data}).execute()
        return True
    except Exception as e:
        log.error(f"Sheet update error: {e}")
        return False

async def screenshot_sheet():
    pub_url = f"https://docs.google.com/spreadsheets/d/e/{SHEET_PUB_ID}/pubhtml?gid={SHEET_GID}&single=true"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await context.new_page()
        try:
            await page.goto(pub_url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(4000)
            await page.screenshot(path="full_sheet.png", full_page=True)
            img = Image.open("full_sheet.png")
            w, h = img.size
            img.crop((0, 25, w, min(h, 450))).save(SHEET_SCREENSHOT)
            return True
        except: return False
        finally: await browser.close()

# ---------------------------------------------
# MAIN
# ---------------------------------------------
async def run_automation():
    log.info("=" * 50)
    log.info("IvyCPG Daily Bot — GITHUB ACTIONS FINAL")
    log.info("=" * 50)

    ist_now = get_ist_time()
    today_ist = ist_now.strftime("%d-%b-%Y")
    table_rows = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"])
        
        # Adding a custom User-Agent to bypass basic bot protection
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # SUPER FIX: Automatically click "OK" on any hidden JavaScript alerts (e.g. "Kill active session?")
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))

        try:
            log.info(f"Opening: {SITE_URL}")
            await page.goto(SITE_URL, wait_until="networkidle", timeout=45000)
            await page.fill('input[name="UserName"]', USERNAME)
            await page.fill('input[name="Password"]', PASSWORD)
            await page.click('input[type="submit"], button[type="submit"], #btnLogin')
            
            log.info("Waiting 15 seconds for dashboard to load completely...")
            await page.wait_for_timeout(15000)
            
            # THE DEBUG TRAP: If it gets stuck on IndexSubmit, take a picture and send it to you.
            if "IndexSubmit" in page.url or "Login" in page.url:
                log.error("BOT IS STUCK ON LOGIN PAGE. Sending debug screenshot to Telegram...")
                await page.screenshot(path="github_stuck.png")
                send_photo_to_telegram(ist_now, "github_stuck.png", "⚠️ GITHUB BOT STUCK:\nThe portal is blocking the login. Look at this screenshot to see what is on the screen.")
                raise Exception("Login failed or blocked by portal. Check Telegram for screenshot.")

            log.info(f"Logged in successfully. URL: {page.url}")

            search_box = page.locator("#MenuSearchBox")
            await search_box.wait_for(state="visible", timeout=30000)
            await search_box.click()
            await search_box.press_sequentially("Daily Order Summary", delay=50)
            await page.wait_for_timeout(2000) 

            dropdown_item = page.locator(".ui-autocomplete li").filter(has_text="Daily Order Summary").first
            await dropdown_item.wait_for(state="visible", timeout=15000)
            await dropdown_item.click()

            await page.wait_for_timeout(4000) 
            target_frame = await find_search_frame(page)
            if not target_frame: raise Exception("Search frame not found.")

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

            await target_frame.evaluate("() => { const btn = document.querySelector('a[onclick*=\"fnLoadSerach\"]'); if(btn) btn.click(); else if (typeof fnLoadSerach === 'function') fnLoadSerach(); }")

            for _ in range(30):
                await page.wait_for_timeout(1000)
                if await target_frame.evaluate("() => document.querySelectorAll('.grid-data-row').length") > 0: break

            await page.wait_for_timeout(2000)
            table_rows = await extract_table_data(target_frame)

        except Exception as e:
            log.error(f"Portal error: {e}")
        finally:
            await browser.close()

    if not table_rows:
        log.warning("No data extracted. Skipping Sheet update.")
        return

    if update_google_sheet(table_rows):
        time.sleep(5)
        if await screenshot_sheet():
            send_photo_to_telegram(ist_now, SHEET_SCREENSHOT)

if __name__ == "__main__":
    asyncio.run(run_automation())
