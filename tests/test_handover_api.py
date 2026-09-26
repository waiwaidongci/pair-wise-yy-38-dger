import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


class ApiError(Exception):
    def __init__(self, status, payload):
        super().__init__(payload.get("message", ""))
        self.status = status
        self.payload = payload


class HandoverApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "api.db"))
        service = Service(self.repo)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            service, str(Path(__file__).resolve().parent.parent / "static")))
        self.port = server.server_address[1]
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.server = server
        self.service = service

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.repo.close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, actor="zhang", role="duty_officer"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Actor", actor)
        req.add_header("X-Role", role)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, json.loads(exc.read().decode("utf-8"))) from exc

    def test_handover_http_flow(self):
        _, created = self._request("POST", "/api/items", {
            "title": "接口交接指令", "description": "d", "severity": "urgent",
            "quantity": 9, "threshold": 5, "external_ref": "API-HD-1",
        })
        item_id = created["id"]

        status, handover = self._request("POST", "/api/handovers", {
            "outgoing_officer": "zhang", "incoming_officer": "wang",
            "reservoir_level": 142.5, "personnel": ["zhang", "wang"],
            "item_ids": [item_id],
        })
        self.assertEqual(status, 201)
        handover_id = handover["id"]
        entry_id = handover["items"][0]["id"]

        # 锁定期流转被拒 409
        with self.assertRaises(ApiError) as caught:
            self._request("POST", f"/api/items/{item_id}/transition",
                          {"target": "checked", "expected_version": 1})
        self.assertEqual(caught.exception.status, 409)

        # 退回缺原因 422
        with self.assertRaises(ApiError) as caught:
            self._request("POST",
                          f"/api/handovers/{handover_id}/items/{entry_id}",
                          {"decision": "returned"})
        self.assertEqual(caught.exception.status, 422)

        # viewer 无权登记 403
        with self.assertRaises(ApiError) as caught:
            self._request("POST", "/api/handovers",
                          {"outgoing_officer": "a", "incoming_officer": "b",
                           "reservoir_level": 1, "personnel": ["a"],
                           "item_ids": [item_id]}, actor="v", role="viewer")
        self.assertEqual(caught.exception.status, 403)

        # 正式退回，指令回 draft，版本+1
        status, result = self._request(
            "POST", f"/api/handovers/{handover_id}/items/{entry_id}",
            {"decision": "returned", "reason": "库位记录缺失"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")

        status, item = self._request("GET", f"/api/items/{item_id}",
                                     actor="v", role="viewer")
        self.assertEqual(item["status"], "draft")
        self.assertEqual(item["version"], 2)
        self.assertNotIn("handover_lock", item)

        _, handovers = self._request("GET", "/api/handovers?status=completed",
                                     actor="v", role="viewer")
        self.assertEqual(len(handovers["handovers"]), 1)

        # 审计链完整
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
