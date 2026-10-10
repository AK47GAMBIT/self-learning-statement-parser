"""ISIN check-digit logic (Luhn algorithm applied to the letters-as-numbers form)."""


def _expand(body: str) -> str:
    """Letters become two-digit numbers (A=10 ... Z=35); digits stay as they are."""
    return "".join(str(int(c, 36)) for c in body)


def _luhn_check_digit(digits: str) -> int:
    total = 0
    # Walk right to left. The check digit will sit to the right of this string,
    # so the rightmost digit of `digits` is doubled.
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def compute_check_digit(first_11: str) -> str:
    return str(_luhn_check_digit(_expand(first_11.upper())))


def make_isin(first_11: str) -> str:
    """Build a valid ISIN from its first 11 characters."""
    return first_11.upper() + compute_check_digit(first_11)


def is_valid_isin(isin: str) -> bool:
    isin = isin.strip().upper()
    if len(isin) != 12 or not isin.isalnum() or not isin[:2].isalpha():
        return False
    if not isin[-1].isdigit():
        return False
    return compute_check_digit(isin[:11]) == isin[-1]