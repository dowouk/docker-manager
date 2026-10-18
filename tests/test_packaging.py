"""Packaging checks: a non-editable `pip install .` (as in the Dockerfile) must ship
the templates and static files, and the examples must carry every required setting."""
import tomllib
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_templates_and_static_are_package_data():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    globs = pyproject["tool"]["setuptools"]["package-data"]["app"]
    assert "templates/*" in globs
    assert "static/*" in globs
    assert "templates/notify/*" in globs
    # the globs are one level deep – fail if anyone adds a sub-folder they would miss
    for folder in ("templates", "static"):
        subdirs = {p.name for p in (ROOT / "app" / folder).iterdir()
                   if p.is_dir() and p.name != "__pycache__"}
        assert subdirs <= ({"notify"} if folder == "templates" else set())


REQUIRED = [name.upper() for name, field in Settings.model_fields.items() if field.is_required()]
EXAMPLES = ["caddy", "traefik"]
MAILCOW_TAG = "# [Mailcow] "


def test_examples_set_every_required_setting_and_mount():
    for name in EXAMPLES:
        text = (ROOT / "examples" / name / "compose.yml").read_text(encoding="utf-8")
        for var in REQUIRED:
            assert f"{var}:" in text, f"{name} example is missing {var}"
        assert "/var/run/docker.sock:/var/run/docker.sock" in text
        assert "/docker:/docker" in text
        # data lives next to the compose file, not in a named volume
        assert "- ./data:/data" in text
        assert "dashboard-data" not in text
        assert "restart: unless-stopped" in text
        # Docker sets HOSTNAME to the container ID; the dashboard uses it to find itself
        keys = [line.split(":", 1)[0].strip() for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        assert "hostname" not in keys
        # only reachable through the proxy – that is what makes trusting X-Forwarded-* safe
        assert "ports" not in keys


def test_examples_carry_the_mailcow_lines_commented_out():
    """Mailcow runs alongside Caddy or Traefik, not on a server of its own, so each
    example carries the host-access lines tagged `[Mailcow]`, commented out. Off by
    default; uncommenting exactly those lines must give valid YAML with the extras."""
    import yaml

    for name in EXAMPLES:
        text = (ROOT / "examples" / name / "compose.yml").read_text(encoding="utf-8")
        tagged = [line for line in text.splitlines() if line.lstrip().startswith(MAILCOW_TAG)]
        assert len(tagged) == 3, f"{name} example should tag exactly three [Mailcow] lines"
        service = yaml.safe_load(text)["services"]["docker-manager"]
        assert "pid" not in service and "privileged" not in service

        uncommented = "\n".join(line.replace(MAILCOW_TAG, "", 1) for line in text.splitlines())
        service = yaml.safe_load(uncommented)["services"]["docker-manager"]
        assert service["pid"] == "host"
        assert service["privileged"] is True
        assert "/opt/mailcow-dockerized:/opt/mailcow-dockerized" in service["volumes"]


# Can be set on the Settings page instead, so the examples carry them commented out
SETTINGS_PAGE_VARS = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                      "ACS_CONNECTION_STRING", "DIGEST_FROM", "DIGEST_TO")


def test_env_example_covers_every_value_the_examples_read_from_it():
    env = (ROOT / "examples" / ".env.example").read_text(encoding="utf-8")
    assert "SESSION_SECRET" not in env, "SESSION_SECRET is no longer needed"
    for var in SETTINGS_PAGE_VARS:
        assert f"\n# {var}=" in env, f".env.example should carry {var} commented out"
        assert f"\n{var}=" not in env
    assert "ALLOWED_" not in env
    for name in EXAMPLES:
        text = (ROOT / "examples" / name / "compose.yml").read_text(encoding="utf-8")
        assert "SESSION_SECRET" not in text
        assert "ALLOWED_" not in text, "ALLOWED_* are only read once, on upgrade"
        keys = [l.split(":", 1)[0].strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        assert "PUBLIC_URL" in keys
        for var in SETTINGS_PAGE_VARS:
            assert f'# {var}: "${{{var}}}"' in text, f"{name} example should carry {var} commented out"


def test_readme_documents_every_setting():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    from app.settings_service import UI_ONLY

    for name in Settings.model_fields:
        if name in UI_ONLY:
            continue  # set on the Settings page only
        assert f"`{name.upper()}`" in readme, f"README is missing {name.upper()}"
    assert "—" not in readme, "README must use en dashes, not em dashes"


def test_dockerfile_bounds_graceful_shutdown():
    import json

    cmd_line = next(line for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
                    if line.startswith("CMD "))
    cmd = json.loads(cmd_line[4:])
    i = cmd.index("--timeout-graceful-shutdown")
    assert cmd[i + 1] == "5"


def test_credential_helper_entry_point():
    from importlib.metadata import entry_points

    found = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert found["docker-credential-docker-manager"] == "app.credhelper:main_cli"
