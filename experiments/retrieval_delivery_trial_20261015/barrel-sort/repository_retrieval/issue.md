# Sorting the list-compatible container fails for larger inputs

The library's list-compatible container sorts small inputs, but sorting a
larger input after inserting another value raises IndexError. Size and prior
insertion should not change whether the operation
works. It must sort all values globally in place, preserve duplicates and the
length, return None, and leave the container usable for later operations.

Reproduce this with a sufficiently large container using the public API.
Preserve empty and small-input behavior. Repair the implementation without
changing checks or replacing the container with a different public type.
