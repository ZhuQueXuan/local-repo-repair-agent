# Bits accepts an integer that does not fit its explicit width

Bits(8, 3) is accepted even though eight cannot fit into three binary digits. Bits.from_int has the same problem. It can create an object whose binary representation is longer than its declared length.

Reject nonnegative values outside the representable range of an explicitly requested nonnegative integer width with ValueError. This also applies when a binary or hexadecimal string is converted by the constructor with an explicit width. Keep zero and the highest fitting value valid, preserve padding, inferred widths and existing conversions, and retain rejection of negative values. Width zero may represent zero only. Negative or non-integer widths and unrelated slicing/formatting edge cases are outside this task. Change implementation only; do not edit tests or metadata.
