FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# service:app, not app:app — app.py holds payload logic and defines no ASGI
# application. See service.py for why the two are kept separate.
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
