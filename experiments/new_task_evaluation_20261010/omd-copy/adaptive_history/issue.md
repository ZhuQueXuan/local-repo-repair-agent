# OrderedMultiDict loses values during standard copying

`copy.copy` and `copy.deepcopy` of an OrderedMultiDict with repeated keys lose earlier values. For example, copying OMD([('a', 1), ('a', 2)]) must preserve both pairs, in order, instead of keeping only the last one.

Restore normal copying behavior: every pair and its order must survive; shallow copies share value objects but have independent container state; deep copies preserve shared references and self references while separating mutable values from the source. Preserve the concrete subclass and existing `.copy()` and pickle behavior. Do not change tests or metadata.
