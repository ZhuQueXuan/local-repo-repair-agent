# Record factories raise IndexError for empty names

The namedtuple and namedlist factories leak IndexError when a type name is empty or an empty string appears in an explicit field-name iterable. These are invalid names and should raise ValueError, consistently with other invalid-name inputs.

Apply the behavior to both factories. Preserve valid record creation, positional/keyword access, the existing field-name string syntax, and rename=True replacement of invalid or duplicate field names. rename=True must not make an empty type name valid. Empty field lists remain valid zero-field records. Do not require a particular error message. Change implementation only, leaving tests and metadata untouched.
