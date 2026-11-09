"""
IvyCPG Daily Automation Bot — GITHUB ACTIONS VERSION
Updated: robust IvyDMS menu detection across the main page and iframes.
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

USERNAME = os.environ.get("IVYDMS_USERNAME", "")
PASSWORD = os.environ.get("IVYDMS_PASSWORD", "")
SITE_URL = "https://cloud01-in.ivydms.com/web/DMS/Welcome"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")

SHEET_ID = "1Tpc1676oOssnI3j2lzlvwFfpUltkDJgkIg5xB2_SX9c"
SHEET_NAME = "SUMMARY"
SHEET_GID = "1428175641"
SHEET_SCREENSHOT = "sheet_screenshot.png"
SHEET_PUB_ID = (
    "2PACX-1vQndLO6fIA0NzpgluI0YPvExs3X-H7bUpvnd6DpYcgnlD4uqWZORg6rF5mTgNlEtd5PG2JMgMk1N1H4"
)

# DSE ID → Sheet row number
DSE_ROW_MAP = {
    "13427": 3,   # AKHIL K A
    "13388": 4,   # BILHAN S S
    "11636": 5,   # AJEESH G
    "11199": 6,   # SUNILKUMAR V I
    "7878": 7,    # MAHESH CHANDRAN M R
    "11141": 8,   # MOHANKUMAR K
    "6334": 9,    # UNNI S K
}

COL_ORDER = "C"
COL_VALUE = "E"


# ---------------------------------------------
# LOGGING
# ---------------------------------------------

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

if not log.handlers:
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

async def dump_available_links(page):
    """Log visible link details from the main page and every iframe."""
    log.warning("Collecting menu diagnostics from all frames...")

    for frame_index, frame in enumerate(page.frames):
        try:
            links = await frame.locator("a").evaluate_all(
                """els => els.map(a => ({
                    text: (a.innerText || a.textContent || '').trim(),
                    title: a.getAttribute('title'),
                    href: a.getAttribute('href')
                })).filter(x => x.text || x.title).slice(0, 100)"""
            )
            log.warning(
                "Frame %s | URL=%s | links=%s",
                frame_index,
                frame.url,
                json.dumps(links, ensure_ascii=False)
            )
        except Exception as exc:
            log.warning("Could not inspect frame %s: %s", frame_index, exc)


async def js_click_title(page, title):
    """
    Find and click a menu in the main page or any iframe.
    First checks title attributes, then exact visible text.
    """
    log.info("Searching for menu: %s", title)

    title_selector = f'[title="{title}"]'

    for attempt in range(30):
        for frame in page.frames:
            try:
                # Method 1: title attribute
                locator = frame.locator(title_selector)
                count = await locator.count()

                for index in range(count):
                    element = locator.nth(index)

                    try:
                        if not await element.is_visible():
                            continue

                        log.info(
                            "Found '%s' by title in frame: %s",
                            title,
                            frame.url
                        )

                        try:
                            await element.click(timeout=5000)
                        except Exception as click_error:
                            log.warning(
                                "Normal click failed for '%s': %s. Trying JS click.",
                                title,
                                click_error
                            )
                            await element.evaluate("(el) => el.click()")

                        await page.wait_for_timeout(2500)
                        log.info("Successfully clicked menu: %s", title)
                        return

                    except Exception as element_error:
                        log.debug(
                            "Title candidate %s for '%s' failed: %s",
                            index,
                            title,
                            element_error
                        )

                # Method 2: exact visible text
                text_locator = frame.get_by_text(title, exact=True)
                text_count = await text_locator.count()

                for index in range(text_count):
                    element = text_locator.nth(index)

                    try:
                        if not await element.is_visible():
                            continue

                        log.info(
                            "Found '%s' by exact text in frame: %s",
                            title,
                            frame.url
                        )

                        try:
                            await element.click(timeout=5000)
                        except Exception as click_error:
                            log.warning(
                                "Text click failed for '%s': %s. Trying JS click.",
                                title,
                                click_error
                            )
                            await element.evaluate("(el) => el.click()")

                        await page.wait_for_timeout(2500)
                        log.info("Successfully clicked menu: %s", title)
                        return

                    except Exception as element_error:
                        log.debug(
                            "Text candidate %s for '%s' failed: %s",
                            index,
                            title,
                            element_error
                        )

            except Exception as frame_error:
                log.debug(
                    "Frame search error while looking for '%s': %s",
                    title,
                    frame_error
                )

        if attempt in (5, 15):
            log.info(
                "Menu '%s' not found yet; retry %s/30",
                title,
                attempt + 1
            )

        await page.wait_for_timeout(500)

    await dump_available_links(page)
    raise Exception(
        f"Could not find menu '{title}' in the main page or any iframe."
    )


async def find_search_frame(page):
    """Find the frame containing IvyDMS's search function or search link."""
    log.info("Searching for IvyDMS report search frame...")

    for attempt in range(40):
        for frame in page.frames:
            try:
                found = await frame.evaluate(
                    """
                    () => typeof fnLoadSerach === 'function' ||
                          !!document.querySelector('a[onclick*="fnLoadSerach"]')
                    """
                )

                if found:
                    log.info("Found search frame: %s", frame.url)
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
        rows = await target_frame.evaluate(
            r"""
            () => {
                const result = [];
                const dataRows = document.querySelectorAll('.grid-data-row');

                dataRows.forEach(row => {
                    const cells = row.querySelectorAll('.grid-data-cell');

                    if (cells.length >= 3) {
                        const salesman =
                            cells[0].getAttribute('title') ||
                            cells[0].innerText.trim();

                        const orderTxt =
                            cells[1].getAttribute('title') ||
                            cells[1].innerText.trim();

                        const valueTxt =
                            cells[2].getAttribute('title') ||
                            cells[2].innerText.trim();

                        const salesmanClean = salesman
                            .replace('Salesman Name : ', '')
                            .trim();

                        const orderCount =
                            parseInt(orderTxt.replace(/\D/g, '')) || 0;

                        const valueClean = parseFloat(
                            valueTxt
                                .replace('Order Value : ', '')
                                .replace(/[₹,\s]/g, '')
                        ) || 0;

                        if (salesmanClean && salesmanClean.includes('_')) {
                            result.push({
                                salesman: salesmanClean,
                                order_count: orderCount,
                                order_value: valueTxt,
                                order_value_raw: valueClean
                            });
                        }
                    }
                });

                return result;
            }
            """
        )

        log.info("Extracted %s rows from portal.", len(rows))

        for row in rows:
            log.info(
                "  %s → Orders:%s Value:%s",
                row["salesman"],
                row["order_count"],
                row["order_value_raw"]
            )

        return rows

    except Exception as exc:
        log.error("Extraction error: %s", exc)
        return []


# ---------------------------------------------
# UPDATE GOOGLE SHEET VIA SERVICE ACCOUNT
# ---------------------------------------------

def update_google_sheet(table_rows):
    log.info("Updating Google Sheet via Service Account...")

    try:
        if not GOOGLE_CREDENTIALS:
            raise ValueError("GOOGLE_CREDENTIALS GitHub Secret is empty.")

        creds_dict = json.loads(GOOGLE_CREDENTIALS)

        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()

        # Build portal data lookup by DSE ID
        portal_data = {}

        for row in table_rows:
            id_match = re.search(r"_\s*(\d+)", row.get("salesman", ""))

            if id_match:
                portal_data[id_match.group(1)] = {
                    "order_count": row.get("order_count", 0),
                    "order_value": round(
                        row.get("order_value_raw", 0), 2
                    )
                }

        log.info("Portal data: %s", portal_data)

        batch_data = []

        for dse_id, row_num in DSE_ROW_MAP.items():
            if dse_id in portal_data:
                orders = portal_data[dse_id]["order_count"]
                value = portal_data[dse_id]["order_value"]
            else:
                orders = 0
                value = 0
                log.warning(
                    "ID %s not found in portal — writing 0",
                    dse_id
                )

            batch_data.append({
                "range": f"{SHEET_NAME}!{COL_ORDER}{row_num}",
                "values": [[orders]]
            })

            batch_data.append({
                "range": f"{SHEET_NAME}!{COL_VALUE}{row_num}",
                "values": [[value]]
            })

            log.info(
                "Row %s ID=%s: Orders=%s Value=%s",
                row_num,
                dse_id,
                orders,
                value
            )

        result = sheet.values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "valueInputOption": "RAW",
                "data": batch_data
            }
        ).execute()

        updated = result.get("totalUpdatedCells", 0)
        log.info("Sheet updated! Total cells updated: %s", updated)

        return True

    except Exception as exc:
        log.error("Sheet update error: %s", exc)
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
                "--disable-setuid-sandbox"
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1600, "height": 900}
        )
        page = await context.new_page()

        try:
            await page.goto(
                pub_url,
                wait_until="networkidle",
                timeout=30000
            )
            await page.wait_for_timeout(4000)

            box = None
            iframe_el = await page.query_selector(
                "iframe#pageswitcher-content"
            )

            if iframe_el:
                frame = await iframe_el.content_frame()

                if frame:
                    table_el = await frame.query_selector("table.waffle")

                    if not table_el:
                        table_el = await frame.query_selector("table")

                    if table_el:
                        box = await table_el.bounding_box()
                        log.info("Table bounding box: %s", box)

            if box and box["width"] > 200:
                pad_x = 12
                pad_top = 5
                pad_bot = 12

                clip = {
                    "x": max(0, box["x"] - pad_x),
                    "y": max(0, box["y"] - pad_top),
                    "width": box["width"] + pad_x * 2,
                    "height": box["height"] + pad_top + pad_bot
                }

                await page.screenshot(
                    path=SHEET_SCREENSHOT,
                    clip=clip
                )

                log.info(
                    "Table screenshot saved: %s",
                    SHEET_SCREENSHOT
                )
                return True

            log.warning(
                "Table not found in iframe — using full-page fallback."
            )

            await page.screenshot(
                path="full_sheet.png",
                full_page=True
            )

            img = Image.open("full_sheet.png")
            width, height = img.size

            cropped = img.crop(
                (0, 25, width, min(height, 450))
            )
            cropped.save(SHEET_SCREENSHOT)

            log.info(
                "Fallback screenshot saved: %s",
                SHEET_SCREENSHOT
            )
            return True

        except Exception as exc:
            log.error("Sheet screenshot error: %s", exc)
            return False

        finally:
            await browser.close()


# ---------------------------------------------
# SEND TO TELEGRAM
# ---------------------------------------------

def send_photo_to_telegram(ist_now):
    now = ist_now.strftime("%d %b %Y %I:%M %p IST")
    caption = f"Daily Sales Report NKA\n{now}"

    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is empty."
            )

        with open(SHEET_SCREENSHOT, "rb") as img:
            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption
                },
                files={"photo": img},
                timeout=30
            )

        if response.ok:
            log.info("Photo sent to Telegram!")
        else:
            log.error(
                "Telegram Error: %s %s",
                response.status_code,
                response.text
            )

    except Exception as exc:
        log.error("Telegram error: %s", exc)


# ---------------------------------------------
# MAIN
# ---------------------------------------------

async def run_automation():
    log.info("=" * 60)
    log.info("IvyCPG Daily Bot — GITHUB ACTIONS VERSION")
    log.info("=" * 60)

    ist_now = get_ist_time()
    today_ist = ist_now.strftime("%d-%b-%Y")

    log.info(
        "IST Date: %s | Time: %s",
        today_ist,
        ist_now.strftime("%I:%M %p")
    )

    if not USERNAME or not PASSWORD:
        raise ValueError(
            "IVYDMS_USERNAME or IVYDMS_PASSWORD GitHub Secret is empty."
        )

    table_rows = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox"
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        try:
            log.info("Opening: %s", SITE_URL)

            await page.goto(
                SITE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )
            await page.wait_for_timeout(3000)

            await page.fill(
                'input[name="UserName"]',
                USERNAME
            )
            await page.fill(
                'input[name="Password"]',
                PASSWORD
            )

            await page.click(
                'input[type="submit"], button[type="submit"], #btnLogin'
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=30000
                )
            except Exception:
                log.warning(
                    "Network did not become idle after login; continuing."
                )

            await page.wait_for_timeout(5000)

            log.info("Logged in. URL: %s", page.url)

            # Save a diagnostic screenshot after login.
            await page.screenshot(
                path="after_login.png",
                full_page=True
            )
            log.info("Saved after_login.png")

            # Navigate through IvyDMS menus.
            await js_click_title(page, "Transactions")
            await js_click_title(page, "Receivables")
            await js_click_title(page, "Orders")

            # Click Daily Order Summary in the main page or any iframe.
            report_clicked = False

            for frame in page.frames:
                try:
                    report_link = frame.get_by_text(
                        "Daily Order Summary",
                        exact=True
                    )

                    count = await report_link.count()

                    for index in range(count):
                        candidate = report_link.nth(index)

                        if await candidate.is_visible():
                            log.info(
                                "Found Daily Order Summary in frame: %s",
                                frame.url
                            )
                            await candidate.click(timeout=10000)
                            report_clicked = True
                            break

                    if report_clicked:
                        break

                except Exception as exc:
                    log.debug(
                        "Report link search error in frame %s: %s",
                        frame.url,
                        exc
                    )

            if not report_clicked:
                await dump_available_links(page)
                raise Exception(
                    "Daily Order Summary menu item was not found."
                )

            await page.wait_for_timeout(3000)

            target_frame = await find_search_frame(page)

            if not target_frame:
                await dump_available_links(page)
                raise Exception("Search frame not found.")

            # Set today's date in the report search form.
            await target_frame.evaluate(
                """
                (today) => {
                    document.querySelectorAll('input').forEach(inp => {
                        const fieldName =
                            (inp.name || inp.id || '').toLowerCase();

                        if (
                            fieldName.includes('date') ||
                            inp.type === 'text'
                        ) {
                            inp.value = today;
                            inp.dispatchEvent(
                                new Event('input', { bubbles: true })
                            );
                            inp.dispatchEvent(
                                new Event('change', { bubbles: true })
                            );
                        }
                    });
                }
                """,
                today_ist
            )

            await page.wait_for_timeout(1000)

            # Click Search.
            await target_frame.evaluate(
                """
                () => {
                    const btn = document.querySelector(
                        'a[onclick*="fnLoadSerach"]'
                    );

                    if (btn) {
                        btn.click();
                        return;
                    }

                    if (typeof fnLoadSerach === 'function') {
                        fnLoadSerach();
                        return;
                    }

                    throw new Error('Search function/button not found');
                }
                """
            )

            log.info("Search clicked.")

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=15000
                )
            except Exception:
                log.info(
                    "Report request still active or network not idle; continuing."
                )

            await page.wait_for_timeout(3000)

            # Wait for report rows.
            for attempt in range(30):
                rows_count = await target_frame.evaluate(
                    "() => document.querySelectorAll('.grid-data-row').length"
                )

                if rows_count > 0:
                    log.info("Grid rows found: %s", rows_count)
                    break

                await page.wait_for_timeout(500)

            await page.wait_for_timeout(2000)
            table_rows = await extract_table_data(target_frame)

        except Exception as exc:
            log.error("Portal error: %s", exc)

            try:
                await page.screenshot(
                    path="portal_error.png",
                    full_page=True
                )
                log.info("Saved portal_error.png")
            except Exception:
                pass

        finally:
            await browser.close()

    if not table_rows:
        log.error(
            "No data extracted from portal. Google Sheet will NOT be updated."
        )
        return

    # Update Google Sheet only when portal data was extracted.
    sheet_updated = update_google_sheet(table_rows)

    if not sheet_updated:
        log.error(
            "Google Sheet update failed. Telegram report will NOT be sent."
        )
        return

    log.info("Waiting 5 seconds for Google Sheet to sync...")
    time.sleep(5)

    # Screenshot the published Google Sheet.
    sheet_ok = await screenshot_sheet()

    # Send screenshot to Telegram.
    if sheet_ok:
        send_photo_to_telegram(ist_now)
    else:
        log.error("Sheet screenshot failed!")


if __name__ == "__main__":
    asyncio.run(run_automation())
