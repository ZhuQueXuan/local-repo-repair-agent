# Task: empty input produces a spurious sliding window

In this checkout, `tuple(more_itertools.windowed((), 2))` produces
`((None, None),)`. For an empty iterable and a positive window width, it
should produce no windows. This also applies to empty lists and iterators,
with a custom fill value or step.

Preserve existing behavior for nonempty iterables: short inputs are padded,
sliding windows advance by the requested step, and `None` can be a real item.
Keep the existing zero-width behavior and invalid-argument validation.

Implement a minimal source change, reproduce the failure, and verify the
repair. Do not modify tests or evaluation scripts. Keep temporary helpers in
`temp/` and do not delete files. Work only in the supplied task checkout.

Origin: https://github.com/more-itertools/more-itertools/issues/462
