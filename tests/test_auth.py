from dataclasses import dataclass

import pytest

from llmops.auth import bearer_token, find_owner, hash_key, new_key


@dataclass
class Owner:
    name: str
    key_sha256: str


def test_new_keys_are_unique_and_prefixed():
    a, b = new_key(), new_key()
    assert a != b and a.startswith("llmops-") and len(a) > 40


@pytest.mark.parametrize(
    "header, token",
    [
        ("Bearer abc", "abc"),
        ("bearer  abc ", "abc"),
        ("Basic abc", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_token(header, token):
    assert bearer_token(header) == token


def test_find_owner():
    owners = [Owner("a", hash_key("key-a")), Owner("b", hash_key("key-b"))]
    assert find_owner("key-b", owners) is owners[1]
    assert find_owner("key-c", owners) is None
