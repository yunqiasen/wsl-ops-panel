import gc
from pathlib import Path
from tests.test_cf_tunnels import make_panel


def test_repeated_cf_status_reads_release_database_connections(tmp_path):
    panel = make_panel(tmp_path)
    database = panel.app.state.data_root / "state.db"

    def handles():
        result = 0
        for fd in Path("/proc/self/fd").iterdir():
            try:
                result += fd.resolve() == database
            except OSError:
                pass
        return result

    gc.collect()
    initial = handles()
    gc.disable()
    try:
        for _ in range(20):
            assert panel.client.get("/api/assets/web/cf").status_code == 200
        assert handles() <= initial + 1
    finally:
        gc.enable()
        gc.collect()
