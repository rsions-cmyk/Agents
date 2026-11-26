"""Bron voor Microsoft 365 (Outlook) via Microsoft Graph.

Rechten: Mail.ReadWrite (gedelegeerd) en offline_access. Mail.Send wordt bewust NIET gevraagd,
dus deze code kan technisch geen mail versturen. Concepten komen als antwoord in je map Concepten.

Eenmalig inloggen: `mailagent login`. Het token wordt lokaal bewaard in data/.token_cache.json.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..models import Email

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite"]


class OutlookBron:
    def __init__(self, naam: str, client_id: str, tenant: str, map_: str, data_dir: Path):
        self.naam, self.client_id, self.tenant, self.map = naam, client_id, tenant, map_
        self.cache_pad = Path(data_dir) / ".token_cache.json"

    # --- authenticatie -------------------------------------------------------
    def _app(self):
        import msal  # optionele afhankelijkheid: pip install 'mailagent[outlook]'
        cache = msal.SerializableTokenCache()
        if self.cache_pad.exists():
            cache.deserialize(self.cache_pad.read_text())
        app = msal.PublicClientApplication(self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant}",
                                           token_cache=cache)
        return app, cache

    def _bewaar(self, cache) -> None:
        if cache.has_state_changed:
            self.cache_pad.write_text(cache.serialize())
            self.cache_pad.chmod(0o600)

    def login(self) -> None:
        app, cache = self._app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Sign-in not started: {flow.get('error_description', flow)}")
        print(flow["message"])
        res = app.acquire_token_by_device_flow(flow)
        if "access_token" not in res:
            raise RuntimeError(f"Inloggen mislukt: {res.get('error_description', res)}")
        self._bewaar(cache)
        print("Ingelogd.")

    def _token(self) -> str:
        app, cache = self._app()
        accounts = app.get_accounts()
        res = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        if not res or "access_token" not in res:
            raise RuntimeError(f"Not signed in to Outlook for account '{self.naam}'. Run first: mailagent login")
        self._bewaar(cache)
        return res["access_token"]

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=GRAPH, timeout=60,
                            headers={"Authorization": f"Bearer {self._token()}",
                                     "Prefer": 'outlook.body-content-type="text"'})

    # --- mail ----------------------------------------------------------------
    def haal_op(self, sinds, maximum: int, overslaan=None) -> list[Email]:
        sinds = sinds or datetime.now(timezone.utc)
        iso = sinds.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "$filter": f"receivedDateTime gt {iso}",
            "$orderby": "receivedDateTime desc",
            "$top": str(min(maximum, 100)),
            "$select": "id,conversationId,subject,from,toRecipients,receivedDateTime,body",
        }
        with self._client() as c:
            r = c.get(f"/me/mailFolders/{self.map}/messages", params=params)
            r.raise_for_status()
            items = r.json().get("value", [])
        mails = []
        for m in items:
            van = m.get("from", {}).get("emailAddress", {})
            aan = ", ".join(x["emailAddress"].get("address", "") for x in m.get("toRecipients", []))
            tekst = m.get("body", {}).get("content", "")
            if m.get("body", {}).get("contentType") == "html":
                tekst = re.sub(r"<[^>]+>", " ", tekst)
            mails.append(Email(
                bron=self.naam, bron_id=m["id"],
                van=f"{van.get('name', '')} <{van.get('address', '')}>",
                aan=aan, onderwerp=m.get("subject") or "",
                datum=datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
                tekst=tekst.strip(), thread_id=m.get("conversationId"),
            ))
        return mails

    def maak_concept(self, bron_id: str, tekst: str, onderwerp: str, aan: str, message_id: str | None = None,
                     verzenden: bool = False) -> str:
        if verzenden:
            raise ValueError("sending directly isn't possible through the Outlook connection (it deliberately has no permission to send)")
        with self._client() as c:
            r = c.post(f"/me/messages/{bron_id}/createReply", json={"comment": html.escape(tekst).replace("\n", "<br>")})
            r.raise_for_status()
        return "the draft reply is in your Outlook Drafts folder"

    def verplaats(self, bron_ids: list[str], doel: str) -> dict[str, str | None]:
        bestemming = {"archief": "archive", "prullenbak": "deleteditems"}[doel]
        resultaat: dict[str, str | None] = {}
        with self._client() as c:
            for i in bron_ids:
                r = c.post(f"/me/messages/{i}/move", json={"destinationId": bestemming})
                resultaat[i] = None if r.is_success else f"HTTP {r.status_code}"
        return resultaat

    def markeer(self, bron_id: str, message_id: str | None, aan: bool, kleur: int = 4) -> None:
        with self._client() as c:
            r = c.patch(f"/me/messages/{bron_id}", json={"flag": {"flagStatus": "flagged" if aan else "notFlagged"}})
            r.raise_for_status()
