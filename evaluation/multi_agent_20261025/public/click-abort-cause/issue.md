# click-abort-cause

When standalone_mode=False, a command callback raising EOFError or KeyboardInterrupt is converted to click.Abort but the original cause is suppressed. Preserve the original exception as an inspectable chained cause, including subclasses. Keep the normal standalone Aborted message/exit status and unrelated exceptions unchanged.
