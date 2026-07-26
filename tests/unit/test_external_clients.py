"""Mojang→PlayerDB fallback on ratelimit; wynncraft with/without token; generic 5xx retry."""

import pytest


def test_mojang_ratelimit_falls_back_to_playerdb():
    pytest.skip("stub")


def test_wynncraft_sends_token_header_when_configured():
    pytest.skip("stub")


def test_wynncraft_works_unauthenticated():
    pytest.skip("stub")


def test_5xx_response_is_retried():
    pytest.skip("stub")
