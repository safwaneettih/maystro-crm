import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5


def lead_record_id(lead, record_type):
    return str(uuid5(NAMESPACE_URL, f"maystro:meta-ad-library:{record_type}:{lead['page_key']}"))


def qualification_evidence(lead):
    return [
        ("Source", "Meta Ad Library"),
        ("Score", f"{lead['score']}/5"),
        ("Signaux", lead["reason"]),
        ("Annonces", lead["ads"]),
        ("Paiement à la livraison", "oui" if lead["cod"] else "non"),
        ("Livraison gratuite", "oui" if lead["free_delivery"] else "non"),
        ("Facebook", lead["facebook"]),
        ("Site", lead["website"]),
        ("Followers", lead["followers"]),
        ("Dernière publication", lead["last_post"]),
        ("Téléphone", " / ".join(filter(None, (lead["phone1"], lead["phone2"])))),
        ("Email", lead["email"]),
        ("Canal conseillé", lead["channel"]),
        ("Détecté le", lead["first_seen"]),
    ]


class TwentyRequestError(RuntimeError):
    def __init__(self, method, path, status=None):
        self.status = status
        super().__init__(f"Twenty {method} {path}: {status or 'connection failed'}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class TwentyClient:
    def __init__(self, url, api_key):
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            raise ValueError("TWENTY_URL must be a valid HTTP(S) URL")
        if not api_key.strip():
            raise ValueError("Set LEADS_TWENTY_API_KEY before importing leads")
        self.url = url.rstrip("/") + "/rest/"
        self.headers = {"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"}
        self.opener = build_opener(NoRedirect())

    @classmethod
    def from_environment(cls):
        return cls(os.environ.get("TWENTY_URL", ""), os.environ.get("LEADS_TWENTY_API_KEY", ""))

    def request(self, method, path, body=None, query=None):
        url = self.url + path + ("?" + urlencode(query) if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(url, data=data, method=method, headers=self.headers)
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=30) as response:
                    return json.load(response)["data"]
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After", "")
                error.close()
                if status not in (429, 502, 503, 504) or attempt == 2:
                    raise TwentyRequestError(method, path, status) from None
                delay = min(60, int(retry_after)) if retry_after.isdecimal() else 2 ** attempt
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise TwentyRequestError(method, path) from None
                delay = 2 ** attempt
            time.sleep(delay)

    def find(self, collection, record_id):
        try:
            return next(iter(self.request("GET", f"{collection}/{record_id}", query={"depth": 0}).values()))
        except TwentyRequestError as error:
            if error.status != 404:
                raise
            return None

    def create_once(self, collection, record_id, fields):
        existing = self.find(collection, record_id)
        if existing:
            return existing["id"]
        try:
            result = self.request("POST", collection, {"id": record_id, **fields}, {"depth": 0})
            return next(iter(result.values()))["id"]
        except TwentyRequestError as error:
            if error.status not in (400, 409):
                raise
            # A concurrent or retried request may already have committed this ID.
            existing = self.find(collection, record_id)
            if not existing:
                raise
            return existing["id"]

    def find_company_by_website(self, website):
        companies = self.request("GET", "companies", query={
            "depth": 0,
            "limit": 1,
            "filter": f"domainName.primaryLinkUrl[eq]:{json.dumps(website)}",
        })["companies"]
        return companies[0]["id"] if companies else None

    def import_lead(self, lead, country):
        def record_id(record_type):
            return lead_record_id(lead, record_type)

        company = {"name": lead["name"], "address": {"addressCountry": country}}
        company_id = None
        if lead["website"]:
            company["domainName"] = {"primaryLinkUrl": lead["website"]}
            company_id = self.find_company_by_website(lead["website"])
        company_id = company_id or self.create_once("companies", record_id("company"), company)
        opportunity_id = self.create_once("opportunities", record_id("opportunity"), {
            "name": f"{lead['name']} — Fulfillment",
            "companyId": company_id,
        })

        markdown = "\n\n".join(f"**{label}** — {value}" for label, value in qualification_evidence(lead)
                               if value not in (None, ""))
        note_id = self.create_once("notes", record_id("note"), {
            "title": "Qualification Meta Ad Library",
            "bodyV2": {"markdown": markdown},
        })
        self.create_once("noteTargets", record_id("note-target"), {
            "noteId": note_id,
            "targetOpportunityId": opportunity_id,
        })
        return opportunity_id

    def sync_note(self, lead):
        note_id = lead_record_id(lead, "note")
        note = self.find("notes", note_id)
        if note is None:
            return
        current = (note.get("bodyV2") or {}).get("markdown") or ""
        existing_lines = set(current.splitlines())
        labels = {"Site", "Followers", "Dernière publication", "Téléphone", "Email", "Canal conseillé"}
        additions = [line for label, value in qualification_evidence(lead)
                     if label in labels and value not in (None, "")
                     and (line := f"**{label}** — {value}") not in existing_lines]
        if additions:
            markdown = "\n\n".join(filter(None, [current.rstrip(), *additions]))
            self.request("PATCH", f"notes/{note_id}", {"bodyV2": {"markdown": markdown, "tiptap": None}},
                         {"depth": 0})
