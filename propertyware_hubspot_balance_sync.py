# ============================================================
# Propertyware -> HubSpot balance sync
# Logs in to Propertyware on every run (no saved session needed),
# pulls each portfolio's Owner Statement, and writes Ending Balance
# and Unpaid Bills to the matching HubSpot custom object record.
# ============================================================

import os  # Read environment variables (secrets / settings)
import sys  # Used to exit with a failure code
import logging  # Structured log output
import csv  # Write the audit CSV file
import re  # Regular expressions for text cleanup
from decimal import Decimal, InvalidOperation  # Exact money maths (no float errors)
from datetime import date  # Today's date for the default report period
from urllib.parse import urlencode  # Build the report URL query string

import requests  # HTTP client for the Propertyware and HubSpot APIs
from bs4 import BeautifulSoup  # Parse the Owner Statement HTML
from playwright.sync_api import sync_playwright  # Headless browser automation
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # Playwright timeouts


# ============================================================
# CONFIGURATION
# ============================================================

# Propertyware REST API base URL
PW_BASE_URL = "https://api.propertyware.com/pw/api/rest/v1"
# Propertyware web login page (override with PW_LOGIN_URL if it differs)
PW_LOGIN_URL = os.getenv(
    "PW_LOGIN_URL",
    "https://app.propertyware.com/pw/login.jsp"
)
# Propertyware web reporting page (used through the browser)
PW_REPORT_URL = "https://app.propertyware.com/pw/reporting/reports.do"

# HubSpot API base URL
HUBSPOT_BASE_URL = "https://api.hubapi.com"
# HubSpot custom object type ID for the Portfolio object
HUBSPOT_OBJECT_TYPE = "2-11351437"

# Propertyware API credentials (must be set, otherwise the script stops)
PW_CLIENT_ID = os.environ["PW_CLIENT_ID"]
PW_CLIENT_SECRET = os.environ["PW_CLIENT_SECRET"]
PW_SYSTEM_ID = os.environ["PW_SYSTEM_ID"]

# Propertyware web login for the dedicated automation user
PW_USERNAME = os.environ["PW_USERNAME"]
PW_PASSWORD = os.environ["PW_PASSWORD"]

# HubSpot private app access token
HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]

# Keep this true until the dry-run audit has been reviewed.
# When true, nothing is written to HubSpot.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

PAGE_SIZE = 100  # Records requested per API page
REQUEST_TIMEOUT = 60  # Seconds before an API request times out

# Default report period: current calendar year to today's date.
# Override either value using GitHub Secrets/Variables if required.
TODAY = date.today()
# Statement start date (defaults to 1 January of this year)
START_DATE = os.getenv(
    "STATEMENT_START_DATE",
    f"01/01/{TODAY.year}"
)
# Statement end date (defaults to today)
END_DATE = os.getenv(
    "STATEMENT_END_DATE",
    TODAY.strftime("%m/%d/%Y")
)

# File name for the audit report
OUTPUT_CSV = "propertyware_balance_results.csv"


# ============================================================
# LOGGING
# ============================================================

# Configure log format: timestamp | level | message
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# Named logger used throughout the script
log = logging.getLogger("PW-HubSpot-Sync")


# ============================================================
# CUSTOM ERRORS
# ============================================================

class LoginError(RuntimeError):
    """Raised when the Propertyware login does not succeed."""


class SessionExpiredError(RuntimeError):
    """Raised when Propertyware sends the browser back to the login page."""


# ============================================================
# HTTP SESSIONS
# ============================================================

# Reusable session for Propertyware API calls
pw_session = requests.Session()

# Attach authentication headers to every Propertyware request
pw_session.headers.update({
    "x-propertyware-system-id": PW_SYSTEM_ID,
    "x-propertyware-client-id": PW_CLIENT_ID,
    "x-propertyware-client-secret": PW_CLIENT_SECRET,
    "Accept": "application/json",
})

# Reusable session for HubSpot API calls
hs_session = requests.Session()

# Attach the bearer token to every HubSpot request
hs_session.headers.update({
    "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
    "Content-Type": "application/json",
})


# ============================================================
# GENERAL HELPERS
# ============================================================

def normalize_name(value):
    # Lower-case, trim and collapse spaces so names compare reliably
    return " ".join(str(value or "").strip().casefold().split())


def unwrap_object(payload):
    # Some responses wrap the real object inside "data", "portfolio" or "result"
    if isinstance(payload, dict):
        for key in ("data", "portfolio", "result"):
            value = payload.get(key)

            # Return the wrapped object when one is found
            if isinstance(value, dict):
                return value

    # Otherwise the payload is already the object
    return payload


def extract_records(payload):
    """
    Supports common Propertyware collection response formats.
    Stops safely if the response structure is not recognized.
    """

    # Response is already a plain list
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        # Look for a list under the common wrapper keys
        for key in ("data", "items", "portfolios", "results"):
            value = payload.get(key)

            # List found directly under the key
            if isinstance(value, list):
                return value

            # Wrapper holds another dict: look one level deeper
            if isinstance(value, dict):
                for nested_key in ("items", "results", "portfolios"):
                    nested = value.get(nested_key)

                    if isinstance(nested, list):
                        return nested

    # Nothing matched: stop rather than guess
    raise ValueError(
        "Unrecognized Propertyware portfolio response structure. "
        "Inspect the API response before continuing."
    )


def money_to_decimal(value):
    # Reject empty values
    if value is None:
        raise ValueError("Balance value is empty.")

    # Work with a trimmed string
    raw = str(value).strip()

    # Accounting style negatives: (123.45) means -123.45
    negative = raw.startswith("(") and raw.endswith(")")

    # Strip currency symbols and text, keep digits . , -
    cleaned = re.sub(r"[^0-9.,-]", "", raw)
    # Remove thousands separators
    cleaned = cleaned.replace(",", "")

    # Nothing usable left after cleaning
    if not cleaned or cleaned in ("-", "."):
        raise ValueError(f"Invalid balance value: {value}")

    try:
        # Convert to an exact decimal number
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(
            f"Cannot parse balance: {value}"
        ) from exc

    # Apply the negative sign for bracketed values
    if negative:
        amount = -abs(amount)

    # Guard against NaN / Infinity
    if not amount.is_finite():
        raise ValueError("Balance is not finite.")

    # Round to 2 decimal places (cents)
    return amount.quantize(Decimal("0.01"))


def extract_unique_summary_amount(matches, label):
    """
    Returns one verified amount from matching Portfolio Summary rows.
    Repeated rows are accepted only when all values agree.
    """
    # The row was never found in the report
    if not matches:
        raise ValueError(
            f"No Portfolio Summary '{label}' row was found."
        )

    # Remove duplicates; more than one distinct value means a conflict
    unique_amounts = sorted(set(matches))

    if len(unique_amounts) > 1:
        # Build a readable list of the conflicting values
        formatted = ", ".join(
            format(value, ".2f")
            for value in unique_amounts
        )

        # Refuse to update with ambiguous data
        raise ValueError(
            f"Multiple Portfolio Summary '{label}' rows contain "
            f"different values: {formatted}. "
            "Skipping to avoid an incorrect update."
        )

    # Row was repeated but every value agrees: safe, just warn
    if len(matches) > 1:
        log.warning(
            "Propertyware repeated the Portfolio Summary %s row "
            "%s times; all values agree at %s. Using the verified "
            "common value.",
            label,
            len(matches),
            format(unique_amounts[0], ".2f")
        )

    # Return the single verified amount
    return unique_amounts[0]


# ============================================================
# PROPERTYWARE API
# ============================================================

def pw_get(path, params=None):
    # Build the full API URL
    url = f"{PW_BASE_URL}{path}"

    # Send the GET request with a timeout
    response = pw_session.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT
    )

    # Raise an error for any 4xx / 5xx response
    response.raise_for_status()

    # Return the parsed JSON body
    return response.json()


def get_all_portfolios():
    log.info("Retrieving Propertyware portfolios.")

    portfolios = []  # Collected portfolio records
    offset = 0  # Pagination position

    while True:
        # Request one page of portfolios
        payload = pw_get(
            "/portfolios",
            params={
                "limit": PAGE_SIZE,
                "offset": offset
            }
        )

        # Pull the list of records out of the response
        batch = extract_records(payload)

        # Empty page means we have everything
        if not batch:
            break

        # Add this page to the running list
        portfolios.extend(batch)

        log.info(
            "Retrieved %s portfolios so far.",
            len(portfolios)
        )

        # A short page means it was the last one
        if len(batch) < PAGE_SIZE:
            break

        # Move to the next page
        offset += len(batch)

    log.info(
        "Total Propertyware portfolios retrieved: %s",
        len(portfolios)
    )

    return portfolios


def get_portfolio_details(portfolio_id):
    # Fetch the full portfolio record (includes the report ID)
    payload = pw_get(f"/portfolios/{portfolio_id}")
    return unwrap_object(payload)


def get_portfolio_id(portfolio):
    # The ID key name varies, so try each known variant
    for key in ("portfolioID", "portfolioId", "id"):
        value = portfolio.get(key)

        if value is not None:
            return str(value)

    # No ID found
    return None


def get_portfolio_name(portfolio):
    # The name key name varies, so try each known variant
    for key in (
        "portfolioName",
        "portfolio_name",
        "name"
    ):
        value = portfolio.get(key)

        if value:
            return str(value).strip()

    # No name found
    return None


# ============================================================
# PROPERTYWARE BROWSER LOGIN
# ============================================================

def is_login_page(page):
    # A visible password field means we are looking at a login form
    try:
        return page.locator("input[type='password']:visible").count() > 0
    except Exception:
        return False



def login(page):
    """
    Logs in to Propertyware using the automation user's credentials.
    Captures diagnostics immediately after login submission so we can
    investigate authentication failures and unexpected redirects.
    """

    log.info("Logging in to Propertyware.")

    # Open the login page
    page.goto(
        PW_LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=120000
    )

    # Wait for the password field
    password_field = page.locator(
        "input[type='password']:visible"
    ).first

    try:
        password_field.wait_for(
            state="visible",
            timeout=60000
        )
    except PlaywrightTimeoutError as exc:
        page.screenshot(
            path="login_form_missing.png",
            full_page=True
        )

        page_text = " ".join(
            page.locator("body").inner_text().split()
        )[:1500]

        log.error(
            "PROPERTYWARE LOGIN FORM MISSING | URL: %s | "
            "TITLE: %s | TEXT: %s",
            page.url,
            page.title(),
            page_text
        )

        raise LoginError(
            f"Login form not found at {page.url}. "
            "See login_form_missing.png."
        ) from exc

    # Find the username field
    username_field = page.locator(
        "input[type='text']:visible, input[type='email']:visible"
    ).first

    if username_field.count() == 0:
        page.screenshot(
            path="username_field_missing.png",
            full_page=True
        )
        raise LoginError(
            "Propertyware username field was not found."
        )

    # Enter credentials
    username_field.fill(PW_USERNAME)
    password_field.fill(PW_PASSWORD)

    # Find the submit button
    submit = page.locator(
        "input[type='submit']:visible, "
        "button[type='submit']:visible"
    ).first

    # Submit the login form
    try:
        with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=60000
        ):
            if submit.count() > 0:
                submit.click()
            else:
                password_field.press("Enter")

    except PlaywrightTimeoutError:
        # Some login forms submit without a full navigation
        log.info(
            "No navigation event detected after login submission."
        )

    # Allow the response to finish
    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=30000
        )
    except PlaywrightTimeoutError:
        pass

    # Allow client-side redirects and page rendering
    page.wait_for_timeout(3000)

    # Capture diagnostics regardless of the login outcome
    try:
        page.screenshot(
            path="propertyware_after_login.png",
            full_page=True
        )
    except Exception as exc:
        log.warning(
            "Could not capture post-login screenshot: %s",
            exc
        )

    try:
        page_text = " ".join(
            page.locator("body").inner_text().split()
        )[:1500]
    except Exception:
        page_text = "Unable to read page text."

    password_count = page.locator(
        "input[type='password']:visible"
    ).count()

    log.info(
        "PROPERTYWARE POST-LOGIN DIAGNOSTICS | "
        "URL: %s | TITLE: %s | TEXT: %s",
        page.url,
        page.title(),
        page_text
    )

    log.info(
        "Password fields visible after login: %s",
        password_count
    )

    # A visible password field means the login form is still present
    if password_count > 0:
        page.screenshot(
            path="login_failed.png",
            full_page=True
        )

        log.error(
            "Propertyware login form is still visible after submission."
        )

        raise LoginError(
            "Propertyware login did not complete. "
            "Review propertyware_after_login.png and login_failed.png "
            "and the post-login diagnostic log."
        )

    log.info(
        "Propertyware login form is no longer visible. "
        "Post-login state captured for verification. Current URL: %s",
        page.url
    )


# ============================================================
# PROPERTYWARE REPORT RETRIEVAL
# ============================================================

def build_report_url(portfolio_id, report_id):
    # Query parameters that tell Propertyware which report to open
    params = {
        "showBackMngmSum": "true",
        "entityID": str(report_id),  # Owner Statement report ID
        "propertyFilterIDs": str(portfolio_id),  # Limit to this portfolio
        "propertyFilterType": "1",
        "dateRangeFunction": "thisYear()",
        "startDate": START_DATE,
        "endDate": END_DATE,
        "retUrl": "../management/management.do",
    }

    # Combine base URL and encoded query string
    return f"{PW_REPORT_URL}?{urlencode(params)}"


def retrieve_statement_html(page, portfolio_id, report_id):
    # Build the report URL for this portfolio
    report_url = build_report_url(
        portfolio_id,
        report_id
    )

    log.info(
        "Opening Owner Statement for portfolio %s.",
        portfolio_id
    )

    # Open the report page in the browser
    page.goto(
        report_url,
        wait_until="domcontentloaded",
        timeout=120000  # 2 minutes
    )

    # Let any JavaScript redirects finish before checking the page
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeoutError:
        pass  # Page may keep polling; carry on to the checks below

    # Detect an expired session (login URL or a password field on the page)
    if is_login_page(page):
        page.screenshot(path="session_expired.png")  # Evidence for debugging
        raise SessionExpiredError(
            "Propertyware redirected to login during the run."
        )

    # Wait for the Ext framework; if it never loads, report which page we are on
    try:
        page.wait_for_function(
            "() => window.Ext && window.Ext.Ajax",
            timeout=60000  # 1 minute
        )
    except PlaywrightTimeoutError as exc:
        page.screenshot(path="ext_timeout.png")
        raise RuntimeError(
            f"Ext framework did not load. Current URL: {page.url}"
        ) from exc

    # Run the report refresh inside the page and return its HTML
    result = page.evaluate("""
() => new Promise((resolve, reject) => {

    // Make sure the Propertyware Ext framework is available
    if (!window.Ext || !Ext.Ajax) {
        reject(new Error("Propertyware Ext application unavailable."));
        return;
    }

    // Ask Propertyware to generate the report
    Ext.Ajax.request({
        url: "reports.do",
        baseParams: window.noIECacheParameter || {},
        timeout: 1200000,  // 20 minutes
        params: {
            action: "RefreshHandmadeReport"
        },

        // Report generated: return the HTML text
        success: function(response) {
            resolve(response.responseText);
        },

        // Report failed: return the HTTP status
        failure: function(response) {
            reject(new Error(
                "Owner Statement request failed. HTTP status: " +
                response.status
            ));
        }
    });
})
""")

    # An empty response cannot be parsed
    if not result or not result.strip():
        raise RuntimeError(
            "Propertyware returned an empty Owner Statement."
        )

    return result


# ============================================================
# HTML SUMMARY EXTRACTION
# ============================================================

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

    # Parse the report HTML
    soup = BeautifulSoup(html, "html.parser")

    ending_balance_matches = []  # Every Ending Balance value found
    unpaid_bills_matches = []  # Every Unpaid Bills value found

    # Check every table row in the report
    for row in soup.find_all("tr"):
        # Only direct cells of this row (ignore nested tables)
        cells = row.find_all(
            ["td", "th"],
            recursive=False
        )

        # Get the visible text of each cell
        values = [
            cell.get_text(" ", strip=True)
            for cell in cells
        ]

        # Drop empty cells
        values = [value for value in values if value]

        # Need at least a label and a value
        if len(values) < 2:
            continue

        # Clean up the label: collapse spaces, drop trailing colon, lower-case
        label = re.sub(
            r"\s+",
            " ",
            values[0]
        ).strip().rstrip(":").casefold()

        # Exact label match: take the last cell as the amount
        if label == "ending balance":
            ending_balance_matches.append(
                money_to_decimal(values[-1])
            )

        elif label == "unpaid bills":
            unpaid_bills_matches.append(
                money_to_decimal(values[-1])
            )

    # Verify each value is present and consistent
    ending_balance = extract_unique_summary_amount(
        ending_balance_matches,
        "Ending Balance"
    )

    unpaid_bills = extract_unique_summary_amount(
        unpaid_bills_matches,
        "Unpaid Bills"
    )

    return ending_balance, unpaid_bills


# ============================================================
# HUBSPOT EXISTING PORTFOLIOS
# ============================================================

def get_hubspot_portfolios():
    log.info("Retrieving existing HubSpot Portfolio records.")

    records = []  # Collected HubSpot records
    after = None  # Pagination cursor

    while True:
        # Request these properties for each record
        params = {
            "limit": 100,
            "properties": (
                "geekly_portfolio_name,"
                "total_portfolio_balance,"
                "unpaid_bills_by_portfolio"
            )
        }

        # Continue from the previous page when we have a cursor
        if after:
            params["after"] = after

        # Fetch one page of Portfolio records
        response = hs_session.get(
            f"{HUBSPOT_BASE_URL}/crm/v3/objects/"
            f"{HUBSPOT_OBJECT_TYPE}",
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        # Raise an error for any failed response
        response.raise_for_status()

        payload = response.json()

        # Add this page of results
        records.extend(payload.get("results", []))

        # Get the cursor for the next page, if any
        next_page = (
            payload.get("paging", {})
            .get("next", {})
            .get("after")
        )

        # No cursor means this was the last page
        if not next_page:
            break

        after = next_page

    log.info(
        "Existing HubSpot Portfolio records retrieved: %s",
        len(records)
    )

    return records


def build_hubspot_name_index(records):
    # Map normalized portfolio name -> list of matching HubSpot records
    index = {}

    for record in records:
        properties = record.get("properties", {})

        # Portfolio name stored in HubSpot
        name = properties.get("geekly_portfolio_name")

        # Skip records without a name
        if not name:
            continue

        # Normalize so matching ignores case and extra spaces
        key = normalize_name(name)

        # A list is used so duplicate names can be detected later
        index.setdefault(key, []).append(record)

    return index


def update_hubspot_portfolio(record_id, ending_balance, unpaid_bills):
    # Format amounts as 2-decimal strings for HubSpot
    ending_balance_value = format(ending_balance, ".2f")
    unpaid_bills_value = format(unpaid_bills, ".2f")

    # HubSpot properties to update
    properties = {
        "total_portfolio_balance": ending_balance_value,
        "unpaid_bills_by_portfolio": unpaid_bills_value
    }

    # Dry run: log what would happen, but change nothing
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

    # Live run: send the update to HubSpot
    response = hs_session.patch(
        f"{HUBSPOT_BASE_URL}/crm/v3/objects/"
        f"{HUBSPOT_OBJECT_TYPE}/{record_id}",
        json={"properties": properties},
        timeout=REQUEST_TIMEOUT
    )

    # Raise an error if the update failed
    response.raise_for_status()

    log.info(
        "Updated HubSpot Portfolio record %s: "
        "total_portfolio_balance = %s; "
        "unpaid_bills_by_portfolio = %s",
        record_id,
        ending_balance_value,
        unpaid_bills_value
    )


# ============================================================
# MAIN SYNCHRONIZATION
# ============================================================

def main():
    log.info("Starting Propertyware to HubSpot balance sync.")

    # Log the run settings for the audit trail
    log.info("Dry run enabled: %s", DRY_RUN)
    log.info("Statement start date: %s", START_DATE)
    log.info("Statement end date: %s", END_DATE)

    # Get every portfolio from Propertyware
    propertyware_portfolios = get_all_portfolios()

    # Get existing HubSpot records and index them by name
    hubspot_records = get_hubspot_portfolios()
    hubspot_index = build_hubspot_name_index(hubspot_records)

    results = []  # One audit row per portfolio

    # Running totals shown in the final summary
    counters = {
        "processed": 0,
        "updated": 0,
        "dry_run": 0,
        "zero_balance": 0,
        "skipped": 0,
        "failed": 0
    }

    with sync_playwright() as playwright:

        # Start a headless Chromium browser
        browser = playwright.chromium.launch(
            headless=True
        )

        # Create a clean browser context (no saved session)
        context = browser.new_context()

        # Open one tab, reused for every portfolio
        page = context.new_page()

        try:
            # Log in once at the start of the run
            login(page)

            for portfolio in propertyware_portfolios:

                counters["processed"] += 1

                # Basic details from the portfolio list
                portfolio_id = get_portfolio_id(portfolio)
                portfolio_name = get_portfolio_name(portfolio)

                # Audit row, filled in as processing progresses
                result = {
                    "portfolio_id": portfolio_id,
                    "portfolio_name": portfolio_name,
                    "ending_balance": "",
                    "unpaid_bills": "",
                    "hubspot_record_id": "",
                    "status": ""
                }

                try:
                    # A portfolio without an ID cannot be processed
                    if not portfolio_id:
                        raise ValueError(
                            "Propertyware portfolio ID missing."
                        )

                    # Fetch full details (contains the report ID)
                    details = get_portfolio_details(portfolio_id)

                    # Prefer the name from the detailed record
                    portfolio_name = (
                        get_portfolio_name(details)
                        or portfolio_name
                    )

                    result["portfolio_name"] = portfolio_name

                    # Owner Statement report ID (key name varies)
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
                    try:
                        html = retrieve_statement_html(
                            page,
                            portfolio_id,
                            report_id
                        )
                    except SessionExpiredError:
                        # Session dropped mid-run: log in again and retry once
                        log.warning(
                            "Session expired mid-run. Logging in again."
                        )
                        login(page)
                        html = retrieve_statement_html(
                            page,
                            portfolio_id,
                            report_id
                        )

                    # Extract the verified summary amounts
                    ending_balance, unpaid_bills = (
                        extract_portfolio_summary(html)
                    )

                    # Store the amounts in the audit row
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

                    # Look up the HubSpot record by normalized name
                    name_key = normalize_name(portfolio_name)

                    matching_records = hubspot_index.get(
                        name_key,
                        []
                    )

                    # No HubSpot record with this name
                    if len(matching_records) == 0:
                        result["status"] = "SKIPPED - NO HUBSPOT MATCH"
                        counters["skipped"] += 1

                        log.warning(
                            "No HubSpot match for non-zero balance portfolio %s.",
                            portfolio_name
                        )

                        results.append(result)
                        continue

                    # More than one record: unsafe to guess which to update
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

                    # Exactly one match: safe to update
                    record_id = matching_records[0]["id"]

                    result["hubspot_record_id"] = record_id

                    # Write to HubSpot (or log only, in dry-run mode)
                    update_hubspot_portfolio(
                        record_id,
                        ending_balance,
                        unpaid_bills
                    )

                    # Record the outcome
                    if DRY_RUN:
                        result["status"] = "DRY RUN - VERIFIED"
                        counters["dry_run"] += 1
                    else:
                        result["status"] = "UPDATED"
                        counters["updated"] += 1

                except (SessionExpiredError, LoginError) as exc:
                    # Login cannot be recovered: stop now instead of
                    # failing every remaining portfolio one by one
                    result["status"] = f"FAILED - {str(exc)}"
                    counters["failed"] += 1

                    log.error(
                        "Stopping run; Propertyware login unavailable: %s",
                        exc
                    )

                    results.append(result)
                    break

                except Exception as exc:
                    # One bad portfolio must not stop the whole run
                    result["status"] = f"FAILED - {str(exc)}"
                    counters["failed"] += 1

                    log.exception(
                        "Portfolio processing failed: %s",
                        portfolio_name
                    )

                # Save this portfolio's audit row
                results.append(result)

        finally:
            # Always close the browser, even after an error
            context.close()
            browser.close()

    # Save audit report.
    with open(
        OUTPUT_CSV,
        "w",
        newline="",
        encoding="utf-8-sig"  # BOM so Excel opens it correctly
    ) as csv_file:

        # Column order of the audit file
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

        writer.writeheader()  # Header row
        writer.writerows(results)  # One row per portfolio

    # Print the final summary
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


# Run main() only when the file is executed directly
if __name__ == "__main__":
    main()
