"""Errors whose message the command line may print as it is."""

from __future__ import annotations


class UserError(Exception):
    """A mistake the user can fix, described with public values only.

    The command line prints most errors as their type alone, because library messages can carry
    proxy URLs or credentials. A UserError's message is written to be shown: it names the bad
    value and what is accepted, and never holds a URL, credential, token or other secret.
    """
