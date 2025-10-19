FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY agent_adapter/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY agent_adapter/ /app/agent_adapter/
EXPOSE 8080
CMD ["uvicorn", "agent_adapter.main:APP", "--host", "0.0.0.0", "--port", "8080"]