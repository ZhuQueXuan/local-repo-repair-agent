# split_after emits an unexpected empty trailing group

The public split_after function behaves inconsistently when maxsplit is reached
at the final element of the input:

```python
from more_itertools import split_after
list(split_after([1], lambda x: x == 1))              # [[1]]
list(split_after([1], lambda x: x == 1, maxsplit=2))  # [[1]]
list(split_after([1], lambda x: x == 1, maxsplit=1))  # currently [[1], []]
```

The last result should also be [[1]]. Do not emit an empty trailing group when
the split limit is reached and there are no remaining elements. Preserve a
nonempty remainder, iterator inputs, unlimited splitting, and maxsplit=0
behavior. Fix the implementation and run the supplied checks and existing tests.
