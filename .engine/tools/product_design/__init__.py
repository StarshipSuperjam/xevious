"""The product-design module's tools — the read-only inspectors that validate the FORM of a project's
committed product specification (its `docs/spec/` tree), never its content. This package marker keeps the
module's tools importable as `product_design.<name>` and discoverable by `unittest discover -s tools`.

It ships `spec_form` (the spec-form check), the lock-integrity re-acceptance check, and
the acceptance-criteria coverage check.
"""
