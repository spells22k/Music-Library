import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests


URL = (
    "https://www.whosampled.com/sample/214877/"
    "Statik-Selektah-Action-Bronson-Joey-Bada%24%24-Mike-Posner-"
    "The-Spark-Arthur-Verocai-Dedicada-a-Ela/"
)

OUTPUT_DIR = Path("whosampled_403_test")
OUTPUT_DIR.mkdir(exist_ok=True)


def print_separator(title):
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def save_response(name, response):
    path = OUTPUT_DIR / name

    try:
        path.write_text(response.text, encoding="utf-8")
    except Exception as exc:
        print(f"Could not save response body: {exc}")
        return

    print(f"BODY SAVED: {path.resolve()}")
    print(f"BODY LENGTH: {len(response.text):,} characters")


def print_response_details(response):
    print(f"STATUS:       {response.status_code}")
    print(f"FINAL URL:    {response.url}")
    print(f"REDIRECTS:    {len(response.history)}")
    print(f"CONTENT-TYPE: {response.headers.get('content-type')}")
    print(f"SERVER:       {response.headers.get('server')}")
    print(f"LOCATION:     {response.headers.get('location')}")
    print(f"CF-RAY:       {response.headers.get('cf-ray')}")
    print(f"CF-MITIGATED: {response.headers.get('cf-mitigated')}")
    print()

    print("RESPONSE HEADERS:")
    for key, value in response.headers.items():
        print(f"  {key}: {value}")


def test_request(name, headers=None):
    print_separator(f"TEST: {name}")

    session = requests.Session()

    # Don't let requests silently turn a redirect into a different
    # request without us seeing what happened.
    try:
        response = session.get(
            URL,
            headers=headers or {},
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(f"REQUEST FAILED: {type(exc).__name__}: {exc}")
        return None

    print_response_details(response)

    print()
    print("BODY PREVIEW:")
    print(response.text[:1000])

    save_response(f"{name}.html", response)

    return response


def test_wayback_availability():
    print_separator("TEST: WAYBACK AVAILABILITY")

    endpoint = "https://archive.org/wayback/available"

    params = {
        "url": URL,
    }

    try:
        response = requests.get(
            endpoint,
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"WAYBACK REQUEST FAILED: {type(exc).__name__}: {exc}")
        return None

    print(f"STATUS:    {response.status_code}")
    print(f"FINAL URL: {response.url}")
    print(f"CONTENT:   {response.text}")

    return response


def test_wayback_cdx():
    print_separator("TEST: WAYBACK CDX EXACT MATCH")

    # Important: exact match only.
    # We are NOT doing a host/prefix crawl.
    endpoint = "https://web.archive.org/cdx/search/cdx"

    params = {
        "url": URL,
        "output": "json",
        "filter": "statuscode:200",
        "fl": "timestamp,original,statuscode,mimetype,digest,length",
        "collapse": "digest",
    }

    try:
        response = requests.get(
            endpoint,
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"CDX REQUEST FAILED: {type(exc).__name__}: {exc}")
        return None

    print(f"STATUS:    {response.status_code}")
    print(f"FINAL URL: {response.url}")
    print()
    print(response.text[:5000])

    return response


def test_archive_save():
    print_separator("TEST: INTERNET ARCHIVE SAVE PAGE NOW")

    # This is the mutation that actually asks the Wayback Machine
    # to archive the current page.
    save_url = "https://web.archive.org/save/"

    try:
        response = requests.get(
            save_url,
            params={"url": URL},
            timeout=60,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        print(f"SAVE REQUEST FAILED: {type(exc).__name__}: {exc}")
        return None

    print_response_details(response)

    print()
    print("BODY PREVIEW:")
    print(response.text[:2000])

    save_response("archive_save_response.html", response)

    return response


def main():
    print_separator("WHOSAMPLED 403 DIAGNOSTIC")

    print(f"TARGET URL:\n{URL}")

    # ------------------------------------------------------------------
    # TEST 1: plain requests
    # ------------------------------------------------------------------

    test_request(
        "01_plain_requests",
        headers={
            "User-Agent": "python-requests/diagnostic",
        },
    )

    time.sleep(2)

    # ------------------------------------------------------------------
    # TEST 2: normal browser-ish request
    # ------------------------------------------------------------------

    test_request(
        "02_browser_headers",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        },
    )

    time.sleep(2)

    # ------------------------------------------------------------------
    # TEST 3: referer
    # ------------------------------------------------------------------

    test_request(
        "03_browser_with_referer",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.whosampled.com/",
            "Upgrade-Insecure-Requests": "1",
        },
    )

    # ------------------------------------------------------------------
    # TEST 4: does Internet Archive already know about this URL?
    # ------------------------------------------------------------------

    test_wayback_availability()

    time.sleep(2)

    # ------------------------------------------------------------------
    # TEST 5: exact CDX lookup
    # ------------------------------------------------------------------

    test_wayback_cdx()

    time.sleep(2)

    # ------------------------------------------------------------------
    # TEST 6: actually ask Wayback to archive it
    # ------------------------------------------------------------------

    test_archive_save()

    print_separator("DONE")

    print(
        """
Interpretation:

A) WhoSampled = 200
   Archive Save = 403

   => WhoSampled is NOT blocking the page request.
      The 403 is coming from the archive operation.

B) WhoSampled = 403
   Archive Save = not reached / also fails

   => We have a WhoSampled access problem that must be diagnosed
      independently of archiving.

C) WhoSampled = 200
   Wayback Availability = existing capture
   Archive Save = 403

   => The URL can already be archived/retrieved by Wayback, but
      the new Save Page Now request is being rejected.

D) WhoSampled = 403
   Browser request = 200

   => The problem is likely request fingerprint/header/session related.

All response bodies and diagnostic information are saved under:

    whosampled_403_test/
"""
    )


if __name__ == "__main__":
    main()
