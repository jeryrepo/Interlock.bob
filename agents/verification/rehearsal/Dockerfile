FROM python:3.11-slim

WORKDIR /probe

COPY probe.py .

# Standard library only — the probe must not depend on anything it is testing.
CMD ["python", "probe.py"]
