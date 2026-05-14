# --- Stage 1: build the React frontend -------------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build
# output: /app/frontend/dist/

# --- Stage 2: python runtime, serves API + built frontend ------------------
FROM python:3.12-slim
WORKDIR /app

# install Python deps first (cache-friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# backend code
COPY backend/ ./backend/

# built frontend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Railway sets $PORT
ENV PORT=8000
EXPOSE 8000
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}
