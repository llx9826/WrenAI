import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from wren_http import main as entrypoint

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


def test_importing_main_has_no_runtime_io(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(SOURCE)!r}); import wren_http.main",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_main_loads_settings_then_builds_and_runs_app(monkeypatch):
    calls = []
    settings = SimpleNamespace(http_host="127.0.0.1", http_port=18001, http_workers=1)
    app = object()

    monkeypatch.setattr(entrypoint, "ServiceSettings", lambda: settings)
    monkeypatch.setattr(
        entrypoint,
        "create_app",
        lambda received: calls.append(("create_app", received)) or app,
    )
    monkeypatch.setattr(
        entrypoint.uvicorn,
        "run",
        lambda received, **options: calls.append(("run", received, options)),
    )

    entrypoint.main()

    assert calls == [
        ("create_app", settings),
        ("run", app, {"host": "127.0.0.1", "port": 18001, "workers": 1}),
    ]
