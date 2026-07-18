#!/usr/bin/env python3
"""
Download Wells Fargo statement PDFs after a user completes manual login.
This is intentionally interactive. It does not automate credentials, MFA, CAPTCHA, or other security challenges.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Locator, Page


START_URL = "https://www.wellsfargo.com/"
STATEMENT_RE = re.compile(r"^Statement\s+(\d{1,2})/(\d{1,2})/(\d{2,4}).*PDF", re.I)


def safe_name(value: str, fallback: str = "Unknown") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or fallback


def full_year(value: str) -> int:
    year = int(value)
    return 2000 + year if year < 70 else (1900 + year if year < 100 else year)


def ordered_periods(values: list[str]) -> list[str]:
    """Return unique explicit four-digit years, oldest-first."""
    unique = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    return sorted((value for value in unique if re.fullmatch(r"\d{4}", value)), key=int)


def normalized_option_text(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    lines = [line for line in lines if line.lower() != "selected"]
    return " ".join(lines)


async def prompt_download_mode() -> str:
    print("\nWhat would you like to download?")
    print("  1. Statements for all years for all available accounts (default)")
    print("  2. Statements for all years for the selected account")
    print("  3. Statements for the selected year for the selected account")
    while True:
        choice = (await asyncio.to_thread(input, "Choose 1, 2, or 3 [1]: ")).strip() or "1"
        if choice in {"1", "2", "3"}:
            return choice
        print("Please enter 1, 2, or 3.")


async def labeled_control(page: "Page", label: str) -> "Locator":
    # Wells Fargo currently renders the label inside a button-like dropdown.
    matches = page.get_by_text(label, exact=True)
    if await matches.count() == 0:
        raise RuntimeError(f"Could not find the '{label}' control.")
    text = matches.first
    for index in range(await matches.count()):
        candidate = matches.nth(index)
        try:
            if await candidate.is_visible():
                text = candidate
                break
        except Exception:
            pass

    button = text.locator("xpath=ancestor::button[1]")
    if await button.count():
        return button

    clickable = text.locator(
        "xpath=ancestor::*[@role='button' or @aria-haspopup][1]"
    )
    if await clickable.count():
        return clickable

    # Last resort for the card-like controls visible in the supplied screenshot.
    return text.locator("xpath=parent::*")


async def control_value(control: "Locator", label: str) -> str:
    text = (await control.inner_text()).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    values = [
        line
        for line in lines
        if line.lower() not in {label.lower(), "selected"}
    ]
    # Join all value lines so multi-line account names remain identical to the
    # normalized dropdown option text.
    return " ".join(values) if values else "Unknown"


async def open_options(page: "Page", label: str) -> list[str]:
    control = await labeled_control(page, label)
    await control.click()
    await page.wait_for_timeout(400)

    popup = page.locator("[role='listbox']:visible, [role='menu']:visible").first

    # The time-period menu is scrollable. Find its actual scrolling element and
    # start at the top so discovery is independent of its prior scroll position.
    if await popup.count():
        await popup.evaluate(
            """root => {
                const nodes = [root, ...root.querySelectorAll('*')];
                const scroller = nodes
                    .filter(node => node.scrollHeight > node.clientHeight + 2)
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) -
                                     (a.scrollHeight - a.clientHeight))[0];
                if (scroller) {
                    scroller.scrollTop = 0;
                    scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
                }
            }"""
        )
        await page.wait_for_timeout(250)

    values: list[str] = []
    for _ in range(30):
        candidates = page.locator(
            "[role='option']:visible, [role='listbox']:visible li:visible, "
            "[role='menu']:visible [role='menuitem']:visible"
        )
        for index in range(await candidates.count()):
            value = normalized_option_text(await candidates.nth(index).inner_text())
            if value and value not in values:
                values.append(value)

        if await popup.count() == 0:
            break
        scroll_state = await popup.evaluate(
            """root => {
                const nodes = [root, ...root.querySelectorAll('*')];
                const scroller = nodes
                    .filter(node => node.scrollHeight > node.clientHeight + 2)
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) -
                                     (a.scrollHeight - a.clientHeight))[0];
                if (!scroller) return {moved: false, bottom: true};
                const before = scroller.scrollTop;
                const maximum = scroller.scrollHeight - scroller.clientHeight;
                scroller.scrollTop = Math.min(maximum, before +
                    Math.max(100, Math.floor(scroller.clientHeight * 0.8)));
                scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
                return {
                    moved: scroller.scrollTop > before,
                    bottom: scroller.scrollTop >= maximum - 1
                };
            }"""
        )
        if not scroll_state.get("moved") or scroll_state.get("bottom"):
            await page.wait_for_timeout(250)
            # One final loop is unnecessary for non-virtualized menus because
            # every option is already in the DOM; for virtualized menus collect
            # the newly rendered bottom options once before exiting.
            candidates = page.locator(
                "[role='option']:visible, [role='listbox']:visible li:visible, "
                "[role='menu']:visible [role='menuitem']:visible"
            )
            for index in range(await candidates.count()):
                value = normalized_option_text(await candidates.nth(index).inner_text())
                if value and value not in values:
                    values.append(value)
            break
        await page.wait_for_timeout(250)
    await page.keyboard.press("Escape")
    return values


async def discover_options(page: "Page", label: str, attempts: int = 4) -> list[str]:
    """Retry a dynamic Wells Fargo dropdown and retain the fullest result."""
    best: list[str] = []
    for attempt in range(attempts):
        try:
            values = await open_options(page, label)
            if len(values) > len(best):
                best = values
        except Exception:
            pass
        if attempt + 1 < attempts:
            await page.wait_for_timeout(1250)
    return best


async def select_option(page: "Page", label: str, value: str) -> None:
    control = await labeled_control(page, label)
    if await control_value(control, label) == value:
        return
    await control.click()
    await page.wait_for_timeout(250)

    option = page.get_by_role("option", name=value, exact=True)
    if await option.count() == 0:
        option = page.get_by_text(value, exact=True).locator(
            "xpath=ancestor-or-self::*[self::li or @role='option' or @role='menuitem'][1]"
        )
    if await option.count() == 0:
        await page.keyboard.press("Escape")
        raise RuntimeError(f"Could not select {label!r} option {value!r}.")

    await option.first.click()
    # Account/year changes briefly remove the controls while statement data is
    # refreshed. Do not let the next operation race that re-render.
    for _ in range(30):
        try:
            await labeled_control(page, "Select account")
            await labeled_control(page, "For time period")
            break
        except Exception:
            await page.wait_for_timeout(500)
    await page.wait_for_timeout(1500)


async def current_selection(page: "Page") -> tuple[str, str]:
    account = await control_value(await labeled_control(page, "Select account"), "Select account")
    year = await control_value(await labeled_control(page, "For time period"), "For time period")
    return account, year


async def ensure_statements_page(page: "Page", statements_url: str) -> bool:
    """Recover from a PDF viewer and verify both statement controls exist."""
    for attempt in range(4):
        account_label = page.get_by_text("Select account", exact=True)
        year_label = page.get_by_text("For time period", exact=True)
        account_visible = year_visible = False
        for index in range(await account_label.count()):
            try:
                account_visible = account_visible or await account_label.nth(index).is_visible()
            except Exception:
                pass
        for index in range(await year_label.count()):
            try:
                year_visible = year_visible or await year_label.nth(index).is_visible()
            except Exception:
                pass
        if account_visible and year_visible:
            return True

        try:
            if attempt < 2 and page.url != statements_url:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
            else:
                await page.goto(statements_url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
    return False


async def download_via_click(
    context: "BrowserContext", page: "Page", link: "Locator", destination: Path
) -> bool:
    """Capture a JavaScript-driven statement link as download, response, or tab."""
    downloads = []
    pdf_responses = []
    original_pages = set(context.pages)
    original_url = page.url

    def on_download(download) -> None:
        downloads.append(download)

    def on_response(response) -> None:
        content_type = response.headers.get("content-type", "").lower()
        if "application/pdf" in content_type:
            pdf_responses.append(response)

    page.on("download", on_download)
    context.on("response", on_response)
    try:
        await link.click()

        # Wells Fargo may emit a browser download, fetch a PDF, or open a PDF tab.
        for _ in range(30):
            if downloads or pdf_responses or set(context.pages) - original_pages:
                break
            await page.wait_for_timeout(200)
        await page.wait_for_timeout(500)

        if downloads:
            await downloads[0].save_as(str(destination))
            return destination.exists() and destination.stat().st_size > 0

        for response in pdf_responses:
            try:
                body = await response.body()
                if body.startswith(b"%PDF"):
                    destination.write_bytes(body)
                    return True
            except Exception:
                pass

        new_pages = list(set(context.pages) - original_pages)
        for pdf_page in new_pages:
            try:
                await pdf_page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            url = pdf_page.url
            if url and not url.startswith(("about:", "blob:", "chrome-extension:")):
                try:
                    response = await context.request.get(url)
                    body = await response.body()
                    content_type = response.headers.get("content-type", "").lower()
                    if response.ok and (body.startswith(b"%PDF") or "pdf" in content_type):
                        destination.write_bytes(body)
                        return True
                except Exception:
                    pass
            try:
                await pdf_page.close()
            except Exception:
                pass
        return False
    finally:
        page.remove_listener("download", on_download)
        context.remove_listener("response", on_response)
        # Close a PDF tab when Wells Fargo opened one instead of navigating the
        # original page.
        for extra_page in list(set(context.pages) - original_pages):
            try:
                await extra_page.close()
            except Exception:
                pass

        # Navigation into Chrome's PDF viewer can finish shortly after the PDF
        # response is captured. Verify the actual controls, not only the URL.
        await page.wait_for_timeout(750)
        await ensure_statements_page(page, original_url)


async def download_current_view(
    context: "BrowserContext",
    page: "Page",
    output: Path,
    overwrite: bool,
    delay: float,
) -> tuple[int, int]:
    account, selected_year = await current_selection(page)
    links = page.locator("a").filter(has_text=re.compile(r"^Statement\s+", re.I))
    statement_labels = [" ".join(value.split()) for value in await links.all_inner_texts()]
    downloaded = skipped = 0
    expected_files: list[Path] = []

    for label in statement_labels:
        match = STATEMENT_RE.search(label)
        if not match:
            continue

        # Re-resolve the link after every PDF because the prior click may have
        # navigated away from and then restored the Statements page.
        link = page.get_by_role("link", name=label, exact=True).first
        try:
            await link.wait_for(state="visible", timeout=15000)
        except Exception:
            link = page.locator("a").filter(has_text=re.compile(re.escape(label), re.I)).first
            await link.wait_for(state="visible", timeout=15000)

        month, day, year = map(int, match.groups())
        year = full_year(str(year))
        folder = output / safe_name(account) / str(year)
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{year:04d}-{month:02d}-{day:02d}_Statement.pdf"
        expected_files.append(destination)

        if destination.exists() and destination.stat().st_size > 0 and not overwrite:
            print(f"  SKIP {destination}")
            skipped += 1
            continue

        href = await link.get_attribute("href")
        saved = False
        if href and not href.lower().startswith("javascript:"):
            try:
                response = await context.request.get(urljoin(page.url, href))
                body = await response.body()
                content_type = response.headers.get("content-type", "").lower()
                if response.ok and (body.startswith(b"%PDF") or "pdf" in content_type):
                    destination.write_bytes(body)
                    saved = True
            except Exception:
                pass

        for attempt in range(3):
            if saved:
                break
            saved = await download_via_click(context, page, link, destination)

            # Wells Fargo returns to "Recent statements" after leaving the PDF
            # viewer. Restore the account/year that this batch is processing.
            try:
                restored_account, restored_year = await current_selection(page)
                if restored_account != account:
                    await select_option(page, "Select account", account)
                    restored_account, restored_year = await current_selection(page)
                if restored_year != selected_year:
                    await select_option(page, "For time period", selected_year)
            except Exception as exc:
                print(
                    f"  WARN PDF was handled, but could not restore "
                    f"{account} / {selected_year}: {exc}"
                )
            if not saved and attempt < 2:
                print(f"  RETRY {attempt + 2}/3 in 3 seconds: {label}")
                await page.wait_for_timeout(3000)

        if saved:
            print(f"  SAVED {destination}")
            downloaded += 1
        else:
            print(f"  WARN Could not capture PDF after clicking: {label}")
        if delay:
            await page.wait_for_timeout(int(delay * 1000))

    if downloaded == 0 and skipped == 0:
        print(f"  WARN No statement PDF links found for {account} / {selected_year}")
    missing = [path for path in expected_files if not path.exists() or path.stat().st_size == 0]
    for path in missing:
        print(f"  MISSING {path}")
    return downloaded, skipped


async def run(args: argparse.Namespace) -> int:
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError:
        print(
            "Playwright is not installed. Run: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    output = Path(args.output).expanduser().resolve()
    profile = Path(args.profile).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        attached = bool(args.connect_cdp)
        browser = None
        if attached:
            try:
                browser = await playwright.chromium.connect_over_cdp(args.connect_cdp)
            except Exception as exc:
                print(f"Could not connect to Chrome at {args.connect_cdp}: {exc}", file=sys.stderr)
                print("Launch Chrome with remote debugging as shown in README.md.", file=sys.stderr)
                return 2
            if not browser.contexts:
                print("Connected to Chrome, but no browser context was available.", file=sys.stderr)
                return 2
            context = browser.contexts[0]
            page = context.pages[-1] if context.pages else await context.new_page()
            print("\nConnected to your existing Chrome window.")
            print("Open Accounts > Statements & Docs in that window, select an")
            print("account/year, and make sure the statement links are visible.")
            await asyncio.to_thread(input, "Then return here and press Enter... ")
        else:
            context = await playwright.chromium.launch_persistent_context(
                str(profile),
                channel=None if args.browser == "chromium" else args.browser,
                headless=False,
                accept_downloads=True,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(START_URL, wait_until="domcontentloaded")

            print(f"\nOpened Wells Fargo in {args.browser}.")
            print("Use the site's Sign On link, sign in manually, complete MFA,")
            print("and open Accounts > Statements & Docs.")
            print("Select any account/year and make sure its statement links are visible.")
            await asyncio.to_thread(input, "Then return here and press Enter... ")

        try:
            account, year = await current_selection(page)
        except Exception as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            print("Make sure the Statements and Disclosures section is open.", file=sys.stderr)
            if not attached:
                await context.close()
            return 2

        print(f"\nCurrent view: {account} / {year}")
        mode = await prompt_download_mode()
        if mode == "3":
            downloaded, skipped = await download_current_view(
                context, page, output, args.overwrite, args.delay
            )
        else:
            statements_url = page.url
            # Bulk modes intentionally skip "Recent statements" because those
            # documents overlap the explicit year views.
            downloaded = skipped = 0
            if mode == "2":
                accounts = [account]
            else:
                accounts = await discover_options(page, "Select account")
                if account not in accounts:
                    accounts.insert(0, account)
                if not accounts:
                    accounts = [account]

            accounts = list(dict.fromkeys(accounts))
            print(f"\nDiscovered {len(accounts)} account choice(s):")
            for value in accounts:
                print(f"  - {value}")

            for account_value in accounts:
                try:
                    if not await ensure_statements_page(page, statements_url):
                        raise RuntimeError("Could not recover the Statements and Documents page")
                    await select_option(page, "Select account", account_value)
                    await page.wait_for_timeout(1500)
                    _, selected_period = await current_selection(page)
                    available_years = await discover_options(page, "For time period")
                    if selected_period not in available_years:
                        available_years.append(selected_period)
                    if account_value == account and year not in available_years:
                        available_years.append(year)
                    if not available_years:
                        _, current_year_value = await current_selection(page)
                        available_years = [current_year_value]

                    years = ordered_periods(available_years)

                    shown_years = ", ".join(years) if years else "none available"
                    print(f"\n{account_value}: {shown_years}")
                    if not years:
                        print(
                            f"  WARN No explicit four-digit year choices were "
                            f"discovered for {account_value}"
                        )
                    for year_value in years:
                        try:
                            if not await ensure_statements_page(page, statements_url):
                                raise RuntimeError(
                                    "Could not recover the Statements and Documents page"
                                )
                            current_account, _ = await current_selection(page)
                            if current_account != account_value:
                                await select_option(page, "Select account", account_value)
                            selected = False
                            for selection_attempt in range(3):
                                await select_option(page, "For time period", year_value)
                                try:
                                    selected_account, selected_year = await current_selection(page)
                                    if (
                                        selected_account == account_value
                                        and selected_year == year_value
                                    ):
                                        selected = True
                                        break
                                except Exception:
                                    pass
                                if selection_attempt < 2:
                                    await page.wait_for_timeout(1500)
                            if not selected:
                                raise RuntimeError(
                                    f"Year selection was not confirmed as {year_value}"
                                )
                            got, had = await download_current_view(
                                context, page, output, args.overwrite, args.delay
                            )
                            downloaded += got
                            skipped += had
                        except Exception as exc:
                            print(f"  WARN {account_value} / {year_value}: {exc}")
                except Exception as exc:
                    print(f"WARN Could not process account {account_value!r}: {exc}")

        print(f"\nFinished: {downloaded} downloaded, {skipped} already present")
        print(f"Output: {output}")
        if attached:
            print("The Chrome window was left open.")
        else:
            await asyncio.to_thread(input, "Press Enter to close the browser... ")
            await context.close()
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default="Wells_Fargo_Statements", help="Destination directory"
    )
    parser.add_argument(
        "--profile", default=".wf-browser-profile", help="Local Chromium profile directory"
    )
    parser.add_argument(
        "--browser",
        choices=("chrome", "chromium"),
        default="chrome",
        help="Installed Chrome (default) or Playwright Chromium",
    )
    parser.add_argument(
        "--connect-cdp",
        metavar="URL",
        help="Connect to an already-open Chrome debugging session (recommended)",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace existing PDFs")
    parser.add_argument(
        "--delay", type=float, default=1.0, help="Seconds between PDF requests (default: 1)"
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
