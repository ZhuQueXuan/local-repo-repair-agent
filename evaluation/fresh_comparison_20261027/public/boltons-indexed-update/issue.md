# IndexedSet.update mishandles multiple input iterables

Calling IndexedSet.update with several list inputs raises TypeError. With tuple or iterator inputs it can instead insert each input object as a member, rather than adding its elements.

Honor update's documented support for one or more iterables. Preserve insertion order, uniqueness, existing members and single-input/no-input behavior. Consume one-shot iterables correctly and flatten only the sequence of input arguments: tuple-valued elements must remain elements. Change implementation only; do not alter tests or metadata.
