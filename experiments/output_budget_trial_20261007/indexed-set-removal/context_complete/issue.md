# IndexedSet slicing returns incorrect items after removal

IndexedSet provides indexing and slicing, but positive-step slices return wrong
items after elements have been removed. Example:

```python
from boltons.setutils import IndexedSet
values = IndexedSet(range(10))
values.pop(2)
list(values)       # [0, 1, 3, 4, 5, 6, 7, 8, 9]
list(values[1:4])  # currently [1, 3, 4, 5]; expected [1, 3, 4]
list(values[-3:])  # currently [8, 9]; expected [7, 8, 9]
```

Positive-step slicing should match slicing list(values), including negative
bounds and bounds beyond the ends, both with and without prior removals.
The public iter_slice method should agree with the same positive-step slices.
Preserve indexing, membership, ordering, and the existing upstream negative-step
behavior; redesigning negative-step slicing is outside this task.
Fix the implementation and run the supplied checks and existing tests.
