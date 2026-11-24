import json
import re


class DomainError(ValueError):
    """An invalid command that must roll back the surrounding transaction."""


def require(condition, message):
    if not condition:
        raise DomainError(message)


def fields(data, names):
    require(isinstance(data, dict) and data.keys() == set(names.split()),
            f"expected fields: {names}")


def token(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", value),
            "expected an identifier of 1–60 letters, digits, underscores or hyphens")
    return value


def number(value, minimum=0, maximum=10**9):
    require(type(value) is int and minimum <= value <= maximum,
            f"expected an integer in [{minimum}, {maximum}]")
    return value


def minute(value):
    return number(value, maximum=1440)


def choice(value, choices):
    require(isinstance(value, str) and value in choices, f"expected one of {', '.join(choices)}")
    return value


def tokens(values):
    require(isinstance(values, list), "expected a list of identifiers")
    result = [token(value) for value in values]
    require(len(result) == len(set(result)), "identifiers must be unique")
    return result


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def decode(value):
    return json.loads(value)
