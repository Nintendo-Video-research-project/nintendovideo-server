FROM python:3.11-slim
WORKDIR /workspace
# Copy dependency definition and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy all application files
COPY . .
# Expose server port 80 NECESSARY
EXPOSE 8000

CMD ["python3", "server.py"]
