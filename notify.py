#!/usr/bin/env python3
"""notify.py: sends one summary email per pipeline run covering every
alert that fired, via Gmail SMTP with an App Password (Gmail blocks plain
password auth for security, hence the separate app-specific password
rather than the account's real password).

Optional: if GMAIL_APP_PASSWORD isn't set, send_alert_email() is a no-op
-- notifications can be disabled entirely just by removing the env var,
without touching run_pipeline.py or alerting.py.

Deliberately batches all of a run's alerts into a single email rather
than one email per alert -- a run that fires many alerts (e.g. after a
long gap, or a broad price-drop event) would otherwise flood the inbox.
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger("notify")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "divijmittal122@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_ADDRESS)

ALERT_REASON_LABELS = {
    "new_low": "New all-time low",
    "price_drop": "Price drop",
    "back_in_stock": "Back in stock",
    "back_in_stock_new_low": "Back in stock + new all-time low",
    "back_in_stock_price_drop": "Back in stock + price drop",
}


def format_alert_line(alert: dict) -> str:
    label = ALERT_REASON_LABELS.get(alert["reason"], alert["reason"])
    price_str = f"Rs {alert['effective_price']:,.0f}"
    reference_price = alert.get("reference_price")
    if reference_price:
        drop_pct = (reference_price - alert["effective_price"]) / reference_price * 100
        return (
            f"- {alert['variant_label']}: {label} -- {price_str} "
            f"(was Rs {reference_price:,.0f}, {drop_pct:.1f}% cheaper)"
        )
    return f"- {alert['variant_label']}: {label} -- {price_str}"


def send_alert_email(fired_alerts: list[dict]) -> bool:
    """Returns True if an email was actually sent (for caller logging) --
    False if there was nothing to send or no app password configured."""
    if not fired_alerts:
        return False
    if not GMAIL_APP_PASSWORD:
        log.info(
            "%d alert(s) fired but GMAIL_APP_PASSWORD isn't set -- skipping email notification.",
            len(fired_alerts),
        )
        return False

    body = "Teleworld Mobile Tracker -- new alert(s):\n\n" + "\n".join(
        format_alert_line(a) for a in fired_alerts
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Teleworld: {len(fired_alerts)} price alert(s)"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_TO

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.error("Failed to send alert email: %s", e)
        return False

    log.info("Sent alert email to %s (%d alert(s)).", GMAIL_TO, len(fired_alerts))
    return True
