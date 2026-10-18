from app.roles import APP_ROLE_CLAIMS, ROLE_LABELS, Role, default_role_for_new, mapped_role

G1 = "11111111-1111-1111-1111-111111111111"


def test_rank_comparison():
    assert Role.ADMIN >= Role.OPERATOR >= Role.VIEWER >= Role.PENDING
    assert not Role.VIEWER >= Role.OPERATOR
    assert Role.ADMIN > Role.VIEWER
    assert ROLE_LABELS[Role.PENDING] == "Waiting for approval"
    assert APP_ROLE_CLAIMS["DockerManager.Admin"] is Role.ADMIN


def test_highest_of_app_role_and_group_wins():
    claims = {"roles": ["DockerManager.Viewer"], "groups": [G1]}
    assert mapped_role(claims, [{"group_id": G1, "role": "admin"}]) is Role.ADMIN


def test_unknown_role_names_ignored():
    assert mapped_role({"roles": ["Other.Role"]}, []) is None


def test_no_groups_claim_uses_only_app_roles():
    claims = {"roles": ["DockerManager.Operator"]}
    assert mapped_role(claims, [{"group_id": G1, "role": "admin"}]) is Role.OPERATOR


def test_nothing_matches():
    assert mapped_role({}, [{"group_id": G1, "role": "admin"}]) is None
    assert mapped_role({"groups": ["other"]}, [{"group_id": G1, "role": "admin"}]) is None


def test_default_role_for_new():
    assert default_role_for_new("admin_legacy") == (Role.ADMIN, True)
    assert default_role_for_new("viewer") == (Role.VIEWER, False)
    assert default_role_for_new("pending") == (Role.PENDING, False)


def test_comparison_with_non_roles_not_implemented():
    import pytest

    with pytest.raises(TypeError):
        Role.ADMIN >= 3
