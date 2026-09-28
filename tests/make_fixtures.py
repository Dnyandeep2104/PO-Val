"""Generate synthetic PO PDFs so the pipeline can be tested with no access.

Two deliberately different layouts:

  ingram_style   - pipe-delimited, matches the prototype's regex, and uses
                   the real quote number, parts and totals from the
                   notebook's successful run (F5Q-00972766).
  techdata_style - borderless columnar table with different header wording
                   and column order. The prototype's regex returns zero
                   lines on this. The geometric extractor should read it.

Replace these with real reseller PDFs as soon as you have them. Synthetic
fixtures prove the code paths work; they cannot prove the parser handles
the messiness of a real Ingram or Tech Data template.
"""

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[1] / "fixtures"

# Real values from the prototype's successful run.
QUOTE = "F5Q-00972766"
LINES = [
    ("F5-BIG-VE-BTA-25MV18", 2, "10,082.52", "20,165.04",
     "BIG-IP VE Best Bundle 25Mbps"),
    ("F5-SVC-BIG-VE+PREL13", 2, "2,038.53", "4,077.06",
     "Premium Plus Service"),
]
TOTAL = "24,242.10"


def ingram_style(path: Path, quote=QUOTE, lines=LINES, total=TOTAL,
                 po_number="4501234567"):
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, "INGRAM MICRO INC.")
    y -= 28
    c.setFont("Helvetica", 9)
    for row in [
        f"Purchase Order Number: {po_number}",
        "PO Date: 03/14/2026",
        "Currency: USD",
        f"Reference Quote: {quote}",
        "Payment Terms: NET 30",
        "Ship Terms: FCA Ship Point",
        "Carrier: FedEx  Method: Ground  Carrier Account #: 123456789",
    ]:
        c.drawString(50, y, row)
        y -= 13

    y -= 10
    c.drawString(50, y, "Vendor:")
    y -= 12
    c.drawString(50, y, "F5, Inc.")
    y -= 12
    c.drawString(50, y, "801 5th Ave, Seattle, WA 98104, United States")

    y -= 22
    c.drawString(50, y, "Bill To:")
    c.drawString(300, y, "Ship To:")
    y -= 12
    for a, b in [("Ingram Micro Inc.", "Contoso Corporation"),
                 ("3351 Michelson Drive", "1 Contoso Way"),
                 ("Irvine, CA 92612", "Seattle, WA 98101 USA"),
                 ("", "Attn: Dana Whitfield"),
                 ("", "Email: dana.whitfield@contoso.com"),
                 ("", "Phone: 206-555-0134")]:
        if a:
            c.drawString(50, y, a)
        c.drawString(300, y, b)
        y -= 12

    y -= 8
    c.drawString(50, y, "End User:")
    y -= 12
    for row in ["Contoso Corporation", "1 Contoso Way, Seattle, WA 98101 USA",
                "Attn: Dana Whitfield", "Email: dana.whitfield@contoso.com",
                "Phone: 206-555-0134"]:
        c.drawString(50, y, row)
        y -= 12

    y -= 16
    c.setFont("Courier", 7)
    c.drawString(40, y, "LINE |  | QTY | UOM |COND |PART NUMBER          |DESCRIPTION"
                        "                  | UNIT PRICE | EXTENDED")
    y -= 12
    for i, (part, qty, unit, ext, desc) in enumerate(lines, start=1):
        # Exactly the shape the prototype's INGRAM_LINE_RE expects.
        row = (f"{i:03d} |  | {qty} | EA |NEW |{part} |{desc:<28}| "
               f"{unit} | {ext}")
        c.drawString(40, y, row)
        y -= 12

    y -= 18
    c.setFont("Helvetica-Bold", 9)
    c.drawString(400, y, f"Total Amount: {total}")
    c.save()
    return path


def techdata_style(path: Path, quote=QUOTE, lines=LINES, total=TOTAL,
                   po_number="TD-88213-A"):
    """No pipes, different header wording, reordered columns."""
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 750
    c.setFont("Helvetica-Bold", 15)
    c.drawString(50, y, "TD SYNNEX")
    y -= 26
    c.setFont("Helvetica", 9)
    c.drawString(50, y, f"P.O. Number: {po_number}")
    c.drawString(330, y, "Order Date: 15-Mar-2026")
    y -= 13
    c.drawString(50, y, f"Vendor Quote #: {quote}")
    c.drawString(330, y, "Currency: USD")
    y -= 13
    c.drawString(50, y, "Payment Terms: NET 45")
    c.drawString(330, y, "Inco Terms: EXW")
    y -= 13
    c.drawString(50, y, "Carrier: UPS  Method: Ground  Carrier Account #: A8C902")
    y -= 24

    c.drawString(50, y, "Vendor:")
    c.drawString(330, y, "Ship To:")
    y -= 12
    for a, b in [("F5, Inc.", "Contoso Corporation"),
                 ("801 5th Ave", "1 Contoso Way"),
                 ("Seattle, WA 98104 United States", "Seattle, WA 98101 USA"),
                 ("", "Attn: Dana Whitfield"),
                 ("", "Email: dana.whitfield@contoso.com"),
                 ("", "Phone: 206-555-0134")]:
        if a:
            c.drawString(50, y, a)
        c.drawString(330, y, b)
        y -= 12

    y -= 8
    c.drawString(50, y, "Bill To:")
    c.drawString(330, y, "End User:")
    y -= 12
    for a, b in [("TD SYNNEX Corporation", "Contoso Corporation"),
                 ("44201 Nobel Drive", "1 Contoso Way, Seattle WA 98101 USA"),
                 ("Fremont, CA 94538", "Attn: Dana Whitfield"),
                 ("", "Email: dana.whitfield@contoso.com"),
                 ("", "Phone: 206-555-0134")]:
        if a:
            c.drawString(50, y, a)
        c.drawString(330, y, b)
        y -= 12

    y -= 20
    # Columnar header, no rules, deliberately different labels and order.
    cols = {"Item": 45, "Product Code": 80, "Description": 235,
            "Qty": 385, "Unit Cost": 425, "Net Amount": 505}
    c.setFont("Helvetica-Bold", 8)
    for label, x in cols.items():
        c.drawString(x, y, label)
    y -= 4
    c.line(45, y, 565, y)
    y -= 14

    c.setFont("Helvetica", 8)
    for i, (part, qty, unit, ext, desc) in enumerate(lines, start=1):
        c.drawString(cols["Item"], y, str(i))
        c.drawString(cols["Product Code"], y, part)
        c.drawString(cols["Description"], y, desc[:24])
        c.drawString(cols["Qty"], y, str(qty))
        c.drawString(cols["Unit Cost"], y, unit)
        c.drawString(cols["Net Amount"], y, ext)
        y -= 14

    y -= 6
    c.line(380, y, 565, y)
    y -= 14
    c.setFont("Helvetica-Bold", 9)
    c.drawString(cols["Unit Cost"], y, "Grand Total")
    c.drawString(cols["Net Amount"], y, total)
    c.save()
    return path


def build_all(out: Path = OUT) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    made = {
        "ingram_ok": ingram_style(out / "ingram_ok.pdf"),
        "techdata_ok": techdata_style(out / "techdata_ok.pdf"),
    }
    # A PO priced above the quote: the mismatch path.
    bad_lines = [
        ("F5-BIG-VE-BTA-25MV18", 2, "10,082.52", "20,165.04",
         "BIG-IP VE Best Bundle 25Mbps"),
        ("F5-SVC-BIG-VE+PREL13", 3, "2,038.53", "6,115.59",
         "Premium Plus Service"),
    ]
    made["ingram_mismatch"] = ingram_style(
        out / "ingram_mismatch.pdf", lines=bad_lines,
        total="26,280.63", po_number="4501234568")
    # A quote number that does not exist anywhere.
    made["unknown_quote"] = techdata_style(
        out / "unknown_quote.pdf", quote="F5Q-99999999",
        po_number="TD-00000-Z")
    return made


if __name__ == "__main__":
    for name, path in build_all().items():
        print(f"{name:<18} {path}")
