FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/
# 시나리오는 이미지 밖에서 온다 — 강사가 수업 중에 고칠 수 있어야 하므로 볼륨이다.
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
