"""Opgemaakte weergave van een mail: HTML uit de MIME-bron halen en veilig maken.

De HTML wordt getoond in een iframe zonder scripts (sandbox) en met een Content-Security-Policy.
Afbeeldingen die in de mail zelf zitten (cid:, zoals logo's en handtekeningen) worden ingesloten
en altijd getoond. Externe afbeeldingen laden alleen als jij daarom vraagt: laden laat de afzender
zien dat en wanneer je de mail opende (trackingpixels). Ook dan laden ze nooit van adressen in je
eigen netwerk, en pixels van 1 bij 1 blijven weg.
"""

from __future__ import annotations

import base64
import email
import email.policy
import html as htmlmod
import ipaddress
import re
from urllib.parse import urlparse

CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
       "img-src data:; style-src 'unsafe-inline'; font-src data:\">")
CSP_EXTERN = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
              "img-src data: https: http:; style-src 'unsafe-inline'; font-src data:\">")
MAX_INGESLOTEN = 4 * 1024 * 1024      # per afbeelding
MAX_TOTAAL = 12 * 1024 * 1024

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
    return _sluit_in(msg, inhoud)


def _sluit_in(msg, html: str) -> str:
    """cid:-verwijzingen vervangen door de afbeelding zelf (data:), uit de bijlagen van de mail."""
    if "cid:" not in html.lower():
        return html
    beelden, totaal = {}, 0
    for deel in msg.walk():
        cid = (deel.get("Content-ID") or "").strip().strip("<>")
        soort = deel.get_content_type()
        if not cid or not soort.startswith("image/") or soort == "image/svg+xml":
            continue
        data = deel.get_payload(decode=True) or b""
        if not data or len(data) > MAX_INGESLOTEN or totaal + len(data) > MAX_TOTAAL:
            continue
        totaal += len(data)
        beelden[cid.lower()] = f"data:{soort};base64,{base64.b64encode(data).decode()}"
    if not beelden:
        return html

    def vervang(m):
        return m.group(1) + beelden.get(m.group(2).strip().lower(), "") + m.group(1)
    return re.sub(r"(?i)([\"'])cid:([^\"']+)\1", vervang, html)


def externe_afbeeldingen(html: str) -> int:
    """Hoeveel externe afbeeldingen (geen pixels) de mail zou laden."""
    n = 0
    for tag in re.findall(r"(?is)<img\b[^>]*>", html):
        if re.search(r"(?i)\ssrc\s*=\s*[\"']?\s*(https?:|//)", tag) and not _is_pixel(tag):
            n += 1
    return n


def _is_pixel(tag: str) -> bool:
    maat = re.findall(r"(?i)\b(?:width|height)\s*=\s*[\"']?\s*(\d+)", tag)
    maat += re.findall(r"(?i)\b(?:width|height)\s*:\s*(\d+)px", tag)
    return any(int(m) <= 2 for m in maat) or bool(re.search(r"(?i)display\s*:\s*none|visibility\s*:\s*hidden", tag))


def _eigen_netwerk(url: str) -> bool:
    """Adressen in je eigen netwerk (router, printer, deze app) laadt een mail nooit."""
    host = (urlparse(url if not url.startswith("//") else "http:" + url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".internal", ".home", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host            # een kale naam als 'printer' is ook lokaal
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def veilig(html: str, extern: bool = False) -> str:
    """Scripts, formulieren, ingesloten frames en event-attributen eruit; CSP en eigen stijl erin.
    extern=True: externe afbeeldingen mogen laden (op jouw verzoek), behalve pixels en je eigen netwerk."""
    # elementen mét inhoud volledig verwijderen
    html = re.sub(r"(?is)<(script|iframe|object|embed|form|noscript|svg)\b[^>]*>.*?</\1\s*>", "", html)
    # losse of niet-afgesloten tags van die soort, en link/meta/base
    html = re.sub(r"(?is)</?(script|iframe|object|embed|form|noscript|svg|link|meta|base)\b[^>]*>", "", html)
    html = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    html = re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*(javascript|vbscript|data:text/html)[^\"']*\2", r"\1=\2#\2", html)
    html = re.sub(r"(?i)@import[^;]+;", "", html)
    if extern:
        # trackingpixels en adressen in je eigen netwerk toch weg; de rest mag laden
        html = re.sub(r"(?is)<img\b[^>]*>", lambda m: "" if _is_pixel(m.group(0)) else m.group(0), html)
        html = re.sub(r"(?i)(src|srcset|background)\s*=\s*([\"'])\s*((?:https?:)?//[^\"'\s]+)([^\"']*)\2",
                      lambda m: m.group(0) if not _eigen_netwerk(m.group(3)) else f"{m.group(1)}={m.group(2)}{m.group(2)}", html)
        html = re.sub(r"(?i)url\(\s*([\"']?)\s*((?:https?:)?//[^)\"']+)\1\s*\)",
                      lambda m: m.group(0) if not _eigen_netwerk(m.group(2)) else "none", html)
    else:
        # externe afbeeldingen en achtergronden weghalen (vaak trackingpixels); ingesloten (data:) blijven staan
        html = re.sub(r"(?i)(<img\b[^>]*?\s)src\s*=\s*([\"'])\s*(https?:|//)[^\"']*\2", r"\1src=\2\2 data-geblokkeerd=\"1\"", html)
        html = re.sub(r"(?i)(<img\b[^>]*?\s)src\s*=\s*(https?:|//)[^\s>]*", r"\1", html)   # zonder aanhalingstekens
        html = re.sub(r"(?i)(srcset|background)\s*=\s*([\"'])[^\"']*(https?:|//)[^\"']*\2", "", html)
        html = re.sub(r"(?i)url\(\s*([\"']?)\s*(https?:|//)[^)]*\)", "none", html)
    if re.search(r"(?i)<body", html):
        html = re.sub(r"(?is)^.*?<body[^>]*>", "", html)
        html = re.sub(r"(?is)</body>.*$", "", html)
    kop = f"<!doctype html><html><head><meta charset=\"utf-8\">{CSP_EXTERN if extern else CSP}{STIJL}<base target=\"_blank\"></head><body>"
    return kop + html + "</body></html>"
