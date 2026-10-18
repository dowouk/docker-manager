from app.roles import Role
from auth_helpers import CSRF, logged_in_client
from test_routes import compose_file, make_app, seeded  # noqa: F401 – fixtures


def post(client, path, **data):
    return client.post(path, data={"csrf_token": CSRF, **data})


def test_form_shows_fields(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR) as c:
        r = c.get("/containers/site/web/rules")
    assert r.status_code == 200
    assert "Watch settings" in r.text and "Set by a label" not in r.text
    assert 'action="/containers/site/web/rules"' in r.text


def test_unknown_key_404(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR) as c:
        assert c.get("/containers/nope/rules").status_code == 404
        assert post(c, "/containers/nope/rules", watch="no").status_code == 404


def test_viewer_forbidden_and_no_dashboard_link(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.VIEWER) as c:
        assert c.get("/containers/site/web/rules").status_code == 403
        assert "Watch settings" not in c.get("/").text
    with logged_in_client(app, Role.OPERATOR) as c:
        assert 'href="/containers/site/web/rules"' in c.get("/").text
        assert 'href="/containers/lone/rules"' in c.get("/").text  # no stack: the name


def test_csrf_refused(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR) as c:
        r = c.post("/containers/site/web/rules", data={"watch": "no", "csrf_token": "bad"})
    assert r.status_code == 403
    assert app.state.rules.saved("site/web") == {}


def test_save_applies_and_audits(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR, follow_redirects=False) as c:
        r = post(c, "/containers/site/web/rules", watch="no", notify="yes",
                 exclude_tags="x.*", include_tags="", release_notes="", layout_ignore="no")
        assert r.status_code == 303
        page = c.get("/containers/site/web/rules").text
    assert app.state.rules.saved("site/web")["watch"] is False
    assert 'value="x.*"' in page
    assert any(a.summary == "Watch rules changed: site/web"
               for a in app.state.store.list_audit())


def test_invalid_input_re_shows_form(settings, tmp_path, seeded):  # noqa: F811
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR) as c:
        r = post(c, "/containers/site/web/rules", include_tags="(", release_notes="ftp://x")
    assert r.status_code == 400
    assert r.text.count("field-error") >= 2
    assert app.state.rules.saved("site/web") == {}


def test_label_set_field_is_disabled(settings, tmp_path, seeded):  # noqa: F811
    seeded.containers[0].container.labels = {"docker-manager.enable": "false"}
    app = make_app(settings, tmp_path, seeded)
    with logged_in_client(app, Role.OPERATOR) as c:
        html = c.get("/containers/site/web/rules").text
    assert "Set by a label on the container" in html
    assert 'name="watch" disabled' in html
    assert 'name="exclude_tags" disabled' not in html
