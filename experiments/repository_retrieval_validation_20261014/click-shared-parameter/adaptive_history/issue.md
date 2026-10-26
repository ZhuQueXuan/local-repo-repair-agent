# Reusing a configured parameter stops working on later commands

A command-line application shares a configured parameter decorator between
several callbacks. The first command gets the requested custom parameter type,
but a later command either falls back to the built-in type or rejects a keyword
that the custom type accepts. The same setup should be reusable.

Fix this for both argument and option configuration. Every application must
create a distinct parameter instance of the requested class, preserve its
configuration, and leave the other command's instance independent. Ordinary
built-in parameters and command execution must keep working. Locate the cause
inside the library rather than changing application code or tests.
