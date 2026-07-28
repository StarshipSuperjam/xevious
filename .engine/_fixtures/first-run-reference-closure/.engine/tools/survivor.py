# A survivor file that still references a removed first-run asset — the dangling reference the
# closure gate must catch. In a freshly set-up project `removed_mod` is gone, so this import would
# make the very first automated check crash before it starts.
import removed_mod  # noqa: F401
