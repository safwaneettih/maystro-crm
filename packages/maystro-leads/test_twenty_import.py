import json
import tempfile
import threading
import unittest
from unittest.mock import Mock
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from leads_daily import DEFAULT_CONFIG, aggregate_cards, ingest, open_db, push_pending, sync_enriched_notes
from twenty_import import TwentyClient


class TwentyImportTest(unittest.TestCase):
    def test_existing_note_gets_enrichment_once_and_keeps_manual_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {**DEFAULT_CONFIG, "db_path": f"{tmp}/leads.db"}
            client = TwentyClient("http://localhost", "test-key")
            client.find = Mock(return_value={"bodyV2": {"markdown": "Call again Friday"}})
            client.request = Mock()
            with closing(open_db(cfg)) as conn:
                conn.execute("INSERT INTO leads (page_key,status,score,email,enriched_at) VALUES (?,?,?,?,?)",
                             ("shop", "pushed", 4, "sales@example.com", "2026-11-13"))
                conn.execute("INSERT INTO pushes VALUES ('shop','twenty','opportunity','2026-11-12')")
                self.assertEqual(sync_enriched_notes(conn, cfg, client), 0)
                self.assertEqual(sync_enriched_notes(conn, cfg, client), 0)
            client.request.assert_called_once()
            method, path, payload, _ = client.request.call_args.args
            self.assertEqual(method, "PATCH")
            self.assertTrue(path.startswith("notes/"))
            self.assertIn("Call again Friday", payload["bodyV2"]["markdown"])
            self.assertIn("sales@example.com", payload["bodyV2"]["markdown"])

    def test_partial_import_can_resume_without_overwriting_an_opportunity(self):
        collections = {name: {} for name in ("companies", "opportunities", "notes", "noteTargets")}
        singular = {"companies": "company", "opportunities": "opportunity", "notes": "note", "noteTargets": "noteTarget"}
        fail_link = True

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps({"data": data}).encode())

            def do_GET(self):
                path = urlsplit(self.path)
                _, _, collection, *record_ids = path.path.split("/")
                records = collections[collection]
                if not record_ids:
                    self.assert_company_filter(path.query)
                    self.respond(200, {collection: list(records.values())})
                elif record_ids[0] in records:
                    self.respond(200, {singular[collection]: records[record_ids[0]]})
                else:
                    self.respond(404, {})

            def assert_company_filter(self, query):
                expected = 'domainName.primaryLinkUrl[eq]:"https://shop.converty.shop"'
                if parse_qs(query).get("filter") != [expected]:
                    raise AssertionError("Company lookup did not use the lead website")

            def do_POST(self):
                collection = urlsplit(self.path).path.split("/")[-1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if collection == "noteTargets" and fail_link:
                    self.respond(422, {})
                    return
                if body["id"] in collections[collection]:
                    self.respond(409, {})
                    return
                collections[collection][body["id"]] = body
                self.respond(201, {"create" + singular[collection][0].upper() + singular[collection][1:]: body})

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as tmp:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                cfg = {**DEFAULT_CONFIG, "db_path": f"{tmp}/leads.db", "output_dir": tmp}
                client = TwentyClient(f"http://127.0.0.1:{server.server_port}", "test-key")
                cards = [{
                    "name": "Test Shop", "url": "https://www.facebook.com/testshop/",
                    "text": "Library ID: 123\nCash on delivery 22 333 444\nshop.converty.shop",
                }]
                with closing(open_db(cfg)) as conn:
                    self.assertEqual(ingest(aggregate_cards(cards), conn, cfg)["new"], 1)
                    self.assertEqual(push_pending(conn, cfg, client), 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM pushes").fetchone()[0], 0)
                    opportunity = next(iter(collections["opportunities"].values()))
                    opportunity["stage"] = "MEETING"
                    fail_link = False
                    self.assertEqual(push_pending(conn, cfg, client), 0)
                    self.assertEqual(push_pending(conn, cfg, client), 0)
                    self.assertEqual(conn.execute("SELECT ref FROM pushes").fetchone()[0], opportunity["id"])
                self.assertTrue(all(len(records) == 1 for records in collections.values()))
                self.assertEqual(opportunity["stage"], "MEETING")
                link = next(iter(collections["noteTargets"].values()))
                self.assertEqual(link["targetOpportunityId"], opportunity["id"])
                self.assertEqual(link["noteId"], next(iter(collections["notes"])))
            finally:
                server.shutdown()
                thread.join()
