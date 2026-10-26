# Compare dictionary-like objects without calling non-function data properties

The comparator throws for ordinary objects whose `valueOf` or `toString` fields contain data, and for maps constructed with a null prototype. Such objects should be compared structurally, returning a boolean for equal and unequal contents. Preserve existing array, nested object, Date, RegExp, boxed-number and NaN behavior.

This task uses the unmodified CommonJS implementation distributed in fast-deep-equal 3.1.3, relocated from index.js to lib/equal.js. Edit that existing JavaScript file directly; its generated-source comment refers to a source template not included in this release fixture. Do not edit the Python bridge, tests, description or metadata. No dependencies need installing. The Python checks call the real local Node.js runtime against lib/equal.js.
