import os
import sys
import json
import base64
import logging
import csv
import re
from decimal import Decimal, InvalidOperation
from datetime import date
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
============================================================
CONFIGURATION
============================================================
PW_BASE_URL = "https://api.propertyware.com/pw/api/rest/v1"
PW_REPORT_URL = "https://app.propertyware.com/pw/reporting/reports.do"

HUBSPOT_BASE_URL = "https://api.hubapi.com"
HUBSPOT_OBJECT_TYPE = "2-11351437"

PW_CLIENT_ID = os.environ["PW_CLIENT_ID"]
PW_CLIENT_SECRET = os.environ["PW_CLIENT_SECRET"]
PW_SYSTEM_ID = os.environ["PW_SYSTEM_ID"]
HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]

STORAGE_STATE_B64 = os.environ["PROPERTYWARE_STORAGE_STATE"]
Keep this true until the dry-run audit has been reviewed.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

PAGE_SIZE = 100
REQUEST_TIMEOUT = 60
Default report period: current calendar year to today's date.
Override either value using GitHub Secrets/Variables if required.
TODAY = date.today()
START_DATE = os.getenv(
"STATEMENT_START_DATE",
f"01/01/{TODAY.year}"
)
END_DATE = os.getenv(
"STATEMENT_END_DATE",
TODAY.strftime("%m/%d/%Y")
)

OUTPUT_CSV = "propertyware_balance_results.csv"
============================================================
LOGGING
============================================================
logging.basicConfig(
level=logging.INFO,
format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("PW-HubSpot-Sync")
============================================================
HTTP SESSIONS
============================================================
pw_session = requests.Session()

pw_session.headers.update({
"x-propertyware-system-id": PW_SYSTEM_ID,
"x-propertyware-client-id": PW_CLIENT_ID,
"x-propertyware-client-secret": PW_CLIENT_SECRET,
"Accept": "application/json",
})

hs_session = requests.Session()

hs_session.headers.update({
"Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
"Content-Type": "application/json",
})
============================================================
GENERAL HELPERS
============================================================
def normalize_name(value):
return " ".join(str(value or "").strip().casefold().split())

def unwrap_object(payload):
if isinstance(payload, dict):
for key in ("data", "portfolio", "result"):
value = payload.get(key)

if isinstance(value, dict):
return value

return payload

def extract_records(payload):
"""
Supports common Propertyware collection response formats.
Stops safely if the response structure is not recognized.
"""

if isinstance(payload, list):
return payload

if isinstance(payload, dict):
for key in ("data", "items", "portfolios", "results"):
value = payload.get(key)

if isinstance(value, list):
return value

if isinstance(value, dict):
for nested_key in ("items", "results", "portfolios"):
nested = value.get(nested_key)

if isinstance(nested, list):
return nested

raise ValueError(
"Unrecognized Propertyware portfolio response structure. "
"Inspect the API response before continuing."
)

def money_to_decimal(value):
if value is None:
raise ValueError("Balance value is empty.")

raw = str(value).strip()

negative = raw.startswith("(") and raw.endswith(")")

cleaned = re.sub(r"[^0-9.,-]", "", raw)
cleaned = cleaned.replace(",", "")

if not cleaned or cleaned in ("-", "."):
raise ValueError(f"Invalid balance value: {value}")

try:
amount = Decimal(cleaned)
except InvalidOperation as exc:
raise ValueError(
f"Cannot parse balance: {value}"
) from exc

if negative:
amount = -abs(amount)

if not amount.is_finite():
raise ValueError("Balance is not finite.")

return amount.quantize(Decimal("0.01"))

def extract_unique_summary_amount(matches, label):
"""
Returns one verified amount from matching Portfolio Summary rows.
Repeated rows are accepted only when all values agree.
"""
if not matches:
raise ValueError(
f"No Portfolio Summary '{label}' row was found."
)

unique_amounts = sorted(set(matches))

if len(unique_amounts) > 1:
formatted = ", ".join(
format(value, ".2f")
for value in unique_amounts
)

raise ValueError(
f"Multiple Portfolio Summary '{label}' rows contain "
f"different values: {formatted}. "
"Skipping to avoid an incorrect update."
)

if len(matches) > 1:
log.warning(
"Propertyware repeated the Portfolio Summary %s row "
"%s times; all values agree at %s. Using the verified "
"common value.",
label,
len(matches),
format(unique_amounts[0], ".2f")
)

return unique_amounts[0]
============================================================
PROPERTYWARE API
============================================================
def pw_get(path, params=None):
url = f"{PW_BASE_URL}{path}"

response = pw_session.get(
url,
params=params,
timeout=REQUEST_TIMEOUT
)

response.raise_for_status()

return response.json()

def get_all_portfolios():
log.info("Retrieving Propertyware portfolios.")

portfolios = []
offset = 0

while True:
payload = pw_get(
"/portfolios",
params={
"limit": PAGE_SIZE,
"offset": offset
}
)

batch = extract_records(payload)

if not batch:
break

portfolios.extend(batch)

log.info(
"Retrieved %s portfolios so far.",
len(portfolios)
)

if len(batch) < PAGE_SIZE:
break

offset += len(batch)

log.info(
"Total Propertyware portfolios retrieved: %s",
len(portfolios)
)

return portfolios

def get_portfolio_details(portfolio_id):
payload = pw_get(f"/portfolios/{portfolio_id}")
return unwrap_object(payload)

def get_portfolio_id(portfolio):
for key in ("portfolioID", "portfolioId", "id"):
value = portfolio.get(key)

if value is not None:
return str(value)

return None

def get_portfolio_name(portfolio):
for key in (
"portfolioName",
"portfolio_name",
"name"
):
value = portfolio.get(key)

if value:
return str(value).strip()

return None
============================================================
PROPERTYWARE AUTHENTICATED BROWSER
============================================================
def load_browser_state():
try:
decoded = base64.b64decode(
STORAGE_STATE_B64,
validate=True
)

state = json.loads(decoded.decode("utf-8"))

except Exception as exc:
raise RuntimeError(
"Could not decode PROPERTYWARE_STORAGE_STATE."
) from exc

session_storage = state.pop("sessionStorage", None)

if not isinstance(session_storage, dict):
raise RuntimeError(
"Session storage is missing from the captured browser state."
)

entries = session_storage.get("entries", {})

if not isinstance(entries, dict) or not entries:
raise RuntimeError(
"No sessionStorage entries were captured."
)

return state, entries

def restore_session_storage(context, entries):
"""
Restore sessionStorage before the Propertyware application loads.
This is necessary because Playwright's normal storage-state
format does not include sessionStorage.
"""

serialized = json.dumps(entries)

script = f"""
(() => {{
if (location.origin === "https://app.propertyware.com") {{
const entries = {serialized};

for (const [key, value] of Object.entries(entries)) {{
sessionStorage.setItem(key, value);
}}
}}
}})();
"""

context.add_init_script(script)

def build_report_url(portfolio_id, report_id):
params = {
"showBackMngmSum": "true",
"entityID": str(report_id),
"propertyFilterIDs": str(portfolio_id),
"propertyFilterType": "1",
"dateRangeFunction": "thisYear()",
"startDate": START_DATE,
"endDate": END_DATE,
"retUrl": "../management/management.do",
}

return f"{PW_REPORT_URL}?{urlencode(params)}"

def retrieve_statement_html(page, portfolio_id, report_id):
report_url = build_report_url(
portfolio_id,
report_id
)

log.info(
"Opening Owner Statement for portfolio %s.",
portfolio_id
)

page.goto(
report_url,
wait_until="domcontentloaded",
timeout=120000
)

if "login" in page.url.lower():
raise RuntimeError(
"Propertyware redirected to login. "
"The saved browser session may have expired."
)

page.wait_for_function(
"() => window.Ext && window.Ext.Ajax",
timeout=60000
)

result = page.evaluate("""
() => new Promise((resolve, reject) => {

if (!window.Ext || !Ext.Ajax) {
reject(new Error("Propertyware Ext application unavailable."));
return;
}

Ext.Ajax.request({
url: "reports.do",
baseParams: window.noIECacheParameter || {},
timeout: 1200000,
params: {
action: "RefreshHandmadeReport"
},

success: function(response) {
resolve(response.responseText);
},

failure: function(response) {
reject(new Error(
"Owner Statement request failed. HTTP status: " +
response.status
));
}
});
})
""")

if not result or not result.strip():
raise RuntimeError(
"Propertyware returned an empty Owner Statement."
)

return result
============================================================
HTML SUMMARY EXTRACTION
============================================================
def extract_portfolio_summary(html):
"""
Extracts these exact Portfolio Summary rows:
- Ending Balance
- Unpaid Bills

Only rows whose first cell exactly matches the label are used.
This excludes dated rows such as "Ending Balance as of ...".

If Propertyware repeats the summary, repeated values must agree.
If they differ or either value is missing, extraction fails safely.
"""

soup = BeautifulSoup(html, "html.parser")

ending_balance_matches = []
unpaid_bills_matches = []

for row in soup.find_all("tr"):
cells = row.find_all(
["td", "th"],
recursive=False
)

values = [
cell.get_text(" ", strip=True)
for cell in cells
]

values = [value for value in values if value]

if len(values) < 2:
continue

label = re.sub(
r"\s+",
" ",
values[0]
).strip().rstrip(":").casefold()

if label == "ending balance":
ending_balance_matches.append(
money_to_decimal(values[-1])
)

elif label == "unpaid bills":
unpaid_bills_matches.append(
money_to_decimal(values[-1])
)

ending_balance = extract_unique_summary_amount(
ending_balance_matches,
"Ending Balance"
)

unpaid_bills = extract_unique_summary_amount(
unpaid_bills_matches,
"Unpaid Bills"
)

return ending_balance, unpaid_bills
============================================================
HUBSPOT EXISTING PORTFOLIOS
============================================================
def get_hubspot_portfolios():
log.info("Retrieving existing HubSpot Portfolio records.")

records = []
after = None

while True:
params = {
"limit": 100,
"properties": (
"geekly_portfolio_name,"
"total_portfolio_balance,"
"unpaid_bills_by_portfolio"
)
}

if after:
params["after"] = after

response = hs_session.get(
f"{HUBSPOT_BASE_URL}/crm/v3/objects/"
f"{HUBSPOT_OBJECT_TYPE}",
params=params,
timeout=REQUEST_TIMEOUT
)

response.raise_for_status()

payload = response.json()

records.extend(payload.get("results", []))

next_page = (
payload.get("paging", {})
.get("next", {})
.get("after")
)

if not next_page:
break

after = next_page

log.info(
"Existing HubSpot Portfolio records retrieved: %s",
len(records)
)

return records

def build_hubspot_name_index(records):
index = {}

for record in records:
properties = record.get("properties", {})

name = properties.get("geekly_portfolio_name")

if not name:
continue

key = normalize_name(name)

index.setdefault(key, []).append(record)

return index

def update_hubspot_portfolio(record_id, ending_balance, unpaid_bills):
ending_balance_value = format(ending_balance, ".2f")
unpaid_bills_value = format(unpaid_bills, ".2f")

properties = {
"total_portfolio_balance": ending_balance_value,
"unpaid_bills_by_portfolio": unpaid_bills_value
}

if DRY_RUN:
log.info(
"[DRY RUN] Would update HubSpot record %s: "
"total_portfolio_balance = %s; "
"unpaid_bills_by_portfolio = %s",
record_id,
ending_balance_value,
unpaid_bills_value
)
return

response = hs_session.patch(
f"{HUBSPOT_BASE_URL}/crm/v3/objects/"
f"{HUBSPOT_OBJECT_TYPE}/{record_id}",
json={"properties": properties},
timeout=REQUEST_TIMEOUT
)

response.raise_for_status()

log.info(
"Updated HubSpot Portfolio record %s: "
"total_portfolio_balance = %s; "
"unpaid_bills_by_portfolio = %s",
record_id,
ending_balance_value,
unpaid_bills_value
)
============================================================
MAIN SYNCHRONIZATION
============================================================
def main():
log.info("Starting Propertyware to HubSpot balance sync.")

log.info("Dry run enabled: %s", DRY_RUN)
log.info("Statement start date: %s", START_DATE)
log.info("Statement end date: %s", END_DATE)

propertyware_portfolios = get_all_portfolios()

hubspot_records = get_hubspot_portfolios()
hubspot_index = build_hubspot_name_index(hubspot_records)

browser_state, session_entries = load_browser_state()

results = []

counters = {
"processed": 0,
"updated": 0,
"dry_run": 0,
"zero_balance": 0,
"skipped": 0,
"failed": 0
}

with sync_playwright() as playwright:

browser = playwright.chromium.launch(
headless=True
)

context = browser.new_context(
storage_state=browser_state
)

restore_session_storage(
context,
session_entries
)

page = context.new_page()

try:
for portfolio in propertyware_portfolios:

counters["processed"] += 1

portfolio_id = get_portfolio_id(portfolio)
portfolio_name = get_portfolio_name(portfolio)

result = {
"portfolio_id": portfolio_id,
"portfolio_name": portfolio_name,
"ending_balance": "",
"unpaid_bills": "",
"hubspot_record_id": "",
"status": ""
}

try:
if not portfolio_id:
raise ValueError(
"Propertyware portfolio ID missing."
)

details = get_portfolio_details(portfolio_id)

portfolio_name = (
get_portfolio_name(details)
or portfolio_name
)

result["portfolio_name"] = portfolio_name

report_id = (
details.get("ownerStatementReportID")
or details.get("ownerStatementReportId")
)

if not report_id:
raise ValueError(
"Owner Statement report ID missing."
)

if not portfolio_name:
raise ValueError(
"Portfolio name missing."
)

# Retrieve and parse the report before matching HubSpot.
html = retrieve_statement_html(
page,
portfolio_id,
report_id
)

ending_balance, unpaid_bills = (
extract_portfolio_summary(html)
)

result["ending_balance"] = format(
ending_balance,
".2f"
)

result["unpaid_bills"] = format(
unpaid_bills,
".2f"
)

log.info(
"%s | Ending Balance: %s | Unpaid Bills: %s",
portfolio_name,
ending_balance,
unpaid_bills
)

# Skip exact zero Ending Balance before HubSpot matching.
if ending_balance == Decimal("0.00"):
result["status"] = "SKIPPED - ZERO BALANCE"
counters["zero_balance"] += 1

log.info(
"Skipping %s because Ending Balance is 0.00.",
portfolio_name
)

results.append(result)
continue

name_key = normalize_name(portfolio_name)

matching_records = hubspot_index.get(
name_key,
[]
)

if len(matching_records) == 0:
result["status"] = "SKIPPED - NO HUBSPOT MATCH"
counters["skipped"] += 1

log.warning(
"No HubSpot match for non-zero balance portfolio %s.",
portfolio_name
)

results.append(result)
continue

if len(matching_records) > 1:
result["status"] = (
"SKIPPED - DUPLICATE HUBSPOT MATCH"
)
counters["skipped"] += 1

log.warning(
"Multiple HubSpot matches for non-zero balance "
"portfolio %s.",
portfolio_name
)

results.append(result)
continue

record_id = matching_records[0]["id"]

result["hubspot_record_id"] = record_id

update_hubspot_portfolio(
record_id,
ending_balance,
unpaid_bills
)

if DRY_RUN:
result["status"] = "DRY RUN - VERIFIED"
counters["dry_run"] += 1
else:
result["status"] = "UPDATED"
counters["updated"] += 1

except Exception as exc:
result["status"] = f"FAILED - {str(exc)}"
counters["failed"] += 1

log.exception(
"Portfolio processing failed: %s",
portfolio_name
)

results.append(result)

finally:
context.close()
browser.close()

# Save audit report.
with open(
OUTPUT_CSV,
"w",
newline="",
encoding="utf-8-sig"
) as csv_file:

fieldnames = [
"portfolio_id",
"portfolio_name",
"ending_balance",
"unpaid_bills",
"hubspot_record_id",
"status"
]

writer = csv.DictWriter(
csv_file,
fieldnames=fieldnames
)

writer.writeheader()
writer.writerows(results)

log.info("=" * 55)
log.info("SYNCHRONIZATION SUMMARY")
log.info("=" * 55)

for key, value in counters.items():
log.info("%s: %s", key.upper(), value)

log.info(
"NON-ZERO BALANCES MATCHED FOR SYNC: %s",
counters["updated"] + counters["dry_run"]
)

log.info("Audit file created: %s", OUTPUT_CSV)

# Fail the workflow if any portfolio failed.
if counters["failed"] > 0:
sys.exit(1)

if name == "main":
main()
