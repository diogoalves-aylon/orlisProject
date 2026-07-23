# Usa a imagem oficial do Python 3.12
FROM python:3.12

# Define o diretório da aplicação
ARG APP_HOME=/app
WORKDIR ${APP_HOME}

# Evita que o Python crie arquivos .pyc
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Instala dependências do sistema operacional (inclui o cliente do PostgreSQL)
RUN apt update && apt install -y postgresql-client

# Copia as dependências primeiro para otimizar cache no build
COPY requirements.txt ${APP_HOME}/requirements.txt

# Instala as dependências do Python
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY . ${APP_HOME}

# Coleta os arquivos estáticos antes de iniciar o servidor
CMD ["sh", "-c", "python manage.py migrate && python manage.py collectstatic --noinput && gunicorn --config gunicorn-cfg.py config.wsgi"]
