"""California court-holiday helpers shared by scrape and coverage scripts."""

from __future__ import annotations

from datetime import date

import holidays as hol


def _us_federal_holidays(years: range) -> object:
    try:
        return hol.country_holidays("US", categories=hol.GOVERNMENT, years=years)
    except (AttributeError, TypeError, ValueError):
        return hol.country_holidays("US", years=years)


def ca_court_holidays(min_year: int, max_year: int) -> set[str]:
    """Return ISO date strings for California court holidays in a year range."""
    years = range(min_year, max_year + 1)
    ca_public = hol.country_holidays("US", subdiv="CA", years=years)
    us_federal = _us_federal_holidays(years)
    combined: set[date] = set(ca_public.keys()) | set(us_federal.keys())

    for year in years:
        lincoln = date(year, 2, 12)
        if lincoln.weekday() == 5:
            lincoln = date(year, 2, 11)
        elif lincoln.weekday() == 6:
            lincoln = date(year, 2, 13)
        combined.add(lincoln)

    return {d.isoformat() for d in combined}
