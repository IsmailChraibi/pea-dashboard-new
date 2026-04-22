# PEA Performance Dashboard — v2

A self-contained dashboard for tracking a French **Plan Epargne d'Actions (PEA)**.
Enter your trades directly in the app — no CSV editing, no code to touch.
Transactions persist automatically to a private Google Sheet.

## What's different from v1

- **In-app transaction editor**: add, edit, delete trades from the Transactions tab
- **Google Sheets persistence**: your data is automatically saved to a private
  Google Sheet. Access it from any device, view the edit history, restore old
  versions — all for free.
- **No transaction data in the repo**: the code is generic; your numbers live
  in your Sheet. The repo can be public without exposing anything personal.
- **Required explicit prices**: cost basis uses your entered prices, never a
  Yahoo Finance estimate. Accurate P&L by construction.

## Setup overview

There are four things to set up, in this order:

1. Create a Google Sheet (1 min)
2. Create a Google Cloud service account with access to the Sheet (5-8 min)
3. Put the service account credentials into Streamlit's secrets (2 min)
4. Deploy the app (2 min)

Total time: ~15 minutes the first time. You only do this once.

---

## Step 1 — Create the Google Sheet

1. Go to [sheets.google.com](https://sheets.google.com) and create a new blank sheet
2. Rename it (e.g. `PEA Transactions`)
3. Rename the first tab to `transactions` (lowercase, double-click the tab name
   at the bottom)
4. Put these four headers in row 1, columns A–D:
   ```
   Date    Ticker    Quantity    Price
   ```
5. **Copy the sheet URL** from your browser address bar — you'll paste it into
   secrets later. It looks like:
   ```
   https://docs.google.com/spreadsheets/d/1aBcDeFg...XYZ/edit
   ```

That's it for the Sheet.

---

## Step 2 — Create a Google Cloud service account

A service account is a "robot user" that the Streamlit app authenticates as when
reading/writing your Sheet. Google Cloud's UI is dense, but the actual steps are
short. Take a breath and follow along.

### 2a. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in
   with the same Google account that owns your Sheet
2. You'll see a project dropdown at the top of the page. Click it, then
   **"New project"**
3. **Project name**: `pea-dashboard` (or anything)
4. Leave Organization as is. Click **Create**. Wait ~15 seconds.
5. Make sure the new project is selected in the top dropdown

### 2b. Enable the Google Sheets and Drive APIs

Google blocks API calls by default; you need to enable the two APIs the app uses.

1. In the left sidebar (hamburger menu), navigate to
   **APIs & Services → Library**
2. Search for **Google Sheets API** → click it → click **Enable**
3. Go back to Library, search for **Google Drive API** → click it → click **Enable**

### 2c. Create the service account

1. Left sidebar: **IAM & Admin → Service Accounts**
2. Click **+ Create Service Account** at the top
3. **Service account name**: `streamlit-pea` (any name is fine)
4. Service account ID will auto-fill. Click **Create and continue**
5. On the "Grant this service account access to project" step, **skip it** —
   click **Continue**. (We'll grant access on the Sheet itself, not the project.)
6. On the "Grant users access to this service account" step, also skip. Click **Done**

You'll be back at the service accounts list. You should see your new account with
an email address like:
```
streamlit-pea@pea-dashboard-12345.iam.gserviceaccount.com
```
**Copy this email address** — you'll need it in a moment.

### 2d. Create a JSON key for the service account

1. Click your new service account in the list
2. Go to the **Keys** tab
3. Click **Add Key → Create new key → JSON → Create**
4. A JSON file downloads to your computer. **Keep it safe** — it's the equivalent
   of a password. Don't email it, don't commit it to Git, don't share it.

### 2e. Share your Google Sheet with the service account

This is the step people forget. Your service account is a user, and it needs
permission on your Sheet like any other user would.

1. Open your `PEA Transactions` Sheet
2. Click the **Share** button top right
3. Paste the service account email (the one from step 2c)
4. Set permission to **Editor**
5. **Uncheck "Notify people"** (sending an email to a service account makes no sense)
6. Click **Share**

---

## Step 3 — Configure Streamlit secrets

Streamlit apps read credentials from a file called `secrets.toml`. This file
is **never committed to your repo** — it lives only in Streamlit Cloud's
secure secret storage (or locally on your machine for local development).

### 3a. Open the service-account JSON file you downloaded

It looks like this:
```json
{
  "type": "service_account",
  "project_id": "pea-dashboard-12345",
  "private_key_id": "abc123...",
  "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQI...\n-----END PRIVATE KEY-----\n",
  "client_email": "streamlit-pea@pea-dashboard-12345.iam.gserviceaccount.com",
  "client_id": "987654...",
  ...
}
```

### 3b. Build the secrets.toml content

Create a block of TOML text like this (fill in your values from the JSON file
and your Sheet URL from step 1):

```toml
[gsheets]
sheet_url = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"
worksheet = "transactions"

[gcp_service_account]
type = "service_account"
project_id = "pea-dashboard-12345"
private_key_id = "abc123..."
private_key = "-----BEGIN PRIVATE KEY-----\nMIIEvQI...\n-----END PRIVATE KEY-----\n"
client_email = "streamlit-pea@pea-dashboard-12345.iam.gserviceaccount.com"
client_id = "987654..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
universe_domain = "googleapis.com"
```

**Important formatting notes**:
- String values must be in double quotes
- The `private_key` must preserve the `\n` newline escapes exactly as they
  appear in the JSON file. Don't expand them to real newlines.
- Copy every field from the JSON file. Don't skip any.

There's a template at `.streamlit/secrets.toml.example` you can copy.

### 3c. Save the secrets

**For local development:**

1. Create a file at `.streamlit/secrets.toml` (next to your `app.py`)
2. Paste your TOML content into it
3. This file is in `.gitignore` and will never be committed

**For Streamlit Cloud:**

1. Deploy the app first (see Step 4 below)
2. On your app's page in Streamlit Cloud, click ⋮ → **Settings → Secrets**
3. Paste the entire TOML content into the big text box
4. Click **Save**. The app auto-redeploys with the new secrets.

---

## Step 4 — Deploy

1. Push this folder to a GitHub repo (public is fine; secrets are not in the repo)
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in
3. Click **Create app** → select your repo → branch `main` → main file `app.py`
4. Click **Deploy**. First build takes ~2 min.
5. Once deployed, add your secrets via the Settings → Secrets interface (step 3c)
6. Lock down viewer access: ⋮ → Settings → Sharing → "Only specific people" →
   add your email

---

## Using the app

**First launch:** The app loads with an empty transactions list. Go to the
**Transactions** tab and either use the "Add a new transaction" form, edit the
inline table directly, or bulk-import a CSV/XLSX.

**Every change is auto-saved to your Google Sheet.** You can verify by opening
the Sheet in a browser tab — it'll update live.

**From any device:** The same Sheet is readable from Google Sheets on your
phone, tablet, or any browser. The Streamlit app reflects whatever's in the
Sheet, so if you edit directly in Google Sheets, the next app load picks it up.

**Required transaction columns:**
- `Date` — YYYY-MM-DD
- `Ticker` — must be one of the supported `EPA:XXX` codes (see `INSTRUMENTS` in app.py)
- `Quantity` — positive for buys, negative for sells
- `Price` — unit price in EUR, always required

## Supported instruments

The app recognizes these Euronext Paris tickers out of the box. Add more by
editing the `INSTRUMENTS` and `GEO_EXPOSURE` dicts at the top of `app.py`.

| Ticker | Name | Category |
|---|---|---|
| EPA:C40 | Amundi CAC 40 ESG ETF | France |
| EPA:ALO | Alstom SA | France |
| EPA:OBLI | Amundi Euro Gov Bond ETF | Eurozone |
| EPA:P500H | Amundi PEA S&P 500 ESG Hedged | US |
| EPA:PE500 | Amundi PEA S&P 500 ESG | US |
| EPA:PLEM | Amundi PEA MSCI EM EMEA | EM EMEA |
| EPA:CW8 | Amundi MSCI World Swap | DM Global |
| EPA:HLT | Amundi STOXX Europe 600 Healthcare | Europe |
| EPA:PAEEM | Amundi PEA MSCI EM ESG | EM Global |

## Troubleshooting

**"Could not read Google Sheet"** at app start → the service account doesn't
have access. Check that you shared the Sheet with the service account email
(step 2e) and that the Sheets API is enabled (step 2b).

**Secrets error on startup** → your `secrets.toml` is malformed. Most common
cause: the `private_key` field lost its `\n` escapes. Re-paste from the JSON
file.

**"This app has encountered an error"** after edit → usually a race condition
on the Sheet. Refresh the page, the changes are most likely saved.

**Changes made in Google Sheets aren't showing** → click **🔁 Reload from
storage** in the sidebar to force a re-read.

**Want to run locally?**
```bash
pip install -r requirements.txt
# Create .streamlit/secrets.toml with your credentials
streamlit run app.py
```

## License

MIT. Personal tool, not investment advice.
