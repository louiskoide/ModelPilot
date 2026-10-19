"""Edge cases for aware datetimes in naturalday()/naturaldate(): yesterday, many offsets, far dates, naive values."""
import datetime as dt
import unittest

from freezegun import freeze_time

import humanize

NOW = '2023-10-15 23:00:00+00:00'


def tz(hours, minutes=0):
    return dt.timezone(dt.timedelta(hours=hours, minutes=minutes))


def by_definition(value):
    """today/tomorrow/yesterday by the calendar date in the value's own timezone."""
    now = dt.datetime(2023, 10, 15, 23, 0, tzinfo=dt.timezone.utc).astimezone(value.tzinfo)
    return {0: 'today', 1: 'tomorrow', -1: 'yesterday'}.get((value.date() - now.date()).days)


class AwareNaturalDayEdgeTests(unittest.TestCase):
    @freeze_time(NOW)
    def test_naturalday_in_the_values_own_timezone(self):
        utc = dt.timezone.utc
        noon = dt.datetime(2023, 10, 15, 12, 0, tzinfo=utc)
        self.assertEqual(humanize.naturalday(noon), 'today')
        self.assertEqual(humanize.naturalday(noon.astimezone(tz(11))), 'yesterday')  # Oct 15 23:00 there, now Oct 16
        future = dt.datetime(2023, 10, 16, 6, 0, tzinfo=utc)
        self.assertEqual(humanize.naturalday(future), 'tomorrow')
        self.assertEqual(humanize.naturalday(future.astimezone(tz(11))), 'today')
        self.assertEqual(humanize.naturalday(future.astimezone(tz(-4))), 'tomorrow')

    @freeze_time(NOW)
    def test_many_offsets_and_instants(self):
        offsets = [tz(h) for h in range(-12, 15)] + [tz(5, 30), tz(-9, -30), tz(12, 45)]
        for hours in range(-60, 61, 3):
            instant = dt.datetime(2023, 10, 15, 23, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=hours)
            for zone in offsets:
                value = instant.astimezone(zone)
                expected = by_definition(value)
                if expected is None:
                    continue
                with self.subTest(value=value):
                    self.assertEqual(humanize.naturalday(value), expected)
                    self.assertEqual(humanize.naturaldate(value), expected)

    @freeze_time(NOW)
    def test_formats_for_other_days(self):
        value = dt.datetime(2023, 10, 18, 1, 0, tzinfo=tz(11))  # Oct 17 14:00 UTC
        self.assertEqual(humanize.naturalday(value), 'Oct 18')
        self.assertEqual(humanize.naturalday(value, '%Y-%m-%d'), '2023-10-18')
        far = dt.datetime(2024, 6, 1, 0, 30, tzinfo=tz(11))
        self.assertEqual(humanize.naturaldate(far), 'Jun 01 2024')
        self.assertEqual(humanize.naturaldate(dt.datetime(2023, 10, 16, 8, 0, tzinfo=tz(10))), 'today')

    @freeze_time(NOW)
    def test_naive_values_and_dates_unchanged(self):
        # freezegun's local date is the frozen UTC date, Oct 15.
        self.assertEqual(humanize.naturalday(dt.datetime(2023, 10, 16, 6, 0)), 'tomorrow')
        self.assertEqual(humanize.naturalday(dt.datetime(2023, 10, 15, 1, 0)), 'today')
        self.assertEqual(humanize.naturalday(dt.date(2023, 10, 14)), 'yesterday')
        self.assertEqual(humanize.naturaldate(dt.date(2023, 10, 16)), 'tomorrow')
        self.assertEqual(humanize.naturaldate(dt.date(2024, 6, 1)), 'Jun 01 2024')
        self.assertEqual(humanize.naturalday('not a date'), 'not a date')


if __name__ == '__main__':
    unittest.main()
