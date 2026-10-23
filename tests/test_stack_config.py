import os
import stat
from datetime import datetime

import pytest

from app import stack_config
from app.inventory import ContainerInfo
from app.stack_config import ConfigError

NOW = datetime(2026, 10, 23, 9, 30)


def make_info(root, **kw) -> ContainerInfo:
    proj = root / "proj"
    base = dict(
        id="abc", name="web", image_ref="nginx:1.0", image_id="sha256:x",
        running_repo_digests=[], local_tag_image_id=None, project="proj",
        working_dir=str(proj), config_files=[str(proj / "compose.yml")],
        service="web", state="running", is_self=False,
    )
    base.update(kw)
    return ContainerInfo(**base)


@pytest.fixture
def root(tmp_path):
    proj = tmp_path / "docker" / "proj"
    proj.mkdir(parents=True)
    (proj / "compose.yml").write_text("services:\n  web:\n    image: nginx:1.0\n")
    (proj / ".env").write_text("DB_PASSWORD=old\nKEEP=1\n")
    os.chmod(proj / ".env", 0o640)
    return tmp_path / "docker"


def test_stack_files_reads_compose_and_default_env(root):
    files = stack_config.stack_files(make_info(root), str(root))
    assert [(os.path.basename(f.path), f.kind, f.editable) for f in files] == [
        ("compose.yml", "compose", True), (".env", "env", True)]
    assert files[1].text == "DB_PASSWORD=old\nKEEP=1\n"
    assert files[1].sha256


def test_recorded_env_files_win_over_the_default(root):
    other = root / "proj" / "prod.env"
    other.write_text("A=1\n")
    files = stack_config.stack_files(make_info(root, env_files=[str(other)]), str(root))
    assert [f.path for f in files if f.kind == "env"] == [str(other)]


def test_files_outside_compose_root_are_read_only(root, tmp_path):
    outside = tmp_path / "elsewhere.yml"
    outside.write_text("services: {}\n")
    files = stack_config.stack_files(make_info(root, config_files=[str(outside)]), str(root))
    assert not files[0].editable and "Outside" in files[0].reason
    assert files[0].text == "services: {}\n"


def test_symlink_out_of_compose_root_is_read_only(root, tmp_path):
    target = tmp_path / "secret.env"
    target.write_text("X=1\n")
    os.symlink(target, root / "proj" / "link.env")
    files = stack_config.stack_files(make_info(root, env_files=[str(root / "proj" / "link.env")]),
                                     str(root))
    assert not files[-1].editable


def test_big_and_missing_files(root):
    (root / "proj" / "compose.yml").write_bytes(b"#" * (stack_config.MAX_BYTES + 1))
    (root / "proj" / ".env").unlink()
    files = stack_config.stack_files(make_info(root), str(root))
    assert not files[0].editable and "1 MB" in files[0].reason
    assert files[1].editable and not files[1].exists and files[1].sha256 == ""


def test_normalise():
    assert stack_config.normalise("a\r\nb") == "a\nb\n"
    assert stack_config.normalise("") == ""


def test_env_summary_names_only():
    s = stack_config.env_summary("A=1\nB=2\n# c\n", "A=1\nB=3\nexport C=4\n")
    assert s == "added C; changed B"
    assert "3" not in s
    assert stack_config.env_summary("A=1\n", "A=1\n# note\n") == "comments or layout only"


def test_save_backs_up_and_keeps_mode(root, tmp_path):
    files = stack_config.stack_files(make_info(root), str(root))
    texts = [files[0].text, "DB_PASSWORD=new\nKEEP=1\n"]
    summary, diff = stack_config.save(files, texts, str(tmp_path / "backups"), "proj", NOW)
    env = root / "proj" / ".env"
    assert env.read_text() == "DB_PASSWORD=new\nKEEP=1\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
    assert summary == f"Edited the config of proj – {env}: changed DB_PASSWORD"
    assert "new" not in summary and diff == ""
    backups = list((tmp_path / "backups" / "proj").iterdir())
    assert [b.read_text() for b in backups] == ["DB_PASSWORD=old\nKEEP=1\n"]


def test_save_compose_gives_a_diff_and_keeps_crlf(root, tmp_path):
    path = root / "proj" / "compose.yml"
    path.write_bytes(b"services:\r\n  web:\r\n    image: nginx:1.0\r\n")
    files = stack_config.stack_files(make_info(root), str(root))
    assert files[0].text == "services:\n  web:\n    image: nginx:1.0\n"
    new = "services:\n  web:\n    image: nginx:1.1\n"
    summary, diff = stack_config.save(files, [new, files[1].text], str(tmp_path / "b"), "proj", NOW)
    assert path.read_bytes() == b"services:\r\n  web:\r\n    image: nginx:1.1\r\n"
    assert "-    image: nginx:1.0" in diff and "+    image: nginx:1.1" in diff


def test_save_creates_a_private_env_file(root, tmp_path):
    (root / "proj" / ".env").unlink()
    files = stack_config.stack_files(make_info(root), str(root))
    stack_config.save(files, [files[0].text, "A=1\n"], str(tmp_path / "b"), "proj", NOW)
    env = root / "proj" / ".env"
    assert env.read_text() == "A=1\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_save_refuses_when_a_file_changed_and_writes_nothing(root, tmp_path):
    files = stack_config.stack_files(make_info(root), str(root))
    (root / "proj" / ".env").write_text("SOMEONE=else\n")
    with pytest.raises(ConfigError, match="changed since you opened it"):
        stack_config.save(files, ["services: {}\n", "A=1\n"], str(tmp_path / "b"), "proj", NOW)
    assert (root / "proj" / "compose.yml").read_text().startswith("services:\n  web")


def test_check_unchanged(root):
    files = stack_config.stack_files(make_info(root), str(root))
    stack_config.check_unchanged(files, [f.sha256 for f in files])
    with pytest.raises(ConfigError):
        stack_config.check_unchanged(files, ["x", files[1].sha256])


class FakeProc:
    def __init__(self, rc, out):
        self.returncode, self.out = rc, out

    async def communicate(self):
        return self.out, b""


async def test_validate_checks_copies_and_names_the_real_files(root, monkeypatch):
    seen = {}

    async def fake_exec(*cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        copy = cmd[cmd.index("-f") + 1]
        seen["copy"] = open(copy).read()
        return FakeProc(1, f"yaml: line 2 in {copy}: bad".encode())

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    info = make_info(root)
    files = stack_config.stack_files(info, str(root))
    with pytest.raises(ConfigError) as e:
        await stack_config.validate(info, files, ["services: [\n", files[1].text])
    assert seen["copy"] == "services: [\n"
    assert str(e.value) == f"yaml: line 2 in {files[0].path}: bad"
    cmd = list(seen["cmd"])
    assert cmd[:6] == ["docker", "compose", "-p", "proj", "--project-directory", info.working_dir]
    assert cmd[-2:] == ["config", "--quiet"] and "--env-file" in cmd
    assert seen["kw"]["env"]["PWD"] == info.working_dir
    # The real files were not touched.
    assert (root / "proj" / "compose.yml").read_text().startswith("services:\n")


async def test_validate_passes(root, monkeypatch):
    async def fake_exec(*cmd, **kw):
        return FakeProc(0, b"")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    info = make_info(root)
    files = stack_config.stack_files(info, str(root))
    await stack_config.validate(info, files, [f.text for f in files])


def _backups(tmp_path, names):
    folder = tmp_path / "backups" / "proj"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in names.items():
        (folder / name).write_text(text)
    return str(tmp_path / "backups")


def test_list_backups_groups_by_time_newest_first(root, tmp_path):
    files = stack_config.stack_files(make_info(root), str(root))
    backup_dir = _backups(tmp_path, {
        "compose.yml.20261020-080000": "old compose\n",
        ".env.20261020-080000": "A=0\n",
        ".env.20261022-120000": "A=1\n",
        ".env.20261022-120000.1": "A=2\n",
        "other.yml.20261023-090000": "not in this stack\n",
        "notes.txt": "ignored\n",
    })
    backups = stack_config.list_backups(files, backup_dir, "proj")
    assert [b.stamp for b in backups] == ["20261022-120000", "20261020-080000"]
    assert backups[0].label == "22 Oct 2026, 12:00:00"
    assert list(backups[0].paths) == [1] and backups[0].paths[1].endswith(".1")
    assert sorted(backups[1].paths) == [0, 1]
    assert stack_config.list_backups(files, str(tmp_path / "none"), "proj") == []
    assert len(stack_config.list_backups(files, backup_dir, "proj", limit=1)) == 1


def test_load_backup_replaces_only_its_files(root, tmp_path):
    files = stack_config.stack_files(make_info(root), str(root))
    backup_dir = _backups(tmp_path, {".env.20261022-120000": "A=1\r\n"})
    backup, texts = stack_config.load_backup(files, backup_dir, "proj", "20261022-120000")
    assert texts == [files[0].text, "A=1\n"]
    with pytest.raises(ConfigError, match="not found"):
        stack_config.load_backup(files, backup_dir, "proj", "20990101-000000")


def test_backups_of_files_with_the_same_name_are_not_mapped(root, tmp_path):
    sub = root / "proj" / "sub"
    sub.mkdir()
    (sub / "compose.yml").write_text("services: {}\n")
    info = make_info(root, config_files=[str(root / "proj" / "compose.yml"), str(sub / "compose.yml")])
    files = stack_config.stack_files(info, str(root))
    backup_dir = _backups(tmp_path, {"compose.yml.20261022-120000": "x\n"})
    assert stack_config.list_backups(files, backup_dir, "proj") == []
