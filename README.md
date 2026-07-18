# Wells Fargo statement downloader

This local Python tool downloads the PDF statements available on Wells Fargo's
**Statements and Documents** page. It does not ask for or store your username,
password, or MFA code. You sign in manually before the script connects.

## Install on macOS

Open Terminal in this folder and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

You do **not** need to run `playwright install chromium`. The recommended
workflow uses your existing Google Chrome installation.

## Recommended: sign in before the script connects

First, completely quit Google Chrome (`Chrome > Quit Google Chrome`). Then run
this command in Terminal to open your installed Chrome with a separate local
profile and a debugging port:

```bash
open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.wf-manual-profile"
```

In that Chrome window, manually type `wellsfargo.com`, sign in, complete MFA,
and open **Accounts > Statements & Docs**. Do this before starting the Python
script. Playwright is not connected while you enter credentials.

With the statement links visible, return to Terminal and run:

```bash
python download_wells_fargo_statements.py \
  --connect-cdp http://127.0.0.1:9222
```

The script connects to that already-authenticated Chrome window. Return to
Terminal and press Enter when prompted. It then presents this menu:

```text
1. Statements for all years for all available accounts (default)
2. Statements for all years for the selected account
3. Statements for the selected year for the selected account
```

Press Enter at the menu to choose option 1. Accounts and time periods are
discovered independently from Wells Fargo at runtime. The script does not
assume an account count, account type, account name, first year, last year, or
current calendar year. It supports scrollable account and year lists, so it can
adapt as accounts are added or removed and as Wells Fargo's rolling history
moves forward. Numeric years are processed oldest-first. `Recent statements`
is intentionally skipped in the two all-years modes because those PDFs overlap
the explicit year views.
PDFs are filed by actual statement dates. Dynamic account/year dropdowns are sampled repeatedly
after account changes. Each failed statement is tried up to three times, with
a three-second pause between attempts, and anything still absent is reported
as `MISSING`. Files are placed under:

```text
Wells_Fargo_Statements/
  Account_Name_...1234/
    YYYY/
      YYYY-MM-DD_Statement.pdf
```

Existing nonempty PDFs are skipped. Add `--overwrite` to replace them.

Wells Fargo currently renders statement links as JavaScript-driven controls.
The downloader clicks each control and captures the resulting browser download,
PDF network response, or newly opened PDF tab. If a statement replaces the
current page with Chrome's PDF viewer, the script returns to the Statements
page, explicitly restores the selected account and year if Wells Fargo resets
the selector to `Recent statements`, and waits for the statement links before
clicking the next statement. It verifies both dropdown controls after every
PDF and can reopen the saved Statements URL if browser history does not restore
the page reliably. Scrollable dropdowns are reset to the top and traversed to
the bottom so older off-screen years are included.

## Useful options

```bash
# Save somewhere else
python download_wells_fargo_statements.py --output ~/Documents/BankStatements

# Slow down between PDFs if the site is sensitive to frequent requests
python download_wells_fargo_statements.py --delay 2.5

# Keep the local browser profile in a different location
python download_wells_fargo_statements.py --profile ~/.wf-statement-browser

# Use Playwright's bundled Chromium instead of installed Google Chrome
python download_wells_fargo_statements.py --browser chromium
```

The browser profile contains Wells Fargo session data. Keep it private. Delete
that profile directory after the download if you do not want the session kept.
The script never tries to bypass authentication, MFA, CAPTCHA, or a Wells Fargo
security challenge.

## If Wells Fargo says the username/password combination does not match

Stop retrying in that browser so you do not risk an account lock. Confirm that
the same credentials still work by manually opening your normal Chrome browser
and typing `wellsfargo.com` yourself; do not follow a link from an email or text.

Use the recommended connect workflow above so you sign in before the downloader
attaches. If Wells Fargo rejects login even before the Python script connects,
stop retrying and verify your credentials in your ordinary Chrome profile.

```bash
python download_wells_fargo_statements.py \
  --connect-cdp http://127.0.0.1:9222
```

If normal Chrome accepts the credentials but the downloader's Chrome window
does not, Wells Fargo is likely declining that automated browser session. Do
not attempt to circumvent the bank's security controls. In that case, close
the script and download statements manually or ask Wells Fargo whether it can
provide a statement archive.

## If Wells Fargo changes the page

The script primarily uses the visible labels from the current page:
`Select account`, `For time period`, and statement links such as
`Statement 12/31/19 (27K, PDF)`. If it reports that a control cannot be found,
choose menu option 3 to test the currently selected account and year. That can
still work when statement links remain visible even if Wells Fargo changes its
dropdown implementation.
