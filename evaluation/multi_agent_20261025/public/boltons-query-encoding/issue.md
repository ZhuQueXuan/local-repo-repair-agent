# boltons-query-encoding

parse_qsl accepts an encoding argument, but percent-encoded non-UTF-8 query components are decoded incorrectly. Honor the caller-selected encoding for keys and values. Preserve default UTF-8, plus-to-space conversion, blank-value options and query ordering.
