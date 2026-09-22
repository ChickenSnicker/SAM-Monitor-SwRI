"""
notifier.py — Slack webhook and SMTP email notification senders.
"""
import logging
import smtplib
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

SAM_SEARCH_URL = "https://sam.gov/search/?index=opp&sort=-modifiedDate"


# ─────────────────────────────────────────────
#  Field formatting
# ─────────────────────────────────────────────

def _link(op: Dict) -> str:
    return op.get("uiLink") or f"https://sam.gov/opp/{op.get('noticeId', '')}/view"


def _deadline(op: Dict) -> str:
    # The SAM.gov API misspells this field as 'reponseDeadLine'.
    raw = op.get("reponseDeadLine") or ""
    if not raw:
        return "Not specified"
    return raw.split("T")[0] if "T" in raw else raw


def _posted(op: Dict) -> str:
    raw = op.get("postedDate") or ""
    if not raw:
        return "Unknown"
    return raw.split("T")[0].split(" ")[0]


def _agency(op: Dict) -> str:
    """Condense the dotted org path into the top few levels."""
    path = op.get("fullParentPathName") or ""
    if not path:
        return "Unknown agency"
    parts = [p.strip() for p in path.split(".") if p.strip()]
    return " › ".join(parts[:3])


def _clip(text: str, width: int) -> str:
    return textwrap.shorten(text or "Untitled", width=width, placeholder="…")


def _esc(text: str) -> str:
    """Minimal HTML escaping for values interpolated into the email body."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─────────────────────────────────────────────
#  Slack
# ─────────────────────────────────────────────

def _build_slack_blocks(opportunities: List[Dict], matched_by: str) -> list:
    """Build a Slack Block Kit message. Slack allows at most 50 blocks."""
    count = len(opportunities)
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{count} new SAM.gov match{'' if count == 1 else 'es'}",
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Search: `{matched_by}`"}],
        },
        {"type": "divider"},
    ]

    for op in opportunities:
        details = [
            f"*Agency:* {_agency(op)}",
            f"*Posted:* {_posted(op)}   *Response due:* {_deadline(op)}",
            f"*NAICS:* {op.get('naicsCode') or 'N/A'}",
        ]
        if op.get("solicitationNumber"):
            details.append(f"*Solicitation:* {op['solicitationNumber']}")
        if op.get("setAside"):
            details.append(f"*Set-aside:* {op['setAside']}")

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{_link(op)}|{_clip(op.get('title'), 120)}>*\n" + "\n".join(details),
            },
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"<{SAM_SEARCH_URL}|Browse all opportunities on SAM.gov>"}],
    })
    return blocks


def send_slack(
    webhook_url: str,
    opportunities: List[Dict],
    matched_by: str,
    max_per_message: int = 10,
) -> bool:
    """Post to Slack via incoming webhook, batching to respect block limits."""
    if not opportunities:
        return True

    ok = True
    for start in range(0, len(opportunities), max_per_message):
        batch = opportunities[start : start + max_per_message]
        payload = {
            "blocks": _build_slack_blocks(batch, matched_by),
            # Fallback text for notifications and unsupported clients.
            "text": f"{len(batch)} new SAM.gov match(es) for {matched_by}",
        }
        try:
            resp = requests.post(webhook_url, json=payload, timeout=20)
            if resp.status_code == 200:
                logger.info("Slack: delivered %d opportunit(ies) for %s", len(batch), matched_by)
            else:
                logger.error("Slack returned HTTP %d: %s", resp.status_code, resp.text[:200])
                ok = False
        except requests.exceptions.RequestException as exc:
            logger.error("Slack delivery failed: %s", exc)
            ok = False
    return ok


# ─────────────────────────────────────────────
#  Email
# ─────────────────────────────────────────────

def _build_email_html(opportunities: List[Dict], matched_by: str) -> str:
    cards = ""
    for op in opportunities:
        link = _esc(_link(op))
        rows = [
            ("Agency", _esc(_agency(op))),
            ("Posted", _esc(_posted(op))),
            ("Response due", _esc(_deadline(op))),
            ("NAICS", _esc(op.get("naicsCode") or "N/A")),
        ]
        if op.get("solicitationNumber"):
            rows.append(("Solicitation", _esc(op["solicitationNumber"])))
        if op.get("setAside"):
            rows.append(("Set-aside", _esc(op["setAside"])))

        row_html = "".join(
            f'<tr><td style="padding:3px 14px 3px 0;color:#555;white-space:nowrap">'
            f"<strong>{label}</strong></td><td style=\"padding:3px 0\">{value}</td></tr>"
            for label, value in rows
        )

        cards += f"""
        <div style="border:1px solid #dcdfe6;border-radius:6px;padding:16px;margin-bottom:14px;background:#fbfcfe">
          <h3 style="margin:0 0 10px 0;font-size:16px;line-height:1.35">
            <a href="{link}" style="color:#1a56db;text-decoration:none">{_esc(op.get('title') or 'Untitled')}</a>
          </h3>
          <table style="border-collapse:collapse;font-size:13px">{row_html}</table>
          <a href="{link}" style="display:inline-block;margin-top:12px;padding:7px 14px;background:#1a56db;
             color:#ffffff;border-radius:4px;text-decoration:none;font-size:13px">View on SAM.gov</a>
        </div>"""

    count = len(opportunities)
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:20px;background:#f4f5f7;
      font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2933">
  <div style="max-width:680px;margin:0 auto">
    <div style="background:#1a56db;padding:18px 22px;border-radius:8px 8px 0 0">
      <h2 style="color:#ffffff;margin:0;font-size:19px">SAM.gov Contract Monitor</h2>
      <p style="color:#cfdefb;margin:6px 0 0 0;font-size:14px">
        {count} new match{'' if count == 1 else 'es'} for <strong>{_esc(matched_by)}</strong>
      </p>
    </div>
    <div style="background:#ffffff;border:1px solid #dcdfe6;border-top:none;
                border-radius:0 0 8px 8px;padding:20px">
      {cards}
      <hr style="border:none;border-top:1px solid #eceef2;margin:18px 0">
      <p style="font-size:12px;color:#8a94a6;margin:0">
        <a href="{SAM_SEARCH_URL}" style="color:#8a94a6">Browse all opportunities on SAM.gov</a>
      </p>
    </div>
  </div>
</body></html>"""


def _build_email_text(opportunities: List[Dict], matched_by: str) -> str:
    lines = [
        f"SAM.gov Contract Monitor — {len(opportunities)} new match(es) for: {matched_by}",
        "=" * 64,
        "",
    ]
    for op in opportunities:
        lines += [
            f"Title:        {op.get('title') or 'Untitled'}",
            f"Agency:       {_agency(op)}",
            f"Posted:       {_posted(op)}",
            f"Response due: {_deadline(op)}",
            f"NAICS:        {op.get('naicsCode') or 'N/A'}",
        ]
        if op.get("solicitationNumber"):
            lines.append(f"Solicitation: {op['solicitationNumber']}")
        lines += [f"Link:         {_link(op)}", "-" * 64, ""]
    lines.append(f"Browse all: {SAM_SEARCH_URL}")
    return "\n".join(lines)


def send_email(
    smtp_host: str,
    smtp_port: int,
    use_tls: bool,
    username: str,
    password: str,
    from_address: str,
    to_addresses: List[str],
    subject_prefix: str,
    opportunities: List[Dict],
    matched_by: str,
) -> bool:
    """Send a multipart HTML/plain-text digest over SMTP."""
    if not opportunities:
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{subject_prefix} {len(opportunities)} new match(es) — {matched_by}"
    msg["From"] = from_address
    msg["To"] = ", ".join(to_addresses)
    msg.attach(MIMEText(_build_email_text(opportunities, matched_by), "plain", "utf-8"))
    msg.attach(MIMEText(_build_email_html(opportunities, matched_by), "html", "utf-8"))

    server = None
    try:
        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        server.login(username, password)
        server.sendmail(from_address, to_addresses, msg.as_string())
        logger.info("Email: delivered %d opportunit(ies) to %s", len(opportunities), ", ".join(to_addresses))
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. For Gmail you must use an App Password, "
            "not your account password: https://myaccount.google.com/apppasswords"
        )
        return False
    except Exception as exc:
        logger.error("Email delivery failed: %s", exc)
        return False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


# ─────────────────────────────────────────────
#  Dispatch
# ─────────────────────────────────────────────

def dispatch_notifications(
    cfg: dict,
    opportunities: List[Dict],
    matched_by: str,
    dry_run: bool = False,
) -> None:
    """Route new opportunities to every enabled delivery channel."""
    if not opportunities:
        return

    if dry_run:
        logger.info("[dry run] would alert %d opportunit(ies) for %s:", len(opportunities), matched_by)
        for op in opportunities:
            logger.info("    %s  %s", _clip(op.get("title"), 70), _link(op))
        return

    slack_cfg = cfg.get("slack", {})
    if slack_cfg.get("enabled"):
        send_slack(
            webhook_url=slack_cfg.get("webhook_url", ""),
            opportunities=opportunities,
            matched_by=matched_by,
            max_per_message=int(slack_cfg.get("max_per_message", 10)),
        )

    email_cfg = cfg.get("email", {})
    if email_cfg.get("enabled"):
        send_email(
            smtp_host=email_cfg.get("smtp_host", ""),
            smtp_port=int(email_cfg.get("smtp_port", 587)),
            use_tls=bool(email_cfg.get("use_tls", True)),
            username=email_cfg.get("username", ""),
            password=email_cfg.get("password", ""),
            from_address=email_cfg.get("from_address") or email_cfg.get("username", ""),
            to_addresses=email_cfg.get("to_addresses", []),
            subject_prefix=email_cfg.get("subject_prefix", "[SAM.gov Monitor]"),
            opportunities=opportunities,
            matched_by=matched_by,
        )
