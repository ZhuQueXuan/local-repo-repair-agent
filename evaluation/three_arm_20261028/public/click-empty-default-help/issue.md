# Show an empty string default in option help

An option with default="" and show_default=True currently omits its default from --help. Display this empty string unambiguously as [default: ""] instead of silently hiding it. This must work for a directly declared empty string and when show_default is inherited from the command context. Do not change the actual value delivered to the command.

Keep show_default=False suppression, custom textual show_default labels, ordinary nonempty and numeric defaults, and the existing behavior of None and a false single boolean flag. Callable defaults remain labeled dynamic without invoking them just to render help. This task covers scalar string defaults, not an empty collection of multiple values. Change implementation only, leaving tests and metadata untouched.
