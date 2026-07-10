FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7107

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7107"]
