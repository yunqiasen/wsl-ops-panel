# WSL Ops Panel

WSL 维护更新管理面板，一个用于 WSL 维护更新操作的 FastAPI 项目骨架。

- 应用入口：`app.main:app`
- 启动命令：`uv run uvicorn app.main:app --reload`
- 测试命令：`pytest tests/test_app_smoke.py -v`
- 当前 Task 1 仅提供：`/healthz`
