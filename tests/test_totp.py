import re

import pyotp

from app import totp

T = 1_700_000_000.0
STEP = int(T // 30)


def test_valid_code_returns_step():
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(T)
    assert totp.verify_code(secret, code, None, now=T) == STEP


def test_replay_rejected():
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(T)
    assert totp.verify_code(secret, code, STEP, now=T) is None


def test_old_code_outside_window_rejected():
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(T - 60)
    assert totp.verify_code(secret, code, None, now=T) is None


def test_adjacent_step_accepted():
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(T - 30)
    assert totp.verify_code(secret, code, None, now=T) == STEP - 1


def test_spaced_code_accepted():
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(T)
    assert totp.verify_code(secret, f"{code[:3]} {code[3:]}", None, now=T) == STEP


def test_bad_input_returns_none():
    secret = totp.new_secret()
    for bad in (None, "abcdef", "12345", "1234567", "", "１２３４５６", 123456):
        assert totp.verify_code(secret, bad, None, now=T) is None


def test_provisioning_uri_and_secret():
    secret = totp.new_secret()
    assert re.fullmatch(r"[A-Z2-7]+", secret)
    uri = totp.provisioning_uri(secret, "a@example.com", "Docker Manager – prod")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=" in uri


def test_recovery_codes():
    codes = totp.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(re.fullmatch(r"[a-km-np-z2-7]{5}-[a-km-np-z2-7]{5}", c) for c in codes)


def test_recovery_hash_normalised():
    assert totp.hash_recovery_code("ABCDE-12345") == totp.hash_recovery_code("abcde12345")
    assert totp.hash_recovery_code(" abcde 12345 ") == totp.hash_recovery_code("abcde-12345")
    assert re.fullmatch(r"[0-9a-f]{64}", totp.hash_recovery_code("abcde-12345"))


def test_qr_svg():
    svg = totp.qr_svg("otpauth://totp/x?secret=ABC")
    assert svg.startswith("<svg")
    assert "<?xml" not in svg
    assert "#fff" in svg.lower()  # light background so it scans in dark theme
