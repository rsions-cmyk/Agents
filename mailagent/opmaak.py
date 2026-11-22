"""Opgemaakte weergave van een mail: HTML uit de MIME-bron halen en veilig maken.

De HTML wordt getoond in een iframe zonder scripts (sandbox) en met een Content-Security-Policy
die alleen ingesloten afbeeldingen toestaat. Externe afbeeldingen (vaak trackingpixels) laden dus niet.
"""

from __future__ import annotations

import email
import email.policy
import html as htmlmod
import re

CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
       "img-src data:; style-src 'unsafe-inline'; font-src data:\">")

STIJL = ("<style>body{font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#151b21;"
         "background:#fdfdfc;margin:16px 18px;word-wrap:break-word}img{max-width:100%;height:auto}"
         "blockquote{margin:8px 0 8px 4px;padding-left:12px;border-left:3px solid #d5dad3;color:#3d4650}"
         "table{max-width:100%}a{color:#24548a}</style>")


def html_uit_bron(bron: str) -> str | None:
    """Kies het HTML-deel van de mail; valt terug op platte tekst als er geen HTML is."""
    msg = email.message_from_string(bron, policy=email.policy.default)
    deel = msg.get_body(preferencelist=("html", "plain"))
    if deel is None:
        return None
    inhoud = deel.get_content()
    if deel.get_content_type() == "text/plain":
        return "<pre style=\"white-space:pre-wrap;font:inherit\">" + htmlmod.escape(inhoud) + "</pre>"
    return inhoud


def veilig(html: str) -> str:
    """Scripts, formulieren, ingesloten frames en event-attributen eruit; CSP en eigen stijl erin."""
    # elementen mét inhoud volledig verwijderen
    html = re.sub(r"(?is)<(script|iframe|object|embed|form|noscript|svg)\b[^>]*>.*?</\1\s*>", "", html)
    # losse of niet-afgesloten tags van die soort, en link/meta/base
    html = re.sub(r"(?is)</?(script|iframe|object|embed|form|noscript|svg|link|meta|base)\b[^>]*>", "", html)
    html = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    html = re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*(javascript|vbscript|data:text/html)[^\"']*\2", r"\1=\2#\2", html)
    # externe afbeeldingen en achtergronden weghalen (vaak trackingpixels); ingesloten (data:) blijven staan
    html = re.sub(r"(?i)(<img\b[^>]*?\s)src\s*=\s*([\"'])\s*(https?:|//)[^\"']*\2", r"\1src=\2\2 data-geblokkeerd=\"1\"", html)
    html = re.sub(r"(?i)(srcset|background)\s*=\s*([\"'])[^\"']*(https?:|//)[^\"']*\2", "", html)
    html = re.sub(r"(?i)url\(\s*([\"']?)\s*(https?:|//)[^)]*\)", "none", html)
    html = re.sub(r"(?i)@import[^;]+;", "", html)
    if re.search(r"(?i)<body", html):
        html = re.sub(r"(?is)^.*?<body[^>]*>", "", html)
        html = re.sub(r"(?is)</body>.*$", "", html)
    kop = f"<!doctype html><html><head><meta charset=\"utf-8\">{CSP}{STIJL}<base target=\"_blank\"></head><body>"
    return kop + html + "</body></html>"
