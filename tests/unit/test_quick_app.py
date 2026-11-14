"""HTTP contract for the Quick page's shared cached result."""

import http.client
import json
import threading

import numpy as np

from zlscan.quick.app import QuickServer


def _request(port, path, method="GET", body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, body=payload, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    result = json.loads(response.read())
    connection.close()
    return response.status, result


def test_quick_row_endpoint_and_previous_run_share_cached_artifacts(tmp_path):
    server = QuickServer(("127.0.0.1", 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first_status, first = _request(
            server.server_address[1], "/api/run", "POST", {"width": 12, "height": 6}
        )
        second_status, second = _request(
            server.server_address[1],
            "/api/run",
            "POST",
            {"width": 12, "height": 6, "noise_sigma_v": 0.01},
        )
        assert first_status == second_status == 200
        assert second["previous"]["run"] == first["run"]
        status, row = _request(server.server_address[1], f"/api/row?run={second['run']}&row=3")
        assert status == 200 and row["row"] == 3
        arrays = np.load(tmp_path / second["run"] / "arrays.npz")
        np.testing.assert_allclose(row["values"]["voltage_v"], arrays["voltage_v"][3])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
