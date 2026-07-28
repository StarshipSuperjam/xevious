#!/usr/bin/env python3
"""Negative fixture for engine/check/in-tool-demo-failure-path: a tool whose `demo` subcommand drives
nothing it can fail on — its only exit is a literal success, so it would report success even when the
behaviour is broken. The check must flag it as a demo that cannot fail."""
import sys


def main(argv):
    if argv and argv[0] == "demo":
        print("the demo ran and everything looks fine")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
