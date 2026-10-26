# Invalid JSON exception does not survive serialization

Calling Response.json() on invalid JSON raises the expected Requests JSONDecodeError,
but serializing that exception with pickle and loading it again fails with a
TypeError about missing constructor arguments. In a process-based application,
this can prevent a worker failure from being delivered normally to the caller.

Make the JSON decoding exception survive a pickle round trip, preserving its
exception type, message, original document and error position. The behavior
should work for exceptions caught from Response.json() as well as directly
constructed instances. Preserve existing exception compatibility and valid JSON
decoding. Keep the repair focused on the library implementation.
