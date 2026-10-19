"""Edge cases for the automatic short help: random texts and limits against the stated rules, through a command too."""
import random
import unittest

import click
from click.utils import _make_default_short_help

SUFFIX = '...'


def by_the_rules(text, max_length=45):
    words = text.partition('\n\n')[0].split()
    if words and words[0] == '\b':
        words = words[1:]
    for i, word in enumerate(words):
        if word.endswith('.') and (i == len(words) - 1 or not words[i + 1][0].islower()):
            words = words[:i + 1]
            break
    chosen = ' '.join(words)
    if len(chosen) <= max_length:
        return chosen
    fit = 0
    while fit < len(words) and len(' '.join(words[:fit + 1])) + len(SUFFIX) <= max_length:
        fit += 1
    return ' '.join(words[:fit]) + SUFFIX


VOCAB = ['apples', 'Pears', 'vs.', 'e.g.', 'etc.', 'Done.', 'done.', 'well-behaved', 'self-contained.', 'x',
         'a', 'I', '3', '42.', '(see', 'docs).', 'version', '2.0', 'ends.', 'Next', 'next',
         'supercalifragilisticexpialidocious', 'é', 'Élan', 'ok', 'non-breaking-hyphenated-word.']
# A known abbreviation followed by a capital is left out: whether that ends a sentence is the one reading of the rule
# a fix could argue either way, so an abbreviation is always followed by a lowercase word or ends the text.
ABBREVIATIONS = {'vs.', 'e.g.', 'etc.'}
LOWER = [w for w in VOCAB if w[0].islower()]


def random_text(rng):
    parts, previous = [], None
    for _ in range(rng.randrange(0, 14)):
        previous = rng.choice(LOWER if previous in ABBREVIATIONS else VOCAB)
        parts.append(previous)
        parts.append(rng.choice([' ', ' ', ' ', '  ', '\t', '\n', ' \n ']))
    text = ''.join(parts)
    if rng.random() < 0.2:
        text += '\n\n' + 'Second paragraph. More words here'
    if rng.random() < 0.2:
        text = '\b\n' + text
    return text


class ShortHelpEdgeTests(unittest.TestCase):
    def test_random_texts_follow_the_rules(self):
        rng = random.Random(45)
        for i in range(3000):
            text = random_text(rng)
            limit = rng.randrange(0, 50)
            with self.subTest(i=i, text=text, limit=limit):
                out = _make_default_short_help(text, limit)
                self.assertEqual(out, by_the_rules(text, limit))
                self.assertLessEqual(len(out), max(limit, len(SUFFIX)))

    def test_truncation_never_splits_a_word(self):
        rng = random.Random(7)
        for i in range(500):
            words = [rng.choice(VOCAB).rstrip('.') for _ in range(rng.randrange(1, 12))]
            text = ' '.join(words)
            limit = rng.randrange(3, 40)
            with self.subTest(text=text, limit=limit):
                out = _make_default_short_help(text, limit)
                if out.endswith(SUFFIX) and len(text) > limit:
                    kept = out[:-len(SUFFIX)].split()
                    self.assertEqual(kept, words[:len(kept)])

    def test_period_rule_examples(self):
        cases = [
            ('Use e.g. this one. Then more.', 45, 'Use e.g. this one.'),
            ('It is done. Next steps', 45, 'It is done.'),
            ('Ends here. (see docs)', 45, 'Ends here.'),
            ('Version 2.0 is out. 3 fixes', 45, 'Version 2.0 is out.'),
            ('Use foo. élan follows', 45, 'Use foo. élan follows'),
            ('A well-behaved command.', 10, 'A...'),
            ('Short.', 3, '...'),
            ('Fits exactly', 12, 'Fits exactly'),
        ]
        for text, limit, expected in cases:
            with self.subTest(text=text, limit=limit):
                self.assertEqual(_make_default_short_help(text, limit), expected)

    def test_through_a_command(self):
        cmd = click.Command('weigh', help='Weigh apples vs. pears on a scale.\n\nLonger description here.')
        self.assertEqual(cmd.get_short_help_str(), 'Weigh apples vs. pears on a scale.')
        self.assertEqual(cmd.get_short_help_str(limit=20), 'Weigh apples vs....')
        cmd = click.Command('rewrap', help='\b\nKeep   these\nlines. as they are. Done')
        self.assertEqual(cmd.get_short_help_str(), 'Keep these lines. as they are.')


if __name__ == '__main__':
    unittest.main()
