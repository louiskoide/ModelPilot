"""Edge cases for optional seconds: each time form, offsets, fractions without seconds, arrays and tables."""
from datetime import date, datetime, time, timedelta, timezone
import unittest

import tomli


class OptionalSecondsEdgeTests(unittest.TestCase):
    def test_every_form_without_seconds(self):
        doc = tomli.loads('t = 07:32\nldt = 1979-05-27T07:32\nsp = 1979-05-27 07:32\n'
                          'z = 1979-05-27T07:32Z\noff = 1979-05-27T07:32-07:00\n')
        self.assertEqual(doc['t'], time(7, 32))
        self.assertEqual(doc['ldt'], datetime(1979, 5, 27, 7, 32))
        self.assertEqual(doc['sp'], datetime(1979, 5, 27, 7, 32))
        self.assertEqual(doc['z'], datetime(1979, 5, 27, 7, 32, tzinfo=timezone.utc))
        self.assertEqual(doc['off'], datetime(1979, 5, 27, 7, 32, tzinfo=timezone(timedelta(hours=-7))))

    def test_seconds_and_fractions_still_work(self):
        doc = tomli.loads('t = 07:32:05.25\ndt = 1979-05-27T07:32:05.5Z\nd = 1979-05-27')
        self.assertEqual(doc['t'], time(7, 32, 5, 250000))
        self.assertEqual(doc['dt'], datetime(1979, 5, 27, 7, 32, 5, 500000, tzinfo=timezone.utc))
        self.assertEqual(doc['d'], date(1979, 5, 27))

    def test_in_arrays_and_inline_tables(self):
        doc = tomli.loads('a = [07:32, 08:00:01]\nx = {t = 23:59, dt = 2025-04-18T20:05}')
        self.assertEqual(doc['a'], [time(7, 32), time(8, 0, 1)])
        self.assertEqual(doc['x'], {'t': time(23, 59), 'dt': datetime(2025, 4, 18, 20, 5)})

    def test_what_stays_invalid(self):
        for doc in ('t = 07:32.5', 'dt = 1979-05-27T07:32.5Z', 't = 7:32', 't = 07:3', 't = 24:00',
                    't = 07:60', 'dt = 1979-05-27T07'):
            with self.subTest(doc=doc):
                with self.assertRaises(tomli.TOMLDecodeError):
                    tomli.loads(doc)


if __name__ == '__main__':
    unittest.main()
