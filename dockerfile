# Stage 1: Build frontend assets (React + TypeScript)
FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build


# Stage 2: Python runtime image
FROM python:3.12-slim

WORKDIR /app

# Install backend dependencies first for better build caching
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ship default dummy media in /config so onboarding can stay zero-touch.
RUN mkdir -p /config \
    && cp /app/dummy.mp4 /config/dummy.mp4 \
    && cp /app/coming_soon_dummy.mp4 /config/coming_soon_dummy.mp4

# Copy built frontend assets from the Node stage into the runtime image
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]