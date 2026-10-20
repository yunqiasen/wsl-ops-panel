from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep
from tests.test_cf_tunnels import make_panel, attach_local_observation


def test_origin_probe_has_a_total_deadline_even_if_bytes_keep_arriving(
    tmp_path, monkeypatch
):
    class Drip(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "65")
            self.end_headers()
            try:
                for _ in range(65):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    observed.info["ports"]["8080/tcp"][0]["HostPort"] = str(server.server_port)
    panel.app.state.cf_service.http_transport = None
    started = monotonic()
    try:
        response = panel.client.get("/api/assets/web/cf/targets")
        assert response.status_code == 400
        assert monotonic() - started < 6
        assert panel.store.list_all() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
