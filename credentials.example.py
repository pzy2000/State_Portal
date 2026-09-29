# Template for the OpenAI-compatible relay-gateway credential source used by the registered paper runs
# (`p5-scale`, `warptest.c1_experiment`, `warptest.c1_calibration`).
#
#   cp credentials.example.py test_model_batch.py   # git-ignored
#
# The file is parsed with `ast` for the three string literals below; it is never
# imported or executed, and the key is never written to a report. During a run
# the values are exposed only as RELAY_API_KEY / RELAY_BASE_URL /
# RELAY_END_USER and restored afterwards.

SECRET = "<your-relay-api-key>"
BASE = "https://<your-relay-gateway>/v1"
END_USER = ""  # optional
