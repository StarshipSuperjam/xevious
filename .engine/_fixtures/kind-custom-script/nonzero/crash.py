import sys

# A deliberately-broken example script: it exits non-zero so the custom/script kind must fail closed.
print("[]")
sys.exit(3)
