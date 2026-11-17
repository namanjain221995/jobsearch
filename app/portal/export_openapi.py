"""Write the API contract.   python -m app.portal.export_openapi > openapi.json"""
import json
import sys

from .app import app

if __name__ == "__main__":
    json.dump(app.openapi(), sys.stdout, indent=1, sort_keys=True)
