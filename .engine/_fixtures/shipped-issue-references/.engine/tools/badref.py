#!/usr/bin/env python3
"""A seeded shipped tool whose docstring cites issue #495 and the chain #862/#923 by bare number.

This file exists only as the negative fixture for engine/check/shipped-issue-references: a bare issue
reference in the prose of a file that would ship into a generated repository. The check must flag it. The
real tool this imitates would write StarshipSuperjam/engine-template#495 instead.
"""


def do_work():
    # see #640 for why a bare reference resolves to the wrong issue downstream
    return None
