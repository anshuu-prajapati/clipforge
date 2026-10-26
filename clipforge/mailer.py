"""Outgoing email over SMTP (password resets, security notices).

SMTP_HOST, SMTP_PORT (587 STARTTLS, or 465 SSL), SMTP_USER, SMTP_PASSWORD, SMTP_FROM.
Works with Gmail (app password), Brevo, SendGrid, Outlook, Amazon SES, …
Without SMTP_HOST, messages are printed to the server console instead (local use).
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and (os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")))


def send(to: str, subject: str, text: str, html: str | None = None) -> None:
    sender = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "")
    if not configured():
        print(f"\n  [email not configured — would send to {to}]\n  Subject: {subject}\n  {text}\n", flush=True)
        return
    msg = EmailMessage()
    msg["From"] = sender if "<" in sender else formataddr(("ClipForge", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls(context=ctx)
    with server:
        if os.environ.get("SMTP_USER"):
            server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(msg)


def html_page(title: str, body: str, button: tuple[str, str] | None = None) -> str:
    """Small email layout in the ClipForge style (inline CSS — mail clients ignore <style>)."""
    btn = (f'<p style="margin:28px 0"><a href="{button[1]}" style="background:#ff4f1a;color:#fff;text-decoration:none;'
           f'font-weight:700;padding:14px 22px;display:inline-block">{button[0]}</a></p>') if button else ""
    return (f'<div style="font-family:Helvetica,Arial,sans-serif;background:#f6f5f1;padding:32px">'
            f'<div style="max-width:520px;margin:auto;background:#fff;border:2px solid #111;padding:28px">'
            f'<div style="font-weight:800;font-size:18px;margin-bottom:18px">'
            f'<span style="display:inline-block;width:10px;height:18px;background:#ff4f1a;vertical-align:middle;margin-right:8px"></span>'
            f'ClipForge</div><h2 style="margin:0 0 12px;font-size:24px;color:#111">{title}</h2>'
            f'<div style="font-size:15px;line-height:1.55;color:#3b3934">{body}</div>{btn}'
            f'<p style="font-size:12px;color:#8f8b80;margin-top:24px">If you didn\'t ask for this, you can ignore this email — '
            f'your account is unchanged.</p></div></div>')
