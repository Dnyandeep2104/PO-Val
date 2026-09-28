# PO Validation — how to run this

This guide assumes you have never run code before. Follow it in order and
do not skip steps. It takes about 20 minutes.

---

# Part 1 — Get the tools (one time only)

## 1.1 Install Python

Python is the language this tool is written in. Your computer probably
does not have the right version yet.

1. Go to **https://www.python.org/downloads/**
2. Click the big yellow **Download Python** button
3. Open the file that downloads
4. **On Windows:** before clicking Install, tick the box at the bottom
   that says **"Add python.exe to PATH"**. This is important. If you miss
   it, nothing later will work.
5. Click Install and wait

## 1.2 Install VS Code

VS Code is the program you will open the project folder in.

1. Go to **https://code.visualstudio.com/**
2. Click **Download**
3. Open the file and install it

## 1.3 Put the project somewhere sensible

Move the `po-validation-local` folder somewhere you will find it again —
your Documents folder is fine. Do not leave it in Downloads.

---

# Part 2 — Open the project

1. Open **VS Code**
2. Click **File** → **Open Folder...**
3. Select the `po-validation-local` folder and click Open
4. If it asks "Do you trust the authors of the files in this folder?",
   click **Yes, I trust the authors**

You should now see the file names down the left-hand side.

## 2.1 Open the terminal

The terminal is where you type commands.

- Click **Terminal** in the top menu → **New Terminal**

A panel opens at the bottom with a blinking cursor. Everything you type
from here on goes there. Type the command, then press **Enter**, then
wait for it to finish before typing the next one.

**A note on `python` vs `python3`:** on Mac use `python3`, on Windows use
`python`. If one gives an error like "command not found", try the other.
This guide writes `python` — swap it if you are on a Mac.

---

# Part 3 — Set it up (one time only)

## 3.1 Install the pieces the tool needs

Type this and press Enter:

```
pip install -r requirements.txt
```

This downloads the libraries the tool depends on. It takes a minute or
two and prints a lot of text. That is normal.

**If you see an error mentioning "externally-managed-environment"**, type
this instead:

```
pip install -r requirements.txt --break-system-packages
```

**If `pip` is not found**, try `pip3` instead of `pip`.

**If you see "Cannot uninstall ... RECORD file not found"**, add
`--ignore-installed` to the end:

```
pip install -r requirements.txt --ignore-installed
```

You are done when you get your blinking cursor back with no red text.

---

# Part 4 — Run it

## 4.1 Create some practice purchase orders

The folder ships with no PDFs. This makes four fake ones to test with:

```
python tests/make_fixtures.py
```

You should see four lines listing the files it created.

## 4.2 Run the tool

```
python run_local.py --folder fixtures --quotes fixtures/quotes.json
```

The `--quotes` part gives it some sample quote data to compare the POs
against. (Real quote data will come from Snowflake later.)

**What you should see at the end:**

```
4 document(s) processed
----------------------------------
  VALIDATED        2
  NEEDS_REVIEW     1
  DEFERRED         1
```

That is the whole tool working. It read four purchase orders, compared
each one against its quote and the SOS checklist, and sorted them:

- **VALIDATED** — nothing wrong, safe to book
- **NEEDS_REVIEW** — something needs a person to look at it
- **REJECTED** — something is definitely wrong
- **DEFERRED** — the quote it mentions isn't in the data yet, so it will
  try again next time rather than reject it. (One of the practice POs
  deliberately cites a quote that doesn't exist, to show this.)

**If you leave off `--quotes`**, all four come back VALIDATED. That is
expected: without quote data, the checks that compare against the quote
are skipped, so only the checklist itself is tested.

## 4.3 See the detail

To see every check and every problem it found, add `--verbose`:

```
python run_local.py --folder fixtures --quotes fixtures/quotes.json --verbose
```

Now each purchase order prints a full report showing exactly which checks
failed and why.

## 4.4 If you run it twice

Run `python run_local.py --folder fixtures` a second time and you will
get:

```
0 document(s) processed
```

That is correct, not a bug. The tool remembers what it has already seen
so it never processes the same purchase order twice. To make it forget
and start again:

```
python run_local.py --folder fixtures --reprocess
```

---

# Part 5 — Run it on real purchase orders

## 5.1 Add your PDFs

There is an empty folder called `my_pos`. Drag your real purchase order
PDF files into it. (Use the actual POs, not the checklist document.)

## 5.2 Run

```
python run_local.py --folder my_pos --verbose
```

You will get a full report for each one.

**About the quote checks.** Seven of the 35 checks compare the PO against
the quote it references. They are **skipped** by default and reported as
"could not run", because no quote data is supplied. That is expected — it
is not a problem with your PO.

## 5.4 Turning on the quote comparison

The comparison logic is written and working. It just needs quote data.
The live Snowflake reader only runs on Databricks, but you can export the
data once and test on your laptop:

1. Open `tools/quote_export.sql` and run it in Databricks or the
   Snowflake UI. It already lists the six quote numbers from your POs.
2. Download the result as a CSV into this folder.
3. Convert it:

```
python tools/quotes_from_csv.py your_export.csv
```

4. Run with it:

```
python run_local.py --folder my_pos --quotes my_quotes.json --verbose
```

Those seven checks now run: whether the quote exists, whether each part's
quantity and price match, whether the order totals agree, whether the
currency matches, and whether an Opportunity is linked.

**The `--folder my_pos` part matters.** If you just type
`python run_local.py` with your POs in `my_pos`, it now finds them
automatically and tells you so. But if it ever says "No PDF files found",
it will print which folders it looked in and what to type instead.

## 5.3 Check how accurate it is

This is the most important command in the whole project.

```
python tests/accuracy.py --pdfs my_pos
```

It compares what the tool extracted against a list of correct answers
that were read off the PDFs by hand. You should see:

```
overall fields : 36/36 (100.0%)
line items     : 23/23 (100.0%)
```

**If those numbers are lower than 36/36 and 23/23, something is wrong.**
Run it again with `-v` on the end to see exactly which value disagreed:

```
python tests/accuracy.py --pdfs my_pos -v
```

Note: this only works for the six purchase orders that already have
correct answers recorded. A new reseller's PO will need its answers added
first — see Part 7.

---

# Part 6 — Change what the tool checks

Everything the tool checks lives in one file: **`rules/sos.yaml`**

Open it in VS Code by clicking it in the list on the left. You do not need
to know any programming to edit it.

Each check looks like this:

```yaml
  - id: po_number_present
    check: field_present
    field: po_number
    label: PO number
    severity: BLOCKER
    checklist_item: "PO Number"
```

The line that matters most is **`severity`**:

| If severity is | Then a failure means |
|---|---|
| `BLOCKER` | The PO is REJECTED |
| `MAJOR` | The PO goes to a person for review |
| `MINOR` | It is noted, but does not hold up the order |

**To make a check less strict**, change `BLOCKER` to `MAJOR`.

**To turn a check off completely**, add a line `enabled: false` underneath
it, like this:

```yaml
  - id: po_number_present
    check: field_present
    field: po_number
    enabled: false
    severity: BLOCKER
```

After any change, save the file (**Ctrl+S**, or **Cmd+S** on Mac) and run
the tool again:

```
python run_local.py --folder my_pos --reprocess --verbose
```

If you make a typing mistake, the tool will tell you straight away and
list the valid options. It will not run with a broken rules file, so you
cannot break anything permanently.

---

# Part 7 — Checking the tool still works after a change

Two commands. Run both after you change anything.

```
python -m pytest tests/ -q
```
Expect: `26 passed`

```
python tests/accuracy.py --pdfs my_pos
```
Expect: `36/36` and `23/23`

If either of those changes, undo what you just did.

## 7.1 Checking how it copes with many POs at once

`accuracy.py` needs correct answers written by hand, so it only covers
POs someone has read. To check a large pile of POs without that:

```
python tests/coverage_report.py --pdfs my_pos
```

It scores each PO against its own internal arithmetic — do the line
totals multiply correctly, do they add up to the total printed on the
document — so no hand-checking is needed. Point it at an archive of past
purchase orders and it tells you what fraction the tool handles cleanly
and what fraction it flags for a person.

## Adding a new reseller's purchase order

When a purchase order from a new reseller arrives:

1. Put the PDF in `my_pos`
2. Run `python run_local.py --folder my_pos --reprocess --verbose`
3. Open the PDF yourself and check the tool read it correctly
4. If it did, add its correct answers to `tests/ground_truth.json`,
   copying the format of the entries already there
5. Run `python tests/accuracy.py --pdfs my_pos -v`

Step 3 matters. The correct answers must come from your eyes, not from
the tool, or the check is meaningless.

---

# What each file is

| File or folder | What it is |
|---|---|
| **`README.md`** | This guide |
| **`rules/sos.yaml`** | **The SOS checklist.** The main file you will edit. |
| `rules/renewals.yaml` | The renewals team's version. Not started yet. |
| **`run_local.py`** | The command that runs everything |
| **`my_pos/`** | Put your real purchase order PDFs here |
| `tests/accuracy.py` | Checks how accurate the tool is |
| `tests/ground_truth.json` | The correct answers, read off the PDFs by hand |
| `tests/test_pipeline.py` | 26 automated tests |
| `tests/make_fixtures.py` | Makes fake purchase orders for practice |
| `requirements.txt` | The list of libraries to install |
| `fixtures/quotes.json` | Fake quote data, so it works without Snowflake |
| `po_validation/` | The actual code. You do not need to open this. |
| `out/` | Created when you run it. Holds the results. Safe to delete. |

---

# When something goes wrong

| What you see | What to do |
|---|---|
| `python: command not found` | Use `python3` instead (Mac), or reinstall Python with "Add to PATH" ticked (Windows) |
| `No module named pdfplumber` | You skipped step 3.1. Run the `pip install` command. |
| `externally-managed-environment` | Add `--break-system-packages` to the end of the pip command |
| `Cannot uninstall ... RECORD file not found` | Add `--ignore-installed` to the end of the pip command |
| `0 document(s) processed` | Either it already processed those files (add `--reprocess`), or you pointed it at the wrong folder. Your real POs go in `my_pos`: `python3 run_local.py --folder my_pos --verbose` |
| `out` folder is empty | Nothing was processed, so nothing was written. See the row above. |
| `No such file or directory` | You are in the wrong folder. In VS Code, close it and use File → Open Folder on `po-validation-local`, then open a new terminal. |
| Red text mentioning `yaml` | You made a typo editing `rules/sos.yaml`. The message says which line. |

If you get stuck, copy the whole error message — all of it, not just the
last line — and send it over.

---

# Part 8 — Later, once access is in place

Everything above runs on your laptop with no access to anything. Three
things are still waiting:

**1. Azure container access** (requested, still waiting)
Lets the tool collect purchase orders automatically instead of you
dragging PDFs into a folder.

**2. Snowflake quote lookup**
Your connection already works — it just needs wiring in. This switches on
seven checks that currently cannot run, including the important ones
that compare the PO against the quote: whether the quote exists, whether
prices and quantities match, and whether the totals agree.

**3. Salesforce**
Creates the Booking Form automatically once a PO passes. Test environment
access is now in hand.

All three are in the separate `po-validation-databricks` folder, which
has its own instructions. Do not start on those until everything in this
guide runs cleanly on your laptop.
