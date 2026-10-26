# Command registration rejects the concise decorator form after customization

A group supplies its own command class so that all registered commands use a
common extension. Registration with an explicit decorator call works, while
the concise form with no parentheses raises an assertion during registration.
The callback itself is valid. The equivalent customization for nested groups
should support the same two forms.

Make concise and called registration behave consistently, respect the parent's
configured class, preserve explicit class overrides, and keep ordinary groups
working. Registration must not execute the callback. Investigate the path from
group registration to command construction; keep the change in implementation
code.
