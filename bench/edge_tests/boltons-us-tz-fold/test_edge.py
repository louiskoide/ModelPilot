"""Edge cases for the US timezones' fold handling: against zoneinfo for all four zones, both directions, many years."""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from boltons.timeutils import Central, Eastern, Mountain, Pacific

ZONES = [(Eastern, 'America/New_York'), (Central, 'America/Chicago'),
         (Mountain, 'America/Denver'), (Pacific, 'America/Los_Angeles')]
# Each rule era, its first and last years (zoneinfo's 1974-1975 energy-crisis rules differ, so they are left out).
YEARS = (1967, 1970, 1980, 1986, 1987, 1995, 2006, 2007, 2011, 2024, 2037)
UTC = timezone.utc


def transitions(zone, year):
    """UTC instants where zoneinfo's offset changes during the year."""
    out, t = [], datetime(year, 1, 1, tzinfo=UTC)
    prev = t.astimezone(zone).utcoffset()
    while t.year == year:
        t += timedelta(hours=1)
        cur = t.astimezone(zone).utcoffset()
        if cur != prev:
            out.append(t)
        prev = cur
    return out


class USTimeZoneFoldEdgeTests(unittest.TestCase):
    def assert_same_local(self, ours, theirs, label):
        self.assertEqual(ours.replace(tzinfo=None), theirs.replace(tzinfo=None), label)
        self.assertEqual(ours.fold, theirs.fold, label)
        self.assertEqual(ours.utcoffset(), theirs.utcoffset(), label)
        self.assertEqual(ours.tzname(), theirs.tzname(), label)

    def test_from_utc_matches_zoneinfo_around_every_transition(self):
        for ours, name in ZONES:
            zone = ZoneInfo(name)
            for year in YEARS:
                found = transitions(zone, year)
                self.assertEqual(len(found), 2, (name, year))
                for when in found:
                    for minutes in range(-240, 241, 15):
                        u = when + timedelta(minutes=minutes)
                        with self.subTest(zone=name, utc=u):
                            self.assert_same_local(u.astimezone(ours), u.astimezone(zone), str(u))

    def test_from_utc_matches_zoneinfo_through_the_year(self):
        for ours, name in ZONES:
            zone = ZoneInfo(name)
            for year in YEARS:
                u = datetime(year, 1, 1, 7, 30, tzinfo=UTC)
                while u.year == year:
                    with self.subTest(zone=name, utc=u):
                        self.assert_same_local(u.astimezone(ours), u.astimezone(zone), str(u))
                    u += timedelta(days=1)

    def test_local_times_with_fold_match_zoneinfo(self):
        for ours, name in ZONES:
            zone = ZoneInfo(name)
            for year in YEARS:
                for when in transitions(zone, year):
                    day = when.astimezone(zone).replace(hour=0, minute=0, tzinfo=None)
                    for minutes in range(0, 5 * 60, 10):
                        wall = day + timedelta(minutes=minutes)
                        back = wall.replace(tzinfo=zone).astimezone(UTC).astimezone(zone)
                        if back.replace(tzinfo=None) != wall:
                            continue  # in the spring gap: the wall time never happens
                        for fold in (0, 1):
                            with self.subTest(zone=name, wall=wall, fold=fold):
                                a = wall.replace(tzinfo=ours, fold=fold)
                                b = wall.replace(tzinfo=zone, fold=fold)
                                self.assertEqual(a.utcoffset(), b.utcoffset())
                                self.assertEqual(a.tzname(), b.tzname())
                                self.assertEqual(a.astimezone(UTC), b.astimezone(UTC))

    def test_repeated_hour_round_trips_through_utc(self):
        for ours, name in ZONES:
            for minute in (0, 1, 30, 59):
                first = datetime(2011, 11, 6, 1, minute, tzinfo=ours, fold=0)
                second = datetime(2011, 11, 6, 1, minute, tzinfo=ours, fold=1)
                with self.subTest(zone=name, minute=minute):
                    self.assertEqual(second.astimezone(UTC) - first.astimezone(UTC), timedelta(hours=1))
                    for local in (first, second):
                        again = local.astimezone(UTC).astimezone(ours)
                        self.assertEqual((again.replace(tzinfo=None), again.fold),
                                         (local.replace(tzinfo=None), local.fold))

    def test_years_without_dst(self):
        for ours, name in ZONES:
            std = ours.utcoffset(datetime(2011, 1, 15, tzinfo=ours))
            for year in (1900, 1950, 1966):
                for month in (1, 4, 7, 10, 11):
                    local = datetime(year, month, 15, 1, 30, tzinfo=ours)
                    with self.subTest(zone=name, local=local):
                        self.assertEqual(local.utcoffset(), std)
                        self.assertEqual(local.replace(fold=1).utcoffset(), std)
                        u = datetime(year, month, 15, 12, tzinfo=UTC).astimezone(ours)
                        self.assertEqual((u.utcoffset(), u.fold), (std, 0))


if __name__ == '__main__':
    unittest.main()
