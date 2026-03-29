FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

EXPOSE 3000

ENTRYPOINT ["terminals"]
CMD ["serve"]
