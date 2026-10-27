# Relative time helpers reject aware datetimes when other is omitted

decimal_relative_time and relative_time raise TypeError for a timezone-aware datetime if the optional comparison datetime is omitted. Supplying an aware comparison explicitly already works.

Support the default comparison for aware input datetimes, including non-UTC offsets, using the current instant. Preserve the existing UTC-based naive-input behavior, explicit-comparison semantics, rounding, cardinalization and human-readable past/future wording. Do not silently remove timezone information from an aware input. Change implementation only; do not alter tests or metadata.
