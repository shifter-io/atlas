"""Deliberately uncovered production code (CI proof, do not merge)."""


def ci_proof_uncovered(values: list[int]) -> int:
    total = 0
    for value in values:
        if value % 2 == 0:
            total += value
        else:
            total -= value
    if total < 0:
        total = -total
    return total


def ci_proof_uncovered_label(value: int) -> str:
    if value > 10:
        return "large"
    if value > 5:
        return "medium"
    return "small"
