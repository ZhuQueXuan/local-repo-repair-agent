# boltons-addlist-iterator

OrderedMultiDict.addlist loses values from one-shot iterables and can leave an empty iterator as a ghost key. Accept an iterator as well as a reusable sequence; preserve existing values, order and mapping invariants. QueryParamDict must obey the same iterable contract. Do not change tests or introduce a new API.
