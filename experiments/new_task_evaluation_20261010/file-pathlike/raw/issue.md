# File interfaces reject pathlib and PathLike inputs

`AtomicSaver` / `atomic_save` reject a `pathlib.Path` destination when using the default temporary filename. `iter_find_files` also rejects a `pathlib.Path` directory. The same errors occur for custom objects implementing `os.PathLike` with string paths.

Accept string-returning PathLike inputs in these public interfaces while preserving existing string behavior, relative paths, Unicode and spaces, explicit temporary filenames, overwrite behavior, and file search patterns / ignored patterns. Ordinary writes must still finish at the requested destination without a leftover temporary file. Byte-returning PathLike support is outside this task. Do not change tests or metadata.
