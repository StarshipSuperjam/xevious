"""A seeded module whose marker records nothing.

This fixture exists so the checker-of-checkers can witness the marker-form check biting a real bad
input. The marker below is deliberately empty — it occupies the recorded form and says nothing.
"""

def write(record):
    return _append(record)                    # ENGINE-TODO:
