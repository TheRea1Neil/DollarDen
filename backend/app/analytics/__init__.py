"""Reporting over the allocation ledger.

Nothing here writes. Every number the user sees on the insights screen
or gets back from a question comes out of this module, computed in SQL
from the same rows that produce envelope balances -- so the two can
never tell different stories.
"""
