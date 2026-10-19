import os
from datetime import datetime

import pytest

from app.compose_edit import (
    EditRefused, apply_options_change, apply_tag_change, plan_service_options, plan_tag_change,
    restore_backup,
)

TWO = """services:
  web:
    image: nginx:1.25
    ports:
      - "80:80"
  api:
    image: nginx:1.25
"""


def write(path, text):
    data = text.encode()
    path.write_bytes(data)
    return str(path)


def test_replaces_only_target_service(tmp_path):
    p = write(tmp_path / "docker-compose.yml", TWO)
    change = plan_tag_change([p], "web", "nginx:1.27")
    assert change.line_no == 3
    assert change.old_line == "    image: nginx:1.25"
    assert change.new_line == "    image: nginx:1.27"
    assert "-    image: nginx:1.25" in change.diff
    apply_tag_change(change, str(tmp_path / "bk"), "proj", datetime(2026, 10, 17, 8, 0, 0))
    assert (tmp_path / "docker-compose.yml").read_text() == TWO.replace(
        "  web:\n    image: nginx:1.25", "  web:\n    image: nginx:1.27"
    )


def test_preserves_quotes_and_comment(tmp_path):
    p = write(tmp_path / "c.yml", 'services:\n  db:\n    image: "postgres:16.2"  # pinned\n')
    change = plan_tag_change([p], "db", "postgres:16.4")
    assert change.new_line == '    image: "postgres:16.4"  # pinned'
    p2 = write(tmp_path / "d.yml", "services:\n  db:\n    image: 'postgres:16.2'\n")
    assert plan_tag_change([p2], "db", "postgres:16.4").new_line == "    image: 'postgres:16.4'"


def test_replaces_whole_image_value(tmp_path):
    p = write(tmp_path / "c.yml", "services:\n  a:\n    image: ghcr.io/o/a:v2.1.0\n")
    assert plan_tag_change([p], "a", "ghcr.io/o/a:v2.1.1").new_line == "    image: ghcr.io/o/a:v2.1.1"


def test_preserves_crlf(tmp_path):
    original = TWO.replace("\n", "\r\n").encode()
    path = tmp_path / "docker-compose.yml"
    path.write_bytes(original)
    change = plan_tag_change([str(path)], "web", "nginx:1.27")
    apply_tag_change(change, str(tmp_path / "bk"), "p", datetime(2026, 10, 17, 8, 0, 0))
    assert path.read_bytes() == original.replace(b"image: nginx:1.25", b"image: nginx:1.27", 1)


def test_refuses_variable(tmp_path):
    p = write(tmp_path / "c.yml", "services:\n  db:\n    image: postgres:${PG}\n")
    with pytest.raises(EditRefused, match="Version set by variable") as e:
        plan_tag_change([p], "db", "postgres:16.4")
    assert str(e.value).endswith("– update by hand")


def test_override_file_wins(tmp_path):
    base = write(tmp_path / "base.yml", "services:\n  web:\n    image: nginx:1.25\n")
    over = write(tmp_path / "over.yml", "services:\n  web:\n    image: nginx:1.26\n")
    change = plan_tag_change([base, over], "web", "nginx:1.27")
    assert change.path == over


def test_falls_back_to_earlier_file_when_override_lacks_image(tmp_path):
    base = write(tmp_path / "base.yml", "services:\n  web:\n    image: nginx:1.25\n")
    over = write(tmp_path / "over.yml", "services:\n  web:\n    environment:\n      A: b\n")
    assert plan_tag_change([base, over], "web", "nginx:1.27").path == base


def test_missing_image_refused(tmp_path):
    p = write(tmp_path / "c.yml", "services:\n  web:\n    build: .\n")
    with pytest.raises(EditRefused, match="Image line not found") as e:
        plan_tag_change([p], "web", "nginx:1.27")
    assert str(e.value).endswith("– update by hand")


def test_nested_image_key_ignored(tmp_path):
    text = "services:\n  web:\n    environment:\n      image: wrong:1\n    image: nginx:1.25\n"
    p = write(tmp_path / "c.yml", text)
    change = plan_tag_change([p], "web", "nginx:1.27")
    assert change.line_no == 5


def test_nested_only_image_is_not_found(tmp_path):
    p = write(tmp_path / "c.yml", "services:\n  web:\n    labels:\n      image: x:1\n")
    with pytest.raises(EditRefused, match="Image line not found"):
        plan_tag_change([p], "web", "nginx:1.27")


def test_does_not_leak_into_next_service(tmp_path):
    p = write(tmp_path / "c.yml", "services:\n  web:\n    build: .\n  api:\n    image: a:1\n")
    with pytest.raises(EditRefused, match="Image line not found"):
        plan_tag_change([p], "web", "a:2")


def test_quoted_keys_and_comments(tmp_path):
    text = '# top\nname: x\n"services":  # svc\n  # c\n  "web":  # w\n    image: a:1\n'
    p = write(tmp_path / "c.yml", text)
    assert plan_tag_change([p], "web", "a:2").line_no == 6


@pytest.mark.parametrize(
    "text",
    [
        "services:\n  web: {image: x:1}\n",
        "services:\n  web:\n    image: *foo\n",
        "services:\n  web:\n    image: &a x:1\n",
        "services:\n  web:\n    image: >\n      x:1\n",
        "services:\n  web:\n    image:\n      x:1\n",
        "services:\n  web:\n    image: x:1\n      continued\n",
        "services:\n  web:\n    image: 'x:1\n      y'\n",
        "services:\n  web:\n    image: \"x:1\" junk\n",
        "services:\n  web:\n\timage: x:1\n",
        "x:\n  services:\n    web:\n      image: x:1\n",
        "services: {web: {image: x:1}}\n",
    ],
)
def test_refuses_unsafe_shapes(tmp_path, text):
    p = write(tmp_path / "c.yml", text)
    with pytest.raises(EditRefused) as e:
        plan_tag_change([p], "web", "x:2")
    assert str(e.value).endswith("– update by hand")


def test_apply_writes_backup_then_file(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(TWO)
    os.chmod(path, 0o640)
    change = plan_tag_change([str(path)], "web", "nginx:1.27")
    bk = apply_tag_change(change, str(tmp_path / "bk"), "proj", datetime(2026, 10, 17, 8, 0, 0))
    assert bk == str(tmp_path / "bk" / "proj" / "docker-compose.yml.20261017-080000")
    assert open(bk).read() == TWO
    assert "nginx:1.27" in path.read_text()
    assert (os.stat(path).st_mode & 0o777) == 0o640
    assert sorted(os.listdir(tmp_path)) == ["bk", "docker-compose.yml"]


def test_apply_refuses_if_changed(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(TWO)
    change = plan_tag_change([str(path)], "web", "nginx:1.27")
    edited = TWO.replace("nginx:1.25", "nginx:1.26", 1)
    path.write_text(edited)
    with pytest.raises(EditRefused, match="Compose file changed since preview"):
        apply_tag_change(change, str(tmp_path / "bk"), "p", datetime(2026, 10, 17, 8, 0, 0))
    assert path.read_text() == edited
    assert not (tmp_path / "bk").exists()


def test_restore(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_bytes(TWO.encode())
    change = plan_tag_change([str(path)], "web", "nginx:1.27")
    bk = apply_tag_change(change, str(tmp_path / "bk"), "p", datetime(2026, 10, 17, 8, 0, 0))
    assert path.read_bytes() != TWO.encode()
    restore_backup(change, bk)
    assert path.read_bytes() == TWO.encode()


@pytest.mark.parametrize("sep", ["\t", " \t"])
def test_tab_after_colon_preserved(tmp_path, sep):
    p = write(tmp_path / "c.yml", f"services:\n  web:\n    image:{sep}nginx:1.25\n")
    assert plan_tag_change([p], "web", "nginx:1.27").new_line == f"    image:{sep}nginx:1.27"


@pytest.mark.parametrize("value", ["*foo", "|", "&a x:1", ">"])
def test_tab_after_colon_still_refuses_special_values(tmp_path, value):
    p = write(tmp_path / "c.yml", f"services:\n  web:\n    image:\t{value}\n")
    with pytest.raises(EditRefused):
        plan_tag_change([p], "web", "x:2")


@pytest.mark.parametrize(
    "bad", ["nginx:1.27\n    privileged: true", 'a"b', "a b", "a:1 #x", "", "a'b", "a\r\nb"]
)
def test_refuses_unsafe_new_image(tmp_path, bad):
    p = write(tmp_path / "c.yml", TWO)
    with pytest.raises(EditRefused) as e:
        plan_tag_change([p], "web", bad)
    assert str(e.value).endswith("– update by hand")


def test_accepts_registry_style_image(tmp_path):
    p = write(tmp_path / "c.yml", TWO)
    plan_tag_change([p], "web", "ghcr.io/o/a:v2.1.1+b1@sha256:abc")


def test_apply_refuses_if_lines_shifted(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(TWO)
    change = plan_tag_change([str(path)], "web", "nginx:1.27")
    path.write_text("# added\n" + TWO)
    with pytest.raises(EditRefused, match="changed since preview"):
        apply_tag_change(change, str(tmp_path / "bk"), "p", datetime(2026, 10, 17, 8, 0, 0))


def test_backup_not_overwritten_and_mode_copied(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(TWO)
    os.chmod(path, 0o600)
    now = datetime(2026, 10, 17, 8, 0, 0)
    c1 = plan_tag_change([str(path)], "web", "nginx:1.27")
    b1 = apply_tag_change(c1, str(tmp_path / "bk"), "p", now)
    c2 = plan_tag_change([str(path)], "web", "nginx:1.28")
    b2 = apply_tag_change(c2, str(tmp_path / "bk"), "p", now)
    assert b2 == b1 + ".1"
    assert open(b1).read() == TWO
    assert "nginx:1.27" in open(b2).read()
    assert os.stat(b1).st_mode & 0o777 == 0o600


# --- the dashboard's own service: Mailcow options --------------------------------

_EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def _write(tmp_path, text, name="compose.yml"):
    path = tmp_path / name
    path.write_bytes(text.encode())
    return str(path)


@pytest.mark.parametrize("proxy", ["caddy", "traefik"])
def test_options_uncomment_the_mailcow_lines_in_the_examples(tmp_path, proxy):
    with open(os.path.join(_EXAMPLES, proxy, "compose.yml"), "rb") as f:
        original = f.read().decode()
    path = _write(tmp_path, original)
    change = plan_service_options([path], "docker-manager")
    assert "-    # [Mailcow] pid: host" in change.diff and "+    pid: host" in change.diff
    assert "+    privileged: true" in change.diff
    new = change.new_text.splitlines()
    old = original.splitlines()
    assert len(new) == len(old)  # replaced in place, nothing else moved
    assert sum(a != b for a, b in zip(old, new)) == 2
    backup = apply_options_change(change, str(tmp_path / "bak"), "dm", datetime(2026, 10, 19, 12))
    assert open(backup).read() == original
    assert plan_service_options([path], "docker-manager") is None  # now nothing to do


def test_options_added_after_the_service_name_when_not_commented(tmp_path):
    path = _write(tmp_path, "services:\n  dm:\n    image: x\n    restart: always\n  other:\n    image: y\n")
    change = plan_service_options([path], "dm")
    assert change.new_text == ("services:\n  dm:\n    pid: host\n    privileged: true\n"
                               "    image: x\n    restart: always\n  other:\n    image: y\n")


def test_options_keeps_what_is_already_right(tmp_path):
    path = _write(tmp_path, "services:\n  dm:\n    image: x\n    privileged: \"true\"\n")
    change = plan_service_options([path], "dm")
    assert change.new_text == "services:\n  dm:\n    pid: host\n    image: x\n    privileged: \"true\"\n"


def test_options_never_overwrite_a_different_value(tmp_path):
    path = _write(tmp_path, "services:\n  dm:\n    image: x\n    pid: \"service:db\"\n")
    with pytest.raises(EditRefused, match="already sets pid"):
        plan_service_options([path], "dm")


def test_options_use_the_file_that_has_the_service_and_keep_crlf(tmp_path):
    base = _write(tmp_path, "services:\r\n  dm:\r\n    image: x\r\n", "a.yml")
    other = _write(tmp_path, "services:\r\n  web:\r\n    image: y\r\n", "b.yml")
    change = plan_service_options([base, other], "dm")
    assert change.path == base and "    pid: host\r\n" in change.new_text
    with pytest.raises(EditRefused):
        plan_service_options([other], "dm")


def test_options_refuse_a_file_changed_since_the_preview(tmp_path):
    path = _write(tmp_path, "services:\n  dm:\n    image: x\n")
    change = plan_service_options([path], "dm")
    _write(tmp_path, "services:\n  dm:\n    image: z\n")
    with pytest.raises(EditRefused, match="changed since preview"):
        apply_options_change(change, str(tmp_path / "bak"), "dm", datetime(2026, 10, 19, 12))


def test_options_find_mailcow_comments_at_the_end_of_the_service(tmp_path):
    path = _write(tmp_path, "services:\n  dm:\n    image: x\n    # [Mailcow] pid: host\n"
                            "  # about the next one\n  web:\n    image: y\n")
    change = plan_service_options([path], "dm")
    assert change.new_text == ("services:\n  dm:\n    privileged: true\n    image: x\n"
                               "    pid: host\n  # about the next one\n  web:\n    image: y\n")
