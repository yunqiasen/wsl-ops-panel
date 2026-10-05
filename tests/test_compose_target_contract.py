from app.adapters.docker_adapter import DockerComposeAdapter
from app.services.docker_lifecycle import execute
from tests.test_docker_lifecycle_repair import FakeDocker, container, context


def test_empty_runtime_files_use_configured_compose_file(tmp_path):
    adapter = DockerComposeAdapter(
        project_dir=str(tmp_path),
        compose_file="selected.yml",
        primary_container="demo",
        compose_service="web",
        runtime_context={"compose_files": []},
    )
    plan = adapter.plan_action("start")
    assert str(tmp_path / "selected.yml") in plan.preview_paths


def test_pull_and_local_build_share_compose_prefix(tmp_path):
    files = ["base.yml", "override.yml"]
    for name in [*files, "vars.env"]:
        (tmp_path / name).write_text("services: {}")
    ctx = context(tmp_path)
    ctx.update(compose_files=files, env_files=["vars.env"])
    docker = FakeDocker([container(cwd=str(tmp_path))])
    execute(ctx, "update_latest", runner=docker, verify_timeout=0)
    pull = next(c for c in docker.commands if c[:2] == ["docker", "compose"])
    adapter = DockerComposeAdapter(
        project_dir=str(tmp_path),
        compose_file=files[0],
        primary_container="demo",
        compose_service="web",
        runtime_context=ctx,
        lifecycle_strategy="compose_local_build_git_tag",
        recipe_repo_dir=str(tmp_path),
        override_file="override.yml",
        local_image_repository="local/fixture",
        local_image_tag_template="{version}",
    )
    plan = adapter.plan_action("deploy_version", "v1.0.0")
    build = next(c for c in plan.commands if "compose" in c and "config" in c)
    assert (
        pull[: pull.index("config")]
        == build[build.index("docker") : build.index("config")]
    )


def test_duplicate_files_normalized_in_runtime_and_build(tmp_path):
    (tmp_path / "compose.yml").write_text("services: {}")
    (tmp_path / "vars.env").write_text("X=1")
    ctx = context(tmp_path)
    ctx.update(
        compose_files=["compose.yml", str(tmp_path / "compose.yml")],
        env_files=["vars.env", "vars.env"],
    )
    docker = FakeDocker([container(cwd=str(tmp_path))])
    execute(ctx, "update_latest", runner=docker, verify_timeout=0)
    cmd = next(c for c in docker.commands if c[:2] == ["docker", "compose"])
    assert cmd.count("-f") == 1
    assert cmd.count("--env-file") == 1


def test_explicit_compose_files_win_over_observed_labels():
    from types import SimpleNamespace
    from app.services.compose_target import capture_compose_context

    primary = SimpleNamespace(
        compose_project="observed",
        labels={"com.docker.compose.project.config_files": "/old/compose.yml"},
    )
    result = capture_compose_context(
        SimpleNamespace(containers=[]), primary, {"compose_files": ["selected.yml"]}
    )
    assert result["compose_files"] == ["selected.yml"]


def test_matching_configured_base_keeps_observed_overrides(tmp_path):
    from types import SimpleNamespace
    from app.services.compose_target import capture_compose_context

    primary = SimpleNamespace(
        compose_project="observed",
        labels={
            "com.docker.compose.project.config_files": f"{tmp_path}/base.yml,{tmp_path}/override.yml"
        },
    )
    result = capture_compose_context(
        SimpleNamespace(containers=[]),
        primary,
        {"project_dir": str(tmp_path), "compose_file": "base.yml"},
    )
    assert result["compose_files"] == [
        str(tmp_path / "base.yml"),
        str(tmp_path / "override.yml"),
    ]
