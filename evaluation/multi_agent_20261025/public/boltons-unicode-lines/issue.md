# boltons-unicode-lines

iter_splitlines corrupts ordinary text containing a space followed by 28 or 29 and fails to recognize the Unicode line/paragraph separators. Recognize actual Unicode separators while keeping normal dates intact. Preserve mixed ordinary line endings, empty input and indent behavior.
