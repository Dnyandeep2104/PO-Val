# Live demo runbook: email → Azure Blob → validation → Salesforce

This is the end-to-end demo. A colleague emails a real PO. It appears on the SOS review screen
within about a minute, with every step lit up live. You review two flagged items and click
**Book**. A real `Booking_Form__c` record appears in the poclab sandbox, with the Notes to RO, an
approval note and the original PO PDF attached.

Allow about an hour for the one-time setup, half an hour for a rehearsal, and 15 minutes on
demo day.

---

## The 90-second story (what the audience sees)

1. **Email arrives.** "Here's a PO my colleague just sent to the inbox." The banner lights up:
   *New purchase order arriving from …*
2. **The engine works through it live:** Received → Read PDF → Quote lookup → SOS checklist →
   Result. Each step shows what it found and how long it took.
3. **The result.** For example: *2 items need an SOS decision*. Everything else was checked
   automatically: the 11-item SOS checklist plus the quote reconciliation, in about half a second.
4. **SOS reviews.** Open the PO. The PO lines sit side by side with the quote lines. The two
   exceptions are explained in plain words. The SOS reviewer accepts them and adds a comment.
5. **One click: Book.** The record is created in Salesforce. Click **Open in Salesforce**: the
   Booking Form is there, with the PO PDF and the notes attached.

---

## 1. Update the code (5 min)

Copy the files from this package over your `po-validation` folder (same paths), then in Terminal:

```bash
cd ~/Desktop/po-validation
source .venv/bin/activate
pip install -r requirements.txt
python3 -m pytest -q tests/
```

## 2. Create your `.env` (5 min)

```bash
cp env.example .env
open -e .env
```

Fill it in as you go through the steps below. Never commit `.env`.

## 3. Salesforce login (10 min)

**Option A (recommended): the Salesforce CLI.** You log in once in the browser (normal SSO). After
that the app gets a fresh token by itself, so nothing expires halfway through the demo.

1. Install the CLI. Either download the macOS installer from
   https://developer.salesforce.com/tools/salesforcecli, or, if you have Node,
   run `npm install --global @salesforce/cli`.
2. Log in to poclab:
   ```bash
   sf org login web --instance-url https://f5--poclab.sandbox.my.salesforce.com --alias poclab
   ```
3. Check it worked: `sf org display --target-org poclab` should say *Connected*.
4. In `.env`, set `SF_CLI_ALIAS=poclab` and leave `SF_ACCESS_TOKEN` empty.

If the browser login is blocked (some orgs restrict the CLI), use Option B.

**Option B: paste a session id.** Get a session id the way you have been doing (Workbench →
Info → Session Information → *Session Id*). Put it in `SF_ACCESS_TOKEN=`. It expires after a few
hours, so get a fresh one on demo day.

## 4. Pick the demo opportunity in poclab (5 min)

The quote data in Snowflake comes from production, so the opportunity it points at usually does
not exist in poclab. The app then links the booking form to a sandbox stand-in opportunity, and
says so on screen.

1. In poclab, open an open opportunity you're allowed to change (or create one called
   "PO Validation Demo").
2. Copy its id from the address bar: the `006…` part of `/lightning/r/Opportunity/006…/view`.
3. In `.env`, set `SF_DEMO_OPPORTUNITY_ID=006…`.

The app sets that opportunity's PO number and amount to match the PO being booked, which is what
`test_poclab.py` was doing by hand to satisfy poclab's validation rules. This only ever happens in
a sandbox. Set `SF_ADAPT_DEMO_OPPORTUNITY=false` if you don't want it.

## 5. Live quotes from Snowflake (optional, 2 min)

Put your F5 email in `SNOWFLAKE_USER=`. When the portal starts, a browser window opens once for
SSO, the same as `test_snowflake.py`. Without it, the app uses `my_quotes.json`. That's fine for the
demo as long as the colleague sends one of those six POs, and the screen will say "quote export
file", not "live".

## 6. Azure Blob access (5 min)

Pick one of these and put it in `.env`:

- **Preferred:** a **container SAS** for `purchaseorders` with only **Read + List** permission and
  an expiry a few days after the demo. Put it in `AZURE_STORAGE_SAS_TOKEN=`; keep
  `AZURE_STORAGE_ACCOUNT_URL` pointing at your storage account. The app only ever reads.
- **Or:** the account's connection string (Azure portal → storage account → *Access keys*) in
  `AZURE_STORAGE_CONNECTION_STRING=`. This works too, but it is a master key, so keep it off
  shared drives.

Leave `AZURE_STORAGE_PREFIX=inbox/` so the app only watches the folder your flow writes to.

## 7. The Power Automate flow: email → Blob (20 min)

> **Licensing:** the Azure Blob Storage connector is **Premium** in Power Automate. If you can't add
> it, use the OneDrive fallback in step 7b. It needs only Standard connectors.

**First, stop your whole inbox from going to Blob.** In Outlook, create a folder **PO Demo** and a
rule: *from your colleague* (or *subject contains "PO"*) → *move to PO Demo*. The flow watches
only that folder. Otherwise every PDF anyone sends you would end up in the container.

Go to https://make.powerautomate.com → **Create** → **Automated cloud flow**.

1. **Trigger:** Office 365 Outlook, **When a new email arrives (V3)**.
   - Folder: `PO Demo`
   - Include Attachments: **Yes**; Only with Attachments: **Yes**
2. **Initialize variable**
   - Name `folder`, Type String, Value (Expression tab):
     ```
     concat('inbox/', formatDateTime(utcNow(),'yyyyMMdd-HHmmss'), '-', substring(guid(),0,6))
     ```
3. **Compose**. This holds the email details the screen shows ("from colleague@…"). Value
   (Expression tab):
   ```
   addProperty(addProperty(addProperty(json('{}'),'from',triggerOutputs()?['body/from']),'subject',triggerOutputs()?['body/subject']),'receivedDateTime',triggerOutputs()?['body/receivedDateTime'])
   ```
4. **Azure Blob Storage, Create blob (V2)**. This writes the email details.
   - Storage account name: your storage account
   - Folder path: `/purchaseorders/` followed by the `folder` variable, i.e.
     `/purchaseorders/@{variables('folder')}`
   - Blob name: `email.json`
   - Blob content (Expression tab): `string(outputs('Compose'))`
5. **Apply to each**, over **Attachments** from the trigger.
   - Inside it, a **Condition**: Expression `endsWith(toLower(items('Apply_to_each')?['name']), '.pdf')`
     *is equal to* `true`. This skips signature images like `image001.png`.
   - Under **If yes**, add **Create blob (V2)**:
     - Folder path: `/purchaseorders/@{variables('folder')}` (same as step 4)
     - Blob name: **Attachments Name**
     - Blob content: **Attachments Content**
6. **Save**, then send yourself a test email with a PO attached. In the Azure portal you should
   see `purchaseorders/inbox/<date-time>/email.json` and the PDF.

If the designer named your loop **For each** rather than **Apply to each**, use
`items('For_each')` in the expressions above.

If the PDF in Blob turns out to be text starting with `JVBERi0` (base64), set Blob content to
`base64ToBinary(items('Apply_to_each')?['contentBytes'])`. The app decodes that case anyway, so
the demo won't break either way.

### 7b. Fallback without Premium: email → OneDrive folder

Same flow, but use **OneDrive for Business → Create file** in place of each **Create blob (V2)**:

- Folder `/PO Demo Inbox`, File name **Attachments Name**, content **Attachments Content**.
- Optional, for the "from colleague@…" line: before the PDF's Create file, add another Create file
  named `@{items('Apply_to_each')?['name']}.json` with content `string(outputs('Compose'))`.

Make sure OneDrive syncs that folder to your Mac. Then in `.env`, set
`PORTAL_INBOX_DIR=/Users/<you>/Library/CloudStorage/OneDrive-<org>/PO Demo Inbox`. The app watches the folder
and moves processed files into a `processed/` subfolder.

## 8. Pre-flight check (2 min)

```bash
python3 review_queue.py --preflight --live
```

You want four OK lines:

```
  OK  Salesforce   <org> sandbox. Live: clicking Book creates real records.
  OK  Quotes       Live: f5-enterprisedataecosystem as YOU (APP_EDE_SALES_EXP_ROLE)
  OK  Azure Blob   Watching container 'purchaseorders' every 4s
  OK  Folder       Drop PDFs into inbound_pos/
```

Anything marked `!!` says exactly what is missing.

---

## 9. Rehearsal (the day before, 30 min)

1. Choose the PO your colleague will send. **Use the Dell PO (`PO706839`):** its quote matches, the
   freight line is handled correctly, and it has exactly two clear SOS decisions. WWT (one Inco
   Terms decision) and PayPal (three decisions) are good backups.
   Avoid NTT, Synnex and Carahsoft for now. Their quote lines in Snowflake are priced as one
   year of a multi-year term, or at zero for subscriptions, so they show price differences that
   are a known data issue, not a PO problem (see "Known gaps" below).
2. Create a `demo_queue/` folder with two or three *other* sample POs (say WWT and PayPal) so the
   queue isn't empty when you start. **Do not put the PO your colleague will email in there.**
   The app recognises an identical PDF and won't process it twice.
3. Start in live mode and run the whole thing once:
   ```bash
   python3 review_queue.py --serve --live --preload demo_queue
   ```
   Open http://127.0.0.1:8080. Have your colleague send the PO, then review it and book it.
   Open the record in Salesforce and check the PDF and notes are there.
4. Note how long the email takes to show up (usually under a minute, occasionally a few).
5. Leave the record you booked in poclab. It's your backup if Salesforce misbehaves on the day.
6. Reset, so demo day starts clean: stop the server (Ctrl+C) and run `python3 review_queue.py --reset`.

## 10. Demo day (T-15 minutes)

```bash
cd ~/Desktop/po-validation && source .venv/bin/activate
python3 review_queue.py --reset
python3 review_queue.py --preflight --live          # four OK lines
python3 review_queue.py --serve --live --preload demo_queue
```

- If you use Snowflake live, complete the SSO window that opens.
- Open http://127.0.0.1:8080 in a full-screen browser window, zoomed to 110–125%.
- The header must say **LIVE · <org>** in green and the tiles must show *Watching "purchaseorders"*.
- Keep the Terminal visible on a second screen if you can; it prints each step as it happens.
- Tell your colleague: send the Dell PO to you, with "PO" in the subject, when you give the signal.

## 11. If something goes wrong

| What happens | What to do (calmly) |
|---|---|
| Email hasn't shown up after 2 minutes | "Power Automate is taking its time. Same PDF, same engine." Click **+ Add a PO** and drop the PDF in. Identical processing. |
| A "Same PDF as … received earlier" notice | You didn't reset after rehearsal. Open the existing PO from the queue; it's the same result. |
| Booking fails with "session expired" | Option A refreshes by itself. With Option B, get a fresh session id, put it in `.env`, restart (Ctrl+C, rerun the serve command). |
| Booking fails with a validation-rule message | The real Salesforce message is shown. Say: "That's poclab's own rule, which is exactly what we'd want it to catch." Open the record you booked in rehearsal. |
| The header shows SIMULATION | You started without `--live`. Restart with it. Orders already on screen keep their state. |
| Laptop asleep / Wi-Fi changed | Restart the server. Nothing is lost: orders are saved in `out/portal/`. |

## 12. Honest answers for Q&A

- **"Is that real Salesforce?"** Yes. It's the poclab sandbox, a real org. The app refuses to
  write to any org that isn't a sandbox.
- **"Whose opportunity is it on?"** A sandbox stand-in, because the production opportunity doesn't
  exist in poclab. The screen says so. In production it links to the opportunity on the quote,
  with no substitution.
- **"Is this AI?"** No. Reading the PDF, the checks and the decisions are deterministic, and every
  number can be traced back to the page. AI is planned only for layouts the parser has never
  seen.
- **"How fast is it?"** The processing time on screen is measured, typically under a second. The
  step-by-step animation is slowed down slightly (`PORTAL_STAGE_PACING_MS`) so people can follow it.
- **"Does it book without a person?"** No. SOS asked for a human decision before every booking
  form, so booking only happens on the click, with the approver's name recorded on the record.

---

## Known gaps (say these before someone else finds them)

1. **Quote prices in Snowflake.** `pi_total_price_c` is the annual price for multi-year support,
   zero for subscription and training lines, and trade-in credits are separate negative lines. NTT,
   Synnex and Carahsoft show differences because of this. The fix is to multiply by term, net the
   credits, or find the right column. This is the next data task.
2. **Party checks against Salesforce** (Bill To = quote account, payment terms ≤ account default,
   End User / Reseller match) show "Not checked": the quote source doesn't return those fields yet.
3. **Single laptop.** The portal has no login and runs on `127.0.0.1`, which is right for a demo
   but not for a team.

## What "deployment-ready" means after the demo (for Digital)

| Area | Today (demo) | For production |
|---|---|---|
| Where it runs | Your laptop | A small container/VM or Databricks App, behind F5 SSO |
| Salesforce auth | CLI login or session id | JWT connected app (`SalesforceClient.from_jwt`, already in the code) |
| Snowflake auth | Browser SSO | Key-pair service user (`SNOWFLAKE_PRIVATE_KEY_PATH`, already supported) |
| Azure auth | SAS / connection string | Managed identity (already supported in `ingest/blob.py`) |
| New-PO detection | Polls Blob every 4 s | Same, or an Event Grid trigger |
| State | JSON files in `out/portal/` | A table (Delta / Postgres) |
| Duplicate protection | Content hash + PO lookup | Add an External ID field on `Booking_Form__c` (ask the SF admin) |
| Opportunity | Sandbox stand-in | The quote's real opportunity, with no substitution (already enforced) |

---

## Reference: commands

```bash
python3 review_queue.py --preflight [--live]          # check every connection, then exit
python3 review_queue.py --serve                        # simulation: nothing written to Salesforce
python3 review_queue.py --serve --live                 # real bookings in the sandbox
python3 review_queue.py --serve --live --preload demo_queue
python3 review_queue.py --serve --backfill             # also process POs already in the container
python3 review_queue.py --reset                        # forget portal orders (fresh start)
python3 -m pytest -q tests/
```
