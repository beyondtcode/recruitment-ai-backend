"""Weekly system health-check email via Gmail SMTP."""

from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText

_STATUS_FROM = "dev@beyondtcode.com"
_STATUS_TO = "dev@beyondtcode.com"
_STATUS_SUBJECT = "✅ דוח תקינות שבועי - מערכת גיוס (Kamatera)"
_STATUS_BODY = (
    "שלום,\n\n"
    "דוח תקינות שבועי — מערכת הגיוס פועלת כראוי.\n"
    "שרת ה-backend וה-scheduler רצים בהצלחה.\n\n"
    "הודעה אוטומטית."
)


def send_weekly_status_email() -> None:
    """Send the weekly Kamatera health-check email via Gmail SMTP (SSL, port 465)."""
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not password:
        raise ValueError("GMAIL_APP_PASSWORD must be set in the environment")

    message = MIMEText(_STATUS_BODY, "plain", "utf-8")
    message["Subject"] = _STATUS_SUBJECT
    message["From"] = _STATUS_FROM
    message["To"] = _STATUS_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(_STATUS_FROM, password)
        server.send_message(message)
