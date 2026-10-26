"""Public API reuse checks, no reference implementation supplied."""
import unittest
import click
from click.testing import CliRunner


class TaggedArgument(click.Argument):
    def __init__(self, *args, marker=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.marker = marker


class TaggedOption(click.Option):
    def __init__(self, *args, marker=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.marker = marker


def callback(value):
    click.echo(value)


class SharedParameterChecks(unittest.TestCase):
    def test_argument_custom_type_survives_reuse(self):
        shared = click.argument("value", cls=TaggedArgument, marker="retained")
        commands = [click.command(name=f"command-{i}")(shared(lambda value: click.echo(value))) for i in range(3)]
        for command in commands:
            self.assertIsInstance(command.params[0], TaggedArgument)
            self.assertEqual(command.params[0].marker, "retained")
            result = CliRunner().invoke(command, ["sample"])
            self.assertEqual(result.exit_code, 0, repr(result.exception))
            self.assertEqual(result.output, "sample\n")
        self.assertEqual(len({id(command.params[0]) for command in commands}), 3)

    def test_option_custom_type_survives_reuse(self):
        shared = click.option("--value", cls=TaggedOption, marker="retained", default="fallback")
        commands = [click.command(name=f"option-{i}")(shared(lambda value: click.echo(value))) for i in range(3)]
        for command in commands:
            self.assertIsInstance(command.params[0], TaggedOption)
            self.assertEqual(command.params[0].marker, "retained")
            result = CliRunner().invoke(command, [])
            self.assertEqual(result.exit_code, 0, repr(result.exception))
            self.assertEqual(result.output, "fallback\n")

    def test_custom_class_without_extra_attributes_is_preserved(self):
        shared = click.option("--value", cls=TaggedOption)
        first = shared(lambda value: value)
        second = shared(lambda value: value)
        self.assertIsInstance(first.__click_params__[0], TaggedOption)
        self.assertIsInstance(second.__click_params__[0], TaggedOption)

    def test_builtin_option_instances_are_independent(self):
        shared = click.option("--value", default="unchanged")
        first = click.command("first")(shared(lambda value: value))
        second = click.command("second")(shared(lambda value: value))
        first.params[0].default = "changed"
        self.assertEqual(second.params[0].default, "unchanged")
        self.assertIsNot(first.params[0], second.params[0])

    def test_unshared_custom_parameter_still_executes(self):
        command = click.command()(click.argument("value", cls=TaggedArgument, marker="one")(callback))
        result = CliRunner().invoke(command, ["ok"])
        self.assertEqual(result.exit_code, 0, repr(result.exception))
        self.assertEqual(result.output, "ok\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
