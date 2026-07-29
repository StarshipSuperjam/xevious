"""A deliberately broken fixture — a tool reading a runtime session variable outside the seam."""
import os

def session():
    return os.environ.get("CLAUDE_CODE_SESSION_ID")
