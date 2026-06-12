# Synthetic Dockerfile for the IaC zoo fixture.
FROM python:3.11-slim
WORKDIR /app
COPY src/ /app/src/
USER 1000
EXPOSE 8080
HEALTHCHECK --interval=30s CMD ["python", "-c", "print('ok')"]
CMD ["python", "-m", "src.server"]
