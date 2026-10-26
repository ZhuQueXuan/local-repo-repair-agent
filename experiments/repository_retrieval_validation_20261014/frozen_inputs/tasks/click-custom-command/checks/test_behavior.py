import unittest
import click
from click.testing import CliRunner


class SpecialCommand(click.Command):
    pass


class ExplicitCommand(click.Command):
    pass


class NestedGroup(click.Group):
    pass


class SpecialGroup(click.Group):
    command_class = SpecialCommand
    group_class = NestedGroup


class RegistrationChecks(unittest.TestCase):
    def test_bare_command_uses_parent_custom_class_and_runs(self):
        parent = SpecialGroup("root")
        executed = []
        @parent.command
        def child():
            executed.append(True)
            click.echo("child-ok")
        self.assertEqual(executed, [])
        self.assertIsInstance(child, SpecialCommand)
        result = CliRunner().invoke(parent, ["child"])
        self.assertEqual(result.exit_code, 0, repr(result.exception))
        self.assertEqual(result.output, "child-ok\n")
        self.assertEqual(executed, [True])

    def test_called_command_form_keeps_parent_custom_class(self):
        parent = SpecialGroup("root")
        @parent.command()
        def child():
            pass
        self.assertIsInstance(child, SpecialCommand)
        self.assertIs(parent.commands["child"], child)

    def test_explicit_command_class_overrides_parent(self):
        parent = SpecialGroup("root")
        @parent.command(cls=ExplicitCommand)
        def child():
            pass
        self.assertIs(type(child), ExplicitCommand)

    def test_bare_group_uses_parent_custom_group_class(self):
        parent = SpecialGroup("root")
        @parent.group
        def nested():
            pass
        self.assertIsInstance(nested, NestedGroup)
        self.assertIs(parent.commands["nested"], nested)

    def test_called_nested_group_form_keeps_class(self):
        parent = SpecialGroup("root")
        @parent.group()
        def nested():
            pass
        self.assertIsInstance(nested, NestedGroup)

    def test_plain_parent_still_accepts_bare_command(self):
        parent = click.Group("root")
        @parent.command
        def child():
            click.echo("plain-ok")
        result = CliRunner().invoke(parent, ["child"])
        self.assertEqual(result.exit_code, 0, repr(result.exception))
        self.assertEqual(result.output, "plain-ok\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
