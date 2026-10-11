"""Sends realistic test emails to your own test inbox.

    python -m samples.send_test_email          # April statements + newsletter + plain email
    python -m samples.send_test_email --may    # a May ABC statement (same layout as April)

Run `python -m samples.generate` and `python -m samples.next_month` first.
"""
import io
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from dotenv import load_dotenv
from reportlab.pdfgen import canvas

PDFS = Path(__file__).parent / "pdfs"


def decoy_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Spring savings offers - product brochure")
    c.drawString(72, 730, "Nothing financial to see here.")
    c.save()
    return buf.getvalue()


def message(address, subject, body, attachments=()):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = address, address, subject
    msg["Message-ID"] = make_msgid(domain="statement-parser.test")
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg


def build(address, may: bool):
    if may:
        return [message(address, "Your May 2026 PMS statement",
                        "Dear investor, your May statement is attached.",
                        [("abc_pms_may.pdf", (PDFS / "abc_pms_may.pdf").read_bytes())])]
    return [
        message(address, "Your April 2026 PMS statements",
                "Dear investor, please find your statements attached.",
                [(n, (PDFS / n).read_bytes()) for n in ("abc_pms.pdf", "xyz_wealth.pdf")]),
        message(address, "Weekly newsletter",
                "Market ideas for your portfolio this week.",
                [("brochure.pdf", decoy_pdf())]),
        message(address, "Lunch on Friday?", "Are you free at 1pm?"),
    ]


if __name__ == "__main__":
    load_dotenv()
    user = os.environ.get("GMAIL_ADDRESS")
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not password:
        sys.exit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first")
    messages = build(user, may="--may" in sys.argv)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        for m in messages:
            smtp.send_message(m)
    print(f"sent {len(messages)} email(s) to {user}")