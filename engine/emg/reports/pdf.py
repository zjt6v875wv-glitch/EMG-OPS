"""Branded agent pay statements (one person each) and the combined PDF.

Layout follows the existing EMG statements: hero numbers, payment summary,
earnings by product, balances, notes, then team summary (managers), sale
detail, chargebacks and non-payable activity. Every cell is a wrapping
Paragraph so long names and evidence never clip.
"""
from __future__ import annotations

import io
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..money import D, cents
from .data import ReportData

NAVY = colors.HexColor("#173247")
TEAL = colors.HexColor("#087F8C")
MUTED = colors.HexColor("#617482")
LIGHT = colors.HexColor("#EDF3F6")
LINE = colors.HexColor("#DCE5E9")
WIDTH = 528  # letter width minus 42pt margins


def _fonts() -> tuple[str, str]:
    for regular, bold in (("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),):
        if Path(regular).exists() and Path(bold).exists():
            if "EMG" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("EMG", regular))
                pdfmetrics.registerFont(TTFont("EMGB", bold))
            return "EMG", "EMGB"
    return "Helvetica", "Helvetica-Bold"


F, FB = _fonts()
S = {
    "cell": ParagraphStyle("cell", fontName=F, fontSize=8, leading=11, textColor=NAVY),
    "cellr": ParagraphStyle("cellr", fontName=F, fontSize=8, leading=11, textColor=NAVY, alignment=2),
    "head": ParagraphStyle("head", fontName=FB, fontSize=8, leading=11, textColor=NAVY),
    "headr": ParagraphStyle("headr", fontName=FB, fontSize=8, leading=11, textColor=NAVY, alignment=2),
    "small": ParagraphStyle("small", fontName=F, fontSize=8, leading=11, textColor=MUTED),
    "name": ParagraphStyle("name", fontName=FB, fontSize=22, leading=28, textColor=NAVY, spaceAfter=6),
    "hero": ParagraphStyle("hero", fontName=FB, fontSize=18, leading=23, textColor=NAVY),
    "section": ParagraphStyle("section", fontName=FB, fontSize=12, leading=16, textColor=NAVY, spaceBefore=14, spaceAfter=7, keepWithNext=True),
}


def dollars(v) -> str:
    v = cents(D(v))
    return ("-$" if v < 0 else "$") + format(abs(v), ",.2f")


def p(v, style="cell") -> Paragraph:
    return Paragraph(escape("" if v is None else str(v)).replace("\n", "<br/>"), S[style])


def table(header: list[str], rows: list[list], widths: list[float], right: set[int] = frozenset()) -> Table:
    data = [[p(h, "headr" if i in right else "head") for i, h in enumerate(header)]] + [[p(c, "cellr" if i in right else "cell") for i, c in enumerate(r)] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _qty(l) -> str:
    return "1 enrollment" if not D(l["kwh"]) else f"{D(l['kwh']):,.1f}"


def statement_story(rd: ReportData, name: str) -> list:
    pr = rd.person(name)
    r = pr["data"] if pr else {}
    g = lambda k: D(r.get(k, 0))  # noqa: E731
    pd = rd.pay_date
    lines, cbs, non = rd.lines_for(name), rd.chargebacks_for(name), rd.nonpayable_for(name)
    story = [p(f"PAY STATEMENT / WEEK {rd.week_no}", "small"), Spacer(1, 4), p(name, "name"),
             p(f"Pay date {pd.strftime('%A, %B')} {pd.day}, {pd.year} | {rd.label}", "small"), Spacer(1, 12)]
    hero = Table([[p("AGENT COMMISSION", "small"), p("OVERRIDES / PREMIUM", "small"), p("FINAL CHECK", "small")],
                  [p(dollars(g("agent_commission")), "hero"), p(dollars(g("manager_override")), "hero"), p(dollars(g("final_check")), "hero")]],
                 colWidths=[WIDTH / 3] * 3)
    hero.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("LINEABOVE", (2, 0), (2, 0), 3, TEAL),
                              ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
    story.append(hero)

    summary = [["Current earnings", dollars(g("gross_compensation"))]]
    for label, k in (("Chargeback recovery", "chargeback_recovery"), ("Held this week (not applied to any debt)", "manual_hold"),
                     ("Clawback reserve", "reserve_withheld"), ("Room repayment", "room_deduction"), ("Advance repayment", "advance_deduction"),
                     ("Flight repayment", "flight_deduction"), ("Other repayment", "other_deduction")):
        if g(k):
            summary.append([label, "-" + dollars(g(k))])
    if g("cash_credits"):
        summary.append(["Refunds / reimbursements / bonuses", dollars(g("cash_credits"))])
    summary.append(["FINAL CHECK", dollars(g("final_check"))])
    story += [p("Payment summary", "section"), table(["Component", "Amount"], summary, [WIDTH - 140, 140], {1})]

    groups: dict = defaultdict(lambda: {"refs": set(), "kwh": Decimal(0), "amount": Decimal(0), "spark": False})
    for l in lines:
        k = (l["vendor"], l["product"] or "")
        grp = groups[k]
        grp["amount"] += D(l["amount"])
        grp["spark"] = not D(l["kwh"])
        if l["activity"] in ("Agent Commission", "Company Payable") or l["rep"] == name:
            if l["enrollment_id"] not in grp["refs"]:
                grp["refs"].add(l["enrollment_id"])
                grp["kwh"] += D(l["kwh"] or 0)
    if groups:
        rows = [[f"{v} / {pr_}", len(gr["refs"]) or "", "—" if gr["spark"] else f"{gr['kwh']:,.1f}", dollars(gr["amount"])] for (v, pr_), gr in sorted(groups.items())]
        story += [p("Earnings by product", "section"), table(["Vendor / product", "Own sales", "kWh", "Earnings"], rows, [WIDTH - 290, 70, 100, 120], {1, 2, 3})]

    bal = [("Advance remaining", "advance_ending"), ("Chargeback carry", "ending_chargeback"), ("Room remaining", "room_ending"),
           ("Flight remaining", "flight_ending"), ("Other remaining", "other_ending")]
    shown = [(l, k) for l, k in bal if g(k)]
    if shown:
        story += [p("Balances after this payroll", "section"), table([l for l, _ in shown], [[dollars(g(k)) for _, k in shown]], [WIDTH / len(shown)] * len(shown))]
    if r.get("advance_by_creditor") and len(r["advance_by_creditor"]) > 1:
        story += [Spacer(1, 6), table(["Advance creditor", "Owed", "Repaid", "Remaining"],
                                      [[c, dollars(v["owed"]), dollars(v["deducted"]), dollars(v["ending"])] for c, v in r["advance_by_creditor"].items()],
                                      [WIDTH - 330, 110, 110, 110], {1, 2, 3})]

    notes = []
    if g("new_advance"):
        notes.append(f"New advances recorded this week: {dollars(g('new_advance'))}.")
    if g("room_new"):
        notes.append(f"New room charges this week: {dollars(g('room_new'))} (prior room balance {dollars(g('room_opening'))}).")
    if g("held_chargebacks") or any(not c["verified"] for c in cbs):
        notes.append("Some vendor chargebacks are held until the original payout is verified; they were not deducted.")
    for debt, basis in (r.get("collection_basis") or {}).items():
        if basis and "standing rule" in basis:  # week instructions are already listed below with their own note
            notes.append(f"{debt.capitalize()} collection: {basis}")
    labels = {"new_advance": "New advance", "room_charge": "Room charge", "flight_charge": "Flight charge", "other_charge": "Charge",
              "collection": "Collection this week", "defer": "Collection deferred this week", "protected_check": "Protected check this week",
              "manual_hold": "Pay held", "cash_credit": "Credit", "reserve": "Clawback reserve"}
    for n in r.get("notes", []):
        kind, _, text = n.partition(": ")
        notes.append(f"{labels.get(kind, kind)}: {text}" if text else n)
    notes.append(f"{rd.label} calculation. Payment release is separate from this statement.")
    story += [Spacer(1, 8)] + [p(n, "small") for n in dict.fromkeys(notes)]

    team: dict = defaultdict(lambda: {"refs": set(), "kwh": Decimal(0), "pay": Decimal(0)})
    for l in lines:
        if l["activity"] == "Manager Override" and l["rep"] != name:
            t = team[l["rep"]]
            t["refs"].add((l["vendor"], l["enrollment_id"]))
            t["kwh"] += D(l["kwh"] or 0)
            t["pay"] += D(l["amount"])
    if team:
        story += [PageBreak(), p("TEAM PERFORMANCE", "small"), p("Team sales summary", "name"),
                  table(["Agent", "Approved sales", "kWh", "Override"], [[n, len(t["refs"]), f"{t['kwh']:,.1f}" if t["kwh"] else "—", dollars(t["pay"])]
                                                                         for n, t in sorted(team.items(), key=lambda q: -q[1]["pay"])], [WIDTH - 300, 90, 100, 110], {1, 2, 3})]

    if lines:
        story += [PageBreak(), p("APPROVED SALES", "small"), p("Sales details", "name")]
        merged: dict = {}
        for l in lines:
            k = (l["vendor"], l["product"] or "", l["enrollment_id"], l["rep"])
            m = merged.setdefault(k, {**l, "amount": Decimal(0), "activities": []})
            m["amount"] += D(l["amount"])
            m["activities"].append(l["activity"])
        for v, prod in sorted({(k[0], k[1]) for k in merged}):
            items = [m for k, m in merged.items() if k[0] == v and k[1] == prod]
            spark = all(not D(m["kwh"]) for m in items)
            rows = [[f"{m['rep']}\n{m['customer'] or ''}", f"{m['utility'] or ''}\n{m['enrollment_id']}" + (f"\nsold {m['sale_date']}" if m["sale_date"] else ""),
                     _qty(m), ", ".join(dict.fromkeys(a.replace("Agent Commission", "Agent").replace("Manager Override", "Override").replace("Personal Rate Top-Up", "Personal premium") for a in m["activities"])),
                     dollars(m["amount"])] for m in items]
            story += [p(f"{v} / {prod}", "section"), table(["Agent / customer", "Utility / reference", "Units" if spark else "kWh", "Type", "Pay"], rows, [150, 150, 70, 88, 70], {2, 4})]

    if cbs:
        story += [PageBreak(), p("CHARGEBACKS", "small"), p("Historical payout reversals", "name"),
                  table(["Customer / reference", "Reversed", "Status", "Evidence"],
                        [[f"{c['data'].get('customer', '')}\n{c['vendor']} {c['enrollment_id']}", dollars(c["amount"]), "Deducted" if c["verified"] else "Held - not deducted", c["data"].get("evidence", "")] for c in cbs],
                        [160, 70, 80, WIDTH - 310], {1})]
    if non:
        story += [PageBreak(), p("ACTIVITY STATUS", "small"), p("Non-payable and pending", "name"),
                  table(["Customer", "Vendor / product / reference", "Sale date", "Status"],
                        [[n["data"].get("customer", ""), f"{n['vendor']} / {n['data'].get('product', '')}\n{n['enrollment_id']}", n["data"].get("sale_date") or "", n["data"].get("status", "") or n["kind"]] for n in non],
                        [130, 150, 70, WIDTH - 350])]
    return story


def _frame(name: str, rd: ReportData):
    def draw(c, doc):
        c.saveState()
        c.setFillColor(NAVY)
        c.setFont(FB, 18)
        c.drawString(42, 751, "EMG")
        c.setFont(F, 8)
        c.drawString(94, 753, "ENERGY MARKETING GROUP")
        c.setStrokeColor(LINE)
        c.line(42, 739, 570, 739)
        c.setFont(F, 7.5)
        c.setFillColor(MUTED)
        c.drawString(42, 25, f"{name} | Week {rd.week_no} | pay date {rd.pay_date.strftime('%m/%d/%Y')} | {rd.label}")
        c.drawRightString(570, 25, f"Page {doc.page}")
        c.restoreState()
    return draw


def statement_pdf(rd: ReportData, name: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=42, rightMargin=42, topMargin=72, bottomMargin=45,
                            title=f"{name} Week {rd.week_no} statement", author="Energy Marketing Group")
    frame = _frame(name, rd)
    doc.build(statement_story(rd, name), onFirstPage=frame, onLaterPages=frame)
    data = buf.getvalue()
    first = PdfReader(io.BytesIO(data)).pages[0].extract_text()
    pr = rd.person(name)
    if dollars(pr["data"]["final_check"] if pr else 0) not in first:  # guard: the check must be visible on page 1
        raise AssertionError(f"Final check missing from first page of {name}'s statement")
    return data


def combine(pdfs: list[bytes]) -> bytes:
    w = PdfWriter()
    for b in pdfs:
        w.append(io.BytesIO(b))
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def company_invoice_pdf(rd: ReportData, payee: str, invoice_no: str, source_invoice: str = "") -> bytes:
    rows = sorted((l for l in rd.lines if l["payee"] == payee), key=lambda l: (l["rep"], str(l["sale_date"]), l["enrollment_id"]))
    by_agent: dict = defaultdict(list)
    for l in rows:
        by_agent[l["rep"]].append(l)
    total = sum((D(l["amount"]) for l in rows), Decimal(0))
    pd = rd.pay_date
    story = [p(f"{payee.upper()} COMMISSION INVOICE", "name"), Spacer(1, 6),
             p(f"Invoice: {invoice_no}"), p(f"Issued: {pd.strftime('%B')} {pd.day}, {pd.year}"), p(f"Payable to: {payee}"), p("Payable by: Energy Marketing Group LLC")]
    if source_invoice:
        story.append(p(f"Source invoice: {source_invoice}"))
    rates = {D(l["rate"]) for l in rows}
    story += [Spacer(1, 12), table(["Agent", "Enrollments", "Rate", "Amount"],
                                   [[a, len(ls), dollars(ls[0]["rate"]) if len({D(x["rate"]) for x in ls}) == 1 else "mixed", dollars(sum(D(x["amount"]) for x in ls))] for a, ls in sorted(by_agent.items())]
                                   + [["TOTAL", len(rows), dollars(next(iter(rates))) if len(rates) == 1 else "", dollars(total)]], [WIDTH - 280, 90, 90, 100], {1, 2, 3}),
              Spacer(1, 10), p(f"{len({l['enrollment_id'] for l in rows})} unique payable enrollments. Pending, rejected, canceled and failed rows are excluded. "
                               f"{payee} is responsible for paying its agents. This invoice does not indicate payment has been made.", "small")]
    for a, ls in sorted(by_agent.items()):
        story += [KeepTogether([p(a, "section"), table(["Sold date", "Enrollment", "Customer", "Amount"],
                                                        [[l["sale_date"] or "", l["enrollment_id"], l["customer"] or "", dollars(l["amount"])] for l in ls], [80, 100, WIDTH - 260, 80], {3})])]
    buf = io.BytesIO()

    def foot(c, doc):
        c.setFont(F, 8)
        c.setFillColor(MUTED)
        c.drawString(42, 25, invoice_no)
        c.drawRightString(570, 25, f"Page {doc.page}")

    SimpleDocTemplate(buf, pagesize=letter, leftMargin=42, rightMargin=42, topMargin=45, bottomMargin=45, title=invoice_no).build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()
