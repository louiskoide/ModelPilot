"""Edge cases for strftime-style field types: other directives, search/findall and unchanged types."""
from datetime import date, datetime, time, timedelta, timezone
import unittest

import parse


class StrftimeEdgeTests(unittest.TestCase):
    def test_twelve_hour_clock_with_am_pm(self):
        self.assertEqual(parse.parse('{:%I:%M %p}', '03:15 PM')[0], time(15, 15))
        self.assertEqual(parse.parse('{:%I:%M %p}', '12:05 AM')[0], time(0, 5))

    def test_two_digit_year_and_names(self):
        self.assertEqual(parse.parse('{:%d/%m/%y}', '16/07/97')[0], date(1997, 7, 16))
        self.assertEqual(parse.parse('{:%A %d %B %Y}', 'Wednesday 16 July 1997')[0], date(1997, 7, 16))
        self.assertEqual(parse.parse('{:%a %d %b %Y}', 'Wed 16 Jul 1997')[0], date(1997, 7, 16))

    def test_negative_offset_with_colon(self):
        r = parse.parse('{:%Y-%m-%d %H:%M %z}', '2023-11-21 13:23 -05:30')
        self.assertEqual(r[0], datetime(2023, 11, 21, 13, 23, tzinfo=timezone(-timedelta(hours=5, minutes=30))))

    def test_missing_year_uses_the_current_year(self):
        self.assertEqual(parse.parse('{:%m-%d}', '07-16')[0], date(datetime.today().year, 7, 16))

    def test_search_findall_and_no_match(self):
        self.assertEqual(parse.search('on {:%Y-%m-%d}', 'Released on 2023-11-25.')[0], date(2023, 11, 25))
        found = [r[0] for r in parse.findall('{:%H:%M:%S}', 'at 01:02:03 and 04:05:06')]
        self.assertEqual(found, [time(1, 2, 3), time(4, 5, 6)])
        self.assertIsNone(parse.parse('{:%Y-%m-%d}', 'not a date'))
        compiled = parse.compile('{when:%Y-%m-%d %H:%M}')
        self.assertEqual(compiled.parse('2024-02-29 23:59').named['when'], datetime(2024, 2, 29, 23, 59))

    def test_directive_table_and_existing_types(self):
        listed = {'%a', '%A', '%w', '%d', '%b', '%B', '%m', '%y', '%Y', '%H', '%I', '%p', '%M', '%S', '%f', '%z',
                  '%j', '%U', '%W'}
        self.assertLessEqual(listed, set(parse.dt_format_to_regex))
        self.assertEqual(parse.parse('{:%}', '50%')[0], 0.5)
        self.assertEqual(parse.parse('{:ti}', '1997-07-16')[0], datetime(1997, 7, 16))
        self.assertEqual(parse.parse('{:d}%', '20%')[0], 20)


if __name__ == '__main__':
    unittest.main()
