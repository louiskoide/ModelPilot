"""Edge cases for version_option's import-name lookup: real installed packages, detected names, precedence, errors."""
import importlib.metadata
import unittest
from unittest import mock

import click
from click.testing import CliRunner


def fake_metadata(versions, mapping):
    def version(name):
        if name in versions:
            return versions[name]
        raise importlib.metadata.PackageNotFoundError(name)

    return (mock.patch.object(importlib.metadata, 'version', version),
            mock.patch.object(importlib.metadata, 'packages_distributions', lambda: mapping))


def invoke(cli):
    return CliRunner().invoke(cli, ['--version'], prog_name='tool')


class PackageNameEdgeTests(unittest.TestCase):
    def test_real_import_names_resolve_to_their_distributions(self):
        for import_name, dist in (('dateutil', 'python-dateutil'), ('_pytest', 'pytest')):
            with self.subTest(import_name=import_name):
                @click.command()
                @click.version_option(package_name=import_name)
                def cli():
                    pass

                result = invoke(cli)
                self.assertEqual(result.exit_code, 0, (result.output, result.exception))
                self.assertEqual(result.output, f'tool, version {importlib.metadata.version(dist)}\n')

    def test_detected_name_is_resolved_as_an_import_name(self):
        # Detection takes this module's top-level name, which no distribution is called.
        detected = __name__.partition('.')[0]
        patch_version, patch_mapping = fake_metadata({'edge-dist': '3.1.4'}, {detected: ['edge-dist']})
        with patch_version, patch_mapping:
            @click.command()
            @click.version_option()
            def cli():
                pass

            result = invoke(cli)
        self.assertEqual(result.exit_code, 0, (result.output, result.exception))
        self.assertEqual(result.output, 'tool, version 3.1.4\n')

    def test_a_matching_distribution_name_wins(self):
        patch_version, patch_mapping = fake_metadata({'foo': '1.0', 'other-dist': '9.9'}, {'foo': ['other-dist']})
        with patch_version, patch_mapping:
            @click.command()
            @click.version_option(package_name='foo')
            def cli():
                pass

            result = invoke(cli)
        self.assertEqual(result.exit_code, 0, (result.output, result.exception))
        self.assertEqual(result.output, 'tool, version 1.0\n')

    def test_several_distributions_are_all_named(self):
        dists = ['alpha-plug', 'beta-plug', 'gamma-plug']
        patch_version, patch_mapping = fake_metadata({d: '1.0' for d in dists}, {'plug': dists})
        with patch_version, patch_mapping:
            @click.command()
            @click.version_option(package_name='plug')
            def cli():
                pass

            result = invoke(cli)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIsInstance(result.exception, RuntimeError)
        message = str(result.exception)
        self.assertIn('multiple installed distributions', message)
        for d in dists:
            self.assertIn(d, message)

    def test_unknown_names_keep_the_not_installed_error(self):
        for mapping in ({}, {'plug': []}, {'other': ['other-dist']}):
            with self.subTest(mapping=mapping):
                patch_version, patch_mapping = fake_metadata({'other-dist': '2.0'}, mapping)
                with patch_version, patch_mapping:
                    @click.command()
                    @click.version_option(package_name='plug')
                    def cli():
                        pass

                    result = invoke(cli)
                self.assertNotEqual(result.exit_code, 0)
                self.assertIsInstance(result.exception, RuntimeError)
                self.assertIn('not installed', str(result.exception))


if __name__ == '__main__':
    unittest.main()
